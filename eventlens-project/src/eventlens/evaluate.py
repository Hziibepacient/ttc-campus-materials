"""Ground-truth windows and per-category error analysis (misses, duplicates, false merges, false positives)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from .analyze import Analysis
from .config import Project, resolve_pieces
from .count import points_in_polygon

MATCH_DIST = 0.12  # normalised foot distance for a machine visit to explain a labelled one


def load_gt(path: Path) -> dict:
    if not path.exists():
        return {"windows": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_gt(path: Path, gt: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(gt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def window(gt: dict, wid: str) -> dict:
    for w in gt["windows"]:
        if w["id"] == wid:
            return w
    raise KeyError(f"No ground-truth window '{wid}'.")


def propose_windows(project: Project, analyses: dict[str, Analysis], debug_per_camera: int = 2, test_per_camera: int = 1,
                    length_samples: int = 60, seed: int = 7, min_separation_s: float = 120.0) -> list[dict]:
    """Pick labelling windows per camera: typical and crowded for debugging, a separated one for testing."""
    rng = random.Random(seed)
    rules = project.rules
    cands: dict[str, list[dict]] = {}
    for v in project.videos:
        a = analyses.get(v.id)
        if a is None:
            continue
        foot = np.stack([(a.boxes[:, 0] + a.boxes[:, 2]) / 2, a.boxes[:, 3]], axis=1)
        hn = a.boxes[:, 3] - a.boxes[:, 1]
        for p in resolve_pieces(v):
            if p.kind != "camera":
                continue
            zone = project.camera(p.camera).zone
            ok = (a.scores >= rules.min_score) & (hn >= rules.min_box_height) & points_in_polygon(foot, zone)
            lo = np.searchsorted(a.frames, p.start)
            hi = np.searchsorted(a.frames, p.end)
            per_sample = np.array([int(ok[a.detections_at(int(a.frames[s]))].sum()) for s in range(lo, hi)])
            L = min(length_samples, hi - lo)
            if L < 5:
                continue
            for s0 in range(0, (hi - lo) - L + 1, max(1, L // 2)):
                f0 = int(a.frames[lo + s0])
                f1 = int(a.frames[lo + s0 + L - 1]) + v.frame_step
                near_edge = s0 == 0 or s0 + L >= (hi - lo)
                cands.setdefault(p.camera, []).append({
                    "video_id": v.id, "camera": p.camera, "start": f0, "end": f1,
                    "density": float(per_sample[s0: s0 + L].mean()), "near_edge": near_edge,
                    "t0": f0 * v.seconds_per_frame, "t1": f1 * v.seconds_per_frame,
                })
    out = []
    for cam, cs in cands.items():
        if not cs:
            continue
        chosen: list[dict] = []
        dens = np.array([c["density"] for c in cs])
        targets = [("typical", np.percentile(dens, 50)), ("crowded", np.percentile(dens, 90)), ("boundary", None)]
        for tag, target in targets[:max(debug_per_camera, 0)]:
            pool = [c for c in cs if not any(_close(c, x, 0) for x in chosen)]
            if tag == "boundary":
                pool = [c for c in pool if c["near_edge"]] or pool
                pick = pool[0] if pool else None
            else:
                pick = min(pool, key=lambda c: abs(c["density"] - target)) if pool else None
            if pick:
                chosen.append({**pick, "role": "debug", "tag": tag})
        test_pool = [c for c in cs if not any(_close(c, x, min_separation_s) for x in chosen)]
        rng.shuffle(test_pool)
        for c in test_pool[:test_per_camera]:
            if not any(_close(c, x, min_separation_s) for x in chosen):
                chosen.append({**c, "role": "test", "tag": "held_out"})
        out.extend(chosen)
    for k, w in enumerate(out, 1):
        w["id"] = f"w{k:02d}-{w['camera']}-{w['role']}"
        w["status"] = "todo"
        w["visits"] = []
        w["density"] = round(w["density"], 2)
        for key in ("t0", "t1", "near_edge"):
            w.pop(key, None)
    return out


def _close(a: dict, b: dict, sep_s: float) -> bool:
    if a["video_id"] != b["video_id"]:
        return False
    return not (a["t1"] + sep_s <= b["t0"] or b["t1"] + sep_s <= a["t0"])


def evaluate_window(win: dict, visits: list[dict]) -> dict:
    ms = [v for v in visits if v["video_id"] == win["video_id"] and v["camera"] == win["camera"]
          and v["start_frame"] < win["end"] and v["end_frame"] > win["start"]]
    gs = win.get("visits", [])

    def ov(x0, x1, y0, y1):
        return max(0, min(x1, y1) - max(x0, y0))

    def compat(g, m) -> float:
        o = ov(g["start_frame"], g["end_frame"], max(m["start_frame"], win["start"]), min(m["end_frame"], win["end"]))
        d = float(np.hypot(g["foot"][0] - m["foot"][0], g["foot"][1] - m["foot"][1]))
        if o <= 0 or d > MATCH_DIST:
            return 0.0
        return o / max(g["end_frame"] - g["start_frame"], 1)

    C = np.array([[compat(g, m) for m in ms] for g in gs]) if gs and ms else np.zeros((len(gs), len(ms)))
    matched_g, matched_m = set(), set()
    if C.size:
        rows, cols = linear_sum_assignment(-C)
        for r, c in zip(rows, cols):
            if C[r, c] > 0:
                matched_g.add(r)
                matched_m.add(c)
    false_merges = []
    for j, m in enumerate(ms):
        hits = [i for i in range(len(gs)) if C[i, j] > 0]
        seq = [(gs[i]["start_frame"], gs[i]["end_frame"]) for i in hits]
        if any(ov(a0, a1, b0, b1) == 0 for k, (a0, a1) in enumerate(seq) for (b0, b1) in seq[k + 1:]):
            false_merges.append(m["key"])
    duplicates, false_pos = [], []
    for j, m in enumerate(ms):
        if j in matched_m:
            continue
        if any(C[i, j] > 0 for i in range(len(gs))):
            duplicates.append(m["key"])
        else:
            false_pos.append(m["key"])
    misses = [gs[i].get("id", str(i)) for i in range(len(gs)) if i not in matched_g]
    return {
        "window": win["id"], "role": win.get("role"), "tag": win.get("tag"), "status": win.get("status"),
        "gt_count": len(gs), "machine_count": len(ms), "matched": len(matched_g),
        "misses": misses, "duplicates": duplicates, "false_positives": false_pos, "false_merges": false_merges,
        "count_error": len(ms) - len(gs),
    }


def evaluate(gt: dict, visits: list[dict], role: str | None = None, only_complete: bool = True) -> dict:
    rows = []
    for w in gt["windows"]:
        if role and w.get("role") != role:
            continue
        if only_complete and w.get("status") != "complete":
            continue
        rows.append(evaluate_window(w, visits))
    tot = {k: sum(r[k] for r in rows) for k in ("gt_count", "machine_count", "matched", "count_error")}
    for k in ("misses", "duplicates", "false_positives", "false_merges"):
        tot[k] = sum(len(r[k]) for r in rows)
    tot["abs_count_error"] = sum(abs(r["count_error"]) for r in rows)
    tot["windows"] = len(rows)
    return {"windows": rows, "total": tot}
