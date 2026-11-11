"""Images for humans and assistants: evidence strips, contact sheets, zone previews."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .analyze import Analysis
from .config import Project, Video, format_hms
from .media import read_frames

GREEN = (60, 200, 60)
YELLOW = (0, 210, 255)
GREY = (170, 170, 170)
RED = (60, 60, 230)


def load_frames(project: Project, video: Video, a: Analysis, frames: list[int]) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    missing = []
    for f in frames:
        p = a.preview_path(f)
        img = cv2.imread(str(p)) if p.exists() else None
        if img is None:
            missing.append(f)
        else:
            out[f] = img
    if missing:
        w, h = a.decode_size
        out.update(read_frames(project.video_paths_resolved(video), missing, w, h))
    return out


def _poly_px(zone, w, h) -> np.ndarray | None:
    if not zone:
        return None
    return np.array([[int(x * w), int(y * h)] for x, y in zone], np.int32)


def annotate(img: np.ndarray, a: Analysis, frame: int, zone=None, highlight=None, show_all=True, numbered=False, min_score=0.0) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    poly = _poly_px(zone, w, h)
    if poly is not None:
        overlay = out.copy()
        cv2.fillPoly(overlay, [poly], (255, 200, 0))
        out = cv2.addWeighted(overlay, 0.12, out, 0.88, 0)
        cv2.polylines(out, [poly], True, (255, 200, 0), 2)
    if show_all:
        fs = max(0.42, w / 900)
        for n, d in enumerate(a.detections_at(frame)):
            if a.scores[d] < min_score:
                continue
            x1, y1, x2, y2 = (a.boxes[d] * [w, h, w, h]).astype(int)
            cv2.rectangle(out, (x1, y1), (x2, y2), YELLOW if numbered else GREY, 1)
            if numbered:
                _label(out, f"{n}", (x1, max(14, y1 - 3)), YELLOW, fs)
    if highlight is not None:
        x1, y1, x2, y2 = (np.asarray(highlight) * [w, h, w, h]).astype(int)
        cv2.rectangle(out, (x1, y1), (x2, y2), YELLOW, 2)
        cv2.circle(out, ((x1 + x2) // 2, y2), 4, YELLOW, -1)
    return out


def _label(img, text, org, color=(255, 255, 255), scale=0.5) -> None:
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def caption(img: np.ndarray, text: str) -> np.ndarray:
    bar = np.full((22, img.shape[1], 3), 30, np.uint8)
    cv2.putText(bar, text, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 240, 240), 1, cv2.LINE_AA)
    return np.vstack([img, bar])


def grid(images: list[np.ndarray], cols: int, pad: int = 4) -> np.ndarray:
    if not images:
        return np.zeros((10, 10, 3), np.uint8)
    h = max(i.shape[0] for i in images)
    w = max(i.shape[1] for i in images)
    rows = (len(images) + cols - 1) // cols
    sheet = np.full((rows * (h + pad) + pad, cols * (w + pad) + pad, 3), 18, np.uint8)
    for k, im in enumerate(images):
        r, c = divmod(k, cols)
        y, x = pad + r * (h + pad), pad + c * (w + pad)
        sheet[y: y + im.shape[0], x: x + im.shape[1]] = im
    return sheet


def visit_strip(project: Project, video: Video, a: Analysis, visit: dict, crop_h: int = 220) -> np.ndarray:
    """Context frame + zoomed crops along the visit, for review."""
    zone = project.camera(visit["camera"]).zone
    frames = [e["frame"] for e in visit["evidence"]]
    imgs = load_frames(project, video, a, frames)
    tiles = []
    first = visit["evidence"][len(visit["evidence"]) // 2]
    ctx = annotate(imgs[first["frame"]], a, first["frame"], zone, first["box"])
    ctx_h = crop_h
    ctx = cv2.resize(ctx, (int(ctx.shape[1] * ctx_h / ctx.shape[0]), ctx_h))
    tiles.append(caption(ctx, f"{visit['key']}  {visit['camera']}"))
    for e in visit["evidence"]:
        img = annotate(imgs[e["frame"]], a, e["frame"], None, e["box"], show_all=False)
        H, W = img.shape[:2]
        x1, y1, x2, y2 = np.asarray(e["box"]) * [W, H, W, H]
        bh = max(y2 - y1, 20)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        half = bh * 1.1
        X1, Y1 = int(max(0, cx - half * 0.8)), int(max(0, cy - half))
        X2, Y2 = int(min(W, cx + half * 0.8)), int(min(H, cy + half))
        crop = img[Y1:Y2, X1:X2]
        if crop.size == 0:
            continue
        crop = cv2.resize(crop, (int(crop.shape[1] * crop_h / crop.shape[0]), crop_h))
        t = video.scene_seconds(e["frame"])
        tiles.append(caption(crop, f"f{e['frame']} {format_hms(t)}"))
    hmax = max(t.shape[0] for t in tiles)
    tiles = [np.vstack([t, np.full((hmax - t.shape[0], t.shape[1], 3), 18, np.uint8)]) if t.shape[0] < hmax else t for t in tiles]
    sep = np.full((hmax, 4, 3), 18, np.uint8)
    row = []
    for t in tiles:
        row += [t, sep]
    return np.hstack(row[:-1])


def frames_sheet(project: Project, video: Video, a: Analysis, frames: list[int], camera: str | None = None, cols: int = 4, width: int = 480, numbered: bool = True) -> np.ndarray:
    zone = project.camera(camera).zone if camera else None
    imgs = load_frames(project, video, a, frames)
    tiles = []
    for f in frames:
        if f not in imgs:
            continue
        img = cv2.resize(imgs[f], (width, int(imgs[f].shape[0] * width / imgs[f].shape[1])), interpolation=cv2.INTER_AREA)
        img = annotate(img, a, f, zone, None, show_all=True, numbered=numbered, min_score=project.rules.min_score)
        n = int(sum(a.scores[a.detections_at(f)] >= project.rules.min_score))
        tiles.append(caption(img, f"frame {f}  t={format_hms(video.scene_seconds(f))}  people={n}"))
    return grid(tiles, cols)


def zone_preview(project: Project, video: Video, a: Analysis, frame: int, camera: str, zone=None) -> np.ndarray:
    """Frame with a labelled 0-1 coordinate grid and the camera's zone, for picking points."""
    imgs = load_frames(project, video, a, [frame])
    img = annotate(imgs[frame], a, frame, zone if zone is not None else project.camera(camera).zone, None, show_all=True)
    h, w = img.shape[:2]
    for k in range(1, 10):
        x, y = int(w * k / 10), int(h * k / 10)
        cv2.line(img, (x, 0), (x, h), (255, 255, 255), 1, cv2.LINE_AA)
        cv2.line(img, (0, y), (w, y), (255, 255, 255), 1, cv2.LINE_AA)
        _label(img, f"{k / 10:.1f}", (x + 3, 14), (255, 255, 255), 0.45)
        _label(img, f"{k / 10:.1f}", (3, y - 4), (255, 255, 255), 0.45)
    return caption(img, f"{video.label} frame {frame} camera {camera}  (x right, y down, 0-1)")


def save(img: np.ndarray, path: Path, quality: int = 85) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if ext in (".jpg", ".jpeg") else []
    ok, enc = cv2.imencode(ext or ".jpg", img, params)
    if not ok:
        raise RuntimeError(f"Could not encode {path}")
    path.write_bytes(enc.tobytes())
    return path
