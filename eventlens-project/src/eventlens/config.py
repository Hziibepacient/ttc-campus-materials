"""Project configuration: one JSON file shared by every command.

Frame numbers are raw frame indices of the source file, starting at 0. Every
interval is half-open: ``[start, end)``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_NAME = "eventlens.json"
SCHEMA_VERSION = 1
SEGMENT_KINDS = ("camera", "blackout", "skip")


class ConfigError(ValueError):
    """A configuration problem that the user (or the assistant) must fix."""


def stable_hash(obj: Any, length: int = 12) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:length]


def eventlens_home() -> Path:
    return Path(os.environ.get("EVENTLENS_HOME", Path.home() / ".eventlens"))


@dataclass
class Segment:
    start: int
    end: int
    kind: str = "camera"
    camera: str | None = None
    note: str = ""


@dataclass
class Video:
    id: str
    path: str
    label: str
    seconds_per_frame: float
    frame_step: int = 1
    n_frames: int | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""
    fingerprint: str = ""
    start_time: str | None = None
    start_time_verified: bool = False
    segments: list[Segment] = field(default_factory=list)

    @property
    def sample_interval_s(self) -> float:
        return self.seconds_per_frame * self.frame_step

    @property
    def coverage_s(self) -> float:
        """Sampling coverage: frames x seconds-per-frame (handoff convention)."""
        return (self.n_frames or 0) * self.seconds_per_frame

    def scene_seconds(self, frame: int) -> float:
        return frame * self.seconds_per_frame


@dataclass
class Camera:
    id: str
    zone: list[list[float]] | None = None
    exclude: list[list[float]] = field(default_factory=list)  # boxes x1,y1,x2,y2 (0-1) of person-like objects
    note: str = ""


@dataclass
class DetectorConfig:
    model: str = "yolox-s"
    model_path: str | None = None
    conf: float = 0.30
    nms: float = 0.45
    provider: str = "auto"
    decode_width: int = 960
    previews: bool = True
    preview_quality: int = 75


@dataclass
class Rules:
    min_dwell_s: float = 4.0
    min_stable_samples: int = 3
    max_foot_spread: float = 0.22
    max_height_ratio: float = 1.35
    min_box_height: float = 0.12
    min_score: float = 0.30
    track_gate: float = 0.5
    track_min_color_sim: float = 0.3
    track_max_gap_s: float = 4.0
    exclude_iou: float = 0.5
    merge_gap_s: float = 30.0
    merge_max_dist: float = 0.8
    merge_min_color_sim: float = 0.55
    time_bin_s: float = 600.0


# Plain-language meaning of every rule, shown by `eventlens rules show`.
RULE_HELP: dict[str, str] = {
    "min_dwell_s": "Minimum stay: first and last stable sample must be at least this many scene seconds apart.",
    "min_stable_samples": "Minimum number of samples inside one stable window.",
    "max_foot_spread": "Max foot-point movement inside a stable window, as a fraction of the person's box height.",
    "max_height_ratio": "Max ratio between tallest and shortest box in a stable window (filters walking towards/away).",
    "min_box_height": "Ignore people whose box is shorter than this fraction of the frame height (too far away).",
    "min_score": "Ignore detections below this confidence.",
    "track_gate": "Max foot movement between two samples (fraction of box height) to keep the same track.",
    "track_min_color_sim": "Min clothing-colour similarity (0-1) to keep the same track between two samples.",
    "exclude_iou": "A detection overlapping a camera's exclusion box by at least this IoU is ignored (standee, poster, mannequin).",
    "track_max_gap_s": "A track survives this many scene seconds of missed detections (occlusion).",
    "merge_gap_s": "Track pieces separated by at most this many scene seconds may be merged into one visit; a longer absence counts as a new visit.",
    "merge_max_dist": "Max foot distance (fraction of box height) between pieces that are merged.",
    "merge_min_color_sim": "Min clothing-colour similarity (0-1) between pieces that are merged.",
    "time_bin_s": "Report bin width in scene seconds.",
}


@dataclass
class Project:
    name: str
    root: Path
    videos: list[Video] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    rules: Rules = field(default_factory=Rules)
    schema_version: int = SCHEMA_VERSION

    # -- persistence -----------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def work_dir(self) -> Path:
        return self.root / ".eventlens"

    @property
    def reviews_path(self) -> Path:
        return self.root / "reviews.jsonl"

    @property
    def groundtruth_path(self) -> Path:
        return self.root / "groundtruth.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "videos": [asdict(v) for v in self.videos],
            "cameras": [asdict(c) for c in self.cameras],
            "detector": asdict(self.detector),
            "rules": asdict(self.rules),
        }

    def save(self) -> None:
        self.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.config_path)

    @classmethod
    def load(cls, root: Path | str) -> "Project":
        root = Path(root).resolve()
        path = root / CONFIG_NAME
        if not path.exists():
            raise ConfigError(f"No {CONFIG_NAME} in {root}. Run `eventlens init` first.")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version", 1) > SCHEMA_VERSION:
            raise ConfigError("This project was written by a newer EventLens; upgrade the tool.")
        videos = []
        for v in data.get("videos", []):
            segs = [Segment(**s) for s in v.pop("segments", [])]
            videos.append(Video(**_known(Video, v), segments=segs))
        proj = cls(
            name=data.get("name", root.name),
            root=root,
            videos=videos,
            cameras=[Camera(**_known(Camera, c)) for c in data.get("cameras", [])],
            detector=DetectorConfig(**_known(DetectorConfig, data.get("detector", {}))),
            rules=Rules(**_known(Rules, data.get("rules", {}))),
        )
        proj.validate()
        return proj

    # -- lookups -----------------------------------------------------------
    def video(self, ref: str) -> Video:
        hits = [v for v in self.videos if v.id == ref or v.label == ref]
        if not hits:
            hits = [v for v in self.videos if v.id.startswith(ref)]
        if len(hits) != 1:
            names = ", ".join(f"{v.label} ({v.id})" for v in self.videos) or "none"
            raise ConfigError(f"Video '{ref}' not found or ambiguous. Known videos: {names}")
        return hits[0]

    def camera(self, cam_id: str, create: bool = False) -> Camera:
        for c in self.cameras:
            if c.id == cam_id:
                return c
        if create:
            cam = Camera(id=cam_id)
            self.cameras.append(cam)
            return cam
        raise ConfigError(f"Camera '{cam_id}' not found. Known: {', '.join(c.id for c in self.cameras) or 'none'}")

    def video_paths_resolved(self, video: Video) -> Path:
        p = Path(video.path)
        return p if p.is_absolute() else (self.root / p)

    # -- validation ----------------------------------------------------------
    def validate(self) -> None:
        cam_ids = {c.id for c in self.cameras}
        if len(cam_ids) != len(self.cameras):
            raise ConfigError("Duplicate camera ids.")
        for cam in self.cameras:
            if cam.zone is not None:
                if len(cam.zone) < 3:
                    raise ConfigError(f"Zone of camera '{cam.id}' needs at least 3 points.")
                for pt in cam.zone:
                    if len(pt) != 2 or not all(-0.01 <= float(x) <= 1.01 for x in pt):
                        raise ConfigError(f"Zone of camera '{cam.id}' must use normalized 0-1 coordinates.")
            for b in cam.exclude:
                if len(b) != 4 or not (0 <= b[0] < b[2] <= 1.001 and 0 <= b[1] < b[3] <= 1.001):
                    raise ConfigError(f"Exclusion box {b} of camera '{cam.id}' must be x1,y1,x2,y2 in 0-1 with x1<x2, y1<y2.")
        ids = [v.id for v in self.videos]
        if len(set(ids)) != len(ids):
            raise ConfigError("The same video file was added twice.")
        for v in self.videos:
            if v.seconds_per_frame <= 0 or not math.isfinite(v.seconds_per_frame):
                raise ConfigError(f"Video {v.label}: seconds_per_frame must be positive.")
            if v.frame_step < 1:
                raise ConfigError(f"Video {v.label}: frame_step must be >= 1.")
            cams = []
            for s in v.segments:
                if s.kind not in SEGMENT_KINDS:
                    raise ConfigError(f"Video {v.label}: unknown segment kind '{s.kind}'.")
                if not (0 <= s.start < s.end):
                    raise ConfigError(f"Video {v.label}: bad segment [{s.start},{s.end}).")
                if v.n_frames is not None and s.end > v.n_frames:
                    raise ConfigError(f"Video {v.label}: segment [{s.start},{s.end}) exceeds {v.n_frames} frames.")
                if s.kind == "camera":
                    if not s.camera:
                        raise ConfigError(f"Video {v.label}: camera segment [{s.start},{s.end}) needs a camera id.")
                    if s.camera not in cam_ids:
                        raise ConfigError(f"Video {v.label}: camera '{s.camera}' is not defined.")
                    cams.append(s)
            cams.sort(key=lambda s: s.start)
            for a, b in zip(cams, cams[1:]):
                if b.start < a.end:
                    raise ConfigError(
                        f"Video {v.label}: camera segments [{a.start},{a.end}) and [{b.start},{b.end}) overlap."
                    )


def _known(cls, data: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


@dataclass(frozen=True)
class Piece:
    """A resolved, non-overlapping frame interval of one video."""

    start: int
    end: int
    kind: str  # camera | blackout | skip | unconfigured
    camera: str | None = None


def resolve_pieces(video: Video) -> list[Piece]:
    """Resolve possibly overlapping segments into disjoint pieces covering [0, n_frames).

    Blackout and skip segments override camera segments; frames covered by no
    segment are ``unconfigured`` and never counted.
    """
    n = video.n_frames
    if n is None:
        n = max([s.end for s in video.segments], default=0)
    cuts = {0, n}
    for s in video.segments:
        cuts.add(min(max(s.start, 0), n))
        cuts.add(min(max(s.end, 0), n))
    bounds = sorted(cuts)
    pieces: list[Piece] = []
    for a, b in zip(bounds, bounds[1:]):
        if a >= b:
            continue
        cover = [s for s in video.segments if s.start <= a and s.end >= b]
        if any(s.kind == "blackout" for s in cover):
            p = Piece(a, b, "blackout")
        elif any(s.kind == "skip" for s in cover):
            p = Piece(a, b, "skip")
        else:
            cam = [s for s in cover if s.kind == "camera"]
            p = Piece(a, b, "camera", cam[0].camera) if cam else Piece(a, b, "unconfigured")
        if pieces and pieces[-1].kind == p.kind and pieces[-1].camera == p.camera and pieces[-1].end == p.start:
            pieces[-1] = Piece(pieces[-1].start, p.end, p.kind, p.camera)
        else:
            pieces.append(p)
    return pieces


def format_hms(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"
