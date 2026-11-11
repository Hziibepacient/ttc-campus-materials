"""Find person-like objects that never move (standees, posters, mannequins, doorways).

A box that the detector reports at the same place for a large share of a
camera's samples is almost never a person standing there all day; it should
be confirmed on an image and then excluded with ``zone exclude``.
"""

from __future__ import annotations

import numpy as np

from .analyze import Analysis
from .config import Project, Video, resolve_pieces
from .count import box_iou


def find_statics(project: Project, video: Video, a: Analysis, camera: str | None = None,
                 min_presence: float = 0.3, iou: float = 0.6, max_candidates: int = 8) -> list[dict]:
    out = []
    for p in resolve_pieces(video):
        if p.kind != "camera" or (camera and p.camera != camera):
            continue
        lo, hi = np.searchsorted(a.frames, p.start), np.searchsorted(a.frames, p.end)
        n_samples = hi - lo
        if n_samples < 10:
            continue
        d0 = np.searchsorted(a.det_frame, a.frames[lo])
        d1 = np.searchsorted(a.det_frame, a.frames[hi - 1], side="right")
        idx = np.arange(d0, d1)
        idx = idx[a.scores[idx] >= project.rules.min_score]
        if len(idx) == 0:
            continue
        boxes = a.boxes[idx]
        # Seed candidates from the most repeated coarse positions, then measure with IoU.
        cx = ((boxes[:, 0] + boxes[:, 2]) / 2 / 0.02).astype(int)
        cy = (boxes[:, 3] / 0.02).astype(int)
        ch = ((boxes[:, 3] - boxes[:, 1]) / 0.04).astype(int)
        keys, counts = np.unique(np.stack([cx, cy, ch], 1), axis=0, return_counts=True)
        taken = np.zeros(len(idx), bool)
        found = 0
        for k in np.argsort(-counts):
            if counts[k] < min_presence * n_samples * 0.3 or found >= max_candidates:
                break
            sel = (cx == keys[k][0]) & (cy == keys[k][1]) & (ch == keys[k][2]) & ~taken
            if not sel.any():
                continue
            ref = np.median(boxes[sel], axis=0)
            members = (box_iou(boxes, [ref.tolist()])[:, 0] >= iou) & ~taken
            frames = np.unique(a.det_frame[idx[members]])
            presence = len(frames) / n_samples
            taken |= members
            if presence < min_presence:
                continue
            found += 1
            out.append({
                "video_id": video.id, "camera": p.camera, "piece": [p.start, p.end],
                "box": [round(float(x), 4) for x in np.median(boxes[members], axis=0)],
                "presence": round(float(presence), 3),
                "mean_score": round(float(a.scores[idx[members]].mean()), 3),
                "example_frame": int(frames[len(frames) // 2]),
            })
    out.sort(key=lambda c: -c["presence"])
    for k, c in enumerate(out, 1):
        c["id"] = k
    return out
