"""Output-path robustness test (post-move verification bug).

Covers the failure reported from Claude Desktop: render succeeded, file was
written, then the *destination* probe failed with "No such file or directory".
The fix probes the video in cache_store/ before the move; these tests pin down
the path normalization and the new finalization order.
"""
import asyncio
import json
import sys
import time as _time
from pathlib import Path

from util import check, make_test_image, video_summary

import server
from project_state import OUTPUT_DIR


def call(fn, **kwargs) -> dict:
    result = fn(**kwargs)
    if asyncio.iscoroutine(result):
        result = asyncio.get_event_loop().run_until_complete(result)
    return json.loads(result)


def main() -> None:
    print("Output path robustness test")
    img = make_test_image("red", "red", "RED")

    proj = call(server.create_project, name="outpath_test", width=640, height=360, fps=24)
    pid = proj["project_id"]
    asset = call(server.import_asset, project_id=pid, file_path=str(img))
    call(server.add_scene, project_id=pid, asset_id=asset["asset_id"], duration=1.5)

    # 1. Relative path (no extension) -> anchored under output/, .mp4 appended.
    r = call(server.render, project_id=pid, output_path="rel_subdir/jumpcut_test")
    check(r["success"], f"relative output path accepted ({r.get('error', '')})")
    expected = OUTPUT_DIR / "rel_subdir" / "jumpcut_test.mp4"
    check(Path(r["output_path"]) == expected and expected.is_file(),
          f"relative path anchored under output/ (got {r['output_path']})")
    check(r["duration"] is not None and r["resolution"] == "640x360",
          "verification metadata present (probed pre-move)")

    # 2. Quoted absolute path (models sometimes wrap paths in quotes).
    quoted = f'"{OUTPUT_DIR / "quoted_test.mp4"}"'
    r = call(server.render, project_id=pid, output_path=quoted)
    check(r["success"] and (OUTPUT_DIR / "quoted_test.mp4").is_file(),
          "surrounding quotes stripped from output path")

    # 3. Existing directory as target -> clear error, no render started.
    r = call(server.render, project_id=pid, output_path=str(OUTPUT_DIR))
    check(not r["success"] and "directory" in r["error"], "directory target rejected with clear error")

    # 4. Wrong extension -> clear error.
    r = call(server.render, project_id=pid, output_path=str(OUTPUT_DIR / "nope.avi"))
    check(not r["success"] and ".mp4" in r["error"], "non-mp4 extension rejected")

    # 5. Background job with a relative path resolves identically.
    r = call(server.render_start, project_id=pid, output_path="bg_rel_test")
    check(r["success"], "render_start accepts relative path")
    jid = r["job_id"]
    for _ in range(60):
        st = call(server.render_status, job_id=jid)
        if st["state"] != "running":
            break
        _time.sleep(0.5)
    check(st["state"] == "done", f"background job finished (state {st['state']}, err {st.get('error')})")
    check(Path(st["result"]["output_path"]) == OUTPUT_DIR / "bg_rel_test.mp4"
          and (OUTPUT_DIR / "bg_rel_test.mp4").is_file(),
          "background job output anchored under output/")

    # 6. Report metadata comes from the pre-move probe, so it must be correct
    #    even for a freshly moved file.
    s = video_summary(OUTPUT_DIR / "bg_rel_test.mp4")
    check(abs(s["duration"] - 1.5) < 0.2 and s["width"] == 640,
          "moved file matches pre-move verification metadata")

    print("Output path robustness test: ALL PASSED")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
