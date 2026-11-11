"""Review decisions: append-only log, reconciled against every recount.

Human decisions are the only source of ``reviewed_*`` statuses. Decisions by
an AI assistant are kept as suggestions and reported separately; they never
turn a machine estimate into a confirmed count.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DECISIONS = ("confirm", "reject", "duplicate", "unsure")
REVIEWERS = ("human", "agent")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_event(path: Path, event: dict) -> dict:
    event = {"ts": now_iso(), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def anchor_of(visit: dict) -> dict:
    return {
        "video_id": visit["video_id"],
        "camera": visit["camera"],
        "start_frame": visit["start_frame"],
        "end_frame": visit["end_frame"],
        "foot": visit["foot"],
    }


def decision_event(visit: dict, decision: str, by: str, note: str = "", duplicate_of: str | None = None, rules_hash: str | None = None) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if by not in REVIEWERS:
        raise ValueError(f"reviewer must be one of {REVIEWERS}")
    if decision == "duplicate" and not duplicate_of:
        raise ValueError("a duplicate decision needs the key of the visit it duplicates")
    return {
        "type": "decision",
        "key": visit["key"],
        "decision": decision,
        "by": by,
        "duplicate_of": duplicate_of,
        "note": note,
        "anchor": anchor_of(visit),
        "rules_hash": rules_hash,
    }


def manual_add_event(video_id: str, camera: str, start_frame: int, end_frame: int, foot: list[float], note: str) -> dict:
    return {
        "type": "add",
        "key": f"manual-{uuid.uuid4().hex[:8]}",
        "by": "human",
        "note": note,
        "anchor": {"video_id": video_id, "camera": camera, "start_frame": int(start_frame), "end_frame": int(end_frame), "foot": foot},
    }


def undo_event(key: str, by: str) -> dict:
    return {"type": "undo", "key": key, "by": by}


def _effective(events: list[dict]) -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
    """Latest decision per (key, reviewer) and live manual additions, after undos."""
    stacks: dict[tuple[str, str], list[dict]] = {}
    adds: dict[str, dict] = {}
    for e in events:
        t = e.get("type")
        if t == "decision":
            stacks.setdefault((e["key"], e["by"]), []).append(e)
        elif t == "add":
            adds[e["key"]] = e
        elif t == "undo":
            if e["key"] in adds and e.get("by", "human") == "human":
                adds.pop(e["key"])
            elif stacks.get((e["key"], e.get("by", "human"))):
                stacks[(e["key"], e.get("by", "human"))].pop()
    latest = {k: v[-1] for k, v in stacks.items() if v}
    return latest, adds


def _match(anchor: dict, visits: list[dict], by_key: dict[str, int]) -> int | None:
    """Find the current visit a past decision refers to (exact key, else overlap + position)."""
    best, best_score = None, 0.0
    for i, v in enumerate(visits):
        if v["video_id"] != anchor["video_id"] or v["camera"] != anchor["camera"]:
            continue
        inter = min(v["end_frame"], anchor["end_frame"]) - max(v["start_frame"], anchor["start_frame"])
        if inter <= 0:
            continue
        union = max(v["end_frame"], anchor["end_frame"]) - min(v["start_frame"], anchor["start_frame"])
        iou = inter / max(union, 1)
        dist = float(np.hypot(v["foot"][0] - anchor["foot"][0], v["foot"][1] - anchor["foot"][1]))
        if iou >= 0.3 and dist <= 0.08:
            score = iou - dist
            if score > best_score:
                best, best_score = i, score
    return best


def reconcile(visits: list[dict], events: list[dict]) -> dict:
    """Attach review state to the current visits and compute status counts."""
    latest, adds = _effective(events)
    by_key = {v["key"]: i for i, v in enumerate(visits)}
    human: dict[int, dict] = {}
    agent: dict[int, dict] = {}
    orphans: list[dict] = []
    for (key, by), ev in latest.items():
        idx = by_key.get(key)
        if idx is None:
            idx = _match(ev["anchor"], visits, by_key)
        if idx is None:
            orphans.append(ev)
            continue
        target = human if by == "human" else agent
        prev = target.get(idx)
        if prev is None or prev["ts"] <= ev["ts"]:
            target[idx] = ev
    for i, v in enumerate(visits):
        h, ag = human.get(i), agent.get(i)
        v["review"] = {"human": _brief(h), "agent": _brief(ag)}
        if h is None:
            v["status"] = "machine_estimate"
        else:
            v["status"] = {
                "confirm": "reviewed_confirmed",
                "reject": "reviewed_rejected",
                "duplicate": "reviewed_duplicate",
                "unsure": "unresolved",
            }[h["decision"]]
    manual = [
        {"key": k, "status": "reviewed_confirmed", "source": "manual_add", **e["anchor"], "note": e.get("note", "")}
        for k, e in adds.items()
    ]
    orphan_confirms = [o for o in orphans if o["by"] == "human" and o["decision"] == "confirm"]
    c = {s: sum(1 for v in visits if v["status"] == s) for s in
         ("machine_estimate", "reviewed_confirmed", "reviewed_rejected", "reviewed_duplicate", "unresolved")}
    machine_total = len(visits)
    confirmed = c["reviewed_confirmed"] + len(manual) + len(orphan_confirms)
    reviewed = c["reviewed_confirmed"] + c["reviewed_rejected"] + c["reviewed_duplicate"]
    pending = c["machine_estimate"]
    if reviewed == 0 and not manual and not orphan_confirms and c["unresolved"] == 0:
        state = "machine_estimate"
    elif pending == 0 and c["unresolved"] == 0:
        state = "fully_reviewed"
    else:
        state = "spot_checked"
    agent_flags = {d: sum(1 for i in agent if agent[i]["decision"] == d and i not in human) for d in DECISIONS}
    return {
        "state": state,
        "machine_total": machine_total,
        "corrected_estimate": machine_total - c["reviewed_rejected"] - c["reviewed_duplicate"] + len(manual) + len(orphan_confirms),
        "confirmed": confirmed,
        "confirmed_from_machine": c["reviewed_confirmed"],
        "manual_adds": len(manual),
        "orphan_confirms": len(orphan_confirms),
        "rejected": c["reviewed_rejected"],
        "duplicates": c["reviewed_duplicate"],
        "unresolved": c["unresolved"],
        "pending": pending,
        "human_review_coverage": round(reviewed / machine_total, 4) if machine_total else None,
        "agent_suggestions_on_pending": agent_flags,
        "manual_visits": manual,
        "orphaned_decisions": orphans,
    }


def _brief(ev: dict | None) -> dict | None:
    if ev is None:
        return None
    return {k: ev.get(k) for k in ("decision", "duplicate_of", "note", "ts")}
