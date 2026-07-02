"""Phase 3 test: scene editing (move/delete/duplicate) + replace_asset,
with the cache-invalidation guarantees from the plan:
  - move_scene / duplicate_scene: caches stay valid (no re-render)
  - delete_scene: remaining scenes untouched
  - replace_asset: only scenes using that asset re-render
"""
from pathlib import Path

from util import check, make_test_image, video_summary

from ffmpeg_renderer import FFmpegRenderer
from project_state import CACHE_DIR, OUTPUT_DIR, ProjectStore, Scene, Transition, _new_id


def main() -> None:
    print("Phase 3 test: scene editing + asset management")
    for clip in CACHE_DIR.glob("scene_*.mp4"):
        clip.unlink()
    img_r = make_test_image("red", "red", "RED")
    img_b = make_test_image("blue", "blue", "BLUE")
    img_g = make_test_image("green", "green", "GREEN")
    img_p = make_test_image("purple", "purple", "PURPLE")

    store = ProjectStore()
    renderer = FFmpegRenderer()
    project = store.create("phase3_test", 960, 540, 30)
    pid = project.project_id

    a = [store.import_asset(pid, str(p), renderer.probe_media(str(p)))
         for p in (img_r, img_b, img_g)]
    for asset, fx in zip(a, ["zoom_in", "zoom_out", "pan_right"]):
        project.scenes.append(Scene(scene_id=_new_id("scene"), asset_id=asset.asset_id,
                                    duration=2.0, effect=fx))
    project.transitions = [Transition(scene_a=0, scene_b=1, type="crossfade", duration=0.5),
                           Transition(scene_a=1, scene_b=2, type="wipe_up", duration=0.5)]
    out = OUTPUT_DIR / "phase3_test.mp4"
    renderer.render_final(project, str(out))

    # --- move: no cache invalidation ---------------------------------------
    order_before = [s.scene_id for s in project.scenes]
    project.move_scene(2, 0)
    check([s.scene_id for s in project.scenes] == [order_before[2], order_before[0], order_before[1]],
          "move_scene reorders correctly")
    r = renderer.render_final(project, str(out))
    check(r["scenes_from_cache"] == [0, 1, 2], "move_scene does not invalidate any scene cache")

    # --- delete: transition remapping ----------------------------------------
    project.delete_scene(1)  # middle scene; transitions at boundaries 0 and 1 touch it
    check(len(project.scenes) == 2, "delete_scene removes the scene")
    check(project.transitions == [], "transitions touching the deleted scene are dropped")
    project.transitions = [Transition(scene_a=0, scene_b=1, type="crossfade", duration=0.5)]
    project.scenes.append(Scene(scene_id=_new_id("scene"), asset_id=a[0].asset_id,
                                duration=1.5, effect="ken_burns"))
    project.delete_scene(0)
    check(project.transitions == [], "leading delete drops boundary-0 transition")
    check(len(project.scenes) == 2, "two scenes remain")

    # deletion index-shift check on a fresh 4-scene timeline
    project.scenes = [Scene(scene_id=_new_id("scene"), asset_id=a[i % 3].asset_id,
                            duration=2.0, effect="none") for i in range(4)]
    project.transitions = [Transition(scene_a=0, scene_b=1, type="crossfade", duration=0.5),
                           Transition(scene_a=2, scene_b=3, type="pixelize", duration=0.5)]
    project.delete_scene(1)
    check(len(project.transitions) == 1 and project.transitions[0].scene_a == 1
          and project.transitions[0].type == "pixelize",
          "transition after deleted scene shifts down by one")

    # --- duplicate: shares cache ------------------------------------------------
    project.transitions = []
    renderer.render_final(project, str(out))
    dup = project.duplicate_scene(0)
    check(project.scenes[1].scene_id == dup.scene_id and dup.scene_id != project.scenes[0].scene_id,
          "duplicate inserted right after original with new id")
    r = renderer.render_final(project, str(out))
    check(r["scenes_from_cache"] == [0, 1, 2, 3], "duplicate scene reuses the original's cached clip")
    check(abs(video_summary(out)["duration"] - 8.0) < 0.3, "duplicated timeline duration correct")

    # --- replace_asset: targeted invalidation --------------------------------
    keys_before = {s.scene_id: s.cache_key for s in project.scenes}
    uses_asset0 = [i for i, s in enumerate(project.scenes) if s.asset_id == a[0].asset_id]
    others = [i for i in range(len(project.scenes)) if i not in uses_asset0]
    store.replace_asset(pid, a[0].asset_id, str(img_p), renderer.probe_media(str(img_p)))
    for i in uses_asset0:
        check(project.scenes[i].cache_key is None, f"scene {i} cache invalidated by replace_asset")
    for i in others:
        check(project.scenes[i].cache_key == keys_before[project.scenes[i].scene_id],
              f"scene {i} (other asset) cache untouched")
    r = renderer.render_final(project, str(out))
    # Scenes 0, 1 and 3 are identical (same asset/effect/duration) so they share
    # one cache key: the first renders, the other two reuse it immediately.
    check(r["scenes_rendered"] == [uses_asset0[0]]
          and sorted(r["scenes_from_cache"]) == sorted(uses_asset0[1:] + others),
          "render after replace_asset re-renders only affected content (dedup across identical scenes)")
    check(Path(project.assets[a[0].asset_id].stored_path).is_file(),
          "replacement file stored under the same asset_id")

    print("Phase 3 test: ALL PASSED")


if __name__ == "__main__":
    main()
