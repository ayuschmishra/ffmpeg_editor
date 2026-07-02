"""Project/timeline/asset data model + JSON persistence for the Reel MCP server.

All state here is backend-agnostic: nothing in this module knows about FFmpeg.
Projects are kept in memory while the server runs and persisted as JSON files
under projects/ so they survive a Claude Desktop restart.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
PROJECTS_DIR = BASE_DIR / "projects"
CACHE_DIR = BASE_DIR / "cache_store"
OUTPUT_DIR = BASE_DIR / "output"

for _d in (ASSETS_DIR, PROJECTS_DIR, CACHE_DIR, OUTPUT_DIR):
    _d.mkdir(exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac", ".opus", ".wma"}


class ProjectError(Exception):
    """Raised for invalid operations on project state."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def file_checksum(path: str | Path) -> str:
    """Full SHA-256 of the file contents (chunked, so large files are fine)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def media_type_for(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    raise ProjectError(
        f"Unsupported file extension '{ext}'. Supported: "
        f"images {sorted(IMAGE_EXTS)}, videos {sorted(VIDEO_EXTS)}, audio {sorted(AUDIO_EXTS)}"
    )


@dataclass
class Asset:
    asset_id: str
    media_type: str            # "image" | "video" | "audio"
    original_path: str         # where the file was imported from
    stored_path: str           # our copy inside assets/
    checksum: str
    metadata: dict = field(default_factory=dict)  # width, height, duration, ...


@dataclass
class Scene:
    scene_id: str
    asset_id: str
    duration: float
    effect: str = "none"
    effect_params: dict = field(default_factory=dict)
    cache_key: Optional[str] = None
    cached_clip_path: Optional[str] = None


@dataclass
class Transition:
    scene_a: int               # index of the first scene of the adjacent pair
    scene_b: int               # must be scene_a + 1
    type: str
    duration: float


@dataclass
class SubtitleEntry:
    text: str
    start: float
    end: float


@dataclass
class AudioTrack:
    asset_id: str
    track_type: str            # "music" | "sfx" | "narration"
    start_time: float = 0.0
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0


@dataclass
class Project:
    project_id: str
    name: str
    width: int
    height: int
    fps: int
    assets: dict[str, Asset] = field(default_factory=dict)
    scenes: list[Scene] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    subtitle_srt_path: Optional[str] = None
    subtitle_entries: list[SubtitleEntry] = field(default_factory=list)
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    output_format: str = "mp4"
    created_at: float = field(default_factory=time.time)
    modified_at: float = field(default_factory=time.time)

    # ---- durations ----------------------------------------------------
    def total_duration(self) -> float:
        """Timeline duration: scene durations minus transition overlaps (xfade model)."""
        total = sum(s.duration for s in self.scenes)
        total -= sum(t.duration for t in self.transitions)
        return max(total, 0.0)

    def scene_start(self, index: int) -> float:
        """Timeline start of a scene, accounting for transition overlaps."""
        self.get_scene(index)
        start = sum(s.duration for s in self.scenes[:index])
        start -= sum(t.duration for t in self.transitions if t.scene_a < index)
        return max(start, 0.0)

    # ---- scene access -------------------------------------------------
    def get_scene(self, index: int) -> Scene:
        if not isinstance(index, int) or index < 0 or index >= len(self.scenes):
            raise ProjectError(
                f"scene_index {index} is out of range; project has "
                f"{len(self.scenes)} scene(s) (valid: 0..{len(self.scenes) - 1})."
            )
        return self.scenes[index]

    def get_asset(self, asset_id: str) -> Asset:
        asset = self.assets.get(asset_id)
        if asset is None:
            known = ", ".join(self.assets) or "(none registered)"
            raise ProjectError(
                f"Unknown asset_id '{asset_id}'. Registered assets: {known}. "
                f"Use import_asset first."
            )
        return asset

    def touch(self) -> None:
        self.modified_at = time.time()

    # ---- timeline editing ----------------------------------------------
    # Transitions are anchored to timeline *positions* (the boundary after
    # scene index t.scene_a), not to scene identities. Edits below keep them
    # consistent with that rule.
    def move_scene(self, index: int, new_position: int) -> None:
        self.get_scene(index)
        if not (0 <= new_position < len(self.scenes)):
            raise ProjectError(
                f"new_position {new_position} is out of range (valid: 0..{len(self.scenes) - 1})."
            )
        scene = self.scenes.pop(index)
        self.scenes.insert(new_position, scene)

    def delete_scene(self, index: int) -> "Scene":
        self.get_scene(index)
        removed = self.scenes.pop(index)
        kept = []
        for t in self.transitions:
            if t.scene_a == index or t.scene_b == index:
                continue  # transition touched the deleted scene — drop it
            if t.scene_a > index:
                t.scene_a -= 1
                t.scene_b -= 1
            kept.append(t)
        self.transitions = kept
        return removed

    def duplicate_scene(self, index: int) -> "Scene":
        src = self.get_scene(index)
        copy = Scene(
            scene_id=_new_id("scene"),
            asset_id=src.asset_id,
            duration=src.duration,
            effect=src.effect,
            effect_params=dict(src.effect_params),
            cache_key=src.cache_key,                 # identical inputs ->
            cached_clip_path=src.cached_clip_path,   # reuse the cached clip
        )
        self.scenes.insert(index + 1, copy)
        for t in self.transitions:
            if t.scene_a > index:
                t.scene_a += 1
                t.scene_b += 1
        return copy

    # ---- (de)serialization ---------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            project_id=data["project_id"],
            name=data["name"],
            width=data["width"],
            height=data["height"],
            fps=data["fps"],
            assets={k: Asset(**v) for k, v in data.get("assets", {}).items()},
            scenes=[Scene(**s) for s in data.get("scenes", [])],
            transitions=[Transition(**t) for t in data.get("transitions", [])],
            subtitle_srt_path=data.get("subtitle_srt_path"),
            subtitle_entries=[SubtitleEntry(**e) for e in data.get("subtitle_entries", [])],
            audio_tracks=[AudioTrack(**a) for a in data.get("audio_tracks", [])],
            output_format=data.get("output_format", "mp4"),
            created_at=data.get("created_at", time.time()),
            modified_at=data.get("modified_at", time.time()),
        )


class ProjectStore:
    """In-memory registry of projects with JSON persistence under projects/."""

    def __init__(self, projects_dir: Path = PROJECTS_DIR, assets_dir: Path = ASSETS_DIR):
        self.projects_dir = projects_dir
        self.assets_dir = assets_dir
        self._projects: dict[str, Project] = {}

    # ---- lifecycle ------------------------------------------------------
    def create(self, name: str, width: int, height: int, fps: int) -> Project:
        project = Project(
            project_id=_new_id("proj"), name=name, width=width, height=height, fps=fps
        )
        self._projects[project.project_id] = project
        self.save(project.project_id)
        return project

    def get(self, project_id: str) -> Project:
        """Return a loaded project, transparently loading from disk if needed."""
        if project_id in self._projects:
            return self._projects[project_id]
        path = self._json_path(project_id)
        if path.exists():
            return self.load(project_id)
        saved = ", ".join(p.stem for p in self.projects_dir.glob("proj_*.json")) or "(none)"
        raise ProjectError(
            f"Unknown project_id '{project_id}'. Saved projects on disk: {saved}. "
            f"Use create_project or load_project first."
        )

    def save(self, project_id: str) -> str:
        project = self.get(project_id)
        project.touch()
        path = self._json_path(project_id)
        path.write_text(json.dumps(project.to_dict(), indent=2), encoding="utf-8")
        return str(path)

    def load(self, project_id: str) -> Project:
        path = self._json_path(project_id)
        if not path.exists():
            raise ProjectError(f"No saved project file at {path}.")
        project = Project.from_dict(json.loads(path.read_text(encoding="utf-8")))
        self._projects[project.project_id] = project
        return project

    def list_saved(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.projects_dir.glob("proj_*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append({
                    "project_id": data["project_id"],
                    "name": data["name"],
                    "resolution": f"{data['width']}x{data['height']}",
                    "fps": data["fps"],
                    "scenes": len(data.get("scenes", [])),
                    "loaded": data["project_id"] in self._projects,
                })
            except (json.JSONDecodeError, KeyError):
                out.append({"project_id": p.stem, "error": "corrupt project file"})
        return out

    def _json_path(self, project_id: str) -> Path:
        return self.projects_dir / f"{project_id}.json"

    # ---- assets ----------------------------------------------------------
    def import_asset(self, project_id: str, file_path: str, metadata: dict) -> Asset:
        """Copy the file into assets/ and register it on the project.

        Copying (rather than referencing in place) protects renders against the
        user later moving or deleting the source file.
        """
        project = self.get(project_id)
        src = Path(file_path).expanduser()
        if not src.is_file():
            raise ProjectError(f"File not found: {src}. Provide an absolute path to an existing file.")
        media_type = media_type_for(src)
        asset_id = _new_id("asset")
        stored = self.assets_dir / f"{asset_id}{src.suffix.lower()}"
        shutil.copy2(src, stored)
        asset = Asset(
            asset_id=asset_id,
            media_type=media_type,
            original_path=str(src),
            stored_path=str(stored),
            checksum=file_checksum(stored),
            metadata=metadata,
        )
        project.assets[asset_id] = asset
        self.save(project_id)
        return asset

    def replace_asset(self, project_id: str, asset_id: str, new_file_path: str, metadata: dict) -> Asset:
        """Swap the underlying file of an existing asset_id (same id, new content).

        Scenes referencing the asset keep working; their cache keys change
        because the checksum changes, which invalidates their cached clips.
        """
        project = self.get(project_id)
        asset = project.get_asset(asset_id)
        src = Path(new_file_path).expanduser()
        if not src.is_file():
            raise ProjectError(f"File not found: {src}.")
        new_type = media_type_for(src)
        if new_type != asset.media_type:
            raise ProjectError(
                f"Replacement must be the same media type: asset '{asset_id}' is "
                f"'{asset.media_type}' but the new file is '{new_type}'."
            )
        old_stored = Path(asset.stored_path)
        stored = self.assets_dir / f"{asset_id}{src.suffix.lower()}"
        shutil.copy2(src, stored)
        if old_stored != stored and old_stored.exists():
            old_stored.unlink()
        asset.original_path = str(src)
        asset.stored_path = str(stored)
        asset.checksum = file_checksum(stored)
        asset.metadata = metadata
        # Invalidate cached clips of scenes using this asset.
        for scene in project.scenes:
            if scene.asset_id == asset_id:
                scene.cache_key = None
                scene.cached_clip_path = None
        self.save(project_id)
        return asset
