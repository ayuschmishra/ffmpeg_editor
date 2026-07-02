"""Phase 6 test: incremental rendering on a full-featured project.

A project with effects + transitions + subtitles + audio is rendered, then one
scene is edited. The re-render must rebuild only that scene while transitions,
subtitles and audio are correctly reassembled around the cached clips. Also
checks progress callback ordering and error surfacing.
"""
from pathlib import Path

from util import check, make_test_audio, make_test_image, video_summary

from ffmpeg_renderer import FFmpegRenderer
from project_state import (AudioTrack, CACHE_DIR, OUTPUT_DIR, ProjectStore,
                           Scene, SubtitleEntry, Transition, _new_id)
from renderer_base import RenderError


def main() -> None:
    print("Phase 6 test: incremental rendering polish")
    for clip in CACHE_DIR.glob("scene_*.mp4"):
        clip.unlink()
    imgs = [make_test_image(n, c, n.upper())
            for n, c in [("red", "red"), ("blue", "blue"), ("green", "green")]]
    music = make_test_audio("tone", seconds=10.0)

    store = ProjectStore()
    renderer = FFmpegRenderer()
    project = store.create("phase6_test", 960, 540, 30)
    pid = project.project_id

    a = [store.import_asset(pid, str(p), renderer.probe_media(str(p))) for p in imgs]
    a_music = store.import_asset(pid, str(music), renderer.probe_media(str(music)))
    for asset, fx in zip(a, ["zoom_in", "ken_burns", "zoom_out"]):
        project.scenes.append(Scene(scene_id=_new_id("scene"), asset_id=asset.asset_id,
                                    duration=3.0, effect=fx))
    project.transitions = [Transition(0, 1, "crossfade", 0.5),
                           Transition(1, 2, "slide_left", 0.5)]
    project.subtitle_entries = [SubtitleEntry("Incremental test", 1.0, 3.0)]
    project.audio_tracks = [AudioTrack(asset_id=a_music.asset_id, track_type="music",
                                       volume=0.8, fade_in=1.0, fade_out=1.0)]

    out = OUTPUT_DIR / "phase6_test.mp4"
    events: list[tuple[float, str]] = []
    report = renderer.render_final(project, str(out), progress=lambda f, m: events.append((f, m)))
    check(report["scenes_rendered"] == [0, 1, 2], "cold render builds all scenes")
    s = video_summary(out)
    check(abs(s["duration"] - 8.0) < 0.3 and s["has_audio"],
          f"full pipeline output correct (dur {s['duration']}, audio {s['has_audio']})")

    fracs = [f for f, _ in events]
    check(fracs == sorted(fracs) and fracs[-1] == 1.0, "progress fractions increase to 1.0")
    stage_msgs = [m for _, m in events if m]  # msg=None = fine-grained encode ticks
    check(any("Subtitles" in m for m in stage_msgs) and any("audio track" in m for m in stage_msgs),
          "progress log covers subtitle and audio stages")
    check(any(m is None for _, m in events), "fine-grained encode progress events present")

    # --- the core phase 6 guarantee -----------------------------------------
    project.scenes[1].effect_params = {"zoom_end": 1.6}  # edit only scene 1
    events.clear()
    report2 = renderer.render_final(project, str(out), progress=lambda f, m: events.append((f, m)))
    check(report2["scenes_rendered"] == [1] and report2["scenes_from_cache"] == [0, 2],
          "editing one scene re-renders exactly that scene")
    s2 = video_summary(out)
    check(abs(s2["duration"] - 8.0) < 0.3 and s2["has_audio"],
          "transitions/subtitles/audio correctly rebuilt around cached clips")
    check(sum(1 for _, m in events if m and "reused from cache" in m) == 2,
          "progress log reports cache reuse per scene")

    # --- error surfacing -------------------------------------------------------
    Path(a[2].stored_path).rename(Path(a[2].stored_path).with_suffix(".gone"))
    try:
        # bypass validation on purpose: renderer errors must still be actionable
        project.scenes[2].effect = "zoom_in"  # force re-render of scene 2
        try:
            renderer.render_final(project, str(out))
            check(False, "render with missing asset file should raise")
        except (RenderError, OSError) as e:
            check(len(str(e)) > 0, f"renderer failure surfaces an error ({type(e).__name__})")
    finally:
        Path(a[2].stored_path).with_suffix(".gone").rename(a[2].stored_path)

    print("Phase 6 test: ALL PASSED")


if __name__ == "__main__":
    main()
