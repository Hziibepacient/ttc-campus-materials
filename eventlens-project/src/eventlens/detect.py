"""Person detection backends. The rest of the pipeline only sees ``Detector``."""

from __future__ import annotations

import hashlib
import platform
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from .config import DetectorConfig, eventlens_home

# Only models whose bytes were verified are listed; anything else goes through
# DetectorConfig.model_path and is fingerprinted at load time.
MODELS: dict[str, dict] = {
    "yolox-s": {
        "file": "yolox_s.onnx",
        "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx",
        "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        "size": 35858002,
        "license": "Apache-2.0 (Megvii YOLOX)",
        "family": "yolox",
    },
}


class Detector(Protocol):
    name: str
    model_sha256: str
    provider: str

    def detect(self, bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(boxes[N,4] as x1,y1,x2,y2 pixels of bgr, scores[N])`` for people."""
        ...


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def model_path(cfg: DetectorConfig) -> Path:
    if cfg.model_path:
        return Path(cfg.model_path).expanduser()
    spec = MODELS.get(cfg.model)
    if not spec:
        raise ValueError(f"Unknown model '{cfg.model}'. Known: {', '.join(MODELS)}; or set detector.model_path.")
    return eventlens_home() / "models" / spec["file"]


def fetch_model(name: str, force: bool = False) -> Path:
    spec = MODELS[name]
    dest = eventlens_home() / "models" / spec["file"]
    if dest.exists() and not force and sha256_file(dest) == spec["sha256"]:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(spec["url"], timeout=60) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    got = sha256_file(tmp)
    if got != spec["sha256"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded {name} has sha256 {got}, expected {spec['sha256']}; refusing to use it.")
    tmp.replace(dest)
    return dest


def available_providers() -> list[str]:
    import onnxruntime as ort

    return list(ort.get_available_providers())


def pick_providers(requested: str) -> list[str]:
    avail = available_providers()
    alias = {
        "cpu": "CPUExecutionProvider",
        "coreml": "CoreMLExecutionProvider",
        "cuda": "CUDAExecutionProvider",
        "dml": "DmlExecutionProvider",
        "directml": "DmlExecutionProvider",
    }
    if requested and requested != "auto":
        name = alias.get(requested.lower(), requested)
        if name not in avail:
            raise RuntimeError(f"Provider {name} is not available here. Available: {', '.join(avail)}")
        return [name, "CPUExecutionProvider"] if name != "CPUExecutionProvider" else [name]
    order = ["CUDAExecutionProvider", "DmlExecutionProvider"]
    if platform.system() == "Darwin":
        order.insert(0, "CoreMLExecutionProvider")
    for name in order:
        if name in avail:
            return [name, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


@dataclass
class _Letterbox:
    ratio: float


class YoloxOnnx:
    """YOLOX ONNX export from the official Megvii release (raw 0-255 BGR input, letterbox pad 114)."""

    def __init__(self, path: Path, provider: str = "auto", conf: float = 0.3, nms: float = 0.45):
        import onnxruntime as ort

        if not path.exists():
            raise FileNotFoundError(f"Model file {path} is missing. Run `eventlens models fetch` first.")
        self.path = path
        self.model_sha256 = sha256_file(path)
        self.name = path.stem
        self.conf = conf
        self.nms = nms
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        last_err: Exception | None = None
        self.session = None
        for providers in (pick_providers(provider), ["CPUExecutionProvider"]):
            try:
                self.session = ort.InferenceSession(str(path), sess_options=opts, providers=providers)
                break
            except Exception as err:  # provider init failures fall back to CPU
                last_err = err
        if self.session is None:
            raise RuntimeError(f"Could not load {path}: {last_err}")
        self.provider = self.session.get_providers()[0]
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.size = int(inp.shape[2]) if isinstance(inp.shape[2], int) else 640
        self._grid, self._stride = self._make_grid(self.size)

    @staticmethod
    def _make_grid(size: int) -> tuple[np.ndarray, np.ndarray]:
        grids, strides = [], []
        for st in (8, 16, 32):
            n = size // st
            yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), st))
        return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)

    def _preprocess(self, bgr: np.ndarray) -> tuple[np.ndarray, _Letterbox]:
        h, w = bgr.shape[:2]
        r = min(self.size / h, self.size / w)
        nw, nh = int(round(w * r)), int(round(h * r))
        canvas = np.full((self.size, self.size, 3), 114, np.uint8)
        canvas[:nh, :nw] = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = canvas.transpose(2, 0, 1)[None].astype(np.float32)
        return np.ascontiguousarray(blob), _Letterbox(r)

    def detect(self, bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        blob, lb = self._preprocess(bgr)
        out = self.session.run(None, {self.input_name: blob})[0][0]
        scores = out[:, 4] * out[:, 5]  # objectness x person-class probability
        keep = scores >= self.conf
        if not np.any(keep):
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)
        o = out[keep]
        xy = (o[:, :2] + self._grid[keep]) * self._stride[keep]
        wh = np.exp(o[:, 2:4]) * self._stride[keep]
        boxes = np.concatenate([xy - wh / 2, xy + wh / 2], axis=1) / lb.ratio
        sc = scores[keep]
        xywh = np.concatenate([boxes[:, :2], boxes[:, 2:] - boxes[:, :2]], axis=1)
        idx = cv2.dnn.NMSBoxes(xywh.tolist(), sc.tolist(), self.conf, self.nms)
        idx = np.array(idx, dtype=np.int64).reshape(-1)
        h, w = bgr.shape[:2]
        boxes = boxes[idx]
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h)
        return boxes.astype(np.float32), sc[idx].astype(np.float32)


def load_detector(cfg: DetectorConfig) -> YoloxOnnx:
    # Every registered model and every custom model_path is a YOLOX-format ONNX export today.
    return YoloxOnnx(model_path(cfg), cfg.provider, cfg.conf, cfg.nms)


# -- appearance ---------------------------------------------------------------

FEATURE_VERSION = 1
_H_BINS, _S_BINS = 12, 3
FEATURE_DIM = 2 * (_H_BINS * _S_BINS + 4)


def appearance(bgr: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    """Clothing colour descriptor: HSV histograms of torso and legs, L2-normalised."""
    feats = np.zeros((len(boxes), FEATURE_DIM), np.float32)
    if len(boxes) == 0:
        return feats
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    H, W = bgr.shape[:2]
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        bh, bw = y2 - y1, x2 - x1
        cx1, cx2 = int(max(0, x1 + 0.2 * bw)), int(min(W, x2 - 0.2 * bw))
        parts = []
        for a, b in ((0.18, 0.50), (0.50, 0.85)):
            ry1, ry2 = int(max(0, y1 + a * bh)), int(min(H, y1 + b * bh))
            crop = hsv[ry1:ry2, cx1:cx2].reshape(-1, 3)
            parts.append(_hist(crop))
        v = np.concatenate(parts)
        n = np.linalg.norm(v)
        feats[i] = v / n if n > 0 else v
    return feats


def _hist(px: np.ndarray) -> np.ndarray:
    out = np.zeros(_H_BINS * _S_BINS + 4, np.float32)
    if len(px) == 0:
        return out
    h, s, v = px[:, 0].astype(np.int32), px[:, 1].astype(np.int32), px[:, 2].astype(np.int32)
    chromatic = (s >= 40) & (v >= 40)
    hb = (h * _H_BINS // 180).clip(0, _H_BINS - 1)
    sb = ((s - 40) * _S_BINS // 216).clip(0, _S_BINS - 1)
    np.add.at(out, hb[chromatic] * _S_BINS + sb[chromatic], 1.0)
    # achromatic pixels: dark / grey / light / white by value
    vb = (v[~chromatic] * 4 // 256).clip(0, 3)
    np.add.at(out, _H_BINS * _S_BINS + vb, 1.0)
    return np.sqrt(out / len(px))
