"""Phase 7 test (performance update): single-pass transition join, draft
quality profile, and cache separation between quality profiles.

The old pairwise join re-encoded the accumulated timeline per transition
(O(n^2)); the new join must produce frame-exact durations for any mix of hard
cuts and transitions in one encode pass.
"""
import time
from pathlib import Path

from util import check, make_test_image, video_summary

from ffmpeg_renderer import FFmpegRenderer
from project_state import CACHE_DIR, OUTPUT_DIR, ProjectStore, Scene, Transition, _new_id
from renderer_base import RenderError


def main() -> None:
    print("Phase 7 test: single-pass join + draft quality")
    for clip in CACHE_DIR.glob("scene_*.mp4"):
        clip.unlink()
    colors = ["red", "blue", "green", "gold", "purple"]
    imgs = [make_test_image(c, c, c.upper()) for c in colors]

    store = ProjectStore()
    renderer = FFmpegRenderer()
    project = store.create("phase7_test", 1280, 720, 30)
    assets = [store.import_asset(project.project_id, str(p), renderer.probe_media(str(p)))
              for p in imgs]

    # 5 scenes; boundaries: transition, HARD CUT, transition, transition —
    # exercises xfade and concat mixed in one graph.
    for i, asset in enumerate(assets):
        project.scenes.append(Scene(scene_id=_new_id("scene"), asset_id=asset.asset_id,
                                    duration=2.0, effect="zoom_in" if i % 2 else "none"))
    project.transitions = [
        Transition(0, 1, "crossfade", 0.5),
        # boundary 1->2 is a hard cut
        Transition(2, 3, "fade_black", 0.8),
        Transition(3, 4, "slide_left", 0.5),
    ]
    expected = 5 * 2.0 - (0.5 + 0.8 + 0.5)  # 8.2s

    out = OUTPUT_DIR / "phase7_final.mp4"
    report = renderer.render_final(project, str(out))
    s = video_summary(out)
    check(abs(s["duration"] - expected) < 0.15,
          f"mixed cuts+transitions frame-exact in one pass (got {s['duration']}, want {expected})")
    check(s["width"] == 1280 and s["height"] == 720, "final quality renders at full resolution")
    check(report["quality"] == "final", "report includes quality profile")
    final_keys = {sc.scene_id: sc.cache_key for sc in project.scenes}

    # ---- draft profile -------------------------------------------------------
    out_draft = OUTPUT_DIR / "phase7_draft.mp4"
    t0 = time.perf_counter()
    report_d = renderer.render_final(project, str(out_draft), quality="draft")
    draft_time = time.perf_counter() - t0
    sd = video_summary(out_draft)
    check(sd["width"] == 640 and sd["height"] == 360, f"draft renders at half resolution (got {sd['width']}x{sd['height']})")
    check(abs(sd["duration"] - expected) < 0.15, "draft duration matches final")
    check(report_d["scenes_rendered"] == list(range(5)),
          "draft uses its own cache keys (no collision with final)")
    for sc in project.scenes:
        check(sc.cache_key != final_keys[sc.scene_id], f"scene {sc.scene_id} draft key differs")
    # (No file-size assertion: ultrafast trades compression efficiency for
    # speed, so draft files can be larger on synthetic content.)
    print(f"  (draft render took {draft_time:.1f}s)")

    # Final-quality cache is intact: re-render at final hits cache everywhere.
    report_f2 = renderer.render_final(project, str(out))
    check(report_f2["scenes_from_cache"] == list(range(5)),
          "final cache untouched by draft render")

    # ---- unknown quality is rejected -------------------------------------------
    try:
        renderer.render_final(project, str(out), quality="potato")
        check(False, "unknown quality should raise")
    except RenderError as e:
        check("quality" in str(e) and "draft" in str(e), "unknown quality raises actionable error")

    # ---- single-scene and no-transition paths still lossless-concat ------------
    project.transitions = []
    report3 = renderer.render_final(project, str(out))
    check(abs(video_summary(out)["duration"] - 10.0) < 0.15, "no-transition concat path intact (10s)")

    print("Phase 7 test: ALL PASSED")


if __name__ == "__main__":
    main()
