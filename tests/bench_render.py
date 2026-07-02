"""Benchmark replicating the reported problem project:
8 scenes x 3s, alternating zoom effects, 7 transitions (crossfade/fade_black,
0.5s/0.8s), 1920x1080 @ 30fps. Measures per-stage wall time to locate the cost.
"""
import time
from pathlib import Path

from util import check, make_test_image, video_summary

from ffmpeg_renderer import FFmpegRenderer
from project_state import CACHE_DIR, OUTPUT_DIR, ProjectStore, Scene, Transition, _new_id


def main() -> None:
    print("Benchmark: 8 scenes / 7 transitions / 1080p30 / zoom effects")
    for clip in CACHE_DIR.glob("scene_*.mp4"):
        clip.unlink()
    colors = ["red", "blue", "green", "gold", "purple", "cyan", "orange", "magenta"]
    imgs = [make_test_image(f"b{i}_{c}", c, f"SCENE {i+1}", size="1920x1080")
            for i, c in enumerate(colors)]

    store = ProjectStore()
    renderer = FFmpegRenderer()
    project = store.create("bench_1080p", 1920, 1080, 30)
    assets = [store.import_asset(project.project_id, str(p), renderer.probe_media(str(p)))
              for p in imgs]
    for i, asset in enumerate(assets):
        project.scenes.append(Scene(scene_id=_new_id("scene"), asset_id=asset.asset_id,
                                    duration=3.0, effect="zoom_in" if i % 2 == 0 else "zoom_out"))
    ttypes = ["crossfade", "fade_black"] * 4
    project.transitions = [
        Transition(i, i + 1, ttypes[i], 0.5 if i % 2 == 0 else 0.8) for i in range(7)
    ]

    events = []
    t0 = time.perf_counter()
    out = OUTPUT_DIR / "bench_1080p.mp4"
    report = renderer.render_final(project, str(out),
                                   progress=lambda f, m: events.append((time.perf_counter() - t0, m)))
    total = time.perf_counter() - t0

    prev = 0.0
    for ts, msg in events:
        print(f"  +{ts - prev:6.1f}s (t={ts:6.1f}s)  {msg}")
        prev = ts
    scenes_done = next(ts for ts, m in events if "8/8" in m)
    print(f"  scene rendering: {scenes_done:.1f}s | join+downstream: {total - scenes_done:.1f}s | TOTAL: {total:.1f}s")

    s = video_summary(out)
    expected = 8 * 3.0 - (0.5 * 4 + 0.8 * 3)
    check(abs(s["duration"] - expected) < 0.3, f"duration correct ({s['duration']}s vs {expected}s expected)")

    # Warm-cache re-render (the incremental case) for comparison.
    t0 = time.perf_counter()
    renderer.render_final(project, str(out))
    print(f"  warm-cache re-render TOTAL: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
