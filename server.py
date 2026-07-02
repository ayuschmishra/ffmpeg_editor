#!/usr/bin/env python3
"""Reel — FFmpeg video editor MCP server (entrypoint + tool definitions).

Claude Desktop calls these tools to assemble a video project step by step:
import assets -> build scenes with effects -> (transitions, subtitles, audio in
later phases) -> render. This layer only validates parameters and delegates to
the project store and the Renderer interface; it never touches FFmpeg directly.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional

import anyio
from pydantic import Field
from mcp.server.fastmcp import Context, FastMCP

import presets
from validator import validate_project as _validate
from ffmpeg_renderer import FFmpegRenderer
from project_state import OUTPUT_DIR, ProjectError, ProjectStore
from renderer_base import RenderError, Renderer

mcp = FastMCP("reel_mcp")

store = ProjectStore()
renderer: Renderer = FFmpegRenderer()  # the only backend in v1; swappable seam

# Sweep temp files orphaned by crashes/kills of previous server runs. Only
# files older than an hour: a concurrently running render's temps are fresh.
from project_state import CACHE_DIR as _CACHE_DIR  # noqa: E402

for _stale in _CACHE_DIR.glob("tmp_*"):
    try:
        if time.time() - _stale.stat().st_mtime > 3600:
            _stale.unlink()
    except OSError:
        pass


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"success": True, **payload}, indent=2)


def _err(e: Exception) -> str:
    kind = type(e).__name__
    return json.dumps({"success": False, "error": f"{kind}: {e}"}, indent=2)


def _timeline_summary(project) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "resolution": f"{project.width}x{project.height}",
        "fps": project.fps,
        "total_duration": round(project.total_duration(), 3),
        "assets": [
            {"asset_id": a.asset_id, "type": a.media_type, "file": a.original_path,
             "metadata": a.metadata}
            for a in project.assets.values()
        ],
        "scenes": [
            {"index": i, "asset_id": s.asset_id, "duration": s.duration,
             "effect": s.effect, "effect_params": s.effect_params,
             "cached": bool(s.cache_key)}
            for i, s in enumerate(project.scenes)
        ],
        "transitions": [
            {"between_scenes": [t.scene_a, t.scene_b], "type": t.type, "duration": t.duration}
            for t in project.transitions
        ],
        "subtitles": {
            "srt_file": project.subtitle_srt_path,
            "manual_entries": len(project.subtitle_entries),
        },
        "audio_tracks": [
            {"index": i, "asset_id": a.asset_id, "type": a.track_type,
             "start_time": a.start_time, "volume": a.volume,
             "fade_in": a.fade_in, "fade_out": a.fade_out}
            for i, a in enumerate(project.audio_tracks)
        ],
    }


# ---------------------------------------------------------------- project tools
@mcp.tool(
    name="create_project",
    annotations={"title": "Create Video Project", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def create_project(
    name: Annotated[str, Field(description="Human-readable project name, e.g. 'holiday_recap'", min_length=1, max_length=80)],
    width: Annotated[int, Field(description="Output width in pixels (default 1920)", ge=16, le=7680)] = 1920,
    height: Annotated[int, Field(description="Output height in pixels (default 1080; use 1080x1920 for vertical reels)", ge=16, le=7680)] = 1080,
    fps: Annotated[int, Field(description="Output frame rate (default 30)", ge=1, le=120)] = 30,
) -> str:
    """Create a new video project and return its project_id.

    All later tool calls reference this project_id. State is auto-saved to disk
    after every change, so it survives restarts (reload with load_project).
    Returns JSON: {success, project_id, name, resolution, fps}.
    """
    try:
        if width % 2 or height % 2:
            raise ProjectError("width and height must be even numbers (H.264 requirement).")
        project = store.create(name=name, width=width, height=height, fps=fps)
        return _ok({"project_id": project.project_id, "name": name,
                    "resolution": f"{width}x{height}", "fps": fps})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="save_project",
    annotations={"title": "Save Project", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def save_project(
    project_id: Annotated[str, Field(description="Project id from create_project")],
) -> str:
    """Persist the project state to its JSON file under projects/.

    Projects are auto-saved after every mutating tool call, so this is mainly a
    manual checkpoint. Returns JSON: {success, saved_to}.
    """
    try:
        return _ok({"saved_to": store.save(project_id)})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="load_project",
    annotations={"title": "Load Project", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def load_project(
    project_id: Annotated[str, Field(description="Project id of a previously saved project")],
) -> str:
    """Load a saved project from disk into memory (e.g. after a restart).

    Returns JSON: {success, ...timeline summary} — same shape as preview_timeline.
    """
    try:
        project = store.load(project_id)
        return _ok(_timeline_summary(project))
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="list_projects",
    annotations={"title": "List Saved Projects", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def list_projects() -> str:
    """List all saved projects with id, name, resolution, fps and scene count.

    Use this to find a project_id after a restart. Returns JSON: {success, projects: [...]}.
    """
    try:
        return _ok({"projects": store.list_saved()})
    except Exception as e:
        return _err(e)


# ----------------------------------------------------------------- asset tools
@mcp.tool(
    name="import_asset",
    annotations={"title": "Import Media Asset", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def import_asset(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    file_path: Annotated[str, Field(description="Absolute path to an image, video or audio file on disk")],
) -> str:
    """Register a media file with the project and return its asset_id.

    The file is copied into the project asset store, so later moves/deletions of
    the original don't break renders. Reference the returned asset_id in
    add_scene / add_audio_track. Returns JSON: {success, asset_id, media_type, metadata}.
    """
    try:
        metadata = renderer.probe_media(file_path)
        asset = store.import_asset(project_id, file_path, metadata)
        return _ok({"asset_id": asset.asset_id, "media_type": asset.media_type,
                    "metadata": asset.metadata})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="list_assets",
    annotations={"title": "List Project Assets", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def list_assets(
    project_id: Annotated[str, Field(description="Project id from create_project")],
) -> str:
    """List all registered assets of a project with their metadata.

    Returns JSON: {success, assets: [{asset_id, media_type, original_path, metadata, file_exists}]}.
    """
    try:
        project = store.get(project_id)
        assets = [
            {"asset_id": a.asset_id, "media_type": a.media_type,
             "original_path": a.original_path, "metadata": a.metadata,
             "file_exists": Path(a.stored_path).is_file()}
            for a in project.assets.values()
        ]
        return _ok({"assets": assets})
    except Exception as e:
        return _err(e)


# ----------------------------------------------------------------- scene tools
@mcp.tool(
    name="add_scene",
    annotations={"title": "Add Scene to Timeline", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def add_scene(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    asset_id: Annotated[str, Field(description="Asset id of an imported image or video")],
    duration: Annotated[float, Field(description="Scene duration in seconds", gt=0, le=3600)],
    effect: Annotated[str, Field(description="Effect preset name (see list_effects), e.g. 'zoom_in'")] = "none",
    effect_params: Annotated[Optional[dict], Field(description="Optional effect parameter overrides, e.g. {'zoom_end': 1.4}")] = None,
) -> str:
    """Append a scene to the end of the timeline.

    The scene shows the given asset for `duration` seconds with the chosen
    effect. Image effects: zoom_in, zoom_out, pan_left, pan_right, ken_burns.
    Video assets currently support effect 'none' (scaled/letterboxed) or
    'custom_filter'. Returns JSON: {success, scene_index, total_scenes, total_duration}.
    """
    try:
        from project_state import Scene, _new_id

        project = store.get(project_id)
        asset = project.get_asset(asset_id)
        # Fail fast on bad effect/media combos instead of at render time.
        presets.build_effect_filter(
            effect, asset.media_type, project.width, project.height,
            project.fps, duration, effect_params or {},
        )
        if asset.media_type == "video":
            src_dur = asset.metadata.get("duration")
            if src_dur and duration > src_dur + 0.05:
                return _err(ProjectError(
                    f"Scene duration {duration}s exceeds the video's length ({src_dur}s). "
                    f"Use a duration <= {src_dur}s."
                ))
        scene = Scene(scene_id=_new_id("scene"), asset_id=asset_id,
                      duration=float(duration), effect=effect,
                      effect_params=effect_params or {})
        project.scenes.append(scene)
        store.save(project_id)
        return _ok({"scene_index": len(project.scenes) - 1,
                    "total_scenes": len(project.scenes),
                    "total_duration": round(project.total_duration(), 3)})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="replace_asset",
    annotations={"title": "Replace Asset File", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def replace_asset(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    asset_id: Annotated[str, Field(description="Asset id whose underlying file should be swapped")],
    new_file_path: Annotated[str, Field(description="Absolute path to the replacement file (same media type)")],
) -> str:
    """Swap the file behind an existing asset_id, keeping the same id.

    Every scene referencing the asset automatically uses the new file on the
    next render — their cached clips are invalidated. The old stored copy is
    replaced. Returns JSON: {success, asset_id, media_type, metadata, scenes_invalidated}.
    """
    try:
        metadata = renderer.probe_media(new_file_path)
        asset = store.replace_asset(project_id, asset_id, new_file_path, metadata)
        project = store.get(project_id)
        affected = [i for i, s in enumerate(project.scenes) if s.asset_id == asset_id]
        return _ok({"asset_id": asset.asset_id, "media_type": asset.media_type,
                    "metadata": asset.metadata, "scenes_invalidated": affected})
    except Exception as e:
        return _err(e)


def _scene_edit_result(project) -> dict[str, Any]:
    return {
        "scenes": [{"index": i, "asset_id": s.asset_id, "duration": s.duration, "effect": s.effect}
                   for i, s in enumerate(project.scenes)],
        "transitions": [{"between_scenes": [t.scene_a, t.scene_b], "type": t.type, "duration": t.duration}
                        for t in project.transitions],
        "total_duration": round(project.total_duration(), 3),
        "note": "Transitions are anchored to timeline positions, not scenes; "
                "review them after reordering.",
    }


@mcp.tool(
    name="move_scene",
    annotations={"title": "Move Scene", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def move_scene(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    scene_index: Annotated[int, Field(description="Current index of the scene to move", ge=0)],
    new_position: Annotated[int, Field(description="Target index (0 = first)", ge=0)],
) -> str:
    """Reorder the timeline by moving a scene to a new position.

    Cached clips stay valid (scene content is unchanged). Transitions keep
    their boundary positions — check them after moving. Returns the updated
    scene order, transitions and total_duration.
    """
    try:
        project = store.get(project_id)
        project.move_scene(scene_index, new_position)
        store.save(project_id)
        return _ok(_scene_edit_result(project))
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="delete_scene",
    annotations={"title": "Delete Scene", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def delete_scene(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    scene_index: Annotated[int, Field(description="Index of the scene to remove", ge=0)],
) -> str:
    """Remove a scene from the timeline.

    Transitions touching the deleted scene are dropped; later transition
    indices shift down automatically. Returns the updated timeline.
    """
    try:
        project = store.get(project_id)
        removed = project.delete_scene(scene_index)
        store.save(project_id)
        return _ok({"deleted_scene_id": removed.scene_id, **_scene_edit_result(project)})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="duplicate_scene",
    annotations={"title": "Duplicate Scene", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def duplicate_scene(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    scene_index: Annotated[int, Field(description="Index of the scene to duplicate", ge=0)],
) -> str:
    """Insert an identical copy of a scene right after the original.

    The copy shares the original's cached clip, so no re-render is needed for
    it. Returns the updated timeline.
    """
    try:
        project = store.get(project_id)
        copy = project.duplicate_scene(scene_index)
        store.save(project_id)
        return _ok({"new_scene_index": scene_index + 1, "new_scene_id": copy.scene_id,
                    **_scene_edit_result(project)})
    except Exception as e:
        return _err(e)


# -------------------------------------------------------------- transition tools
@mcp.tool(
    name="set_transition",
    annotations={"title": "Set Transition Between Scenes", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def set_transition(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    scene_a: Annotated[int, Field(description="Index of the first scene of the adjacent pair", ge=0)],
    scene_b: Annotated[int, Field(description="Index of the second scene; must be scene_a + 1", ge=1)],
    type: Annotated[str, Field(description="Transition preset name (see list_transitions), e.g. 'crossfade'")],
    duration: Annotated[float, Field(description="Transition length in seconds (overlaps the two scenes)", gt=0, le=10)] = 1.0,
) -> str:
    """Define (or replace) the transition between two adjacent scenes.

    The transition overlaps the end of scene_a with the start of scene_b, so it
    shortens the timeline by `duration` seconds. Setting a new transition on the
    same pair replaces the old one. Returns JSON: {success, transitions, total_duration}.
    """
    try:
        from project_state import Transition

        project = store.get(project_id)
        sa, sb = project.get_scene(scene_a), project.get_scene(scene_b)
        if scene_b != scene_a + 1:
            raise ProjectError(
                f"Transitions only apply between adjacent scenes: scene_b must be "
                f"scene_a + 1 (got {scene_a} and {scene_b})."
            )
        if type not in presets.TRANSITIONS:
            raise ProjectError(
                f"Unknown transition '{type}'. Available: {', '.join(presets.TRANSITIONS)}."
            )
        limit = min(sa.duration, sb.duration)
        if duration >= limit:
            raise ProjectError(
                f"Transition duration {duration}s must be shorter than both adjacent "
                f"scenes (max here: just under {limit}s)."
            )
        project.transitions = [t for t in project.transitions if t.scene_a != scene_a]
        project.transitions.append(Transition(scene_a=scene_a, scene_b=scene_b,
                                              type=type, duration=float(duration)))
        project.transitions.sort(key=lambda t: t.scene_a)
        store.save(project_id)
        return _ok({
            "transitions": [{"between_scenes": [t.scene_a, t.scene_b],
                             "type": t.type, "duration": t.duration}
                            for t in project.transitions],
            "total_duration": round(project.total_duration(), 3),
        })
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------- subtitle/audio tools
@mcp.tool(
    name="add_subtitles",
    annotations={"title": "Attach SRT Subtitle File", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def add_subtitles(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    srt_path: Annotated[str, Field(description="Absolute path to a .srt subtitle file")],
) -> str:
    """Attach a full SRT subtitle file; it will be burned into the rendered video.

    The file is copied into the project store. Replaces any previously attached
    SRT. Cannot be combined with manual add_subtitle_text entries — use one or
    the other (clear_subtitles switches). Returns JSON: {success, srt_path}.
    """
    try:
        import shutil as _sh

        from project_state import ASSETS_DIR

        project = store.get(project_id)
        src = Path(srt_path).expanduser()
        if not src.is_file():
            raise ProjectError(f"SRT file not found: {src}")
        if src.suffix.lower() != ".srt":
            raise ProjectError(f"Expected a .srt file, got '{src.suffix}'.")
        stored = ASSETS_DIR / f"subs_{project.project_id}.srt"
        _sh.copyfile(src, stored)
        project.subtitle_srt_path = str(stored)
        store.save(project_id)
        return _ok({"srt_path": str(stored),
                    "note": "Subtitles are burned in at render time."})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="add_subtitle_text",
    annotations={"title": "Add Manual Subtitle Line", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def add_subtitle_text(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    text: Annotated[str, Field(description="Subtitle text to display", min_length=1, max_length=500)],
    start: Annotated[float, Field(description="Start time in seconds (relative to the scene if scene_index is given, else to the whole video)", ge=0)],
    end: Annotated[float, Field(description="End time in seconds (same reference as start)", gt=0)],
    scene_index: Annotated[Optional[int], Field(description="Optional scene index; when set, start/end are relative to that scene's start on the timeline", ge=0)] = None,
) -> str:
    """Add a single subtitle line manually (burned in at render time).

    With scene_index, times are relative to when that scene starts playing —
    they are converted to global timestamps immediately, so reordering scenes
    afterwards does NOT re-anchor existing lines. Cannot be combined with an
    attached SRT file. Returns JSON: {success, entry, total_entries}.
    """
    try:
        from project_state import SubtitleEntry

        project = store.get(project_id)
        if end <= start:
            raise ProjectError(f"end ({end}s) must be after start ({start}s).")
        offset = project.scene_start(scene_index) if scene_index is not None else 0.0
        entry = SubtitleEntry(text=text, start=round(start + offset, 3),
                              end=round(end + offset, 3))
        project.subtitle_entries.append(entry)
        store.save(project_id)
        return _ok({"entry": {"text": entry.text, "start": entry.start, "end": entry.end},
                    "total_entries": len(project.subtitle_entries)})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="clear_subtitles",
    annotations={"title": "Clear All Subtitles", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
def clear_subtitles(
    project_id: Annotated[str, Field(description="Project id from create_project")],
) -> str:
    """Remove the attached SRT file reference and all manual subtitle entries.

    Use this to fix mistakes or to switch between SRT and manual subtitles.
    Returns JSON: {success, cleared}.
    """
    try:
        project = store.get(project_id)
        cleared = {"srt": project.subtitle_srt_path is not None,
                   "manual_entries": len(project.subtitle_entries)}
        project.subtitle_srt_path = None
        project.subtitle_entries = []
        store.save(project_id)
        return _ok({"cleared": cleared})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="add_audio_track",
    annotations={"title": "Add Audio Track", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def add_audio_track(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    asset_id: Annotated[str, Field(description="Asset id of an imported audio file (or video with an audio stream)")],
    type: Annotated[str, Field(description="Track role: 'music', 'sfx' or 'narration' (informational in v1; all mix identically)", pattern="^(music|sfx|narration)$")] = "music",
    start_time: Annotated[float, Field(description="When the track starts, in seconds from video start", ge=0)] = 0.0,
    volume: Annotated[float, Field(description="Volume multiplier: 1.0 = original, 0.5 = half, 2.0 = double", ge=0, le=10)] = 1.0,
    fade_in: Annotated[float, Field(description="Fade-in length in seconds", ge=0, le=60)] = 0.0,
    fade_out: Annotated[float, Field(description="Fade-out length in seconds", ge=0, le=60)] = 0.0,
) -> str:
    """Add music, SFX or narration to the final mix.

    Tracks are mixed together (not concatenated): overlapping tracks play
    simultaneously. Audio is trimmed automatically at the video's end. Returns
    JSON: {success, track_index, audio_tracks}.
    """
    try:
        from project_state import AudioTrack

        project = store.get(project_id)
        asset = project.get_asset(asset_id)
        if asset.media_type == "image":
            raise ProjectError(f"Asset '{asset_id}' is an image; audio tracks need audio (or video-with-audio) assets.")
        if not asset.metadata.get("has_audio") and asset.media_type != "audio":
            raise ProjectError(f"Asset '{asset_id}' has no audio stream.")
        project.audio_tracks.append(AudioTrack(
            asset_id=asset_id, track_type=type, start_time=float(start_time),
            volume=float(volume), fade_in=float(fade_in), fade_out=float(fade_out)))
        store.save(project_id)
        return _ok({"track_index": len(project.audio_tracks) - 1,
                    "audio_tracks": _timeline_summary(project)["audio_tracks"]})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="remove_audio_track",
    annotations={"title": "Remove Audio Track", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def remove_audio_track(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    track_index: Annotated[int, Field(description="Index of the audio track to remove (see preview_timeline)", ge=0)],
) -> str:
    """Remove one audio track by index. Returns the remaining tracks."""
    try:
        project = store.get(project_id)
        if track_index >= len(project.audio_tracks):
            raise ProjectError(
                f"track_index {track_index} out of range; project has "
                f"{len(project.audio_tracks)} audio track(s).")
        project.audio_tracks.pop(track_index)
        store.save(project_id)
        return _ok({"audio_tracks": _timeline_summary(project)["audio_tracks"]})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------- inspection tools
@mcp.tool(
    name="list_effects",
    annotations={"title": "List Effect Presets", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def list_effects() -> str:
    """List all available scene effect presets with default parameters.

    Returns JSON: {success, effects: [{name, description, default_params, media_types}]}.
    """
    return _ok({"effects": presets.list_effect_presets()})


@mcp.tool(
    name="list_transitions",
    annotations={"title": "List Transition Presets", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def list_transitions() -> str:
    """List all available transition presets usable with set_transition.

    Returns JSON: {success, transitions: [{name, description}]}.
    """
    return _ok({"transitions": presets.list_transition_presets()})


@mcp.tool(
    name="preview_timeline",
    annotations={"title": "Preview Timeline", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def preview_timeline(
    project_id: Annotated[str, Field(description="Project id from create_project")],
) -> str:
    """Return a full structured summary of the current project state.

    Includes assets, ordered scenes (with cache status), transitions, subtitles
    and audio tracks. Use this to verify the timeline before rendering.
    """
    try:
        return _ok(_timeline_summary(store.get(project_id)))
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="validate_project",
    annotations={"title": "Validate Project", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def validate_project(
    project_id: Annotated[str, Field(description="Project id from create_project")],
) -> str:
    """Run all pre-render checks without rendering.

    Checks assets exist on disk, transitions are adjacent and fit their scenes,
    subtitles/audio fall inside the timeline, and the output dir is writable.
    Returns JSON: {success, valid, errors: [{code, message, where}], warnings: [...]}.
    Errors block rendering; warnings do not. render() runs this automatically.
    """
    try:
        return _ok(_validate(store.get(project_id)))
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------ render tools
class _RenderCancelled(Exception):
    """Raised inside a render job's progress callback to abort cooperatively."""


_render_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _default_output(project) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", project.name).strip("_") or "video"
    return str(OUTPUT_DIR / f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}.mp4")


def _resolve_output(project, output_path: Optional[str]) -> str:
    """Normalize a user/model-supplied output path so it can't land somewhere
    surprising: strip stray quotes, expand ~, anchor relative paths under
    output/ (the server's cwd is whatever the MCP host chose — never trust
    it), enforce .mp4, and reject directory targets."""
    if not output_path or not output_path.strip():
        return _default_output(project)
    cleaned = output_path.strip().strip("'\"").strip()
    path = Path(cleaned).expanduser()
    if path.is_dir():
        raise ProjectError(
            f"output_path is an existing directory: {path}. Give a full file "
            f"path ending in .mp4, or omit output_path for an auto-named file."
        )
    if not path.suffix:
        path = path.with_suffix(".mp4")
    elif path.suffix.lower() != ".mp4":
        raise ProjectError(
            f"Output must be an .mp4 file (got '{path.suffix}'). v1 renders "
            f"H.264/MP4 only."
        )
    if not path.is_absolute():
        path = OUTPUT_DIR / path
    return str(path)


def _run_render_job(job: dict, project, output_path: str, quality: str) -> None:
    def on_progress(frac: float, msg: Optional[str]) -> None:
        if job["cancel"].is_set():
            raise _RenderCancelled()
        job["progress"] = frac
        job["updated_at"] = time.time()
        if msg:  # msg=None events are fine-grained encode progress, not stages
            job["log"].append(msg)

    try:
        report = renderer.render_final(project, output_path, on_progress, quality)
        store.save(project.project_id)
        # Independent completion check: never mark done unless the file the
        # report points at actually exists and is non-empty.
        out = Path(report["output_path"])
        if not (out.is_file() and out.stat().st_size > 0):
            job.update(state="failed", error=(
                f"Render reported success but no output file exists at {out}. "
                f"If that folder is cloud-synced or antivirus-scanned, render "
                f"to a local folder (or omit output_path)."))
            return
        job.update(state="done", progress=1.0, result=report)
    except _RenderCancelled:
        job.update(state="cancelled")
        job["log"].append("Render cancelled (cached scene clips are kept).")
    except Exception as e:
        job.update(state="failed", error=f"{type(e).__name__}: {e}")
    finally:
        job["updated_at"] = time.time()


@mcp.tool(
    name="render_start",
    annotations={"title": "Start Background Render", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def render_start(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    output_path: Annotated[Optional[str], Field(description="Optional output .mp4 path. Absolute paths are used as-is; relative paths land under the server's output/ folder; omit for output/<name>_<timestamp>.mp4")] = None,
    quality: Annotated[str, Field(description="'final' (full quality, default) or 'draft' (half resolution, fastest encode — quick preview)", pattern="^(final|draft)$")] = "final",
) -> str:
    """Start a render in the background and return immediately with a job_id.

    Use this instead of render for large projects (many scenes/transitions or
    long durations) — it can never hit a client-side tool timeout. Validation
    runs first and blocks the start on errors. Poll render_status(job_id) for
    progress and the final output path. Returns JSON: {success, job_id, state}.
    """
    try:
        project = store.get(project_id)
        out = _resolve_output(project, output_path)
        validation = _validate(project, out)
        if not validation["valid"]:
            return json.dumps({
                "success": False,
                "error": "Validation failed — render not started. Fix the errors and retry.",
                **validation,
            }, indent=2)
        with _jobs_lock:
            running = next((j for j in _render_jobs.values()
                            if j["project_id"] == project_id and j["state"] == "running"), None)
            if running is not None:
                return _ok({"job_id": running["job_id"], "state": "running",
                            "note": "A render for this project is already running; "
                                    "poll render_status or render_cancel it first."})
            job = {"job_id": f"job_{uuid.uuid4().hex[:10]}", "project_id": project_id,
                   "state": "running", "progress": 0.0, "log": [], "result": None,
                   "error": None, "quality": quality, "output_path": out,
                   "cancel": threading.Event(), "started_at": time.time(),
                   "updated_at": time.time(), "thread": None}
            _render_jobs[job["job_id"]] = job
        worker = threading.Thread(target=_run_render_job, args=(job, project, out, quality),
                                  daemon=True, name=job["job_id"])
        job["thread"] = worker
        worker.start()
        return _ok({"job_id": job["job_id"], "state": "running", "quality": quality,
                    "warnings": validation["warnings"],
                    "note": "Poll render_status with this job_id for progress."})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="render_status",
    annotations={"title": "Check Render Job Status", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def render_status(
    job_id: Annotated[str, Field(description="Job id returned by render_start")],
) -> str:
    """Check a background render job.

    Returns JSON: {success, state: running|done|failed|cancelled, progress
    (0..1), elapsed_seconds, log (last 10 stages), result (when done),
    error (when failed)}.
    """
    try:
        job = _render_jobs.get(job_id)
        if job is None:
            known = ", ".join(_render_jobs) or "(none)"
            raise ProjectError(
                f"Unknown job_id '{job_id}'. Known jobs this session: {known}. "
                f"Jobs do not survive a server restart — if the server was "
                f"restarted mid-render, check the output folder and re-run "
                f"render_start (cached scene clips make restarts cheap).")
        # A job can only be 'running' while its worker thread is alive; if the
        # thread died without reaching a terminal state, surface that as a
        # failure instead of reporting 'running' forever.
        thread = job.get("thread")
        if job["state"] == "running" and thread is not None and not thread.is_alive():
            job.update(state="failed", error=(
                "Render worker thread terminated unexpectedly (server "
                "interrupted?). Cached scene clips are kept — call "
                "render_start again to resume cheaply."))
        return _ok({
            "job_id": job_id,
            "state": job["state"],
            "progress": round(job["progress"], 3),
            "elapsed_seconds": round(time.time() - job["started_at"], 1),
            "seconds_since_progress": round(time.time() - job["updated_at"], 1),
            "quality": job["quality"],
            "log": job["log"][-10:],
            "result": job["result"],
            "error": job["error"],
        })
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="render_cancel",
    annotations={"title": "Cancel Render Job", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
def render_cancel(
    job_id: Annotated[str, Field(description="Job id returned by render_start")],
) -> str:
    """Request cancellation of a background render job.

    Takes effect at the next progress update — usually within a second, even
    mid-encode (the running ffmpeg process is killed). Scene clips already
    rendered stay cached, so restarting later is cheap. Returns JSON:
    {success, state}.
    """
    try:
        job = _render_jobs.get(job_id)
        if job is None:
            raise ProjectError(f"Unknown job_id '{job_id}'.")
        if job["state"] != "running":
            return _ok({"state": job["state"], "note": "Job already finished."})
        job["cancel"].set()
        return _ok({"state": "cancelling",
                    "note": "Takes effect at the next stage boundary; poll render_status."})
    except Exception as e:
        return _err(e)


@mcp.tool(
    name="render",
    annotations={"title": "Render Final Video", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def render(
    project_id: Annotated[str, Field(description="Project id from create_project")],
    output_path: Annotated[Optional[str], Field(description="Optional output .mp4 path. Absolute paths are used as-is; relative paths land under the server's output/ folder; omit for output/<name>_<timestamp>.mp4")] = None,
    quality: Annotated[str, Field(description="'final' (full quality, default) or 'draft' (half resolution, fastest encode — quick preview)", pattern="^(final|draft)$")] = "final",
    ctx: Context = None,
) -> str:
    """Render the project to an MP4 file synchronously (cached scenes reused).

    Best for small/short projects. For larger ones (roughly 5+ scenes with
    transitions, or long durations) prefer render_start + render_status: this
    call blocks until done and can be killed by the MCP client's tool timeout.
    Validation runs first and blocks the render on errors. Returns JSON:
    {success, output_path, quality, duration, resolution, scenes_rendered,
    scenes_from_cache, render_log, warnings}.
    """
    try:
        project = store.get(project_id)
        output_path = _resolve_output(project, output_path)
        validation = _validate(project, output_path)
        if not validation["valid"]:
            return json.dumps({
                "success": False,
                "error": "Validation failed — nothing was rendered. Fix the errors and retry.",
                **validation,
            }, indent=2)

        log: list[str] = []

        def on_progress(frac: float, msg: Optional[str]) -> None:
            if msg:  # msg=None events are fine-grained encode progress
                log.append(msg)
            if ctx is not None:
                try:  # runs in the worker thread; hop back to the event loop
                    anyio.from_thread.run(ctx.report_progress, frac, 1.0, msg)
                except Exception:
                    pass  # progress is best-effort; never fail the render over it

        report = await anyio.to_thread.run_sync(
            lambda: renderer.render_final(project, output_path, on_progress, quality)
        )
        store.save(project_id)  # persist refreshed cache keys
        return _ok({**report, "render_log": log, "warnings": validation["warnings"]})
    except Exception as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run()
