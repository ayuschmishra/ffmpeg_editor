"""Phase 1 test: assets -> scenes with zoom effects -> rendered MP4, with caching.

Exercises the core modules directly (project store + FFmpegRenderer through the
Renderer interface). The MCP layer is tested separately in test_mcp_client.py.
"""
from util import check, make_test_image, make_test_video, video_summary

from ffmpeg_renderer import FFmpegRenderer
from project_state import OUTPUT_DIR, ProjectStore
from project_state import Scene, _new_id
from cache import cached_clip_path, has_cached_clip
from renderer_base import Renderer
from pathlib import Path


def main() -> None:
    print("Phase 1 test: core slideshow pipeline")
    from project_state import CACHE_DIR
    for clip in CACHE_DIR.glob("scene_*.mp4"):  # fresh cache for a repeatable run
        clip.unlink()
    img1 = make_test_image("red", "red", "SCENE 1")
    img2 = make_test_image("blue", "blue", "SCENE 2")
    vid1 = make_test_video("clip", seconds=4.0)

    store = ProjectStore()
    renderer: Renderer = FFmpegRenderer()

    project = store.create("phase1_test", 1280, 720, 30)
    check(project.project_id.startswith("proj_"), "project created with id")
    check((Path(store.projects_dir) / f"{project.project_id}.json").exists(),
          "project JSON persisted on create")

    a1 = store.import_asset(project.project_id, str(img1), renderer.probe_media(str(img1)))
    a2 = store.import_asset(project.project_id, str(img2), renderer.probe_media(str(img2)))
    a3 = store.import_asset(project.project_id, str(vid1), renderer.probe_media(str(vid1)))
    check(a1.media_type == "image" and a1.metadata["width"] == 1600, "image asset probed correctly")
    check(a3.media_type == "video" and abs(a3.metadata["duration"] - 4.0) < 0.3, "video asset probed correctly")
    check(Path(a1.stored_path).is_file(), "asset copied into assets/ store")

    def add(asset_id: str, dur: float, effect: str, params: dict | None = None) -> None:
        project.scenes.append(Scene(scene_id=_new_id("scene"), asset_id=asset_id,
                                    duration=dur, effect=effect, effect_params=params or {}))

    add(a1.asset_id, 2.0, "zoom_in")
    add(a2.asset_id, 2.0, "zoom_out", {"zoom_start": 1.4})
    add(a3.asset_id, 3.0, "none")
    store.save(project.project_id)

    out = OUTPUT_DIR / "phase1_test.mp4"
    report = renderer.render_final(project, str(out))
    check(Path(report["output_path"]).is_file(), "final video file exists")
    check(report["scenes_rendered"] == [0, 1, 2], "all 3 scenes rendered on first pass")

    s = video_summary(out)
    check(s["width"] == 1280 and s["height"] == 720, f"output resolution is 1280x720 (got {s['width']}x{s['height']})")
    check(s["fps"] == 30.0, f"output fps is 30 (got {s['fps']})")
    check(abs(s["duration"] - 7.0) < 0.3, f"output duration ~7.0s (got {s['duration']})")

    # --- caching: second render must reuse every clip -----------------------
    mtimes = {sc.cache_key: Path(cached_clip_path(sc.cache_key)).stat().st_mtime_ns
              for sc in project.scenes}
    report2 = renderer.render_final(project, str(out))
    check(report2["scenes_from_cache"] == [0, 1, 2], "second render served all scenes from cache")
    for sc in project.scenes:
        check(Path(cached_clip_path(sc.cache_key)).stat().st_mtime_ns == mtimes[sc.cache_key],
              f"cached clip untouched for scene {sc.scene_id}")

    # --- cache key changes when params change --------------------------------
    old_key = project.scenes[0].cache_key
    project.scenes[0].duration = 2.5
    report3 = renderer.render_final(project, str(out))
    check(project.scenes[0].cache_key != old_key, "cache key changed after editing scene duration")
    check(report3["scenes_rendered"] == [0] and report3["scenes_from_cache"] == [1, 2],
          "only the edited scene re-rendered")
    check(abs(video_summary(out)["duration"] - 7.5) < 0.3, "new duration reflected in output")

    # --- persistence round-trip ----------------------------------------------
    store2 = ProjectStore()
    reloaded = store2.load(project.project_id)
    check(len(reloaded.scenes) == 3 and reloaded.scenes[0].effect == "zoom_in",
          "project state survives reload from JSON")

    print("Phase 1 core test: ALL PASSED")


if __name__ == "__main__":
    main()
