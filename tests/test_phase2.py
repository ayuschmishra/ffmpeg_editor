"""Phase 2 test: transitions between 3+ scenes, mixed with a plain cut.

Timeline: [A zoom_in 3s] --crossfade 1s-- [B zoom_out 3s] --(hard cut)--
          [C pan_left 2s] --slide_left 0.5s-- [D none 2s]
Expected duration: 3 + 3 + 2 + 2 - 1 - 0.5 = 8.5s
"""
from pathlib import Path

from util import check, make_test_image, video_summary

from ffmpeg_renderer import FFmpegRenderer
from project_state import OUTPUT_DIR, ProjectStore, Scene, Transition, _new_id
from renderer_base import RenderError


def main() -> None:
    print("Phase 2 test: transitions")
    imgs = [make_test_image(n, c, n.upper())
            for n, c in [("red", "red"), ("blue", "blue"), ("green", "green"), ("gold", "gold")]]

    store = ProjectStore()
    renderer = FFmpegRenderer()
    project = store.create("phase2_test", 1280, 720, 30)

    assets = [store.import_asset(project.project_id, str(p), renderer.probe_media(str(p)))
              for p in imgs]
    effects = [("zoom_in", 3.0), ("zoom_out", 3.0), ("pan_left", 2.0), ("none", 2.0)]
    for asset, (fx, dur) in zip(assets, effects):
        project.scenes.append(Scene(scene_id=_new_id("scene"), asset_id=asset.asset_id,
                                    duration=dur, effect=fx))
    project.transitions = [
        Transition(scene_a=0, scene_b=1, type="crossfade", duration=1.0),
        Transition(scene_a=2, scene_b=3, type="slide_left", duration=0.5),
    ]
    store.save(project.project_id)
    check(abs(project.total_duration() - 8.5) < 0.01, "model total_duration accounts for overlaps")

    out = OUTPUT_DIR / "phase2_test.mp4"
    report = renderer.render_final(project, str(out))
    check(Path(report["output_path"]).is_file(), "final video exists")
    s = video_summary(out)
    check(abs(s["duration"] - 8.5) < 0.3, f"output duration ~8.5s with 2 transitions (got {s['duration']})")
    check(s["width"] == 1280 and s["fps"] == 30.0, "resolution/fps preserved through xfade")

    # All-transition timeline (every boundary): 3+3+2+2 - 3*0.5 = 8.5s
    project.transitions = [
        Transition(scene_a=i, scene_b=i + 1, type=t, duration=0.5)
        for i, t in enumerate(["crossfade", "wipe_up", "pixelize"])
    ]
    report2 = renderer.render_final(project, str(out))
    check(report2["scenes_from_cache"] == [0, 1, 2, 3],
          "changing transitions does NOT invalidate scene caches")
    s2 = video_summary(out)
    check(abs(s2["duration"] - 8.5) < 0.3, f"3-transition timeline duration correct (got {s2['duration']})")

    # Error surface: unknown transition type fails with a clear message.
    project.transitions = [Transition(scene_a=0, scene_b=1, type="warp_zoom", duration=0.5)]
    try:
        renderer.render_final(project, str(out))
        check(False, "unknown transition type should raise")
    except RenderError as e:
        check("Unknown transition" in str(e) and "crossfade" in str(e),
              "unknown transition raises actionable RenderError")

    print("Phase 2 test: ALL PASSED")


if __name__ == "__main__":
    main()
