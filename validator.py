"""Pre-render project validation (§3.4 of the plan).

validate_project() returns a structured report — never a bare boolean — so
Claude can relay specific, actionable problems instead of a raw FFmpeg error.
It is also wired as a mandatory pre-step inside the render tool.

Backend-agnostic: checks only project state and the filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from presets import EFFECTS, TRANSITIONS
from project_state import OUTPUT_DIR, Project


def _issue(code: str, message: str, where: str) -> dict[str, str]:
    return {"code": code, "message": message, "where": where}


def validate_project(project: Project, output_path: str | None = None) -> dict[str, Any]:
    """Run all pre-render checks. Returns:
    {valid: bool, errors: [{code,message,where}], warnings: [...]}.
    `valid` is False only when there are errors; warnings never block a render.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    total = project.total_duration()

    # ---- scenes ----------------------------------------------------------
    if not project.scenes:
        errors.append(_issue("no_scenes", "Project has no scenes; use add_scene first.", "timeline"))

    for i, scene in enumerate(project.scenes):
        where = f"scene {i}"
        asset = project.assets.get(scene.asset_id)
        if asset is None:
            errors.append(_issue(
                "missing_asset",
                f"Scene {i} references unknown asset_id '{scene.asset_id}'.", where))
            continue
        if not Path(asset.stored_path).is_file():
            errors.append(_issue(
                "asset_file_missing",
                f"Stored file for asset '{asset.asset_id}' is missing "
                f"({asset.stored_path}). Re-import it with import_asset or "
                f"replace_asset.", where))
        if scene.duration <= 0:
            errors.append(_issue(
                "bad_duration", f"Scene {i} duration must be positive (got {scene.duration}).", where))
        preset = EFFECTS.get(scene.effect)
        if preset is None:
            errors.append(_issue(
                "unknown_effect",
                f"Scene {i} uses unknown effect '{scene.effect}'. "
                f"Available: {', '.join(EFFECTS)}.", where))
        elif asset.media_type not in preset["media"]:
            errors.append(_issue(
                "effect_media_mismatch",
                f"Effect '{scene.effect}' does not support {asset.media_type} "
                f"assets (scene {i}); use one of the effects listed by "
                f"list_effects for that media type.", where))
        if asset.media_type == "audio":
            errors.append(_issue(
                "audio_as_scene",
                f"Scene {i} references an audio asset; scenes need images or "
                f"videos. Use add_audio_track for audio.", where))
        if asset.media_type == "video":
            src_dur = asset.metadata.get("duration")
            if src_dur and scene.duration > src_dur + 0.05:
                errors.append(_issue(
                    "scene_exceeds_source",
                    f"Scene {i} duration {scene.duration}s exceeds the source "
                    f"video length ({src_dur}s).", where))

    # ---- transitions ------------------------------------------------------
    seen_boundaries: set[int] = set()
    for t in project.transitions:
        where = f"transition {t.scene_a}->{t.scene_b}"
        if t.scene_b != t.scene_a + 1:
            errors.append(_issue(
                "non_adjacent_transition",
                f"Transition {t.scene_a}->{t.scene_b} must join adjacent scenes "
                f"(scene_b = scene_a + 1).", where))
            continue
        if not (0 <= t.scene_a < len(project.scenes) - 1):
            errors.append(_issue(
                "transition_out_of_range",
                f"Transition {t.scene_a}->{t.scene_b} references scene indices "
                f"outside the timeline (0..{len(project.scenes) - 1}).", where))
            continue
        if t.scene_a in seen_boundaries:
            errors.append(_issue(
                "duplicate_transition",
                f"Multiple transitions defined between scenes {t.scene_a} and "
                f"{t.scene_b}; keep only one.", where))
        seen_boundaries.add(t.scene_a)
        if t.type not in TRANSITIONS:
            errors.append(_issue(
                "unknown_transition",
                f"Unknown transition '{t.type}'. Available: {', '.join(TRANSITIONS)}.", where))
        if t.duration <= 0:
            errors.append(_issue(
                "bad_duration", f"Transition duration must be positive (got {t.duration}).", where))
        else:
            a, b = project.scenes[t.scene_a], project.scenes[t.scene_b]
            if t.duration >= min(a.duration, b.duration):
                errors.append(_issue(
                    "transition_too_long",
                    f"Transition {t.scene_a}->{t.scene_b} ({t.duration}s) must be "
                    f"shorter than both adjacent scenes "
                    f"({a.duration}s and {b.duration}s).", where))

    # A middle scene overlapped by transitions on both sides must be long
    # enough to host both overlaps.
    for i in range(1, len(project.scenes) - 1):
        before = next((t for t in project.transitions if t.scene_a == i - 1 and t.scene_b == i), None)
        after = next((t for t in project.transitions if t.scene_a == i), None)
        if before and after and before.duration + after.duration > project.scenes[i].duration:
            errors.append(_issue(
                "transitions_overlap_scene",
                f"Scene {i} ({project.scenes[i].duration}s) is shorter than its "
                f"incoming + outgoing transitions "
                f"({before.duration}s + {after.duration}s). Lengthen the scene "
                f"or shorten the transitions.", f"scene {i}"))

    # ---- subtitles ----------------------------------------------------------
    if project.subtitle_srt_path and project.subtitle_entries:
        errors.append(_issue(
            "subtitles_conflict",
            "Project has both an SRT file and manual subtitle entries; only one "
            "source is supported. Use clear_subtitles, then re-add one kind.",
            "subtitles"))
    if project.subtitle_srt_path and not Path(project.subtitle_srt_path).is_file():
        errors.append(_issue(
            "srt_missing",
            f"Subtitle file not found: {project.subtitle_srt_path}.", "subtitles"))
    for j, e in enumerate(project.subtitle_entries):
        where = f"subtitle entry {j}"
        if e.end <= e.start:
            errors.append(_issue(
                "bad_subtitle_range",
                f"Subtitle {j} end ({e.end}s) must be after start ({e.start}s).", where))
        elif total and e.start >= total:
            warnings.append(_issue(
                "subtitle_out_of_range",
                f"Subtitle {j} starts at {e.start}s but the video is only "
                f"{total:.2f}s long; it will never be shown.", where))
        elif total and e.end > total + 0.05:
            warnings.append(_issue(
                "subtitle_clipped",
                f"Subtitle {j} ends at {e.end}s, past the video end "
                f"({total:.2f}s); it will be cut off.", where))

    # ---- audio tracks ---------------------------------------------------------
    for k, tr in enumerate(project.audio_tracks):
        where = f"audio track {k}"
        asset = project.assets.get(tr.asset_id)
        if asset is None:
            errors.append(_issue(
                "missing_asset", f"Audio track {k} references unknown asset_id '{tr.asset_id}'.", where))
            continue
        if not Path(asset.stored_path).is_file():
            errors.append(_issue(
                "asset_file_missing",
                f"Stored file for audio asset '{asset.asset_id}' is missing.", where))
        if not asset.metadata.get("has_audio") and asset.media_type != "audio":
            errors.append(_issue(
                "no_audio_stream",
                f"Audio track {k} uses asset '{tr.asset_id}' which has no audio stream.", where))
        if tr.start_time < 0:
            errors.append(_issue(
                "bad_start_time", f"Audio track {k} start_time must be >= 0.", where))
        elif total and tr.start_time >= total:
            errors.append(_issue(
                "audio_out_of_range",
                f"Audio track {k} starts at {tr.start_time}s but the video is "
                f"only {total:.2f}s long.", where))
        if tr.volume < 0:
            errors.append(_issue("bad_volume", f"Audio track {k} volume must be >= 0.", where))
        src_dur = asset.metadata.get("duration") if asset else None
        if src_dur and tr.fade_in + tr.fade_out > src_dur:
            warnings.append(_issue(
                "fades_exceed_audio",
                f"Audio track {k}: fade_in + fade_out ({tr.fade_in}s + {tr.fade_out}s) "
                f"exceed the audio length ({src_dur}s).", where))

    # ---- output ------------------------------------------------------------------
    out_dir = Path(output_path).parent if output_path else OUTPUT_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(out_dir, os.W_OK):
            raise PermissionError
    except (OSError, PermissionError):
        errors.append(_issue(
            "output_not_writable", f"Output directory is not writable: {out_dir}.", "output"))

    return {"valid": not errors, "errors": errors, "warnings": warnings}
