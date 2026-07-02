# ffmpeg_editor (Reel) — FFmpeg Video Editor MCP Server

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that
lets Claude Desktop edit video by calling structured tools. Claude assembles a
project step by step — import assets, build scenes with effects, add
transitions, subtitles and audio — and the server owns all state, validation
and FFmpeg rendering. **Claude never touches FFmpeg directly**; every call is
validated and translated into filter graphs by this server.

Internally the MCP server is named `reel` (see `server.py` / the config
examples below) — `ffmpeg_editor` is the project/repo name.

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configure Claude Desktop](#configure-claude-desktop)
- [Verify the install](#verify-the-install)
- [Tools reference](#tools-reference)
- [Effects and transitions](#effects-and-transitions)
- [Example workflow](#example-workflow)
- [Rendering performance](#rendering-performance)
- [Behavior notes / design decisions](#behavior-notes--design-decisions)
- [Project structure](#project-structure)
- [Development and testing](#development-and-testing)
- [Troubleshooting](#troubleshooting)
- [Out of scope for v1](#out-of-scope-for-v1)
- [License](#license)

## Features

- **Full project model**: multi-scene timelines with per-scene effects,
  transitions, burned-in subtitles (SRT or manual lines) and mixed audio
  tracks (music/SFX/narration), persisted as JSON so a session survives a
  restart.
- **Six built-in image effects** (`zoom_in`, `zoom_out`, `pan_left`,
  `pan_right`, `ken_burns`, `none`) plus a `custom_filter` escape hatch for
  raw FFmpeg `-vf` chains.
- **Fourteen transition presets** (crossfade, fade to black/white, slides,
  wipes, pixelize, circle open, dissolve), joined in a **single encode pass**
  regardless of transition count.
- **Content-addressed render cache**: each scene renders once, keyed by a
  hash of its asset checksum + effect + params + duration + resolution/fps.
  Editing one scene only re-renders that scene.
- **Background rendering with live progress**: `render_start` returns
  instantly; `render_status` streams FFmpeg's own progress (plus a stall
  heartbeat); `render_cancel` kills the running FFmpeg process within about a
  second while keeping cached scene clips.
- **Draft-quality previews**: `quality="draft"` renders at half resolution
  with the fastest encoder preset, cached separately from final renders.
- **Structured pre-render validation**: every render is checked first and
  returns actionable `{code, message, where}` errors instead of a raw FFmpeg
  stack trace.
- **Optional hardware encoding** via `h264_nvenc` / `h264_qsv` / `h264_amf`.
- **Backend-agnostic seam** (`renderer_base.py`): the FFmpeg backend is the
  only implementation today, but nothing above it imports FFmpeg specifics.

## Architecture

```
Claude Desktop (MCP client)
        │  tool calls with structured params
        ▼
  server.py            MCP tool layer (FastMCP)
  project_state.py     project/timeline/asset model + JSON persistence
  validator.py         structured pre-render checks
  cache.py             content-addressed scene render cache
        │
  renderer_base.py     Renderer abstract interface  ← backend-agnostic seam
        ▼
  ffmpeg_renderer.py   FFmpegRenderer (v1's only backend) + presets.py
        ▼
  output/*.mp4
```

Rendering is **staged and cached**: each scene renders to its own clip keyed
by a hash of (asset checksum, effect, params, duration, resolution, fps).
Editing one scene re-renders only that scene; transitions, subtitles and
audio are cheap downstream steps rebuilt around the cached clips.

## Requirements

- **Python 3.10+** (developed against 3.13)
- **FFmpeg** on `PATH` — `ffmpeg -version` and `ffprobe -version` must both
  work. Install via your OS package manager (see below) or from
  [ffmpeg.org](https://ffmpeg.org/download.html).
- **Claude Desktop**, or any other MCP-compatible client that can launch a
  local stdio server.
- **git** (to clone the repo).

Installing FFmpeg:

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` (or `choco install ffmpeg`) |
| macOS | `brew install ffmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |

## Quick start

Clone the repo, create a virtual environment, and install the one runtime
dependency (`mcp`, which pulls in `pydantic`, `anyio`, `httpx`, `uvicorn`
transitively — no other packages are required).

### Windows (PowerShell)

```powershell
git clone https://github.com/ayuschmishra/ffmpeg_editor.git
cd ffmpeg_editor
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
ffmpeg -version   # confirm FFmpeg is on PATH before continuing
```

### macOS / Linux (bash/zsh)

```bash
git clone https://github.com/ayuschmishra/ffmpeg_editor.git
cd ffmpeg_editor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ffmpeg -version   # confirm FFmpeg is on PATH before continuing
```

That's it — no database, no external services, no build step. The server
creates its own `assets/`, `projects/`, `cache_store/` and `output/`
directories on first run.

## Configure Claude Desktop

Claude Desktop's config file lives at:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

Merge the `mcpServers.reel` block below into that file (create the file if it
doesn't exist yet), replacing the path with the **absolute path** to where
you cloned this repo. Example templates are also provided in the repo:
`claude_desktop_config.windows.example.json` and
`claude_desktop_config.macos-linux.example.json`.

**Windows:**

```json
{
  "mcpServers": {
    "reel": {
      "command": "C:\\path\\to\\ffmpeg_editor\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\ffmpeg_editor\\server.py"]
    }
  }
}
```

**macOS / Linux:**

```json
{
  "mcpServers": {
    "reel": {
      "command": "/path/to/ffmpeg_editor/.venv/bin/python",
      "args": ["/path/to/ffmpeg_editor/server.py"]
    }
  }
}
```

To enable hardware encoding, add an `"env"` block to the server entry, e.g.
`"env": {"REEL_VIDEO_ENCODER": "h264_nvenc"}` (see
[Rendering performance](#rendering-performance)).

Fully restart Claude Desktop (quit, not just close the window) after editing
the config. The `reel` tools should then appear in a new conversation's tool
list (look for the 🔨 icon).

## Verify the install

Before wiring this into Claude Desktop, confirm the server itself runs
cleanly in isolation:

```powershell
# Windows
.venv\Scripts\python tests\test_phase1.py
```

```bash
# macOS / Linux
.venv/bin/python tests/test_phase1.py
```

This exercises the core pipeline (project creation, asset import, scene
rendering, caching) end to end using FFmpeg-generated test fixtures — no
external media required. See [Development and testing](#development-and-testing)
for the full suite, including `tests/test_mcp_client.py`, which drives the
server over a real MCP stdio connection exactly like Claude Desktop would.

## Tools reference

| Group | Tools |
|---|---|
| Project | `create_project`, `save_project`, `load_project`, `list_projects` |
| Assets | `import_asset`, `list_assets`, `replace_asset` |
| Scenes | `add_scene`, `move_scene`, `delete_scene`, `duplicate_scene` |
| Transitions | `set_transition` |
| Subtitles | `add_subtitles` (SRT), `add_subtitle_text`, `clear_subtitles` |
| Audio | `add_audio_track`, `remove_audio_track` |
| Inspect/render | `list_effects`, `list_transitions`, `preview_timeline`, `validate_project`, `render`, `render_start`, `render_status`, `render_cancel` |

Typical flow: `create_project` → `import_asset` (per file) → `add_scene` (per
scene, with an effect) → `set_transition` → `add_subtitle_text` /
`add_audio_track` → `render`. Then keep editing and re-rendering — only
changed scenes are rebuilt.

### Effects (images)

`zoom_in`, `zoom_out`, `pan_left`, `pan_right`, `ken_burns`, `none`, plus a
`custom_filter` escape hatch (raw `-vf` chain). Video assets use `none`
(scale + letterbox) or `custom_filter`.

### Transitions

`crossfade`, `fade_black`, `fade_white`, `slide_*`, `wipe_*`, `pixelize`,
`circle_open`, `dissolve` — implemented with `xfade`; a transition overlaps
its two scenes, shortening the timeline by its duration.

## Example workflow

A minimal end-to-end session, as Claude would call it (tool → key args):

1. `create_project(name="holiday_recap", width=1920, height=1080, fps=30)`
2. `import_asset(project_id, file_path="C:/photos/beach.jpg")` → `asset_id`
3. `add_scene(project_id, asset_id, duration=4, effect="ken_burns")`
4. Repeat 2–3 for each photo/clip.
5. `set_transition(project_id, scene_a=0, scene_b=1, type="crossfade", duration=0.75)`
6. `add_audio_track(project_id, asset_id=<music>, type="music", fade_in=1, fade_out=2)`
7. `add_subtitle_text(project_id, text="Summer 2026", start=0, end=3, scene_index=0)`
8. `validate_project(project_id)` — fix any reported errors.
9. `render_start(project_id, quality="draft")` → poll `render_status(job_id)`
   for a quick preview.
10. `render_start(project_id, quality="final", output_path="output/holiday_recap.mp4")`
    → poll `render_status(job_id)` until `state == "done"`.

## Rendering performance

- **Transitions are joined in a single encode pass** (one `filter_complex`
  chaining xfade/concat with frame-exact offsets), regardless of transition
  count. The earlier pairwise fold re-encoded the accumulated timeline per
  boundary (O(n²)); on an 8-scene/7-transition 1080p30 project that was 62s
  total — now ~16s cold, ~8s warm-cache.
- **Progress is live**: FFmpeg's `-progress` stream is parsed, so
  `render_status` shows movement *during* encodes (plus a
  `seconds_since_progress` heartbeat for spotting stalls), `render_cancel`
  kills the running FFmpeg within ~a second, a job is only marked `done` after
  the output file is confirmed on disk, and a dead worker thread is reported
  as `failed` instead of `running` forever.
- **Use `render_start` + `render_status` for anything big** (roughly 5+
  scenes with transitions, or long durations). The synchronous `render`
  blocks until done and can be killed by the *MCP client's* tool timeout
  (~60s in Claude Desktop) — the server itself has no timeout. `render_start`
  returns a job_id instantly; `render_cancel` stops a job at the next stage
  boundary (cached scene clips are kept, so restarting is cheap — the cache
  is the checkpoint).
- **`quality="draft"`** on `render`/`render_start` gives a fast preview: half
  resolution + fastest encoder preset, cached under separate keys so draft
  iteration never touches final-quality cache.
- **Hardware encoding**: set the `REEL_VIDEO_ENCODER` env var (in the Claude
  Desktop server config's `"env"` block, or your shell before running tests)
  to `h264_nvenc` (NVIDIA), `h264_qsv` (Intel) or `h264_amf` (AMD). Default is
  `libx264` (CPU). Verify your FFmpeg build supports it:
  `ffmpeg -encoders | findstr nvenc` (Windows) or
  `ffmpeg -encoders | grep nvenc` (macOS/Linux).

## Behavior notes / design decisions

- **Output verification happens before the file moves to its destination.**
  The rendered video is probed while still in `cache_store/`; after the move
  only existence is re-checked (with retries). Verifying at the destination
  used to fail spuriously ("No such file or directory" right after a
  successful write) when the target folder was cloud-synced (OneDrive,
  iCloud Drive, Dropbox) or antivirus-scanned. Prefer local, non-synced
  output folders for large renders.
- **Output paths are normalized**: stray quotes stripped, `~` expanded,
  relative paths anchored under `output/` (never the server's cwd — the MCP
  host controls that), `.mp4` appended if missing, directory targets
  rejected.
- **Defaults**: 1920x1080 @ 30 fps, MP4/H.264. Use 1080x1920 for vertical
  reels.
- **Assets are copied** into `assets/` on import, so moving/deleting the
  source later doesn't break renders. `replace_asset` swaps content under
  the same id and invalidates only the affected scenes' caches.
- **Transitions anchor to timeline positions**, not scene identities.
  Deleting a scene drops transitions touching it and shifts the rest; review
  transitions after `move_scene`.
- **Scene clips are silent**; a video asset's own audio is not used. All
  sound comes from `add_audio_track` (mixing, not concatenation —
  overlapping tracks play simultaneously; audio is trimmed at the video's
  end).
- **Subtitles** come from *either* one SRT file *or* manual entries, not
  both; `clear_subtitles` switches. Manual entries with `scene_index` are
  converted to global timestamps at add time (reordering later does not
  re-anchor them).
- **Identical scenes share one cached clip** (content-addressed cache), so
  duplicates render once.
- **`render` always validates first** and refuses to render on errors,
  returning `{errors: [{code, message, where}], warnings: [...]}`.
- Everything in `cache_store/` and `tests/media/` is disposable (both are
  regenerated on demand and gitignored); `projects/` holds the saved state,
  `assets/` the imported media, `output/` the results.

## Project structure

```
ffmpeg_editor/
├── server.py                      # MCP tool layer (entrypoint)
├── project_state.py               # project/timeline/asset model + persistence
├── validator.py                   # pre-render validation
├── cache.py                       # scene render cache
├── renderer_base.py               # backend-agnostic Renderer interface
├── ffmpeg_renderer.py             # FFmpeg backend
├── presets.py                     # effect + transition filter definitions
├── requirements.txt               # single runtime dependency (mcp)
├── claude_desktop_config.windows.example.json
├── claude_desktop_config.macos-linux.example.json
├── tests/                         # per-phase test scripts (no pytest needed)
│   ├── test_phase1.py … test_phase7.py
│   ├── test_mcp_client.py         # real MCP stdio client, end-to-end
│   ├── bench_render.py            # timing benchmark
│   └── media/                     # auto-generated fixtures (gitignored)
├── assets/                        # imported media copies   (gitignored, auto-created)
├── projects/                      # saved project JSON      (gitignored, auto-created)
├── cache_store/                   # cached rendered clips    (gitignored, auto-created)
└── output/                        # rendered final videos    (gitignored, auto-created)
```

The four data directories (`assets/`, `projects/`, `cache_store/`,
`output/`) are created automatically on first run by `project_state.py` — a
fresh clone needs nothing pre-created.

## Development and testing

Per-phase test suites live in `tests/` (plain scripts, no pytest dependency):

```powershell
# Windows
.venv\Scripts\python tests\test_phase1.py   # core pipeline + caching
.venv\Scripts\python tests\test_phase2.py   # transitions
.venv\Scripts\python tests\test_phase3.py   # editing + cache invalidation
.venv\Scripts\python tests\test_phase4.py   # validator
.venv\Scripts\python tests\test_phase5.py   # subtitles + audio (signal-verified)
.venv\Scripts\python tests\test_phase6.py   # incremental rendering + progress
.venv\Scripts\python tests\test_phase7.py   # single-pass join + draft quality
.venv\Scripts\python tests\test_mcp_client.py  # real MCP stdio client end-to-end
.venv\Scripts\python tests\bench_render.py  # 8-scene 1080p timing benchmark
```

```bash
# macOS / Linux — same scripts, different interpreter path
.venv/bin/python tests/test_phase1.py
.venv/bin/python tests/test_phase2.py
.venv/bin/python tests/test_phase3.py
.venv/bin/python tests/test_phase4.py
.venv/bin/python tests/test_phase5.py
.venv/bin/python tests/test_phase6.py
.venv/bin/python tests/test_phase7.py
.venv/bin/python tests/test_mcp_client.py
.venv/bin/python tests/bench_render.py
```

Test fixtures under `tests/media/` are generated on demand with FFmpeg
(`lavfi` solid-color clips/tones) — nothing needs to be downloaded or
committed.

To add a rendering backend later: subclass `Renderer` in `renderer_base.py`
and swap the instance in `server.py` — nothing else imports FFmpeg
specifics.

## Troubleshooting

- **Claude Desktop doesn't show the `reel` tools**: confirm the config file
  path/JSON is valid (a trailing comma will silently break it), that the
  `command` path points at the venv's Python interpreter (not the system
  Python), and that you fully quit and reopened Claude Desktop.
- **`ffmpeg: command not found` / probing fails**: `ffmpeg` and `ffprobe`
  must both be on `PATH` for the *user account Claude Desktop runs as* — a
  shell-only PATH addition may not be visible to a GUI app until you log out
  and back in (Windows) or restart (macOS/Linux).
- **Render reports success but the output file is missing**: the target
  folder is likely cloud-synced (OneDrive/iCloud/Dropbox) or
  antivirus-scanned, delaying the file becoming visible. Render to a local,
  non-synced folder, or pass an explicit `output_path` there.
- **A render seems to hang**: use `render_start` + `render_status` instead
  of the synchronous `render` tool — the latter can be silently killed by
  the MCP client's own tool timeout (~60s in Claude Desktop) even though the
  server keeps working. `render_status` also exposes
  `seconds_since_progress` to distinguish a stall from normal encode time.
- **Hardware encoder not used / errors on `REEL_VIDEO_ENCODER`**: confirm
  your FFmpeg build actually includes it (`ffmpeg -encoders | findstr nvenc`
  on Windows, `| grep nvenc` on macOS/Linux) — many distro/package-manager
  FFmpeg builds omit proprietary hardware encoders.
- **Project won't load after a restart**: use `list_projects` to find the
  correct `project_id` — projects are keyed by id, not name, and the
  in-memory server state does not survive a restart (only the JSON on disk
  does; reload it with `load_project`).

## Out of scope for v1

Undo/redo, multi-track video compositing, plugin effects, non-FFmpeg
backends, cloud rendering.

## License

MIT — see [LICENSE](LICENSE).
