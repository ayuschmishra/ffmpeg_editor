"""End-to-end MCP test: spawn server.py over stdio and drive it as a client,
exactly the way Claude Desktop would. Grows with each phase — it exercises
every tool registered so far.
"""
import asyncio
import json
import sys
from pathlib import Path

from util import check, make_test_audio, make_test_image, video_summary, REPO_DIR

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PYTHON = str(REPO_DIR / ".venv" / "Scripts" / "python.exe")
SERVER = str(REPO_DIR / "server.py")

# Tools that must be registered, updated as phases land.
EXPECTED_TOOLS = {
    "create_project", "save_project", "load_project", "list_projects",
    "import_asset", "list_assets", "add_scene",
    "move_scene", "delete_scene", "duplicate_scene", "replace_asset",
    "set_transition", "validate_project",
    "add_subtitles", "add_subtitle_text", "clear_subtitles",
    "add_audio_track", "remove_audio_track",
    "list_effects", "list_transitions", "preview_timeline",
    "render", "render_start", "render_status", "render_cancel",
}


async def call(session: ClientSession, tool: str, args: dict | None = None) -> dict:
    result = await session.call_tool(tool, args or {})
    payload = json.loads(result.content[0].text)
    return payload


async def main() -> None:
    print("MCP end-to-end test (stdio client -> server.py)")
    img1 = make_test_image("red", "red", "SCENE 1")
    img2 = make_test_image("blue", "blue", "SCENE 2")

    params = StdioServerParameters(command=PYTHON, args=[SERVER], cwd=str(REPO_DIR))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name for t in (await session.list_tools()).tools}
            missing = EXPECTED_TOOLS - tools
            check(not missing, f"all expected tools registered (missing: {missing or 'none'})")

            effects = await call(session, "list_effects")
            names = {e["name"] for e in effects["effects"]}
            check({"zoom_in", "zoom_out", "ken_burns", "none"} <= names, "effect presets listed")

            proj = await call(session, "create_project",
                              {"name": "mcp_e2e", "width": 640, "height": 360, "fps": 24})
            check(proj["success"], "create_project succeeds")
            pid = proj["project_id"]

            a1 = await call(session, "import_asset", {"project_id": pid, "file_path": str(img1)})
            a2 = await call(session, "import_asset", {"project_id": pid, "file_path": str(img2)})
            check(a1["success"] and a1["media_type"] == "image", "import_asset returns asset metadata")

            s1 = await call(session, "add_scene",
                            {"project_id": pid, "asset_id": a1["asset_id"],
                             "duration": 2.0, "effect": "zoom_in"})
            s2 = await call(session, "add_scene",
                            {"project_id": pid, "asset_id": a2["asset_id"],
                             "duration": 1.5, "effect": "pan_left"})
            check(s1["scene_index"] == 0 and s2["scene_index"] == 1, "scenes appended in order")

            bad = await call(session, "add_scene",
                             {"project_id": pid, "asset_id": "asset_nope",
                              "duration": 2.0})
            check(not bad["success"] and "Unknown asset_id" in bad["error"],
                  "bad asset_id returns actionable error")

            # Phase 2: transition between the two scenes (shortens timeline by 0.5s)
            tr = await call(session, "set_transition",
                            {"project_id": pid, "scene_a": 0, "scene_b": 1,
                             "type": "crossfade", "duration": 0.5})
            check(tr["success"] and tr["total_duration"] == 3.0, "set_transition updates timeline")
            bad_tr = await call(session, "set_transition",
                                {"project_id": pid, "scene_a": 0, "scene_b": 1,
                                 "type": "crossfade", "duration": 2.0})
            check(not bad_tr["success"] and "shorter than both" in bad_tr["error"],
                  "over-long transition rejected with clear error")

            tl = await call(session, "preview_timeline", {"project_id": pid})
            check(tl["total_duration"] == 3.0 and len(tl["scenes"]) == 2, "preview_timeline reflects state")

            rendered = await call(session, "render", {"project_id": pid})
            check(rendered["success"], f"render succeeds ({rendered.get('error', '')})")
            check(len(rendered.get("render_log", [])) >= 3 and "Wrote" in rendered["render_log"][-1],
                  "render response includes a stage-by-stage log")
            out = Path(rendered["output_path"])
            check(out.is_file(), "rendered file exists")
            s = video_summary(out)
            check(s["width"] == 640 and s["fps"] == 24.0 and abs(s["duration"] - 3.0) < 0.3,
                  f"output matches project settings incl. transition overlap (got {s})")

            # Phase 3: edit the timeline through the MCP layer
            dup = await call(session, "duplicate_scene", {"project_id": pid, "scene_index": 0})
            check(dup["success"] and len(dup["scenes"]) == 3, "duplicate_scene via MCP")
            mv = await call(session, "move_scene",
                            {"project_id": pid, "scene_index": 2, "new_position": 0})
            check(mv["success"] and mv["scenes"][0]["effect"] == "pan_left", "move_scene via MCP")
            dl = await call(session, "delete_scene", {"project_id": pid, "scene_index": 0})
            check(dl["success"] and len(dl["scenes"]) == 2, "delete_scene via MCP")

            # Persistence across server restarts is covered by load_project:
            lp = await call(session, "load_project", {"project_id": pid})
            check(lp["success"] and len(lp["scenes"]) == 2, "load_project restores state from disk")

            # Perf update: draft quality + background render jobs
            draft = await call(session, "render", {"project_id": pid, "quality": "draft"})
            check(draft["success"] and draft["quality"] == "draft", "draft render via MCP")
            sd = video_summary(Path(draft["output_path"]))
            check(sd["width"] == 320 and sd["height"] == 180,
                  f"draft output at half resolution (got {sd['width']}x{sd['height']})")

            job = await call(session, "render_start", {"project_id": pid})
            check(job["success"] and job["state"] == "running", "render_start returns job immediately")
            jid = job["job_id"]
            status = None
            for _ in range(120):  # poll up to ~60s
                status = await call(session, "render_status", {"job_id": jid})
                if status["state"] != "running":
                    break
                await asyncio.sleep(0.5)
            check(status["state"] == "done", f"background job completes (state: {status['state']}, err: {status.get('error')})")
            check(Path(status["result"]["output_path"]).is_file(), "background job wrote output file")
            check(status["progress"] == 1.0 and len(status["log"]) >= 2,
                  "job status carries progress and stage log")
            cancel = await call(session, "render_cancel", {"job_id": jid})
            check(cancel["success"] and cancel["state"] == "done",
                  "cancelling a finished job reports it already finished")
            bad_status = await call(session, "render_status", {"job_id": "job_nope"})
            check(not bad_status["success"] and "Unknown job_id" in bad_status["error"],
                  "unknown job_id returns actionable error")

            # Phase 5: subtitles + audio through the MCP layer
            sub = await call(session, "add_subtitle_text",
                             {"project_id": pid, "text": "Scene two speaking",
                              "start": 0.2, "end": 1.2, "scene_index": 1})
            check(sub["success"] and sub["entry"]["start"] > 0.2,
                  "scene-relative subtitle converted to global time")
            music = make_test_audio("tone", seconds=10.0)
            am = await call(session, "import_asset", {"project_id": pid, "file_path": str(music)})
            tr_a = await call(session, "add_audio_track",
                              {"project_id": pid, "asset_id": am["asset_id"],
                               "type": "music", "volume": 0.6, "fade_out": 1.0})
            check(tr_a["success"] and tr_a["track_index"] == 0, "add_audio_track via MCP")
            bad_audio = await call(session, "add_audio_track",
                                   {"project_id": pid, "asset_id": a1["asset_id"]})
            check(not bad_audio["success"] and "image" in bad_audio["error"],
                  "image asset rejected as audio track")
            rendered2 = await call(session, "render", {"project_id": pid})
            check(rendered2["success"], f"render with subs+audio succeeds ({rendered2.get('error', '')})")
            s2 = video_summary(Path(rendered2["output_path"]))
            check(s2["has_audio"], "MCP-built video has audio stream")
            rm = await call(session, "remove_audio_track", {"project_id": pid, "track_index": 0})
            cl = await call(session, "clear_subtitles", {"project_id": pid})
            check(rm["success"] and cl["success"] and cl["cleared"]["manual_entries"] == 1,
                  "remove_audio_track and clear_subtitles work")

            # Phase 4: validation tool + render refusing an invalid project
            v = await call(session, "validate_project", {"project_id": pid})
            check(v["success"] and v["valid"], "validate_project passes on a good project")
            await call(session, "delete_scene", {"project_id": pid, "scene_index": 1})
            await call(session, "delete_scene", {"project_id": pid, "scene_index": 0})
            blocked = await call(session, "render", {"project_id": pid})
            check(not blocked["success"]
                  and any(e["code"] == "no_scenes" for e in blocked.get("errors", [])),
                  "render blocks on validation errors with structured report")

    print("MCP end-to-end test: ALL PASSED")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
