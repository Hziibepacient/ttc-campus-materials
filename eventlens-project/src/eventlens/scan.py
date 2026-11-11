"""Suggest blackout, camera-move and camera-view segments from cached scene stats.

Suggestions are proposals for a human to confirm on boundary frames; they are
never applied silently.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .analyze import Analysis


@dataclass
class ScanParams:
    dark_luma: float = 22.0
    dark_std: float = 12.0
    unstable_ncc: float = 0.72
    same_view_ncc: float = 0.82
    min_view_samples: int = 5
    drift_ncc: float = 0.85


def _znorm(thumbs: np.ndarray) -> np.ndarray:
    t = thumbs.astype(np.float32)
    t = np.stack([cv2.GaussianBlur(x, (3, 3), 0) for x in t]) if len(t) else t
    flat = t.reshape(len(t), -1)
    flat = flat - flat.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(flat, axis=1, keepdims=True)
    norm[norm < 1e-6] = 1.0
    return flat / norm


def _ncc(a: np.ndarray, b: np.ndarray, keep: float = 0.8) -> float:
    """Correlation that ignores the most-changed pixels (people), so only a view change lowers it."""
    d = np.abs(a - b)
    m = d <= np.quantile(d, keep)
    x, y = a[m] - a[m].mean(), b[m] - b[m].mean()
    den = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / den) if den > 1e-9 else 0.0


def scene_stats(a: Analysis, params: ScanParams | None = None) -> dict[str, np.ndarray]:
    p = params or ScanParams()
    z = _znorm(a.thumbs)
    dark = (a.luma < p.dark_luma) & (a.luma_std < p.dark_std)
    ncc_prev = np.ones(len(z), np.float32)
    for i in range(1, len(z)):
        ncc_prev[i] = _ncc(z[i], z[i - 1])
    ncc_prev[dark] = np.nan
    return {"z": z, "dark": dark, "ncc_prev": ncc_prev}


def suggest_segments(a: Analysis, step: int, params: ScanParams | None = None) -> dict:
    p = params or ScanParams()
    st = scene_stats(a, p)
    z, dark, ncc_prev = st["z"], st["dark"], st["ncc_prev"]
    frames = a.frames
    n = len(frames)

    def raw_span(i: int, j: int) -> list[int]:  # samples [i, j] inclusive -> raw [start, end)
        return [int(frames[i]), int(frames[j]) + step]

    blackouts = _runs(dark)
    unstable = np.zeros(n, bool)
    for i in range(1, n):
        if dark[i] or dark[i - 1]:
            continue
        if ncc_prev[i] < p.unstable_ncc:
            unstable[i - 1] = unstable[i] = True
    # Stable runs: not dark, consecutive samples similar to each other.
    stable_mask = ~dark
    runs = []
    i = 0
    while i < n:
        if not stable_mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and stable_mask[j + 1] and not (np.isfinite(ncc_prev[j + 1]) and ncc_prev[j + 1] < p.unstable_ncc):
            j += 1
        runs.append([i, j])
        i = j + 1
    # Merge neighbouring runs that show the same view (e.g. someone walked past the lens).
    merged: list[list[int]] = []
    for r in runs:
        if merged:
            prev = merged[-1]
            a_ref = _median_ref(z, prev[1] - 4, prev[1])
            b_ref = _median_ref(z, r[0], r[0] + 4)
            gap_dark = dark[prev[1] + 1: r[0]].any()
            if _ncc(a_ref, b_ref) >= p.same_view_ncc and (r[0] - prev[1] <= 3 or not gap_dark):
                prev[1] = r[1]
                continue
        merged.append(r)
    views, moves = [], []
    cam_refs: list[np.ndarray] = []
    for r in merged:
        length = r[1] - r[0] + 1
        if length < p.min_view_samples:
            moves.append(raw_span(r[0], r[1]))
            continue
        ref = _median_ref(z, r[0], min(r[1], r[0] + 19))
        # Re-use a camera id when the view matches an earlier one (camera put back).
        cam_idx = next((k for k, cr in enumerate(cam_refs) if _ncc(cr, ref) >= p.same_view_ncc), None)
        if cam_idx is None:
            cam_refs.append(ref)
            cam_idx = len(cam_refs) - 1
        sims = np.array([_ncc(z[k], ref) for k in range(r[0], r[1] + 1)])
        low = np.where(sims < p.drift_ncc)[0]
        views.append(
            {
                "camera": f"cam{cam_idx + 1}",
                "frames": raw_span(r[0], r[1]),
                "samples": int(length),
                "min_similarity_to_start": round(float(sims.min()), 3),
                "drift_from_frame": int(frames[r[0] + low[0]]) if len(low) else None,
            }
        )
    # Gaps between views that are not blackouts are camera moves / unusable frames.
    covered = sorted([v["frames"] for v in views] + [raw_span(b0, b1) for b0, b1 in blackouts])
    pos = 0
    end_all = int(frames[-1]) + step if n else 0
    for s, e in covered:
        if s > pos:
            moves.append([pos, s])
        pos = max(pos, e)
    if pos < end_all:
        moves.append([pos, end_all])
    moves = _merge_spans(moves)
    segments = [{"start": v["frames"][0], "end": v["frames"][1], "kind": "camera", "camera": v["camera"]} for v in views]
    segments += [{"start": s, "end": e, "kind": "blackout", "camera": None} for s, e in (raw_span(b0, b1) for b0, b1 in blackouts)]
    return {
        "blackouts": [raw_span(b0, b1) for b0, b1 in blackouts],
        "moves_or_unusable": moves,
        "views": views,
        "suggested_segments": sorted(segments, key=lambda s: s["start"]),
        "boundary_frames": sorted({f for v in views for f in (v["frames"][0], v["frames"][1] - step)}),
    }


def _median_ref(z: np.ndarray, i: int, j: int) -> np.ndarray:
    i = max(i, 0)
    j = min(max(j, i), len(z) - 1)
    ref = np.median(z[i: j + 1], axis=0)
    ref = ref - ref.mean()
    nrm = np.linalg.norm(ref)
    return ref / nrm if nrm > 1e-6 else ref


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _merge_spans(spans: list[list[int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out
