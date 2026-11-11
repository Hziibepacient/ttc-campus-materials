"""Single decode pass per video: detections, appearance, scene stats and previews.

Everything downstream (segments, zones, dwell rules, merging, reports) reads
this cache, so changing a rule never requires decoding the video again. The
cache key covers the video content, model bytes and detector settings; any
change to those produces a new cache directory instead of silently reusing
stale results.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import __version__
from .config import Project, Video, stable_hash
from .detect import FEATURE_VERSION, appearance, load_detector
from .media import MediaError, decode_size, default_hwaccel, fingerprint, iter_frames, probe

ANALYSIS_VERSION = 1
THUMB_W, THUMB_H = 48, 27
CHECKPOINT_EVERY = 400

ProgressFn = Callable[[dict], None]


@dataclass
class Analysis:
    dir: Path
    meta: dict
    frames: np.ndarray  # raw frame index of each analysed sample
    luma: np.ndarray
    luma_std: np.ndarray
    thumbs: np.ndarray  # uint8 [n, THUMB_H, THUMB_W] grey
    det_frame: np.ndarray  # raw frame index of each detection
    boxes: np.ndarray  # [N,4] x1,y1,x2,y2 normalised to 0-1
    scores: np.ndarray
    feats: np.ndarray

    def preview_path(self, frame: int) -> Path:
        return self.dir / "previews" / f"{int(frame):07d}.jpg"

    def detections_at(self, frame: int) -> np.ndarray:
        lo = np.searchsorted(self.det_frame, frame, side="left")
        hi = np.searchsorted(self.det_frame, frame, side="right")
        return np.arange(lo, hi)

    @property
    def decode_size(self) -> tuple[int, int]:
        w, h = self.meta["decode_size"]
        return int(w), int(h)


def analysis_key(video: Video, project: Project, model_sha256: str) -> str:
    d = project.detector
    return stable_hash(
        {
            "analysis_version": ANALYSIS_VERSION,
            "feature_version": FEATURE_VERSION,
            "fingerprint": video.fingerprint,
            "model_sha256": model_sha256,
            "conf": d.conf,
            "nms": d.nms,
            "decode_width": d.decode_width,
            "frame_step": video.frame_step,
        }
    )


def analysis_root(project: Project, video: Video) -> Path:
    return project.work_dir / "analysis" / video.id


def find_analysis(project: Project, video: Video) -> Analysis | None:
    """Return the complete analysis matching the current settings, if any."""
    root = analysis_root(project, video)
    if not root.exists():
        return None
    from .detect import model_path

    mp = model_path(project.detector)
    if not mp.exists():
        return None
    key = analysis_key(video, project, _cached_sha(mp))
    d = root / key
    if (d / "meta.json").exists():
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta.get("status") == "complete":
            return _load(d, meta)
    return None


_SHA_CACHE: dict[tuple[str, float, int], str] = {}


def _cached_sha(path: Path) -> str:
    from .detect import sha256_file

    st = path.stat()
    k = (str(path), st.st_mtime, st.st_size)
    if k not in _SHA_CACHE:
        _SHA_CACHE[k] = sha256_file(path)
    return _SHA_CACHE[k]


def _load(d: Path, meta: dict) -> Analysis:
    f = np.load(d / "frames.npz")
    det = np.load(d / "detections.npz")
    return Analysis(
        dir=d,
        meta=meta,
        frames=f["frames"],
        luma=f["luma"],
        luma_std=f["luma_std"],
        thumbs=f["thumbs"],
        det_frame=det["frame"],
        boxes=det["boxes"],
        scores=det["scores"],
        feats=det["feats"],
    )


def require_analysis(project: Project, video: Video) -> Analysis:
    a = find_analysis(project, video)
    if a is None:
        raise MediaError(
            f"Video {video.label} has not been analysed with the current detector settings. Run `eventlens analyze`."
        )
    return a


def run_analysis(
    project: Project,
    video: Video,
    progress: ProgressFn | None = None,
    provider: str | None = None,
    force: bool = False,
) -> Analysis:
    from .detect import model_path

    cfg = project.detector
    if provider:
        cfg = type(cfg)(**{**cfg.__dict__, "provider": provider})
    mp = model_path(cfg)
    if not mp.exists():
        raise MediaError(f"Model file {mp} is missing. Run `eventlens models fetch` first.")
    # The cache key needs only the recorded fingerprint and the model bytes, so a
    # finished analysis is reused even when the video file is not on this machine.
    key = analysis_key(video, project, _cached_sha(mp))
    d = analysis_root(project, video) / key
    meta_path = d / "meta.json"
    if meta_path.exists() and not force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("status") == "complete":
            return _load(d, meta)

    src = project.video_paths_resolved(video)
    if not src.exists():
        raise MediaError(f"Video file not found: {src}. Update its path with `eventlens video relink`.")
    if fingerprint(src) != video.fingerprint:
        raise MediaError(f"{src} no longer matches the file that was added (content changed). Add it again.")
    detector = load_detector(cfg)
    if detector.model_sha256 != _cached_sha(mp):
        raise MediaError("Loaded model bytes differ from the model file used for the cache key.")
    d.mkdir(parents=True, exist_ok=True)
    prev_dir = d / "previews"
    if cfg.previews:
        prev_dir.mkdir(exist_ok=True)

    info = probe(src)
    dw, dh = decode_size(info.width, info.height, cfg.decode_width)

    # Resume from a checkpoint when one exists for exactly this key.
    buf = _Buffers()
    resume_after = -1
    part = d / "partial.npz"
    if part.exists() and not force:
        try:
            buf.load(part)
            resume_after = int(buf.frames[-1]) if buf.frames else -1
        except Exception:
            buf = _Buffers()
            resume_after = -1

    expected = info.n_frames or (int(round(info.duration_s * info.fps)) if info.duration_s and info.fps else None)
    expected_samples = (expected + video.frame_step - 1) // video.frame_step if expected else None
    pool = ThreadPoolExecutor(max_workers=2)
    pending = []
    t0 = time.perf_counter()
    t_last = 0.0
    infer_s = 0.0
    n_new = 0
    hw = default_hwaccel()
    last_idx = -1
    for idx, frame in iter_frames(src, dw, dh, video.frame_step, hw):
        last_idx = idx
        if idx <= resume_after:
            continue
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        thumb = cv2.resize(grey, (THUMB_W, THUMB_H), interpolation=cv2.INTER_AREA)
        ti = time.perf_counter()
        boxes, scores = detector.detect(frame)
        infer_s += time.perf_counter() - ti
        feats = appearance(frame, boxes)
        norm = boxes / np.array([dw, dh, dw, dh], np.float32)
        buf.add(idx, float(grey.mean()), float(grey.std()), thumb, norm, scores, feats)
        if cfg.previews:
            pending.append(pool.submit(_write_jpeg, prev_dir / f"{idx:07d}.jpg", frame.copy(), cfg.preview_quality))
            if len(pending) > 64:
                pending = [p for p in pending if not p.done()]
        n_new += 1
        if n_new % CHECKPOINT_EVERY == 0:
            buf.save(part)
        now = time.perf_counter()
        if progress and now - t_last > 2.0:
            t_last = now
            done = len(buf.frames)
            rate = n_new / max(now - t0, 1e-6)
            eta = (expected_samples - done) / rate if expected_samples and rate > 0 else None
            progress({"video": video.label, "done": done, "total": expected_samples, "fps": rate, "eta_s": eta})
    for p in pending:
        p.result()
    pool.shutdown(wait=True)
    elapsed = time.perf_counter() - t0
    if not buf.frames:
        raise MediaError(f"No frames decoded from {src}.")

    if video.frame_step == 1:
        n_frames = last_idx + 1
    else:
        n_frames = expected if expected and expected > last_idx else last_idx + 1
    if video.n_frames != n_frames:
        # Segments that ran to the old (probed) end follow the exact decoded count.
        old = video.n_frames
        for s in video.segments:
            if old is not None and s.end == old:
                s.end = n_frames
            s.end = min(s.end, n_frames)
        video.segments = [s for s in video.segments if s.start < s.end]
        video.n_frames = n_frames
        project.save()

    buf.save_final(d)
    meta = {
        "status": "complete",
        "tool_version": __version__,
        "analysis_version": ANALYSIS_VERSION,
        "key": key,
        "video_id": video.id,
        "fingerprint": video.fingerprint,
        "source_path_at_analysis": str(src),
        "model": detector.name,
        "model_sha256": detector.model_sha256,
        "provider": detector.provider,
        "hwaccel": hw,
        "conf": cfg.conf,
        "nms": cfg.nms,
        "decode_size": [dw, dh],
        "source_size": [info.width, info.height],
        "frame_step": video.frame_step,
        "n_frames": n_frames,
        "probe_n_frames": info.n_frames,
        "n_samples": len(buf.frames),
        "n_detections": int(sum(len(s) for s in buf.scores)),
        "resumed_after_frame": resume_after if resume_after >= 0 else None,
        "previews": cfg.previews,
        "timing": {
            "wall_s": round(elapsed, 2),
            "inference_s": round(infer_s, 2),
            "samples_per_s": round(n_new / elapsed, 2) if elapsed > 0 else None,
        },
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    part.unlink(missing_ok=True)
    return _load(d, meta)


def _write_jpeg(path: Path, frame: np.ndarray, quality: int) -> None:
    ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if ok:
        path.write_bytes(enc.tobytes())


class _Buffers:
    def __init__(self) -> None:
        self.frames: list[int] = []
        self.luma: list[float] = []
        self.luma_std: list[float] = []
        self.thumbs: list[np.ndarray] = []
        self.boxes: list[np.ndarray] = []
        self.scores: list[np.ndarray] = []
        self.feats: list[np.ndarray] = []

    def add(self, idx, luma, std, thumb, boxes, scores, feats) -> None:
        self.frames.append(idx)
        self.luma.append(luma)
        self.luma_std.append(std)
        self.thumbs.append(thumb)
        self.boxes.append(boxes)
        self.scores.append(scores)
        self.feats.append(feats)

    def _arrays(self) -> dict[str, np.ndarray]:
        from .detect import FEATURE_DIM

        counts = [len(s) for s in self.scores]
        return {
            "frames": np.asarray(self.frames, np.int32),
            "luma": np.asarray(self.luma, np.float32),
            "luma_std": np.asarray(self.luma_std, np.float32),
            "thumbs": np.stack(self.thumbs).astype(np.uint8) if self.thumbs else np.zeros((0, THUMB_H, THUMB_W), np.uint8),
            "counts": np.asarray(counts, np.int32),
            "boxes": np.concatenate(self.boxes).astype(np.float32) if counts and sum(counts) else np.zeros((0, 4), np.float32),
            "scores": np.concatenate(self.scores).astype(np.float32) if counts and sum(counts) else np.zeros((0,), np.float32),
            "feats": np.concatenate(self.feats).astype(np.float32) if counts and sum(counts) else np.zeros((0, FEATURE_DIM), np.float32),
        }

    def save(self, path: Path) -> None:
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez(tmp, **self._arrays())
        tmp.replace(path)

    def load(self, path: Path) -> None:
        z = np.load(path)
        counts = z["counts"]
        offs = np.concatenate([[0], np.cumsum(counts)])
        self.frames = [int(x) for x in z["frames"]]
        self.luma = [float(x) for x in z["luma"]]
        self.luma_std = [float(x) for x in z["luma_std"]]
        self.thumbs = list(z["thumbs"])
        self.boxes = [z["boxes"][offs[i]: offs[i + 1]] for i in range(len(counts))]
        self.scores = [z["scores"][offs[i]: offs[i + 1]] for i in range(len(counts))]
        self.feats = [z["feats"][offs[i]: offs[i + 1]] for i in range(len(counts))]

    def save_final(self, d: Path) -> None:
        a = self._arrays()
        det_frame = np.repeat(a["frames"], a["counts"]).astype(np.int32)
        np.savez(d / "frames.npz", frames=a["frames"], luma=a["luma"], luma_std=a["luma_std"], thumbs=a["thumbs"])
        np.savez(d / "detections.npz", frame=det_frame, boxes=a["boxes"], scores=a["scores"], feats=a["feats"])
