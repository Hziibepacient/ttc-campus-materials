"""Reports: one data model for JSON, CSV and HTML so no two outputs can disagree."""

from __future__ import annotations

import csv
import html
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .analyze import Analysis
from .config import RULE_HELP, Project, format_hms
from .count import count_project
from .review import load_events, now_iso, reconcile

STATE_TEXT = {
    "machine_estimate": "机器估算（未经人工核验）",
    "spot_checked": "抽查修正后的估计（部分人工核验）",
    "fully_reviewed": "逐条人工核验",
}
STATUS_TEXT = {
    "machine_estimate": "待核验",
    "reviewed_confirmed": "人工确认",
    "reviewed_rejected": "人工排除",
    "reviewed_duplicate": "人工判为重复",
    "unresolved": "无法确认",
}


def sensitivity(project: Project, analyses: dict[str, Analysis], values=(4.0, 6.0, 10.0, 20.0, 60.0)) -> list[dict]:
    """Full recounts (not re-filters) with different minimum stays."""
    rows = []
    base = project.rules
    for val in values:
        project.rules = replace(base, min_dwell_s=float(val))
        try:
            res = count_project(project, analyses)
        finally:
            project.rules = base
        rows.append({"min_dwell_s": val, "machine_total": len(res["visits"]),
                     "by_video": {s["label"]: s["n_visits"] for s in res["videos"]}})
    return rows


def time_bins(project: Project, result: dict, visits: list[dict]) -> list[dict]:
    width = project.rules.time_bin_s
    rows = []
    for vs in result["videos"]:
        video = project.video(vs["video_id"])
        spf = video.seconds_per_frame
        n_bins = int(max(1, -(-vs["coverage_s"] // width)))
        for b in range(n_bins):
            t0, t1 = b * width, min((b + 1) * width, vs["coverage_s"])
            obs = 0.0
            for p in vs["pieces"]:
                if p.get("observable"):
                    obs += max(0.0, min(t1, p["end"] * spf) - max(t0, p["start"] * spf))
            in_bin = [v for v in visits if v["video_id"] == video.id and t0 <= v["start_s"] < t1]
            row = {
                "video_id": video.id, "video_label": video.label, "bin": b,
                "start_rel": format_hms(t0), "end_rel": format_hms(t1),
                "observable_s": round(obs, 1),
                "machine_visits": len(in_bin),
                "confirmed": sum(1 for v in in_bin if v["status"] == "reviewed_confirmed"),
                "rejected_or_duplicate": sum(1 for v in in_bin if v["status"] in ("reviewed_rejected", "reviewed_duplicate")),
            }
            if video.start_time:
                try:
                    st = datetime.fromisoformat(video.start_time)
                    row["clock_start"] = (st + timedelta(seconds=t0)).strftime("%H:%M:%S")
                    row["clock_verified"] = video.start_time_verified
                except ValueError:
                    pass
            rows.append(row)
    return rows


def build(project: Project, analyses: dict[str, Analysis], with_sensitivity: bool = True) -> dict:
    result = count_project(project, analyses)
    visits = result["visits"]
    events = load_events(project.reviews_path)
    review = reconcile(visits, events)
    missing = [v.label for v in project.videos if v.id not in analyses]
    warnings = []
    if missing:
        warnings.append(f"未分析的视频没有计入：{', '.join(missing)}")
    for cam in project.cameras:
        if cam.zone is None:
            warnings.append(f"机位 {cam.id} 还没有设置附近区域，当前按全画面统计。")
    for vs in result["videos"]:
        if not any(p["kind"] == "camera" for p in vs["pieces"]):
            warnings.append(f"{vs['label']} 没有任何机位区间，没有可统计的画面。")
    per_video = []
    for vs in result["videos"]:
        vv = [v for v in visits if v["video_id"] == vs["video_id"]]
        by_cam: dict[str, int] = {}
        for v in vv:
            by_cam[v["camera"]] = by_cam.get(v["camera"], 0) + 1
        per_video.append({**vs, "by_camera": by_cam,
                          "confirmed": sum(1 for v in vv if v["status"] == "reviewed_confirmed"),
                          "excluded": sum(1 for v in vv if v["status"] in ("reviewed_rejected", "reviewed_duplicate")),
                          "unresolved": sum(1 for v in vv if v["status"] == "unresolved")})
    analysis_meta = {vid: {k: a.meta.get(k) for k in ("key", "model", "model_sha256", "provider", "decode_size", "n_samples", "n_detections", "timing", "tool_version", "created_at")}
                     for vid, a in analyses.items()}
    return {
        "tool": "eventlens",
        "tool_version": __version__,
        "generated_at": now_iso(),
        "project": project.name,
        "state": review["state"],
        "state_text": STATE_TEXT[review["state"]],
        "headline": _headline(review),
        "counts": {k: review[k] for k in ("machine_total", "corrected_estimate", "confirmed", "confirmed_from_machine", "manual_adds",
                                         "orphan_confirms", "rejected", "duplicates", "unresolved", "pending", "human_review_coverage",
                                         "agent_suggestions_on_pending")},
        "rules": result["rules"],
        "rules_hash": result["rules_hash"],
        "cameras": [asdict(c) for c in project.cameras],
        "videos": per_video,
        "time_bins": time_bins(project, result, visits),
        "sensitivity": sensitivity(project, analyses) if with_sensitivity else [],
        "analysis": analysis_meta,
        "visits": visits,
        "manual_visits": review["manual_visits"],
        "orphaned_decisions": review["orphaned_decisions"],
        "warnings": warnings,
        "not_verified": [
            "未经人工核验的记录只是机器估算，不代表准确人数。",
            "人次不是独立人数：同一人离开超过合并间隔后返回会再计一次。",
            "不跨机位、黑屏和移动区间做身份匹配；这些区间不可观察，不补算。",
            "AI 助手的核验建议单独列出，不计入人工确认。",
            "检测准确率与人次误差需用标注窗口（eventlens eval）实测。",
        ],
    }


def _headline(r: dict) -> str:
    if r["state"] == "machine_estimate":
        return f"机器估算 {r['machine_total']} 人次（未经人工核验，不是已数准的人数）"
    if r["state"] == "fully_reviewed":
        return f"逐条核验 {r['confirmed']} 人次"
    return (f"抽查修正后估计 {r['corrected_estimate']} 人次：人工确认 {r['confirmed']}，"
            f"排除 {r['rejected'] + r['duplicates']}，无法确认 {r['unresolved']}，未核验 {r['pending']}")


CSV_FIELDS = ["visit_key", "video_id", "video_label", "camera", "start_frame", "end_frame", "start_rel", "end_rel",
              "start_s", "end_s", "duration_s", "longest_stable_s", "n_samples", "evidence_frames", "merge_basis",
              "flags", "status", "human_decision", "agent_suggestion", "review_note"]


def _csv_row(v: dict) -> dict:
    h = (v.get("review") or {}).get("human") or {}
    ag = (v.get("review") or {}).get("agent") or {}
    return {
        "visit_key": v["key"], "video_id": v["video_id"], "video_label": v["video_label"], "camera": v["camera"],
        "start_frame": v["start_frame"], "end_frame": v["end_frame"],
        "start_rel": format_hms(v["start_s"]), "end_rel": format_hms(v["end_s"]),
        "start_s": v["start_s"], "end_s": v["end_s"], "duration_s": v["duration_s"],
        "longest_stable_s": v["longest_stable_s"], "n_samples": v["n_samples"],
        "evidence_frames": " ".join(str(e["frame"]) for e in v["evidence"]),
        "merge_basis": json.dumps({"tracks": v["tracks"], "merges": v["merges"]}, ensure_ascii=False),
        "flags": " ".join(v["flags"]), "status": v["status"],
        "human_decision": h.get("decision") or "", "agent_suggestion": ag.get("decision") or "",
        "review_note": h.get("note") or ag.get("note") or "",
    }


def write(project: Project, rep: dict, out_dir: Path, evidence_dir_rel: str | None = "evidence") -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    p = out_dir / "report.json"
    p.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=_jsonable), encoding="utf-8")
    paths["json"] = p
    p = out_dir / "visits.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for v in rep["visits"]:
            w.writerow(_csv_row(v))
    paths["visits_csv"] = p
    p = out_dir / "unresolved.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for v in rep["visits"]:
            if v["status"] == "unresolved":
                w.writerow(_csv_row(v))
    paths["unresolved_csv"] = p
    p = out_dir / "time_bins.csv"
    if rep["time_bins"]:
        keys = sorted({k for r in rep["time_bins"] for k in r}, key=lambda k: list(rep["time_bins"][0]).index(k) if k in rep["time_bins"][0] else 99)
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rep["time_bins"])
        paths["time_bins_csv"] = p
    p = out_dir / "report.html"
    p.write_text(_html(rep, evidence_dir_rel), encoding="utf-8")
    paths["html"] = p
    return paths


def _jsonable(o):
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    raise TypeError(f"{type(o).__name__} is not JSON serialisable")


def _e(x) -> str:
    return html.escape(str(x))


def _html(rep: dict, ev: str | None) -> str:
    c = rep["counts"]
    vids = "".join(
        f"<tr><td>{_e(v['label'])}<div class=sub>{_e(v['video_id'])}</div></td><td>{v['n_frames']}</td>"
        f"<td>{format_hms(v['coverage_s'])}</td><td>{format_hms(v['observable_s'])}</td><td>{format_hms(v['unobservable_s'])}</td>"
        f"<td><b>{v['n_visits']}</b></td><td>{v['confirmed']}</td><td>{v['excluded']}</td><td>{v['unresolved']}</td>"
        f"<td>{_e(', '.join(f'{k}: {n}' for k, n in v['by_camera'].items()))}</td></tr>"
        for v in rep["videos"])
    pieces = "".join(
        f"<tr><td>{_e(v['label'])}</td><td>[{p['start']}, {p['end']})</td><td>{_e(p['kind'])}</td><td>{_e(p.get('camera') or '')}</td>"
        f"<td>{format_hms(p['seconds'])}</td><td>{'可统计' if p.get('observable') else '不可观察，不补算'}</td></tr>"
        for v in rep["videos"] for p in v["pieces"])
    sens = "".join(f"<tr><td>{r['min_dwell_s']:g} 秒</td><td>{r['machine_total']}</td></tr>" for r in rep["sensitivity"])
    rules = "".join(f"<tr><td><code>{_e(k)}</code></td><td>{_e(val)}</td><td>{_e(RULE_HELP.get(k, ''))}</td></tr>" for k, val in rep["rules"].items())
    bins = "".join(
        f"<tr><td>{_e(b['video_label'])}</td><td>{b['start_rel']}–{b['end_rel']}{(' (' + b['clock_start'] + ('' if b.get('clock_verified') else '，未核实') + ')') if b.get('clock_start') else ''}</td>"
        f"<td>{format_hms(b['observable_s'])}</td><td>{b['machine_visits']}</td><td>{b['confirmed']}</td></tr>" for b in rep["time_bins"])
    rows = []
    for v in rep["visits"]:
        img = f"<img loading=lazy src='{ev}/{_e(v['key'])}.jpg'>" if ev else ""
        rows.append(
            f"<div class='visit s-{_e(v['status'])}'><div class=vh><b>{_e(v['key'])}</b> · {_e(v['video_label'])} · {_e(v['camera'])} · "
            f"帧 [{v['start_frame']}, {v['end_frame']}) · {format_hms(v['start_s'])}–{format_hms(v['end_s'])} · 停留约 {v['duration_s']:g} 秒 · "
            f"<span class=st>{_e(STATUS_TEXT.get(v['status'], v['status']))}</span>"
            f"{' · AI 建议：' + _e(v['review']['agent']['decision']) if v.get('review', {}).get('agent') else ''}"
            f"{' · <span class=fl>' + _e(' '.join(v['flags'])) + '</span>' if v['flags'] else ''}</div>{img}</div>")
    warn = "".join(f"<li>{_e(w)}</li>" for w in rep["warnings"])
    notv = "".join(f"<li>{_e(w)}</li>" for w in rep["not_verified"])
    model = next(iter(rep["analysis"].values()), {}) if rep["analysis"] else {}
    return f"""<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{_e(rep['project'])} · EventLens</title><style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a66;--line:#e4e1da;--acc:#2f5d8a;--warn:#9a5b00;--ok:#2d7a3e;--bad:#a23b3b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--mut:#a3a19a;--line:#33322f;--acc:#8db6e0;--warn:#e0a85a;--ok:#7cc48b;--bad:#e08a8a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:17px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.head{{font-size:22px;font-weight:650;margin:12px 0 4px}}.state{{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--warn);color:var(--warn);font-size:13px}}
.sub{{color:var(--mut);font-size:12px}}table{{border-collapse:collapse;width:100%;font-size:13.5px}}td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
.scroll{{overflow-x:auto}}.visit{{border:1px solid var(--line);border-radius:8px;margin:10px 0;padding:8px}}.visit img{{max-width:100%;display:block;margin-top:6px;border-radius:4px}}
.vh{{font-size:13px}}.st{{font-weight:600}}.s-reviewed_confirmed .st{{color:var(--ok)}}.s-reviewed_rejected .st,.s-reviewed_duplicate .st{{color:var(--bad)}}.s-unresolved .st{{color:var(--warn)}}
.fl{{color:var(--mut)}}code{{font-size:12.5px}}ul{{margin:6px 0}}
</style></head><body><main>
<h1>{_e(rep['project'])}</h1><div class=sub>EventLens {_e(rep['tool_version'])} · 生成于 {_e(rep['generated_at'])} · 规则 {_e(rep['rules_hash'])}</div>
<div class=head>{_e(rep['headline'])}</div><span class=state>{_e(rep['state_text'])}</span>
<p class=sub>机器记录 {c['machine_total']} 条 · 人工确认 {c['confirmed']} · 排除 {c['rejected'] + c['duplicates']} · 无法确认 {c['unresolved']} · 未核验 {c['pending']} · 补录漏计 {c['manual_adds']} · 人工核验覆盖 {'' if c['human_review_coverage'] is None else f"{c['human_review_coverage'] * 100:.1f}%"}</p>
{'<h2>注意</h2><ul>' + warn + '</ul>' if warn else ''}
<h2>各视频</h2><div class=scroll><table><tr><th>视频</th><th>原始帧</th><th>采样覆盖</th><th>可统计</th><th>不可观察</th><th>机器记录</th><th>确认</th><th>排除</th><th>无法确认</th><th>按机位</th></tr>{vids}</table></div>
<h2>分时段（相对素材起点）</h2><div class=scroll><table><tr><th>视频</th><th>时段</th><th>可统计时长</th><th>机器记录</th><th>确认</th></tr>{bins}</table></div>
<h2>区间与可观察性</h2><div class=scroll><table><tr><th>视频</th><th>帧区间</th><th>类型</th><th>机位</th><th>折合现场</th><th>说明</th></tr>{pieces}</table></div>
<h2>停留门槛敏感性（每行都是完整重算）</h2><table><tr><th>最短停留</th><th>机器记录</th></tr>{sens}</table>
<p class=sub>这只说明结果依赖门槛，不是误差范围或置信区间。</p>
<h2>本次使用的规则</h2><div class=scroll><table><tr><th>参数</th><th>值</th><th>含义</th></tr>{rules}</table></div>
<p class=sub>模型 {_e(model.get('model', ''))} · SHA-256 {_e(model.get('model_sha256', ''))} · 推理 {_e(model.get('provider', ''))}</p>
<h2>尚未验证的内容</h2><ul>{notv}</ul>
<h2>逐条记录</h2>{''.join(rows)}
</main></body></html>"""
