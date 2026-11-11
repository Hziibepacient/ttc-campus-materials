"""Command line entry point. Every command prints short text, or JSON with --json."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import time
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import (CONFIG_NAME, RULE_HELP, Camera, ConfigError, Project, Rules, Segment, Video, eventlens_home,
                     format_hms, resolve_pieces)

EXIT_USAGE = 2


class CliError(RuntimeError):
    pass


# -- helpers -------------------------------------------------------------------

def _root(args) -> Path:
    return Path(args.project or os.environ.get("EVENTLENS_PROJECT") or os.getcwd()).resolve()


def _load(args) -> Project:
    return Project.load(_root(args))


def _out(args, data, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif text is not None:
        print(text)


def _progress(info: dict) -> None:
    total = info.get("total")
    pct = f" ({info['done'] / total * 100:4.1f}%)" if total else ""
    eta = f", ETA {format_hms(info['eta_s'])}" if info.get("eta_s") is not None else ""
    print(f"[analyze] {info['video']}: {info['done']}/{total or '?'} samples{pct}, {info['fps']:.1f}/s{eta}", file=sys.stderr, flush=True)


def _analyses(project: Project, required: bool = True) -> dict:
    from .analyze import find_analysis

    out = {}
    for v in project.videos:
        a = find_analysis(project, v)
        if a is not None:
            out[v.id] = a
    if required and not out:
        raise CliError("No analysed videos with the current detector settings. Run `eventlens analyze` first.")
    return out


def _counted(project: Project):
    from .count import count_project
    from .review import load_events, reconcile

    analyses = _analyses(project)
    result = count_project(project, analyses)
    review = reconcile(result["visits"], load_events(project.reviews_path))
    return analyses, result, review


def _find_visit(visits: list[dict], key: str) -> dict:
    hits = [v for v in visits if v["key"] == key] or [v for v in visits if v["key"].startswith(key)]
    if len(hits) != 1:
        raise CliError(f"Visit '{key}' not found or ambiguous ({len(hits)} matches). Use `eventlens visits list`.")
    return hits[0]


def _parse_frames(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if ":" in part:
            bits = [int(x) for x in part.split(":")]
            a, b = bits[0], bits[1]
            step = bits[2] if len(bits) > 2 else 1
            out.extend(range(a, b, step))
        else:
            out.append(int(part))
    return out


def _parse_points(spec: str) -> list[list[float]]:
    pts = []
    for tok in spec.replace(";", " ").split():
        x, y = tok.split(",")
        pts.append([float(x), float(y)])
    if len(pts) < 3:
        raise CliError("A zone needs at least 3 points, e.g. --points \"0.2,0.6 0.8,0.6 0.8,0.95 0.2,0.95\".")
    return pts


def _default_out(project: Project, name: str) -> Path:
    return project.root / "outputs" / name


# -- commands ------------------------------------------------------------------

def cmd_doctor(args) -> int:
    from .detect import MODELS, available_providers, model_path, pick_providers, sha256_file
    from .media import MediaError, ffmpeg_bin, ffprobe_bin
    import subprocess

    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("python", sys.version_info >= (3, 10), f"{platform.python_version()} on {platform.system()} {platform.machine()}")
    for getter, name in ((ffmpeg_bin, "ffmpeg"), (ffprobe_bin, "ffprobe")):
        try:
            b = getter()
            ver = subprocess.run([b, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
            add(name, True, f"{b} · {ver}")
        except MediaError as err:
            add(name, False, str(err))
    try:
        add("onnxruntime providers", True, ", ".join(available_providers()) + f" → using {pick_providers('auto')[0]}")
    except Exception as err:
        add("onnxruntime", False, str(err))
    root = _root(args)
    cfg = None
    if (root / CONFIG_NAME).exists():
        try:
            cfg = Project.load(root)
            add("project", True, f"{root} · {len(cfg.videos)} video(s)")
        except ConfigError as err:
            add("project", False, str(err))
    from .config import DetectorConfig

    det = cfg.detector if cfg else DetectorConfig()
    mp = model_path(det)
    if mp.exists():
        spec = MODELS.get(det.model)
        sha = sha256_file(mp)
        ok = spec is None or det.model_path or sha == spec["sha256"]
        add("model", ok, f"{mp} · sha256 {sha[:16]}…" + ("" if ok else " (MISMATCH: re-run `eventlens models fetch --force`)"))
    else:
        add("model", False, f"{mp} missing · run `eventlens models fetch` (downloads ~36 MB from the official YOLOX release)")
    if cfg:
        for v in cfg.videos:
            p = cfg.video_paths_resolved(v)
            add(f"video {v.label}", p.exists(), str(p) if p.exists() else f"missing: {p} · use `eventlens video relink`")
    ok = all(c["ok"] for c in checks)
    text = "\n".join(f"[{'ok' if c['ok'] else '!!'}] {c['check']}: {c['detail']}" for c in checks)
    _out(args, {"ok": ok, "checks": checks}, text)
    return 0 if ok else 1


def cmd_models_fetch(args) -> int:
    from .detect import MODELS, fetch_model

    name = args.model
    if name not in MODELS:
        raise CliError(f"Unknown model {name}. Known: {', '.join(MODELS)}")
    spec = MODELS[name]
    print(f"Fetching {name} ({spec['size'] / 1e6:.1f} MB) from {spec['url']}", file=sys.stderr)
    path = fetch_model(name, force=args.force)
    _out(args, {"model": name, "path": str(path), "sha256": spec["sha256"]}, f"{name} ready at {path} (sha256 verified)")
    return 0


def cmd_init(args) -> int:
    root = _root(args)
    if (root / CONFIG_NAME).exists():
        raise CliError(f"{root / CONFIG_NAME} already exists.")
    proj = Project(name=args.name or root.name, root=root)
    proj.save()
    if not (root / ".gitignore").exists():
        (root / ".gitignore").write_text(".eventlens/\noutputs/\n", encoding="utf-8")
    _out(args, {"project": str(root)}, f"Created {root / CONFIG_NAME}")
    return 0


def cmd_video_add(args) -> int:
    from .media import fingerprint, probe

    proj = _load(args)
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        raise CliError(f"{path} does not exist.")
    info = probe(path)
    if args.timelapse_interval is None and not args.realtime:
        raise CliError(
            "Say how frames map to real time: --timelapse-interval SECONDS for a time-lapse "
            "(e.g. 2 when the camera took one photo every 2 s), or --realtime [--sample-every SECONDS] for a normal video."
        )
    if args.timelapse_interval is not None:
        spf, step = float(args.timelapse_interval), 1
    else:
        if not info.fps:
            raise CliError("Could not read the frame rate; use --timelapse-interval instead.")
        spf = 1.0 / info.fps
        step = max(1, int(round(args.sample_every * info.fps)))
    fp = fingerprint(path)
    vid = fp[:12]
    if any(v.id == vid for v in proj.videos):
        raise CliError(f"This file is already in the project as {proj.video(vid).label}.")
    n = info.n_frames or (int(round(info.duration_s * info.fps)) if info.duration_s and info.fps else None)
    ordinal = len(proj.videos) + 1
    cam_id = args.camera or f"v{ordinal}-cam1"
    proj.camera(cam_id, create=True)
    label = args.label or path.stem
    if any(v.label == label for v in proj.videos):
        label = f"{label}-{vid[:4]}"
    stored = os.path.relpath(path, proj.root) if args.relative else str(path)
    v = Video(id=vid, path=stored, label=label, seconds_per_frame=spf, frame_step=step, n_frames=n,
              width=info.width, height=info.height, fps=info.fps, codec=info.codec, fingerprint=fp,
              start_time=args.start_time, segments=[Segment(0, n, "camera", cam_id)] if n else [])
    proj.videos.append(v)
    proj.save()
    data = {"video_id": vid, "label": label, "n_frames": n, "codec": info.codec, "size": [info.width, info.height],
            "fps": round(info.fps, 3), "seconds_per_frame": spf, "frame_step": step,
            "sample_interval_s": spf * step, "coverage_s": (n or 0) * spf, "camera": cam_id}
    text = (f"Added {label} ({vid}): {n} frames, {info.width}x{info.height} {info.codec}, "
            f"{spf:g} s per raw frame, analysing every {step} frame(s) = one sample per {spf * step:g} s; "
            f"covers {format_hms((n or 0) * spf)} of real time. Camera {cam_id} covers all frames until segments are set.")
    _out(args, data, text)
    return 0


def cmd_video_list(args) -> int:
    proj = _load(args)
    from .analyze import find_analysis

    rows = []
    for v in proj.videos:
        a = find_analysis(proj, v)
        rows.append({"video_id": v.id, "label": v.label, "path": v.path, "n_frames": v.n_frames,
                     "seconds_per_frame": v.seconds_per_frame, "frame_step": v.frame_step,
                     "coverage": format_hms(v.coverage_s), "analysed": a is not None,
                     "start_time": v.start_time, "start_time_verified": v.start_time_verified})
    text = "\n".join(f"{r['label']} ({r['video_id']}): {r['n_frames']} frames, coverage {r['coverage']}, "
                     f"{'analysed' if r['analysed'] else 'not analysed'}" for r in rows) or "No videos yet."
    _out(args, rows, text)
    return 0


def cmd_video_relink(args) -> int:
    from .media import fingerprint

    proj = _load(args)
    v = proj.video(args.video)
    path = Path(args.path).expanduser().resolve()
    if fingerprint(path) != v.fingerprint:
        raise CliError(f"{path} is a different file from {v.label} (content fingerprint differs).")
    v.path = str(path)
    proj.save()
    _out(args, {"video_id": v.id, "path": str(path)}, f"{v.label} now points to {path}")
    return 0


def cmd_video_set(args) -> int:
    proj = _load(args)
    v = proj.video(args.video)
    if args.start_time is not None:
        datetime.fromisoformat(args.start_time)
        v.start_time = args.start_time
    if args.start_time_verified is not None:
        v.start_time_verified = args.start_time_verified == "yes"
    if args.label:
        v.label = args.label
    proj.save()
    _out(args, asdict(v), f"Updated {v.label}")
    return 0


def cmd_analyze(args) -> int:
    from .analyze import run_analysis

    proj = _load(args)
    vids = [proj.video(r) for r in args.videos] if args.videos else proj.videos
    if not vids:
        raise CliError("No videos in the project. Add one with `eventlens video add`.")
    results = []
    for v in vids:
        t0 = time.perf_counter()
        a = run_analysis(proj, v, progress=None if args.quiet else _progress, provider=args.provider, force=args.force)
        took = time.perf_counter() - t0
        m = a.meta
        results.append({"video": v.label, "video_id": v.id, "samples": m["n_samples"], "detections": m["n_detections"],
                        "provider": m["provider"], "wall_s": m["timing"]["wall_s"], "this_call_s": round(took, 2),
                        "samples_per_s": m["timing"]["samples_per_s"], "cache": str(a.dir)})
    text = "\n".join(
        f"{r['video']}: {r['samples']} samples, {r['detections']} person boxes (box count, not people), "
        f"{r['provider']}, analysis took {format_hms(r['wall_s'])} ({r['samples_per_s']}/s)"
        + (" [cached]" if r["this_call_s"] < 0.5 * (r["wall_s"] or 0) else "") for r in results)
    _out(args, results, text)
    return 0


def cmd_segments_suggest(args) -> int:
    from .analyze import require_analysis
    from .scan import suggest_segments

    proj = _load(args)
    v = proj.video(args.video)
    a = require_analysis(proj, v)
    sug = suggest_segments(a, v.frame_step)
    ordinal = proj.videos.index(v) + 1
    for s in sug["suggested_segments"]:
        if s["camera"]:
            s["camera"] = f"v{ordinal}-{s['camera']}"
    for view in sug["views"]:
        view["camera"] = f"v{ordinal}-{view['camera']}"
    if args.apply:
        v.segments = [Segment(s["start"], s["end"], s["kind"], s["camera"], "suggested") for s in sug["suggested_segments"]]
        for s in v.segments:
            if s.camera:
                proj.camera(s.camera, create=True)
        used = {s.camera for vv in proj.videos for s in vv.segments if s.camera}
        proj.cameras = [c for c in proj.cameras if c.id in used or c.zone is not None]
        proj.save()
    lines = [f"{v.label}: {len(sug['views'])} camera view(s), {len(sug['blackouts'])} blackout(s), "
             f"{len(sug['moves_or_unusable'])} move/unusable span(s)"]
    for view in sug["views"]:
        drift = f", drifts from frame {view['drift_from_frame']}" if view["drift_from_frame"] is not None else ""
        lines.append(f"  view {view['camera']}: frames [{view['frames'][0]}, {view['frames'][1]}) "
                     f"({view['samples']} samples{drift})")
    for b in sug["blackouts"]:
        lines.append(f"  blackout: [{b[0]}, {b[1]}) = {format_hms((b[1] - b[0]) * v.seconds_per_frame)}")
    for m in sug["moves_or_unusable"]:
        lines.append(f"  move/unusable: [{m[0]}, {m[1]})")
    lines.append("Applied to the project." if args.apply else
                 "Not applied. Check boundary frames with `eventlens frames`, then re-run with --apply or set segments by hand.")
    _out(args, sug, "\n".join(lines))
    return 0


def cmd_segments_list(args) -> int:
    proj = _load(args)
    vids = [proj.video(args.video)] if args.video else proj.videos
    data = []
    lines = []
    for v in vids:
        pieces = resolve_pieces(v)
        data.append({"video": v.label, "segments": [asdict(s) for s in v.segments], "resolved": [p.__dict__ for p in pieces]})
        lines.append(f"{v.label} ({v.n_frames} frames):")
        for p in pieces:
            lines.append(f"  [{p.start}, {p.end}) {p.kind}{' ' + p.camera if p.camera else ''} = "
                         f"{format_hms((p.end - p.start) * v.seconds_per_frame)}")
    _out(args, data, "\n".join(lines))
    return 0


def cmd_segments_add(args) -> int:
    proj = _load(args)
    v = proj.video(args.video)
    if args.kind == "camera":
        if not args.camera:
            raise CliError("--camera is required for a camera segment.")
        proj.camera(args.camera, create=True)
        if args.replace_overlaps:
            v.segments = [s for s in v.segments if not (s.kind == "camera" and s.start < args.end and s.end > args.start)]
    v.segments.append(Segment(args.start, args.end, args.kind, args.camera if args.kind == "camera" else None, args.note or ""))
    v.segments.sort(key=lambda s: (s.start, s.kind != "camera"))
    proj.save()
    return cmd_segments_list(argparse.Namespace(**{**vars(args), "video": v.id}))


def cmd_segments_clear(args) -> int:
    proj = _load(args)
    v = proj.video(args.video)
    v.segments = []
    proj.save()
    _out(args, {"video": v.label, "segments": []}, f"Cleared all segments of {v.label}; nothing in it is counted until segments are added.")
    return 0


def cmd_camera_list(args) -> int:
    proj = _load(args)
    rows = []
    for c in proj.cameras:
        used = [f"{v.label}[{s.start},{s.end})" for v in proj.videos for s in v.segments if s.camera == c.id]
        rows.append({"camera": c.id, "zone": c.zone, "exclude": c.exclude, "used_by": used, "note": c.note})
    text = "\n".join(f"{r['camera']}: zone {'set (' + str(len(r['zone'])) + ' points)' if r['zone'] else 'NOT SET'}; "
                     f"{len(r['exclude'])} exclusion box(es); {', '.join(r['used_by']) or 'unused'}" for r in rows) or "No cameras."
    _out(args, rows, text)
    return 0


def cmd_camera_rename(args) -> int:
    proj = _load(args)
    old = proj.camera(args.old)
    target = next((c for c in proj.cameras if c.id == args.new), None)
    for v in proj.videos:
        for s in v.segments:
            if s.camera == args.old:
                s.camera = args.new
    if target is None:
        old.id = args.new
    else:
        if target.zone is None:
            target.zone = old.zone
        proj.cameras = [c for c in proj.cameras if c.id != args.old]
    proj.save()
    _out(args, {"renamed": args.old, "to": args.new, "merged": target is not None},
         f"{args.old} → {args.new}" + (" (merged into existing camera)" if target else ""))
    return 0


def cmd_frames(args) -> int:
    from .analyze import require_analysis
    from .render import frames_sheet, save

    proj = _load(args)
    v = proj.video(args.video)
    a = require_analysis(proj, v)
    frames = _parse_frames(args.at)
    if len(frames) > 48:
        raise CliError("At most 48 frames per sheet.")
    cam = args.camera
    if cam is None:
        pieces = resolve_pieces(v)
        cams = [p.camera for p in pieces if p.kind == "camera" and p.start <= frames[0] < p.end]
        cam = cams[0] if cams else None
    img = frames_sheet(proj, v, a, frames, cam, cols=args.cols, width=args.width)
    out = Path(args.out) if args.out else _default_out(proj, f"sheets/{v.label}-frames-{frames[0]}-{frames[-1]}.jpg")
    save(img, out)
    _out(args, {"image": str(out), "frames": frames}, f"Wrote {out}")
    return 0


def _norm_points(proj: Project, cam_id: str, pts: list[list[float]], size: str | None) -> list[list[float]]:
    if all(0 <= x <= 1 and 0 <= y <= 1 for x, y in pts):
        return pts
    if size:
        w, h = (float(x) for x in size.lower().split("x"))
    else:
        vids = [v for v in proj.videos if any(s.camera == cam_id for s in v.segments)]
        if not vids:
            raise CliError("Pixel coordinates need --pixel-size WxH (no video uses this camera yet).")
        w, h = float(vids[0].width), float(vids[0].height)
    return [[round(x / w, 4), round(y / h, 4)] for x, y in pts]


def cmd_zone_set(args) -> int:
    proj = _load(args)
    cam = proj.camera(args.camera, create=False)
    cam.zone = _norm_points(proj, cam.id, _parse_points(args.points), args.pixel_size)
    if args.note is not None:
        cam.note = args.note
    proj.save()
    _out(args, {"camera": cam.id, "zone": cam.zone}, f"Zone of {cam.id} set ({len(cam.zone)} points). Check it with `eventlens zone show {cam.id}`.")
    return 0


def cmd_zone_show(args) -> int:
    from .analyze import require_analysis
    from .render import save, zone_preview

    proj = _load(args)
    cam = proj.camera(args.camera)
    pairs = [(v, s) for v in proj.videos for s in v.segments if s.camera == cam.id]
    if args.video:
        pairs = [(v, s) for v, s in pairs if v.id == proj.video(args.video).id]
    if not pairs:
        raise CliError(f"No video segment uses camera {cam.id}.")
    v, seg = pairs[0]
    a = require_analysis(proj, v)
    frame = args.frame if args.frame is not None else (seg.start + seg.end) // 2
    frame -= frame % v.frame_step
    zone = _norm_points(proj, cam.id, _parse_points(args.points), args.pixel_size) if args.points else None
    img = zone_preview(proj, v, a, frame, cam.id, zone)
    out = Path(args.out) if args.out else _default_out(proj, f"sheets/zone-{cam.id}-f{frame}.jpg")
    save(img, out)
    _out(args, {"image": str(out), "frame": frame, "zone": zone or cam.zone}, f"Wrote {out}")
    return 0


def cmd_statics(args) -> int:
    import cv2
    from .analyze import require_analysis
    from .render import caption, grid, load_frames, save
    from .statics import find_statics

    proj = _load(args)
    vids = [proj.video(args.video)] if args.video else proj.videos
    found, tiles = [], []
    for v in vids:
        a = require_analysis(proj, v)
        cands = find_statics(proj, v, a, args.camera, args.min_presence)
        for c in cands:
            c["id"] = len(found) + 1
            found.append(c)
            img = load_frames(proj, v, a, [c["example_frame"]])[c["example_frame"]]
            h, w = img.shape[:2]
            x1, y1, x2, y2 = (int(c["box"][0] * w), int(c["box"][1] * h), int(c["box"][2] * w), int(c["box"][3] * h))
            cv2.rectangle(img, (x1, y1), (x2, y2), (60, 60, 230), 3)
            cv2.putText(img, f"#{c['id']}", (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (60, 60, 230), 2, cv2.LINE_AA)
            img = cv2.resize(img, (480, int(h * 480 / w)))
            tiles.append(caption(img, f"#{c['id']} {c['camera']} present in {c['presence'] * 100:.0f}% of samples, frame {c['example_frame']}"))
    out = None
    if tiles:
        out = Path(args.out) if args.out else _default_out(proj, "sheets/statics.jpg")
        save(grid(tiles, min(2, len(tiles))), out)
    for c in found:
        c["exclude_command"] = f"eventlens zone exclude {c['camera']} --box {','.join(str(x) for x in c['box'])}"
    lines = [f"#{c['id']} {c['camera']}: box {c['box']} present in {c['presence'] * 100:.0f}% of samples "
             f"(score {c['mean_score']})" for c in found] or ["No persistent person-like boxes found."]
    if out:
        lines.append(f"Image: {out}  (confirm on the image before excluding; a person may really stand there)")
    _out(args, {"candidates": found, "image": str(out) if out else None}, "\n".join(lines))
    return 0


def cmd_zone_exclude(args) -> int:
    proj = _load(args)
    cam = proj.camera(args.camera)
    if args.remove is not None:
        if not 0 <= args.remove < len(cam.exclude):
            raise CliError(f"{cam.id} has {len(cam.exclude)} exclusion box(es); index {args.remove} is out of range.")
        removed = cam.exclude.pop(args.remove)
        proj.save()
        _out(args, {"camera": cam.id, "removed": removed, "exclude": cam.exclude}, f"Removed exclusion {removed} from {cam.id}")
        return 0
    if not args.box:
        raise CliError("Give --box x1,y1,x2,y2 (0-1) or --remove INDEX.")
    box = [float(x) for x in args.box.split(",")]
    if len(box) != 4:
        raise CliError("--box needs four numbers x1,y1,x2,y2.")
    cam.exclude.append(box)
    proj.save()
    _out(args, {"camera": cam.id, "exclude": cam.exclude},
         f"{cam.id} now ignores detections overlapping {box} (IoU >= {proj.rules.exclude_iou}). Run `eventlens count` to see the effect.")
    return 0


def cmd_zone_clear(args) -> int:
    proj = _load(args)
    proj.camera(args.camera).zone = None
    proj.save()
    _out(args, {"camera": args.camera, "zone": None}, f"Zone of {args.camera} cleared (whole frame will be counted, with a warning).")
    return 0


def cmd_rules_show(args) -> int:
    proj = _load(args)
    r = asdict(proj.rules)
    intervals = sorted({v.sample_interval_s for v in proj.videos})
    lines = []
    for k, val in r.items():
        lines.append(f"{k} = {val}    # {RULE_HELP.get(k, '')}")
    for iv in intervals:
        n = max(proj.rules.min_stable_samples, int(-(-proj.rules.min_dwell_s // iv)) + 1)
        lines.append(f"With one sample every {iv:g} s, a stay needs >= {n} stable samples "
                     f"(first to last >= {proj.rules.min_dwell_s:g} s of real time).")
    _out(args, {"rules": r, "help": RULE_HELP, "sample_intervals_s": intervals}, "\n".join(lines))
    return 0


def cmd_rules_set(args) -> int:
    proj = _load(args)
    types = {f.name: f.type for f in fields(Rules)}
    changes = {}
    for item in args.assignments:
        if "=" not in item:
            raise CliError(f"Use key=value, got '{item}'.")
        k, val = item.split("=", 1)
        if k not in types:
            raise CliError(f"Unknown rule '{k}'. Known: {', '.join(types)}")
        cast = int if types[k] in (int, "int") else float
        changes[k] = cast(val)
    for k, val in changes.items():
        setattr(proj.rules, k, val)
    proj.save()
    _out(args, {"changed": changes, "rules": asdict(proj.rules)}, "Updated: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    return 0


def cmd_detector_set(args) -> int:
    from .config import DetectorConfig

    proj = _load(args)
    types = {f.name: f.type for f in fields(DetectorConfig)}
    changes = {}
    for item in args.assignments:
        k, _, val = item.partition("=")
        if k not in types:
            raise CliError(f"Unknown detector setting '{k}'. Known: {', '.join(types)}")
        t = str(types[k])
        if "bool" in t:
            changes[k] = val.lower() in ("1", "true", "yes", "on")
        elif "int" in t and "float" not in t:
            changes[k] = int(val)
        elif "float" in t:
            changes[k] = float(val)
        else:
            changes[k] = None if val in ("", "none", "null") else val
    for k, val in changes.items():
        setattr(proj.detector, k, val)
    proj.save()
    _out(args, {"changed": changes, "detector": asdict(proj.detector)},
         "Updated: " + ", ".join(f"{k}={v}" for k, v in changes.items())
         + ". Settings that change detections need `eventlens analyze` again (a new cache is created; the old one is kept).")
    return 0


def cmd_count(args) -> int:
    proj = _load(args)
    t0 = time.perf_counter()
    analyses, result, review = _counted(proj)
    took = time.perf_counter() - t0
    flags: dict[str, int] = {}
    for v in result["visits"]:
        for f in v["flags"]:
            flags[f] = flags.get(f, 0) + 1
    data = {"state": review["state"], "machine_total": review["machine_total"],
            "corrected_estimate": review["corrected_estimate"], "confirmed": review["confirmed"],
            "pending": review["pending"], "unresolved": review["unresolved"],
            "by_video": [{"label": s["label"], "visits": s["n_visits"], "observable": format_hms(s["observable_s"]),
                          "unobservable": format_hms(s["unobservable_s"])} for s in result["videos"]],
            "flags": flags, "rules_hash": result["rules_hash"], "seconds": round(took, 2)}
    head = ("MACHINE ESTIMATE (not reviewed)" if review["state"] == "machine_estimate" else review["state"].upper())
    lines = [f"{head}: {review['machine_total']} visits"
             + (f"; corrected {review['corrected_estimate']}, confirmed {review['confirmed']}, pending {review['pending']}"
                if review["state"] != "machine_estimate" else "")]
    for s in data["by_video"]:
        lines.append(f"  {s['label']}: {s['visits']} visits, observable {s['observable']}, unobservable {s['unobservable']}")
    if flags:
        lines.append("  flags: " + ", ".join(f"{k} {n}" for k, n in sorted(flags.items())))
    lines.append(f"  recount took {took:.2f}s (rules {result['rules_hash']})")
    _out(args, data, "\n".join(lines))
    return 0


def cmd_visits_list(args) -> int:
    proj = _load(args)
    _, result, review = _counted(proj)
    vs = result["visits"]
    if args.status:
        want = {"pending": "machine_estimate", "confirmed": "reviewed_confirmed", "rejected": "reviewed_rejected",
                "duplicate": "reviewed_duplicate", "unresolved": "unresolved"}.get(args.status, args.status)
        vs = [v for v in vs if v["status"] == want]
    if args.flag:
        vs = [v for v in vs if args.flag in v["flags"]]
    if args.video:
        vid = proj.video(args.video).id
        vs = [v for v in vs if v["video_id"] == vid]
    if args.camera:
        vs = [v for v in vs if v["camera"] == args.camera]
    if args.sort == "priority":
        vs = sorted(vs, key=lambda v: (-len(v["flags"]), v["longest_stable_s"]))
    total = len(vs)
    vs = vs[: args.limit] if args.limit else vs
    rows = [{"key": v["key"], "video": v["video_label"], "camera": v["camera"], "frames": [v["start_frame"], v["end_frame"]],
             "time": f"{format_hms(v['start_s'])}-{format_hms(v['end_s'])}", "duration_s": v["duration_s"],
             "stable_s": v["longest_stable_s"], "flags": v["flags"], "status": v["status"],
             "agent": (v["review"]["agent"] or {}).get("decision")} for v in vs]
    text = "\n".join(f"{r['key']}  {r['camera']}  f[{r['frames'][0]},{r['frames'][1]})  {r['time']}  "
                     f"stay {r['duration_s']:g}s  {r['status']}{'  agent:' + r['agent'] if r['agent'] else ''}"
                     f"{'  [' + ' '.join(r['flags']) + ']' if r['flags'] else ''}" for r in rows)
    _out(args, {"total": total, "shown": len(rows), "visits": rows}, (text or "No visits match.") + f"\n({len(rows)} of {total})")
    return 0


def cmd_visits_show(args) -> int:
    from .render import save, visit_strip
    import numpy as np

    proj = _load(args)
    analyses, result, _ = _counted(proj)
    strips, keys = [], []
    for key in args.keys:
        v = _find_visit(result["visits"], key)
        video = proj.video(v["video_id"])
        strips.append(visit_strip(proj, video, analyses[video.id], v))
        keys.append(v["key"])
    w = max(s.shape[1] for s in strips)
    padded = [np.hstack([s, np.full((s.shape[0], w - s.shape[1], 3), 18, np.uint8)]) if s.shape[1] < w else s for s in strips]
    sep = np.full((6, w, 3), 18, np.uint8)
    img = np.vstack([x for s in padded for x in (s, sep)][:-1])
    out = Path(args.out) if args.out else _default_out(proj, f"sheets/visits-{keys[0]}{'-etc' if len(keys) > 1 else ''}.jpg")
    save(img, out)
    details = [_find_visit(result["visits"], k) for k in keys]
    _out(args, {"image": str(out), "visits": details}, f"Wrote {out}")
    return 0


def cmd_review_set(args) -> int:
    from .review import append_event, decision_event

    proj = _load(args)
    _, result, _ = _counted(proj)
    v = _find_visit(result["visits"], args.key)
    dup = None
    if args.decision == "duplicate":
        if not args.of:
            raise CliError("--of KEY is required for a duplicate.")
        dup = _find_visit(result["visits"], args.of)["key"]
    ev = append_event(proj.reviews_path, decision_event(v, args.decision, args.by, args.note or "", dup, result["rules_hash"]))
    _out(args, ev, f"Recorded {args.by} decision '{args.decision}' for {v['key']}")
    return 0


def cmd_review_add(args) -> int:
    from .review import append_event, manual_add_event

    proj = _load(args)
    v = proj.video(args.video)
    proj.camera(args.camera)
    ev = append_event(proj.reviews_path, manual_add_event(v.id, args.camera, args.start, args.end, [args.x, args.y], args.note or ""))
    _out(args, ev, f"Added manual visit {ev['key']} (counts as human-confirmed)")
    return 0


def cmd_review_undo(args) -> int:
    from .review import append_event, undo_event

    proj = _load(args)
    ev = append_event(proj.reviews_path, undo_event(args.key, args.by))
    _out(args, ev, f"Undid the latest {args.by} entry for {args.key}")
    return 0


def cmd_review_status(args) -> int:
    proj = _load(args)
    _, _, review = _counted(proj)
    data = {k: v for k, v in review.items() if k not in ("manual_visits",)}
    text = (f"state {review['state']}: machine {review['machine_total']}, corrected {review['corrected_estimate']}, "
            f"confirmed {review['confirmed']} (manual adds {review['manual_adds']}), rejected {review['rejected']}, "
            f"duplicates {review['duplicates']}, unresolved {review['unresolved']}, pending {review['pending']}; "
            f"agent suggestions on pending: {review['agent_suggestions_on_pending']}; orphaned {len(review['orphaned_decisions'])}")
    _out(args, data, text)
    return 0


def cmd_gt_propose(args) -> int:
    from .evaluate import load_gt, propose_windows, save_gt

    proj = _load(args)
    analyses = _analyses(proj)
    wins = propose_windows(proj, analyses, args.debug, args.test, args.length)
    gt = load_gt(proj.groundtruth_path)
    if args.apply:
        existing = {(w["video_id"], w["start"], w["end"]) for w in gt["windows"]}
        offset = len(gt["windows"])
        for k, w in enumerate(x for x in wins if (x["video_id"], x["start"], x["end"]) not in existing):
            w["id"] = f"w{offset + k + 1:02d}-{w['camera']}-{w['role']}"
            gt["windows"].append(w)
        save_gt(proj.groundtruth_path, gt)
    lines = [f"{w['id']}: {proj.video(w['video_id']).label} {w['camera']} frames [{w['start']}, {w['end']}) "
             f"{w['role']}/{w['tag']} density {w['density']}" for w in wins]
    lines.append("Saved to groundtruth.json." if args.apply else "Not saved; re-run with --apply.")
    _out(args, wins, "\n".join(lines))
    return 0


def cmd_gt_list(args) -> int:
    from .evaluate import load_gt

    proj = _load(args)
    gt = load_gt(proj.groundtruth_path)
    rows = [{"id": w["id"], "video": proj.video(w["video_id"]).label, "camera": w["camera"], "frames": [w["start"], w["end"]],
             "role": w["role"], "tag": w.get("tag"), "status": w["status"], "labelled_visits": len(w["visits"])} for w in gt["windows"]]
    text = "\n".join(f"{r['id']}: {r['video']} [{r['frames'][0]},{r['frames'][1]}) {r['role']} {r['status']} "
                     f"({r['labelled_visits']} labelled)" for r in rows) or "No windows. Run `eventlens gt propose --apply`."
    _out(args, rows, text)
    return 0


def cmd_gt_sheet(args) -> int:
    from .analyze import require_analysis
    from .evaluate import load_gt, window
    from .render import frames_sheet, save

    proj = _load(args)
    w = window(load_gt(proj.groundtruth_path), args.window)
    v = proj.video(w["video_id"])
    a = require_analysis(proj, v)
    frames = list(range(w["start"], w["end"], v.frame_step * args.every))
    outs = []
    per = args.per_sheet
    for k in range(0, len(frames), per):
        chunk = frames[k: k + per]
        img = frames_sheet(proj, v, a, chunk, w["camera"], cols=args.cols, width=args.width, numbered=True)
        out = (Path(args.out_dir) if args.out_dir else _default_out(proj, "labelling")) / f"{w['id']}-{k // per + 1:02d}.jpg"
        save(img, out)
        outs.append(str(out))
    _out(args, {"window": w["id"], "images": outs, "frames": frames}, "\n".join(f"Wrote {o}" for o in outs))
    return 0


def cmd_gt_add(args) -> int:
    from .analyze import require_analysis
    from .evaluate import load_gt, save_gt, window

    proj = _load(args)
    gt = load_gt(proj.groundtruth_path)
    w = window(gt, args.window)
    if args.det is not None:
        if args.frame is None:
            raise CliError("--det needs --frame (the frame where that numbered box appears).")
        v = proj.video(w["video_id"])
        a = require_analysis(proj, v)
        idx = a.detections_at(args.frame)
        if args.det >= len(idx):
            raise CliError(f"Frame {args.frame} has only {len(idx)} boxes.")
        b = a.boxes[idx[args.det]]
        foot = [round(float((b[0] + b[2]) / 2), 4), round(float(b[3]), 4)]
    elif args.x is not None and args.y is not None:
        foot = [args.x, args.y]
    else:
        raise CliError("Give either --frame F --det N (numbered box on the sheet) or --x --y (feet position, 0-1).")
    if not (w["start"] <= args.start < args.end <= w["end"]):
        raise CliError(f"Visit must lie inside the window [{w['start']}, {w['end']}).")
    vid = f"g{len(w['visits']) + 1:03d}"
    w["visits"].append({"id": vid, "start_frame": args.start, "end_frame": args.end, "foot": foot, "note": args.note or "",
                        "by": args.by})
    w["status"] = "in_progress" if w["status"] == "todo" else w["status"]
    save_gt(proj.groundtruth_path, gt)
    _out(args, w["visits"][-1], f"Labelled {vid} in {w['id']}")
    return 0


def cmd_gt_remove(args) -> int:
    from .evaluate import load_gt, save_gt, window

    proj = _load(args)
    gt = load_gt(proj.groundtruth_path)
    w = window(gt, args.window)
    before = len(w["visits"])
    w["visits"] = [x for x in w["visits"] if x["id"] != args.id]
    save_gt(proj.groundtruth_path, gt)
    _out(args, {"removed": before - len(w["visits"])}, f"Removed {before - len(w['visits'])} label(s)")
    return 0


def cmd_gt_done(args) -> int:
    from .evaluate import load_gt, save_gt, window

    proj = _load(args)
    gt = load_gt(proj.groundtruth_path)
    w = window(gt, args.window)
    w["status"] = "complete"
    w["annotator"] = args.annotator
    save_gt(proj.groundtruth_path, gt)
    _out(args, {"window": w["id"], "status": "complete"}, f"{w['id']} marked complete with {len(w['visits'])} visits")
    return 0


def cmd_eval(args) -> int:
    from .evaluate import evaluate, load_gt

    proj = _load(args)
    _, result, _ = _counted(proj)
    res = evaluate(load_gt(proj.groundtruth_path), result["visits"], args.role, only_complete=not args.include_incomplete)
    t = res["total"]
    lines = [f"{r['window']}: truth {r['gt_count']}, machine {r['machine_count']}, matched {r['matched']}, "
             f"missed {len(r['misses'])}, duplicates {len(r['duplicates'])}, false merges {len(r['false_merges'])}, "
             f"false positives {len(r['false_positives'])}" for r in res["windows"]]
    lines.append(f"TOTAL ({t['windows']} windows): truth {t['gt_count']}, machine {t['machine_count']}, "
                 f"missed {t['misses']}, duplicates {t['duplicates']}, false merges {t['false_merges']}, "
                 f"false positives {t['false_positives']}, sum |count error| {t['abs_count_error']}")
    if not res["windows"]:
        lines = ["No completed ground-truth windows yet (`eventlens gt done WINDOW` after labelling)."]
    _out(args, res, "\n".join(lines))
    return 0


def cmd_report(args) -> int:
    from . import report as rp
    from .render import save, visit_strip

    proj = _load(args)
    analyses = _analyses(proj)
    t0 = time.perf_counter()
    rep = rp.build(proj, analyses, with_sensitivity=not args.no_sensitivity)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else _default_out(proj, f"{stamp}-{rep['rules_hash'][:6]}")
    ev = None if args.no_evidence else "evidence"
    if ev:
        n = len(rep["visits"])
        for k, v in enumerate(rep["visits"], 1):
            video = proj.video(v["video_id"])
            save(visit_strip(proj, video, analyses[video.id], v, crop_h=160), out / ev / f"{v['key']}.jpg", quality=78)
            if k % 200 == 0:
                print(f"[report] evidence {k}/{n}", file=sys.stderr, flush=True)
    paths = rp.write(proj, rep, out, ev)
    (proj.root / "outputs").mkdir(exist_ok=True)
    (proj.root / "outputs" / "LATEST.txt").write_text(str(out) + "\n", encoding="utf-8")
    data = {"headline": rep["headline"], "state": rep["state"], "counts": rep["counts"], "warnings": rep["warnings"],
            "files": {k: str(p) for k, p in paths.items()}, "seconds": round(time.perf_counter() - t0, 1)}
    text = "\n".join([rep["headline"], *[f"warning: {w}" for w in rep["warnings"]],
                      *[f"{k}: {p}" for k, p in paths.items()]])
    _out(args, data, text)
    return 0


def cmd_run(args) -> int:
    rc = cmd_analyze(argparse.Namespace(**{**vars(args), "videos": [], "force": False}))
    if rc:
        return rc
    return cmd_report(argparse.Namespace(**{**vars(args), "out": None}))


def cmd_bench(args) -> int:
    import numpy as np
    from .config import DetectorConfig
    from .detect import load_detector
    from .media import decode_size, default_hwaccel, iter_frames, probe

    path = Path(args.video).expanduser()
    info = probe(path)
    dw, dh = decode_size(info.width, info.height, args.decode_width)
    res = {"video": str(path), "codec": info.codec, "source": [info.width, info.height], "decode": [dw, dh]}
    t0 = time.perf_counter()
    frames = []
    for i, (idx, fr) in enumerate(iter_frames(path, dw, dh, args.step, default_hwaccel())):
        if len(frames) < 32:
            frames.append(fr.copy())
        if i + 1 >= args.frames:
            break
    n_dec = i + 1
    res["decode_fps"] = round(n_dec / (time.perf_counter() - t0), 1)
    det = load_detector(DetectorConfig(provider=args.provider))
    det.detect(frames[0])
    t0 = time.perf_counter()
    for k in range(args.infer_frames):
        det.detect(frames[k % len(frames)])
    res["provider"] = det.provider
    res["inference_fps"] = round(args.infer_frames / (time.perf_counter() - t0), 1)
    est = 1.0 / (1.0 / res["decode_fps"] + 1.0 / res["inference_fps"])
    res["serial_estimate_fps"] = round(est, 1)
    text = (f"decode {res['decode_fps']} frames/s ({info.codec} {info.width}x{info.height} → {dw}x{dh}), "
            f"inference {res['inference_fps']} frames/s on {det.provider}; "
            f"8,300-frame job ≈ {format_hms(8300 / max(res['inference_fps'], 1e-6))} if decode overlaps inference")
    _out(args, res, text)
    return 0


SAMPLE = {
    "file": "Pedestrian_area_Ljubljana.webm",
    "url": "https://upload.wikimedia.org/wikipedia/commons/b/bc/Pedestrian_area_Ljubljana.webm",
    "page": "https://commons.wikimedia.org/wiki/File:Pedestrian_area_Ljubljana.webm",
    "sha256": "6adfe8c699913e4d1e14186b0a3aa05d3ae83231a1f97ea58429221b682c349a",
    "size": 24400451,
    "license": "CC BY 3.0, Sounds of Changes, via Wikimedia Commons",
}


def cmd_demo(args) -> int:
    import urllib.request
    from .detect import sha256_file

    sample = eventlens_home() / "samples" / SAMPLE["file"]
    if not sample.exists():
        if not args.yes:
            raise CliError(f"The demo needs a public sample video ({SAMPLE['size'] / 1e6:.1f} MB, {SAMPLE['license']}) "
                           f"from {SAMPLE['page']}. Re-run with --yes to download it.")
        sample.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(SAMPLE["url"], headers={"User-Agent": f"eventlens/{__version__}"})
        tmp = sample.with_suffix(".part")
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out)
        if sha256_file(tmp) != SAMPLE["sha256"]:
            tmp.unlink(missing_ok=True)
            raise CliError("Downloaded sample does not match the expected sha256; not using it.")
        tmp.replace(sample)
    root = Path(args.dir).expanduser().resolve()
    if (root / CONFIG_NAME).exists():
        raise CliError(f"{root} already has a project; choose another --dir.")
    run = lambda *a: main(["--project", str(root), *a])  # noqa: E731
    steps = [
        ("init", "--name", "EventLens demo (Ljubljana, CC BY 3.0)"),
        ("video", "add", str(sample), "--realtime", "--sample-every", "1", "--label", "ljubljana", "--camera", "street"),
        ("analyze",),
        ("zone", "set", "street", "--points", "0.55,0.42 1.0,0.42 1.0,0.75 0.55,0.75", "--note", "right-hand pavement"),
        ("count",),
        ("report", "--out", str(root / "outputs" / "demo")),
    ]
    for step in steps:
        rc = run(*step)
        if rc:
            return rc
    print(f"Demo done. Open {root / 'outputs' / 'demo' / 'report.html'}")
    return 0


def cmd_skill_install(args) -> int:
    src = Path(__file__).parent / "skill" / "eventlens"
    if not (src / "SKILL.md").exists():
        raise CliError(f"Skill files missing at {src}")
    targets = []
    home = Path.home()
    if args.target in ("codex", "all"):
        targets.append(home / ".codex" / "skills" / "eventlens")
    if args.target in ("claude", "all"):
        targets.append(home / ".claude" / "skills" / "eventlens")
    if args.dest:
        targets = [Path(args.dest).expanduser() / "eventlens"]
    done = []
    for t in targets:
        t.parent.mkdir(parents=True, exist_ok=True)
        if t.is_symlink() or t.exists():
            if not args.force:
                raise CliError(f"{t} already exists; use --force to replace it.")
            if t.is_symlink() or t.is_file():
                t.unlink()
            else:
                shutil.rmtree(t)
        if args.link:
            t.symlink_to(src, target_is_directory=True)
        else:
            shutil.copytree(src, t)
        done.append(str(t))
    _out(args, {"installed": done}, "\n".join(f"Installed skill at {d}" for d in done))
    return 0


# -- parser ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS keeps a subcommand's unset flag from overwriting one given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", "-p", default=argparse.SUPPRESS,
                        help="Project folder (default: $EVENTLENS_PROJECT or current folder)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Machine-readable output")

    # The top-level parser gets its own copies: argparse shares action objects
    # through `parents`, so changing their defaults here would leak into subcommands.
    p = argparse.ArgumentParser(prog="eventlens", description="Count people who stop near something in fixed-camera or time-lapse video.")
    p.add_argument("--project", "-p", default=None, help="Project folder (default: $EVENTLENS_PROJECT or current folder)")
    p.add_argument("--json", action="store_true", default=False, help="Machine-readable output")
    p.add_argument("--version", action="version", version=f"eventlens {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, parent=sub):
        sp = parent.add_parser(name, help=help_, parents=[common])
        sp.set_defaults(fn=fn)
        return sp

    add("doctor", cmd_doctor, "Check FFmpeg, ONNX Runtime, model and project files")
    models = sub.add_parser("models", help="Manage detection models").add_subparsers(dest="sub", required=True)
    sp = add("fetch", cmd_models_fetch, "Download and verify a model", models)
    sp.add_argument("--model", default="yolox-s")
    sp.add_argument("--force", action="store_true")

    sp = add("init", cmd_init, "Create eventlens.json in the project folder")
    sp.add_argument("--name")

    video = sub.add_parser("video", help="Add and manage videos").add_subparsers(dest="sub", required=True)
    sp = add("add", cmd_video_add, "Add a video file", video)
    sp.add_argument("path")
    sp.add_argument("--timelapse-interval", type=float, help="Real seconds between two raw frames (time-lapse)")
    sp.add_argument("--realtime", action="store_true", help="Normal-speed video")
    sp.add_argument("--sample-every", type=float, default=2.0, help="Real seconds between analysed frames for --realtime")
    sp.add_argument("--label")
    sp.add_argument("--camera", help="Camera id for the whole video (default v<N>-cam1)")
    sp.add_argument("--start-time", help="ISO local time of frame 0 if known, e.g. 2026-10-18T10:55:51")
    sp.add_argument("--relative", action="store_true", help="Store the path relative to the project folder")
    add("list", cmd_video_list, "List videos", video)
    sp = add("relink", cmd_video_relink, "Point a video at its new location", video)
    sp.add_argument("video")
    sp.add_argument("path")
    sp = add("set", cmd_video_set, "Set label or start time", video)
    sp.add_argument("video")
    sp.add_argument("--label")
    sp.add_argument("--start-time")
    sp.add_argument("--start-time-verified", choices=["yes", "no"])

    sp = add("analyze", cmd_analyze, "Decode once: detect people, cache stats and previews")
    sp.add_argument("videos", nargs="*")
    sp.add_argument("--provider", help="auto | cpu | coreml | cuda | dml")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--quiet", action="store_true")

    seg = sub.add_parser("segments", help="Camera views, blackouts and skipped spans").add_subparsers(dest="sub", required=True)
    sp = add("suggest", cmd_segments_suggest, "Propose segments from scene changes", seg)
    sp.add_argument("video")
    sp.add_argument("--apply", action="store_true")
    sp = add("list", cmd_segments_list, "Show segments", seg)
    sp.add_argument("video", nargs="?")
    sp = add("add", cmd_segments_add, "Add one segment [start, end)", seg)
    sp.add_argument("video")
    sp.add_argument("--kind", choices=["camera", "blackout", "skip"], required=True)
    sp.add_argument("--start", type=int, required=True)
    sp.add_argument("--end", type=int, required=True)
    sp.add_argument("--camera")
    sp.add_argument("--note")
    sp.add_argument("--replace-overlaps", action="store_true", help="Drop camera segments this one overlaps")
    sp = add("clear", cmd_segments_clear, "Remove all segments of a video", seg)
    sp.add_argument("video")

    cam = sub.add_parser("camera", help="Camera ids").add_subparsers(dest="sub", required=True)
    add("list", cmd_camera_list, "List cameras and zones", cam)
    sp = add("rename", cmd_camera_rename, "Rename or merge a camera", cam)
    sp.add_argument("old")
    sp.add_argument("new")

    sp = add("frames", cmd_frames, "Contact sheet of frames (e.g. around a boundary)")
    sp.add_argument("video")
    sp.add_argument("--at", required=True, help="e.g. 3086:3094 or 200,211,212")
    sp.add_argument("--camera")
    sp.add_argument("--cols", type=int, default=4)
    sp.add_argument("--width", type=int, default=480)
    sp.add_argument("--out")

    zone = sub.add_parser("zone", help="'Nearby' area per camera").add_subparsers(dest="sub", required=True)
    sp = add("set", cmd_zone_set, "Set the polygon", zone)
    sp.add_argument("camera")
    sp.add_argument("--points", required=True, help="\"x,y x,y x,y ...\" in 0-1 (or pixels)")
    sp.add_argument("--pixel-size", help="WxH when points are in pixels")
    sp.add_argument("--note")
    sp = add("show", cmd_zone_show, "Render the zone on a frame with a coordinate grid", zone)
    sp.add_argument("camera")
    sp.add_argument("--video")
    sp.add_argument("--frame", type=int)
    sp.add_argument("--points", help="Preview these points without saving")
    sp.add_argument("--pixel-size")
    sp.add_argument("--out")
    sp = add("clear", cmd_zone_clear, "Remove the polygon", zone)
    sp.add_argument("camera")
    sp = add("exclude", cmd_zone_exclude, "Ignore a person-like object (standee, poster) by its box", zone)
    sp.add_argument("camera")
    sp.add_argument("--box", help="x1,y1,x2,y2 in 0-1, e.g. from `eventlens statics`")
    sp.add_argument("--remove", type=int, help="Remove the exclusion box with this index")

    sp = add("statics", cmd_statics, "Find boxes that never move (likely objects, not people)")
    sp.add_argument("--video")
    sp.add_argument("--camera")
    sp.add_argument("--min-presence", type=float, default=0.3, help="Share of samples the box must appear in")
    sp.add_argument("--out")

    rules = sub.add_parser("rules", help="Dwell and merge rules").add_subparsers(dest="sub", required=True)
    add("show", cmd_rules_show, "Show rules with meaning", rules)
    sp = add("set", cmd_rules_set, "Set rules: key=value ...", rules)
    sp.add_argument("assignments", nargs="+")

    det = sub.add_parser("detector", help="Detector settings").add_subparsers(dest="sub", required=True)
    sp = add("set", cmd_detector_set, "Set detector settings: key=value ...", det)
    sp.add_argument("assignments", nargs="+")

    add("count", cmd_count, "Recount visits with the current rules (instant)")

    vis = sub.add_parser("visits", help="Inspect counted visits").add_subparsers(dest="sub", required=True)
    sp = add("list", cmd_visits_list, "List visits", vis)
    sp.add_argument("--status", help="pending | confirmed | rejected | duplicate | unresolved")
    sp.add_argument("--flag", help="e.g. short, crowded, near_boundary, merged, low_score")
    sp.add_argument("--video")
    sp.add_argument("--camera")
    sp.add_argument("--sort", choices=["time", "priority"], default="time")
    sp.add_argument("--limit", type=int, default=50)
    sp = add("show", cmd_visits_show, "Evidence image for one or more visits", vis)
    sp.add_argument("keys", nargs="+")
    sp.add_argument("--out")

    rev = sub.add_parser("review", help="Record review decisions").add_subparsers(dest="sub", required=True)
    sp = add("set", cmd_review_set, "Decide on a visit", rev)
    sp.add_argument("key")
    sp.add_argument("--decision", choices=["confirm", "reject", "duplicate", "unsure"], required=True)
    sp.add_argument("--of", help="Key of the visit this one duplicates")
    sp.add_argument("--by", choices=["human", "agent"], required=True,
                    help="human = the person said so; agent = the assistant's own judgement (kept as a suggestion)")
    sp.add_argument("--note")
    sp = add("add", cmd_review_add, "Add a visit the machine missed (human only)", rev)
    sp.add_argument("--video", required=True)
    sp.add_argument("--camera", required=True)
    sp.add_argument("--start", type=int, required=True)
    sp.add_argument("--end", type=int, required=True)
    sp.add_argument("--x", type=float, required=True)
    sp.add_argument("--y", type=float, required=True)
    sp.add_argument("--note")
    sp = add("undo", cmd_review_undo, "Undo the latest entry for a key", rev)
    sp.add_argument("key")
    sp.add_argument("--by", choices=["human", "agent"], default="human")
    add("status", cmd_review_status, "Review progress", rev)

    gt = sub.add_parser("gt", help="Ground-truth windows for accuracy checks").add_subparsers(dest="sub", required=True)
    sp = add("propose", cmd_gt_propose, "Pick debug/test windows per camera", gt)
    sp.add_argument("--debug", type=int, default=2)
    sp.add_argument("--test", type=int, default=1)
    sp.add_argument("--length", type=int, default=60, help="Samples per window")
    sp.add_argument("--apply", action="store_true")
    add("list", cmd_gt_list, "List windows", gt)
    sp = add("sheet", cmd_gt_sheet, "Numbered contact sheets for labelling a window", gt)
    sp.add_argument("window")
    sp.add_argument("--every", type=int, default=1)
    sp.add_argument("--per-sheet", type=int, default=9)
    sp.add_argument("--cols", type=int, default=3)
    sp.add_argument("--width", type=int, default=560)
    sp.add_argument("--out-dir")
    sp = add("add", cmd_gt_add, "Label one true visit in a window", gt)
    sp.add_argument("window")
    sp.add_argument("--start", type=int, required=True)
    sp.add_argument("--end", type=int, required=True)
    sp.add_argument("--frame", type=int)
    sp.add_argument("--det", type=int, help="Box number on the sheet at --frame")
    sp.add_argument("--x", type=float)
    sp.add_argument("--y", type=float)
    sp.add_argument("--by", choices=["human", "agent"], default="human")
    sp.add_argument("--note")
    sp = add("remove", cmd_gt_remove, "Remove a label", gt)
    sp.add_argument("window")
    sp.add_argument("id")
    sp = add("done", cmd_gt_done, "Mark a window fully labelled", gt)
    sp.add_argument("window")
    sp.add_argument("--annotator", default="human")

    sp = add("eval", cmd_eval, "Compare machine visits with labelled windows")
    sp.add_argument("--role", choices=["debug", "test"])
    sp.add_argument("--include-incomplete", action="store_true")

    sp = add("report", cmd_report, "Write report.html / visits.csv / report.json")
    sp.add_argument("--out")
    sp.add_argument("--no-evidence", action="store_true")
    sp.add_argument("--no-sensitivity", action="store_true")

    sp = add("run", cmd_run, "analyze + report")
    sp.add_argument("--provider")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--no-evidence", action="store_true")
    sp.add_argument("--no-sensitivity", action="store_true")

    sp = add("bench", cmd_bench, "Measure decode and inference speed on this machine")
    sp.add_argument("video")
    sp.add_argument("--frames", type=int, default=300)
    sp.add_argument("--infer-frames", type=int, default=100)
    sp.add_argument("--step", type=int, default=1)
    sp.add_argument("--decode-width", type=int, default=960)
    sp.add_argument("--provider", default="auto")

    sp = add("demo", cmd_demo, "End-to-end demo on a public sample video")
    sp.add_argument("--dir", default="eventlens-demo")
    sp.add_argument("--yes", action="store_true", help="Allow downloading the 24 MB public sample")

    skill = sub.add_parser("skill", help="AI assistant skill").add_subparsers(dest="sub", required=True)
    sp = add("install", cmd_skill_install, "Install the EventLens skill for Claude Code / Codex", skill)
    sp.add_argument("--target", choices=["claude", "codex", "all"], default="all")
    sp.add_argument("--dest", help="Custom skills folder")
    sp.add_argument("--link", action="store_true", help="Symlink instead of copy (for development)")
    sp.add_argument("--force", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (ConfigError, CliError) as err:
        _err(args, str(err))
        return EXIT_USAGE
    except Exception as err:  # surface the reason plainly; full traceback with EVENTLENS_DEBUG=1
        if os.environ.get("EVENTLENS_DEBUG"):
            raise
        _err(args, f"{type(err).__name__}: {err}")
        return 1


def _err(args, msg: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps({"error": msg}, ensure_ascii=False))
    else:
        print(f"error: {msg}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
