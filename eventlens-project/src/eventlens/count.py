"""Dwell counting: tracks -> stable windows -> merged visits.

All functions here are pure over the cached analysis, so a full recount of a
4.5 hour time-lapse takes about a second and every rule change can be re-run
end to end rather than approximated by filtering an earlier result.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from .analyze import Analysis
from .config import Piece, Project, Rules, Video, resolve_pieces, stable_hash

COUNT_VERSION = 1


def points_in_polygon(xy: np.ndarray, poly: list[list[float]] | None) -> np.ndarray:
    if poly is None:
        return np.ones(len(xy), bool)
    px = np.asarray([p[0] for p in poly], np.float64)
    py = np.asarray([p[1] for p in poly], np.float64)
    x, y = xy[:, 0][:, None], xy[:, 1][:, None]
    x1, y1 = px[None, :], py[None, :]
    x2, y2 = np.roll(px, -1)[None, :], np.roll(py, -1)[None, :]
    cond = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xint = (x2 - x1) * (y - y1) / (y2 - y1) + x1
    return (np.sum(cond & (x < xint), axis=1) % 2) == 1


def box_iou(boxes: np.ndarray, refs: list[list[float]]) -> np.ndarray:
    """IoU of every box against every reference box -> [N, R]."""
    if len(refs) == 0 or len(boxes) == 0:
        return np.zeros((len(boxes), len(refs)), np.float32)
    r = np.asarray(refs, np.float32)
    x1 = np.maximum(boxes[:, None, 0], r[None, :, 0])
    y1 = np.maximum(boxes[:, None, 1], r[None, :, 1])
    x2 = np.minimum(boxes[:, None, 2], r[None, :, 2])
    y2 = np.minimum(boxes[:, None, 3], r[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_b = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    area_r = (r[:, 2] - r[:, 0]) * (r[:, 3] - r[:, 1])
    return inter / np.maximum(area_b[:, None] + area_r[None, :] - inter, 1e-9)


@dataclass
class Track:
    id: int
    dets: list[int] = field(default_factory=list)
    samples: list[int] = field(default_factory=list)
    missed: int = 0


@dataclass
class Visit:
    key: str
    video_id: str
    video_label: str
    camera: str
    piece: list[int]
    start_frame: int
    end_frame: int
    start_s: float
    end_s: float
    duration_s: float
    longest_stable_s: float
    n_samples: int
    tracks: list[int]
    merges: list[dict]
    foot: list[float]
    box: list[float]
    mean_score: float
    evidence: list[dict]
    flags: list[str]
    status: str = "machine_estimate"

    def to_dict(self) -> dict:
        return asdict(self)


class _Geo:
    """Per-detection geometry in source-pixel units for one analysis."""

    def __init__(self, a: Analysis, video: Video):
        w = float(video.width or a.meta["source_size"][0])
        h = float(video.height or a.meta["source_size"][1])
        b = a.boxes
        self.foot = np.stack([(b[:, 0] + b[:, 2]) / 2 * w, b[:, 3] * h], axis=1)
        self.foot_norm = np.stack([(b[:, 0] + b[:, 2]) / 2, b[:, 3]], axis=1)
        self.height = (b[:, 3] - b[:, 1]) * h
        self.height_norm = b[:, 3] - b[:, 1]


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def build_tracks(a: Analysis, geo: _Geo, sample_idx: np.ndarray, valid: np.ndarray, rules: Rules, interval_s: float) -> list[Track]:
    """Greedy-optimal frame-to-frame association for sparse (seconds apart) samples."""
    max_missed = max(0, int(math.floor(rules.track_max_gap_s / interval_s + 1e-9)) - 1)
    tracks: list[Track] = []
    active: list[Track] = []
    for s in sample_idx:
        dets = [d for d in a.detections_at(int(a.frames[s])) if valid[d]]
        if active and dets:
            cost = np.full((len(active), len(dets)), 1e6)
            for ti, t in enumerate(active):
                ld = t.dets[-1]
                gate = rules.track_gate * (1.0 + 0.5 * t.missed)
                for di, d in enumerate(dets):
                    hm = 0.5 * (geo.height[ld] + geo.height[d])
                    dist = float(np.linalg.norm(geo.foot[ld] - geo.foot[d])) / max(hm, 1.0)
                    hr = max(geo.height[ld], geo.height[d]) / max(min(geo.height[ld], geo.height[d]), 1.0)
                    if dist <= gate and hr <= 1.6:
                        sim = _cos(a.feats[ld], a.feats[d])
                        if sim >= rules.track_min_color_sim:
                            cost[ti, di] = dist + 0.3 * (1.0 - sim)
            rows, cols = linear_sum_assignment(cost)
            used_t, used_d = set(), set()
            for r, c in zip(rows, cols):
                if cost[r, c] < 1e5:
                    active[r].dets.append(dets[c])
                    active[r].samples.append(int(s))
                    active[r].missed = 0
                    used_t.add(r)
                    used_d.add(c)
            for ti, t in enumerate(active):
                if ti not in used_t:
                    t.missed += 1
            new = [Track(id=len(tracks) + k, dets=[dets[c]], samples=[int(s)]) for k, c in enumerate(i for i in range(len(dets)) if i not in used_d)]
        else:
            for t in active:
                t.missed += 1
            new = [Track(id=len(tracks) + k, dets=[d], samples=[int(s)]) for k, d in enumerate(dets)]
        tracks.extend(new)
        active = [t for t in active if t.missed <= max_missed] + new
    return tracks


def stable_window(times: np.ndarray, fx: np.ndarray, fy: np.ndarray, hh: np.ndarray, rules: Rules, max_gap_s: float) -> tuple[float, int, int, int]:
    """Longest stable window in a sequence of in-zone samples.

    Returns ``(longest_seconds, n_samples_of_longest, start_i, end_i)``. A
    window is stable when foot movement stays within ``max_foot_spread`` box
    heights and box height varies by at most ``max_height_ratio``.
    """
    n = len(times)
    best = (0.0, 0, -1, -1)
    if n == 0:
        return best
    mins = {k: deque() for k in ("x", "y", "h")}
    maxs = {k: deque() for k in ("x", "y", "h")}
    vals = {"x": fx, "y": fy, "h": hh}
    left = 0

    def push(j: int) -> None:
        for k, arr in vals.items():
            dq = mins[k]
            while dq and arr[dq[-1]] >= arr[j]:
                dq.pop()
            dq.append(j)
            dq = maxs[k]
            while dq and arr[dq[-1]] <= arr[j]:
                dq.pop()
            dq.append(j)

    def pop_left(i: int) -> None:
        for k in vals:
            if mins[k] and mins[k][0] == i:
                mins[k].popleft()
            if maxs[k] and maxs[k][0] == i:
                maxs[k].popleft()

    def ok() -> bool:
        hmin, hmax = hh[mins["h"][0]], hh[maxs["h"][0]]
        if hmax > rules.max_height_ratio * max(hmin, 1e-6):
            return False
        spread = max(fx[maxs["x"][0]] - fx[mins["x"][0]], fy[maxs["y"][0]] - fy[mins["y"][0]])
        return spread <= rules.max_foot_spread * 0.5 * (hmin + hmax)

    for j in range(n):
        if j > 0 and times[j] - times[j - 1] > max_gap_s + 1e-6:
            while left < j:
                pop_left(left)
                left += 1
        push(j)
        while left < j and not ok():
            pop_left(left)
            left += 1
        dur = float(times[j] - times[left])
        cnt = j - left + 1
        if (dur, cnt) > best[:2]:
            best = (dur, cnt, left, j)
    return best


def count_video(project: Project, video: Video, a: Analysis) -> tuple[list[Visit], dict]:
    rules = project.rules
    interval = video.sample_interval_s
    spf = video.seconds_per_frame
    geo = _Geo(a, video)
    valid_score = a.scores >= rules.min_score
    big_enough = geo.height_norm >= rules.min_box_height
    pieces = resolve_pieces(video)
    visits: list[Visit] = []
    info_pieces = []
    for piece in pieces:
        entry = {"start": piece.start, "end": piece.end, "kind": piece.kind, "camera": piece.camera,
                 "seconds": round((piece.end - piece.start) * spf, 3)}
        if piece.kind != "camera":
            entry["observable"] = False
            info_pieces.append(entry)
            continue
        cam = project.camera(piece.camera)
        entry["observable"] = True
        entry["zone"] = "full_frame_unconfigured" if cam.zone is None else "configured"
        info_pieces.append(entry)
        lo = np.searchsorted(a.frames, piece.start, side="left")
        hi = np.searchsorted(a.frames, piece.end, side="left")
        sample_idx = np.arange(lo, hi)
        if len(sample_idx) == 0:
            continue
        usable = valid_score.copy()
        if cam.exclude:
            usable &= ~(box_iou(a.boxes, cam.exclude).max(axis=1) >= rules.exclude_iou)
        in_zone = points_in_polygon(geo.foot_norm, cam.zone) & big_enough & usable
        tracks = build_tracks(a, geo, sample_idx, usable, rules, interval)
        qualified = []
        for t in tracks:
            zi = [k for k, d in enumerate(t.dets) if in_zone[d]]
            if len(zi) < rules.min_stable_samples:
                continue
            dets = np.asarray([t.dets[k] for k in zi])
            times = a.frames[np.asarray([t.samples[k] for k in zi])].astype(np.float64) * spf
            dur, cnt, i0, i1 = stable_window(times, geo.foot[dets, 0], geo.foot[dets, 1], geo.height[dets], rules, rules.track_max_gap_s)
            if cnt >= rules.min_stable_samples and dur + 1e-6 >= rules.min_dwell_s:
                qualified.append({"track": t, "dets": dets, "times": times, "stable_s": dur, "stable_n": cnt})
        groups = _merge(qualified, a, geo, rules)
        first_t = float(a.frames[sample_idx[0]]) * spf
        last_t = float(a.frames[sample_idx[-1]]) * spf
        for g in groups:
            visits.append(_make_visit(g, project, video, a, geo, piece, first_t, last_t, in_zone, usable))
    visits.sort(key=lambda v: (v.start_frame, v.foot[0]))
    seen: dict[str, int] = {}
    for v in visits:
        if v.key in seen:
            seen[v.key] += 1
            v.key = f"{v.key}-{chr(ord('a') + seen[v.key])}"
        else:
            seen[v.key] = 0
    observable = sum(p["seconds"] for p in info_pieces if p.get("observable"))
    summary = {
        "video_id": video.id,
        "label": video.label,
        "n_frames": video.n_frames,
        "seconds_per_frame": spf,
        "frame_step": video.frame_step,
        "sample_interval_s": interval,
        "coverage_s": video.coverage_s,
        "observable_s": round(observable, 3),
        "unobservable_s": round(video.coverage_s - observable, 3),
        "pieces": info_pieces,
        "analysis_key": a.meta.get("key"),
        "model_sha256": a.meta.get("model_sha256"),
        "n_visits": len(visits),
    }
    return visits, summary


def _merge(qualified: list[dict], a: Analysis, geo: _Geo, rules: Rules) -> list[list[dict]]:
    for q in qualified:
        q["t0"], q["t1"] = float(q["times"][0]), float(q["times"][-1])
        q["feat"] = a.feats[q["dets"]].mean(axis=0)
    order = sorted(range(len(qualified)), key=lambda i: qualified[i]["t0"])
    links = []
    for ii, i in enumerate(order):
        A = qualified[i]
        for j in order[ii + 1:]:
            B = qualified[j]
            gap = B["t0"] - A["t1"]
            if gap > rules.merge_gap_s:
                break
            if gap <= 0:
                continue  # seen at the same time -> two different people
            da, db = A["dets"][-1], B["dets"][0]
            hm = 0.5 * (geo.height[da] + geo.height[db])
            dist = float(np.linalg.norm(geo.foot[da] - geo.foot[db]) / max(hm, 1.0))
            hr = float(max(geo.height[da], geo.height[db]) / max(min(geo.height[da], geo.height[db]), 1.0))
            sim = _cos(A["feat"], B["feat"])
            if dist <= rules.merge_max_dist and hr <= 1.5 and sim >= rules.merge_min_color_sim:
                score = dist + (1.0 - sim) + 0.5 * gap / max(rules.merge_gap_s, 1e-6)
                links.append((score, i, j, {"gap_s": round(gap, 2), "dist": round(dist, 3), "color_sim": round(sim, 3)}))
    links.sort(key=lambda x: x[0])
    nxt: dict[int, int] = {}
    prv: dict[int, int] = {}
    chain_of = {i: [i] for i in range(len(qualified))}
    reasons: dict[tuple[int, int], dict] = {}
    for _, i, j, why in links:
        if i in nxt or j in prv:
            continue
        ca, cb = chain_of[i], chain_of[j]
        if ca is cb:
            continue
        if any(_overlap(qualified[x], qualified[y]) for x in ca for y in cb):
            continue
        nxt[i], prv[j] = j, i
        merged = ca + cb
        for m in merged:
            chain_of[m] = merged
        reasons[(i, j)] = why
    groups, seen = [], set()
    for i in order:
        if id(chain_of[i]) in seen:
            continue
        seen.add(id(chain_of[i]))
        members = sorted(chain_of[i], key=lambda k: qualified[k]["t0"])
        g = [qualified[k] for k in members]
        for x, y in zip(members, members[1:]):
            g_reason = reasons.get((x, y))
            if g_reason:
                qualified[y]["merge_reason"] = {"from_track": qualified[x]["track"].id, "to_track": qualified[y]["track"].id, **g_reason}
        groups.append(g)
    return groups


def _overlap(A: dict, B: dict) -> bool:
    return not (A["t1"] < B["t0"] or B["t1"] < A["t0"])


def _make_visit(g, project, video, a, geo, piece: Piece, first_t, last_t, in_zone, valid_score) -> Visit:
    rules = project.rules
    spf = video.seconds_per_frame
    dets = np.concatenate([q["dets"] for q in g])
    times = np.concatenate([q["times"] for q in g])
    order = np.argsort(times, kind="stable")
    dets, times = dets[order], times[order]
    frames = (times / spf).round().astype(int)
    start_f, end_f = int(frames[0]), int(frames[-1]) + video.frame_step
    foot = np.median(geo.foot_norm[dets], axis=0)
    box = np.median(a.boxes[dets], axis=0)
    flags = []
    if times[0] <= first_t + 1e-6:
        flags.append("truncated_start")
    if times[-1] >= last_t - 1e-6:
        flags.append("truncated_end")
    if times[0] - first_t <= rules.merge_gap_s or last_t - times[-1] <= rules.merge_gap_s:
        flags.append("near_boundary")
    stable_s = max(q["stable_s"] for q in g)
    if stable_s < 2 * rules.min_dwell_s:
        flags.append("short")
    mean_score = float(a.scores[dets].mean())
    if mean_score < 0.5:
        flags.append("low_score")
    if len(g) > 1:
        flags.append("merged")
    # Very long and almost perfectly still: often a person-like object, not a person.
    spread = max(np.ptp(geo.foot[dets, 0]), np.ptp(geo.foot[dets, 1])) / max(float(np.median(geo.height[dets])), 1.0)
    if times[-1] - times[0] >= 120 and spread < 0.06:
        flags.append("long_static")
    # crowded: other confident in-zone people standing close by
    crowd = 0
    for d, f in zip(dets[:: max(1, len(dets) // 8)], frames[:: max(1, len(dets) // 8)]):
        others = [o for o in a.detections_at(int(f)) if o != d and valid_score[o] and in_zone[o]]
        near = [o for o in others if np.linalg.norm(geo.foot[o] - geo.foot[d]) <= geo.height[d]]
        crowd = max(crowd, len(near))
    if crowd >= 2:
        flags.append("crowded")
    pick = np.unique(np.linspace(0, len(dets) - 1, num=min(6, len(dets))).round().astype(int))
    evidence = [{"frame": int(frames[k]), "box": [round(float(x), 4) for x in a.boxes[dets[k]]]} for k in pick]
    key = f"{video.id[:6]}-{piece.camera}-{start_f:07d}-{int(foot[0] * 99):02d}{int(foot[1] * 99):02d}"
    return Visit(
        key=key,
        video_id=video.id,
        video_label=video.label,
        camera=piece.camera,
        piece=[piece.start, piece.end],
        start_frame=start_f,
        end_frame=end_f,
        start_s=round(float(times[0]), 3),
        end_s=round(float(times[-1]) + video.sample_interval_s, 3),
        duration_s=round(float(times[-1] - times[0]) + video.sample_interval_s, 3),
        longest_stable_s=round(float(stable_s), 3),
        n_samples=int(len(dets)),
        tracks=[int(q["track"].id) for q in g],
        merges=[q["merge_reason"] for q in g if "merge_reason" in q],
        foot=[round(float(foot[0]), 4), round(float(foot[1]), 4)],
        box=[round(float(x), 4) for x in box],
        mean_score=round(mean_score, 3),
        evidence=evidence,
        flags=flags,
    )


def rules_fingerprint(project: Project) -> str:
    return stable_hash(
        {
            "count_version": COUNT_VERSION,
            "rules": asdict(project.rules),
            "cameras": [asdict(c) for c in project.cameras],
            "segments": {v.id: [asdict(s) for s in v.segments] for v in project.videos},
        }
    )


def count_project(project: Project, analyses: dict[str, Analysis]) -> dict:
    all_visits: list[Visit] = []
    summaries = []
    for v in project.videos:
        a = analyses.get(v.id)
        if a is None:
            continue
        visits, summary = count_video(project, v, a)
        all_visits.extend(visits)
        summaries.append(summary)
    return {
        "count_version": COUNT_VERSION,
        "rules_hash": rules_fingerprint(project),
        "rules": asdict(project.rules),
        "videos": summaries,
        "visits": [v.to_dict() for v in all_visits],
    }
