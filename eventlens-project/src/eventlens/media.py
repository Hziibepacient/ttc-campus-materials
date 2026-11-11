"""FFmpeg/FFprobe access: probing, content fingerprints and fast frame streaming."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator

import numpy as np


class MediaError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    return _find_bin("EVENTLENS_FFMPEG", "ffmpeg")


def ffprobe_bin() -> str:
    return _find_bin("EVENTLENS_FFPROBE", "ffprobe")


def _find_bin(env: str, name: str) -> str:
    override = os.environ.get(env)
    if override:
        if not Path(override).exists():
            raise MediaError(f"{env} points to {override}, which does not exist.")
        return override
    found = shutil.which(name)
    if not found:
        raise MediaError(f"{name} not found. Install FFmpeg and add it to PATH, or set {env}.")
    return found


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    n_frames: int | None
    duration_s: float | None
    codec: str
    pix_fmt: str


def probe(path: Path | str) -> VideoInfo:
    cmd = [
        ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,pix_fmt:format=duration",
        "-of", "json", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise MediaError(f"ffprobe could not read {path}: {res.stderr.strip()[:300]}")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise MediaError(f"No video stream in {path}.")
    st = streams[0]
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            fr = Fraction(st.get(key, "0/1"))
            if fr > 0:
                fps = float(fr)
                break
        except (ValueError, ZeroDivisionError):
            continue
    n = st.get("nb_frames")
    duration = data.get("format", {}).get("duration")
    return VideoInfo(
        width=int(st["width"]),
        height=int(st["height"]),
        fps=fps,
        n_frames=int(n) if n not in (None, "N/A") else None,
        duration_s=float(duration) if duration not in (None, "N/A") else None,
        codec=st.get("codec_name", ""),
        pix_fmt=st.get("pix_fmt", ""),
    )


def fingerprint(path: Path | str, chunk: int = 4 << 20) -> str:
    """Content fingerprint that survives renames and machine moves."""
    path = Path(path)
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(size - chunk, chunk))
            h.update(f.read(chunk))
    return h.hexdigest()


def default_hwaccel() -> str | None:
    if os.environ.get("EVENTLENS_HWACCEL"):
        val = os.environ["EVENTLENS_HWACCEL"]
        return None if val in ("none", "off", "") else val
    if platform.system() == "Darwin":
        # Measured on Apple Silicon: software HEVC decode + downscale (≈580 fps) beats
        # VideoToolbox with its GPU->CPU copy (≈370 fps), and inference runs on the ANE/GPU.
        return None
    return "auto"


def decode_size(width: int, height: int, target_width: int) -> tuple[int, int]:
    w = min(target_width, width) if target_width else width
    w -= w % 2
    h = int(round(height * w / width / 2.0)) * 2
    return w, h


def iter_frames(
    path: Path | str,
    width: int,
    height: int,
    step: int = 1,
    hwaccel: str | None = None,
    queue_size: int = 16,
) -> Iterator[tuple[int, np.ndarray]]:
    """Yield ``(raw_frame_index, bgr_frame)`` for every ``step``-th decoded frame.

    Frames are decoded in order with passthrough timing, so the k-th yielded
    frame is raw frame ``k * step`` of the file.
    """
    filters = []
    if step > 1:
        filters.append(f"select='not(mod(n\\,{step}))'")
    filters.append(f"scale={width}:{height}:flags=area")
    cmd = [ffmpeg_bin(), "-nostdin", "-v", "error"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    cmd += [
        "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-fps_mode", "passthrough", "-vf", ",".join(filters),
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    frame_bytes = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes * 4)
    q: queue.Queue = queue.Queue(maxsize=queue_size)
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                buf = proc.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    break
                q.put(buf)
        finally:
            q.put(None)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    k = 0
    try:
        while True:
            buf = q.get()
            if buf is None:
                break
            yield k * step, np.frombuffer(buf, np.uint8).reshape(height, width, 3)
            k += 1
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
        while t.is_alive():
            try:
                q.get_nowait()
            except queue.Empty:
                t.join(timeout=0.05)
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait()
        if proc.returncode not in (0, -9, None) and k == 0:
            raise MediaError(f"ffmpeg failed on {path}: {err.strip()[:400]}")


def read_frames(path: Path | str, indices: list[int], width: int, height: int) -> dict[int, np.ndarray]:
    """Decode specific raw frames in one sequential pass (fallback when previews are off)."""
    wanted = sorted(set(int(i) for i in indices))
    if not wanted:
        return {}
    out: dict[int, np.ndarray] = {}
    last = wanted[-1]
    want = set(wanted)
    for idx, frame in iter_frames(path, width, height, 1, default_hwaccel()):
        if idx in want:
            out[idx] = frame.copy()
        if idx >= last:
            break
    return out
