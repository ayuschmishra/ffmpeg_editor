"""Async render pipeline hardening test.

Pins the four reported concerns:
  1. encoding progress is tracked LIVE (values move during a single encode)
  2. jobs are never marked done without the output file existing on disk
  3. failures are always reported (including a dead worker thread)
  4. cancellation takes effect mid-encode, not only at stage boundaries
"""
import json
import threading
import time
from pathlib import Path

from util import check, make_test_image

import server
from project_state import CACHE_DIR, OUTPUT_DIR


def call(fn, **kwargs) -> dict:
    return json.loads(fn(**kwargs))


def make_slow_project(name: str, seconds: float) -> str:
    """A single long 1080p ken_burns scene — a multi-second single encode."""
    img = make_test_image(f"slow_{name}", "teal", name.upper(), size="1920x1080")
    p = call(server.create_project, name=name, width=1920, height=1080, fps=30)
    a = call(server.import_asset, project_id=p["project_id"], file_path=str(img))
    call(server.add_scene, project_id=p["project_id"], asset_id=a["asset_id"],
         duration=seconds, effect="ken_burns")
    return p["project_id"]


def main() -> None:
    print("Async render pipeline test")
    for clip in CACHE_DIR.glob("scene_*.mp4"):
        clip.unlink()
    for orphan in CACHE_DIR.glob("tmp_*"):  # orphans predating the leak fix
        orphan.unlink()

    # ---- 1. live encode progress -------------------------------------------
    pid = make_slow_project("live_progress", 20.0)
    job = call(server.render_start, project_id=pid)
    check(job["success"], "job started")
    seen: list[float] = []
    while True:
        st = call(server.render_status, job_id=job["job_id"])
        seen.append(st["progress"])
        if st["state"] != "running":
            break
        time.sleep(0.1)
    check(st["state"] == "done", f"job finished (state {st['state']}, err {st.get('error')})")
    mid_scene = {p for p in seen if 0.0 < p < 0.7}
    check(len(mid_scene) >= 3,
          f"progress moves DURING the scene encode (saw {len(mid_scene)} intermediate values)")
    check(st["seconds_since_progress"] is not None, "status exposes progress heartbeat")

    # ---- 4. mid-encode cancellation ------------------------------------------
    pid2 = make_slow_project("cancel_me", 25.0)
    job2 = call(server.render_start, project_id=pid2)
    # wait until the encode is demonstrably in progress, then cancel
    for _ in range(100):
        st2 = call(server.render_status, job_id=job2["job_id"])
        if st2["progress"] > 0.05 or st2["state"] != "running":
            break
        time.sleep(0.1)
    check(st2["state"] == "running" and 0 < st2["progress"] < 0.7,
          f"caught job mid-encode (progress {st2['progress']})")
    t_cancel = time.perf_counter()
    call(server.render_cancel, job_id=job2["job_id"])
    while True:
        st2 = call(server.render_status, job_id=job2["job_id"])
        if st2["state"] != "running" or time.perf_counter() - t_cancel > 15:
            break
        time.sleep(0.1)
    took = time.perf_counter() - t_cancel
    check(st2["state"] == "cancelled", f"job cancelled (state {st2['state']})")
    check(took < 10, f"cancel took effect mid-encode in {took:.1f}s (not after the full encode)")

    # temp files from the killed encode must not linger
    stale = [f for f in CACHE_DIR.glob("tmp_*.mp4")]
    check(not stale, f"no orphaned temp clips after cancel (found {[f.name for f in stale]})")

    # ---- 3. dead worker thread is reported as failure ---------------------------
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    server._render_jobs["job_deadtest"] = {
        "job_id": "job_deadtest", "project_id": pid, "state": "running",
        "progress": 0.3, "log": [], "result": None, "error": None,
        "quality": "final", "output_path": "x", "cancel": threading.Event(),
        "started_at": time.time(), "updated_at": time.time(), "thread": dead,
    }
    st3 = call(server.render_status, job_id="job_deadtest")
    check(st3["state"] == "failed" and "thread terminated" in st3["error"],
          "dead worker thread surfaces as failed with actionable error")
    del server._render_jobs["job_deadtest"]

    # ---- 2. done requires the file to exist -------------------------------------
    class LyingRenderer:
        def render_final(self, project, output_path, progress=None, quality="final"):
            return {"output_path": str(OUTPUT_DIR / "never_written.mp4"),
                    "quality": quality, "scenes_rendered": [], "scenes_from_cache": [],
                    "duration": 1.0, "resolution": "1x1"}

    (OUTPUT_DIR / "never_written.mp4").unlink(missing_ok=True)
    real = server.renderer
    server.renderer = LyingRenderer()
    try:
        fake_job = {"job_id": "job_liar", "project_id": pid, "state": "running",
                    "progress": 0.0, "log": [], "result": None, "error": None,
                    "quality": "final", "output_path": "x",
                    "cancel": threading.Event(), "started_at": time.time(),
                    "updated_at": time.time(), "thread": None}
        server._run_render_job(fake_job, server.store.get(pid),
                               str(OUTPUT_DIR / "never_written.mp4"), "final")
        check(fake_job["state"] == "failed" and "no output file exists" in fake_job["error"],
              "job never marked done without the file on disk")
    finally:
        server.renderer = real

    print("Async render pipeline test: ALL PASSED")


if __name__ == "__main__":
    main()
