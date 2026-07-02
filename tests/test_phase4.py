"""Phase 4 test: validator covers every check in §3.4 with structured output."""
from pathlib import Path

from util import check, make_test_image, make_test_video, make_test_audio

from ffmpeg_renderer import FFmpegRenderer
from project_state import (AudioTrack, ProjectStore, Scene, SubtitleEntry,
                           Transition, _new_id)
from validator import validate_project


def codes(report: dict, kind: str) -> list[str]:
    return [i["code"] for i in report[kind]]


def main() -> None:
    print("Phase 4 test: validation")
    img = make_test_image("red", "red", "RED")
    vid = make_test_video("clip", seconds=4.0)
    aud = make_test_audio("tone", seconds=10.0)

    store = ProjectStore()
    renderer = FFmpegRenderer()
    project = store.create("phase4_test", 960, 540, 30)
    pid = project.project_id

    r = validate_project(project)
    check(not r["valid"] and "no_scenes" in codes(r, "errors"), "empty project -> no_scenes error")

    a_img = store.import_asset(pid, str(img), renderer.probe_media(str(img)))
    a_vid = store.import_asset(pid, str(vid), renderer.probe_media(str(vid)))
    a_aud = store.import_asset(pid, str(aud), renderer.probe_media(str(aud)))

    def scene(asset_id: str, dur: float, effect: str = "none") -> Scene:
        return Scene(scene_id=_new_id("scene"), asset_id=asset_id, duration=dur, effect=effect)

    # Valid baseline project must pass cleanly.
    project.scenes = [scene(a_img.asset_id, 3.0, "zoom_in"), scene(a_vid.asset_id, 3.0)]
    project.transitions = [Transition(0, 1, "crossfade", 1.0)]
    r = validate_project(project)
    check(r["valid"] and not r["errors"] and not r["warnings"], "valid project passes cleanly")

    # Each §3.4 check, one at a time --------------------------------------------
    project.scenes.append(scene("asset_ghost", 2.0))
    check("missing_asset" in codes(validate_project(project), "errors"), "unknown asset_id detected")
    project.scenes.pop()

    stored = Path(a_img.stored_path)
    stored.rename(stored.with_suffix(".hidden"))
    try:
        check("asset_file_missing" in codes(validate_project(project), "errors"),
              "missing asset file on disk detected")
    finally:
        stored.with_suffix(".hidden").rename(stored)

    project.scenes[0].duration = -1
    r = validate_project(project)
    check("bad_duration" in codes(r, "errors"), "negative scene duration detected")
    project.scenes[0].duration = 3.0

    project.scenes[0].effect = "explode"
    check("unknown_effect" in codes(validate_project(project), "errors"), "unknown effect detected")
    project.scenes[0].effect = "zoom_in"

    project.scenes[1].effect = "ken_burns"  # zoompan preset on a video asset
    check("effect_media_mismatch" in codes(validate_project(project), "errors"),
          "image-only effect on video asset detected")
    project.scenes[1].effect = "none"

    project.scenes[1].duration = 99.0
    check("scene_exceeds_source" in codes(validate_project(project), "errors"),
          "scene longer than source video detected")
    project.scenes[1].duration = 3.0

    project.transitions = [Transition(0, 1, "crossfade", 5.0)]
    check("transition_too_long" in codes(validate_project(project), "errors"),
          "transition longer than scenes detected")
    project.transitions = [Transition(0, 5, "crossfade", 0.5)]
    r = validate_project(project)
    check({"non_adjacent_transition"} & set(codes(r, "errors")) or
          {"transition_out_of_range"} & set(codes(r, "errors")),
          "out-of-range/non-adjacent transition detected")
    project.transitions = [Transition(0, 1, "crossfade", 0.5), Transition(0, 1, "pixelize", 0.5)]
    check("duplicate_transition" in codes(validate_project(project), "errors"),
          "duplicate transition on one boundary detected")

    # Middle scene shorter than incoming+outgoing transitions.
    project.scenes = [scene(a_img.asset_id, 4.0), scene(a_img.asset_id, 1.0, "zoom_in"),
                      scene(a_img.asset_id, 4.0, "zoom_out")]
    project.transitions = [Transition(0, 1, "crossfade", 0.8), Transition(1, 2, "crossfade", 0.8)]
    check("transitions_overlap_scene" in codes(validate_project(project), "errors"),
          "middle scene overlap conflict detected")
    project.transitions = []

    project.subtitle_srt_path = "C:/nowhere/missing.srt"
    check("srt_missing" in codes(validate_project(project), "errors"), "missing SRT file detected")
    project.subtitle_srt_path = None

    project.subtitle_entries = [SubtitleEntry("late", 500.0, 502.0)]
    check("subtitle_out_of_range" in codes(validate_project(project), "warnings"),
          "out-of-range subtitle -> warning")
    project.subtitle_entries = [SubtitleEntry("bad", 2.0, 1.0)]
    check("bad_subtitle_range" in codes(validate_project(project), "errors"),
          "inverted subtitle range -> error")
    project.subtitle_entries = []

    project.audio_tracks = [AudioTrack(asset_id=a_aud.asset_id, track_type="music", start_time=500.0)]
    check("audio_out_of_range" in codes(validate_project(project), "errors"),
          "audio starting past video end detected")
    project.audio_tracks = [AudioTrack(asset_id=a_img.asset_id, track_type="music")]
    check("no_audio_stream" in codes(validate_project(project), "errors"),
          "audio track on silent asset detected")
    project.audio_tracks = [AudioTrack(asset_id=a_aud.asset_id, track_type="music",
                                       fade_in=8.0, fade_out=8.0)]
    check("fades_exceed_audio" in codes(validate_project(project), "warnings"),
          "fades exceeding audio length -> warning")
    project.audio_tracks = []

    r = validate_project(project)
    check(r["valid"], "project is valid again after reverting all breakage")
    print("Phase 4 test: ALL PASSED")


if __name__ == "__main__":
    main()
