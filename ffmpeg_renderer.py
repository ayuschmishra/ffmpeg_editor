"""FFmpegRenderer — v1's only Renderer implementation.

All FFmpeg specifics live here (and in presets.py). Rendering is staged:
  1. each scene -> standalone cached clip (cache.py decides reuse)
  2. clips joined: lossless concat for hard cuts; when transitions exist, ONE
     filter_complex chains every xfade/concat in a single encode pass
  3. subtitles burned in
  4. audio mixed + attached (video stream-copied)
Editing one scene therefore only re-renders that scene; the cheaper downstream
steps are re-run to rebuild the final file.

Quality profiles: "final" (default) or "draft" — draft renders at half
resolution with the fastest encoder preset for quick previews, and caches its
scene clips under separate keys so previews never pollute final-quality cache.

The video encoder defaults to libx264; set the REEL_VIDEO_ENCODER environment
variable to h264_nvenc / h264_qsv / h264_amf to use hardware encoding.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from cache import cached_clip_path, has_cached_clip, scene_cache_key
from presets import TRANSITIONS, build_effect_filter
from project_state import CACHE_DIR, Asset, Project, Scene, Transition
from renderer_base import Renderer, RenderError

VIDEO_ENCODER = os.environ.get("REEL_VIDEO_ENCODER", "libx264")

# Per-encoder rate-control/speed args for each quality profile.
_ENCODER_QUALITY_ARGS: dict[str, dict[str, list[str]]] = {
    "libx264":    {"final": ["-preset", "medium", "-crf", "20"],
                   "draft": ["-preset", "ultrafast", "-crf", "30"]},
    "h264_nvenc": {"final": ["-preset", "p5", "-cq", "21"],
                   "draft": ["-preset", "p1", "-cq", "32"]},
    "h264_qsv":   {"final": ["-global_quality", "21"],
                   "draft": ["-global_quality", "32"]},
    "h264_amf":   {"final": ["-quality", "balanced"],
                   "draft": ["-quality", "speed"]},
}

QUALITY_PROFILES = ("final", "draft")


def encode_args(quality: str = "final") -> list[str]:
    """Consistent encode settings per quality profile; identical settings for
    every intermediate clip so the concat demuxer can stream-copy."""
    if quality not in QUALITY_PROFILES:
        raise RenderError(f"Unknown quality '{quality}'. Use one of: {', '.join(QUALITY_PROFILES)}.")
    qargs = _ENCODER_QUALITY_ARGS.get(VIDEO_ENCODER, {}).get(quality, [])
    return ["-c:v", VIDEO_ENCODER, *qargs,
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-video_track_timescale", "90000"]


class FFmpegRenderer(Renderer):
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    # ---- low-level helpers -------------------------------------------------
    def _run(self, cmd: list[str], what: str, cwd: str | None = None,
             progress_cb=None, expected_duration: float | None = None) -> None:
        """Run an ffmpeg/ffprobe command.

        With progress_cb + expected_duration, ffmpeg's `-progress` stream is
        parsed live and translated into progress_cb(fraction in 0..1) calls
        during the encode. If progress_cb raises (cooperative cancellation),
        the ffmpeg process is killed immediately — cancel takes effect
        mid-encode, not just at stage boundaries.
        """
        live = progress_cb is not None and expected_duration and expected_duration > 0
        try:
            if not live:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", cwd=cwd)
                returncode, stderr_text = proc.returncode, proc.stderr or ""
            else:
                cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
                stderr_path = self._tmp(".log")
                try:
                    with open(stderr_path, "w", encoding="utf-8", errors="replace") as err_f:
                        proc = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=err_f,
                            text=True, encoding="utf-8", errors="replace", cwd=cwd)
                        try:
                            last = 0.0
                            for line in proc.stdout:
                                key, _, val = line.strip().partition("=")
                                # ffmpeg's out_time_ms is actually microseconds
                                # (long-standing quirk; some builds emit
                                # out_time_us instead).
                                if key in ("out_time_ms", "out_time_us"):
                                    try:
                                        us = int(val)
                                    except ValueError:
                                        continue
                                    frac = min(us / 1_000_000 / expected_duration, 1.0)
                                    if frac - last >= 0.02:
                                        last = frac
                                        progress_cb(frac)
                            proc.wait()
                        except BaseException:
                            proc.kill()
                            proc.wait()
                            raise
                    returncode = proc.returncode
                    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                finally:
                    stderr_path.unlink(missing_ok=True)
        except FileNotFoundError as e:
            raise RenderError(
                f"{what} failed: '{cmd[0]}' not found. Install FFmpeg and make sure "
                f"it is on PATH (check with 'ffmpeg -version')."
            ) from e
        if returncode != 0:
            tail = "\n".join(stderr_text.strip().splitlines()[-12:])
            raise RenderError(f"{what} failed (ffmpeg exit code {returncode}). Last output:\n{tail}")

    def _tmp(self, suffix: str = ".mp4") -> Path:
        return CACHE_DIR / f"tmp_{uuid.uuid4().hex[:10]}{suffix}"

    def _run_to(self, tmp_out: Path, cmd: list[str], what: str, **kw) -> None:
        """_run wrapper for commands writing to a temp output: on any failure
        (ffmpeg error OR cancellation kill) the partial output is removed —
        ffmpeg creates the file even when it fails, which used to leave
        orphaned tmp_*.mp4 clips in cache_store/."""
        try:
            self._run(cmd, what, **kw)
        except BaseException:
            tmp_out.unlink(missing_ok=True)
            raise

    @staticmethod
    def _dims(project: Project, quality: str) -> tuple[int, int]:
        """Target render dimensions: full size, or half (kept even) for drafts."""
        if quality == "draft":
            return max((project.width // 2) & ~1, 2), max((project.height // 2) & ~1, 2)
        return project.width, project.height

    # ---- probing -------------------------------------------------------------
    def probe_media(self, path: str) -> dict[str, Any]:
        cmd = [self.ffprobe, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            raise RenderError(f"Could not read media file '{path}': {tail[0]}")
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = data.get("format", {})
        duration = None
        if fmt.get("duration"):
            try:
                duration = round(float(fmt["duration"]), 3)
            except ValueError:
                pass
        meta: dict[str, Any] = {"has_audio": audio is not None, "duration": duration}
        if video:
            meta["width"] = video.get("width")
            meta["height"] = video.get("height")
            # Still images report 1 frame / tiny duration.
            if video.get("codec_name") in {"png", "mjpeg", "bmp", "webp", "tiff", "gif"}:
                meta["duration"] = None
        return meta

    # ---- stage 1: scenes ------------------------------------------------------
    @staticmethod
    def _scene_key(project: Project, scene: Scene, asset: Asset, quality: str) -> str:
        return scene_cache_key(
            asset.checksum, scene.effect, scene.effect_params,
            scene.duration, project.width, project.height, project.fps,
            quality=quality,
        )

    def render_scene(self, project: Project, scene: Scene, asset: Asset,
                     quality: str = "final", progress_cb=None) -> str:
        key = self._scene_key(project, scene, asset, quality)
        clip = cached_clip_path(key)
        scene.cache_key = key
        scene.cached_clip_path = str(clip)
        if has_cached_clip(key):
            return str(clip)

        w, h = self._dims(project, quality)
        vf = build_effect_filter(
            scene.effect, asset.media_type, w, h,
            project.fps, scene.duration, scene.effect_params,
        )
        n_frames = max(int(round(scene.duration * project.fps)), 1)
        tmp = self._tmp()
        if asset.media_type == "image":
            cmd = [self.ffmpeg, "-y", "-loop", "1", "-i", asset.stored_path,
                   "-vf", vf, "-frames:v", str(n_frames), "-an", *encode_args(quality), str(tmp)]
        elif asset.media_type == "video":
            cmd = [self.ffmpeg, "-y", "-i", asset.stored_path,
                   "-vf", vf, "-t", f"{scene.duration:.3f}", "-an", *encode_args(quality), str(tmp)]
        else:
            raise RenderError(
                f"Asset '{asset.asset_id}' is audio and cannot be used as a scene. "
                f"Use add_audio_track for audio files."
            )
        try:
            self._run(cmd, f"Rendering scene (effect '{scene.effect}')",
                      progress_cb=progress_cb, expected_duration=scene.duration)
            tmp.replace(clip)  # atomic-ish: cache never holds partial renders
        finally:
            tmp.unlink(missing_ok=True)
        return str(clip)

    # ---- stage 2: joining ------------------------------------------------------
    def _concat(self, clips: list[str], out: Path) -> None:
        """Lossless join of identically-encoded clips via the concat demuxer."""
        list_file = self._tmp(".txt")
        list_file.write_text(
            "".join(f"file '{Path(c).as_posix()}'\n" for c in clips), encoding="utf-8"
        )
        try:
            self._run_to(
                out,
                [self.ffmpeg, "-y", "-f", "concat", "-safe", "0",
                 "-i", str(list_file), "-c", "copy", str(out)],
                "Concatenating scene clips",
            )
        finally:
            list_file.unlink(missing_ok=True)

    def render_transition(self, project: Project, clip_a: str, clip_b: str,
                          transition: Transition, a_duration: float,
                          quality: str = "final") -> str:
        """Join two clips with an xfade across their boundary. Retained for the
        Renderer contract; _join_clips handles full timelines in one pass."""
        preset = TRANSITIONS.get(transition.type)
        if preset is None:
            raise RenderError(
                f"Unknown transition '{transition.type}'. Available: {', '.join(TRANSITIONS)}."
            )
        offset = a_duration - transition.duration
        if offset < 0:
            raise RenderError(
                f"Transition duration {transition.duration}s is longer than the "
                f"preceding footage ({a_duration:.2f}s). Shorten the transition."
            )
        out = self._tmp()
        graph = (
            f"[0:v][1:v]xfade=transition={preset['xfade']}:"
            f"duration={transition.duration:.3f}:offset={offset:.3f},"
            f"format=yuv420p[v]"
        )
        self._run_to(
            out,
            [self.ffmpeg, "-y", "-i", clip_a, "-i", clip_b,
             "-filter_complex", graph, "-map", "[v]", "-an", *encode_args(quality), str(out)],
            f"Rendering transition '{transition.type}'",
        )
        return str(out)

    def _join_clips(self, project: Project, clips: list[str], quality: str,
                    progress_cb=None) -> str:
        """Join all scene clips left-to-right, applying transitions where set.

        No transitions: lossless concat demuxer (stream copy, zero encode).
        With transitions: a SINGLE filter_complex chains every boundary —
        xfade for transitions, the concat filter for hard cuts — so the whole
        timeline is encoded exactly once regardless of transition count
        (the old pairwise fold re-encoded the accumulated video per boundary,
        which was O(n²) and dominated render time).
        Returns a temp file the caller owns.
        """
        result = self._tmp()
        if not project.transitions:
            if len(clips) == 1:
                shutil.copyfile(clips[0], result)
            else:
                self._concat(clips, result)
            return str(result)

        by_boundary = {t.scene_a: t for t in project.transitions}
        fps = project.fps
        # Clip lengths in frames are exact by construction (-frames:v at CFR),
        # so xfade offsets can be computed arithmetically — no probing needed.
        frames = [max(int(round(s.duration * fps)), 1) for s in project.scenes]

        cmd = [self.ffmpeg, "-y"]
        for c in clips:
            cmd += ["-i", c]
        # xfade demands identical timebases on both inputs, but the concat
        # filter rewrites its output timebase — normalize every input to AVTB
        # up front so xfade and concat can be chained in any order.
        filters: list[str] = [f"[{i}:v]settb=AVTB[s{i}]" for i in range(len(clips))]
        cur_label, cur_frames = "s0", frames[0]
        for i in range(1, len(clips)):
            t = by_boundary.get(i - 1)
            out_label = f"j{i}"
            if t is not None:
                preset = TRANSITIONS.get(t.type)
                if preset is None:
                    raise RenderError(
                        f"Unknown transition '{t.type}'. Available: {', '.join(TRANSITIONS)}."
                    )
                t_frames = max(int(round(t.duration * fps)), 1)
                if t_frames >= cur_frames:
                    raise RenderError(
                        f"Transition at scenes {t.scene_a}->{t.scene_b} "
                        f"({t.duration}s) is longer than the footage before it."
                    )
                offset = (cur_frames - t_frames) / fps
                filters.append(
                    f"[{cur_label}][s{i}]xfade=transition={preset['xfade']}:"
                    f"duration={t.duration:.3f}:offset={offset:.3f}[{out_label}]"
                )
                cur_frames += frames[i] - t_frames
            else:
                filters.append(f"[{cur_label}][s{i}]concat=n=2:v=1:a=0[{out_label}]")
                cur_frames += frames[i]
            cur_label = out_label
        filters.append(f"[{cur_label}]format=yuv420p[vout]")

        self._run_to(
            result,
            cmd + ["-filter_complex", ";".join(filters), "-map", "[vout]",
                   "-an", *encode_args(quality), str(result)],
            f"Joining {len(clips)} scenes with {len(project.transitions)} transition(s)",
            progress_cb=progress_cb, expected_duration=project.total_duration(),
        )
        return str(result)

    # ---- stage 3: subtitles ---------------------------------------------------
    @staticmethod
    def _srt_timestamp(seconds: float) -> str:
        ms = int(round(seconds * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _srt_from_entries(self, project: Project) -> str:
        blocks = [
            f"{i}\n{self._srt_timestamp(e.start)} --> {self._srt_timestamp(e.end)}\n{e.text}\n"
            for i, e in enumerate(
                sorted(project.subtitle_entries, key=lambda e: e.start), start=1)
        ]
        return "\n".join(blocks)

    def render_subtitles(self, project: Project, video_path: str,
                         quality: str = "final", progress_cb=None) -> str:
        """Burn the project's subtitles (SRT file or manual entries) into the video.

        The SRT is staged into cache_store/ and referenced by a relative
        filename with cwd set there — the subtitles filter's path parser and
        Windows drive-letter colons do not mix.
        """
        srt_local = self._tmp(".srt")
        if project.subtitle_srt_path:
            shutil.copyfile(project.subtitle_srt_path, srt_local)
        else:
            srt_local.write_text(self._srt_from_entries(project), encoding="utf-8")
        out = self._tmp()
        try:
            self._run_to(
                out,
                [self.ffmpeg, "-y", "-i", video_path,
                 "-vf", f"subtitles={srt_local.name}", "-an", *encode_args(quality), str(out)],
                "Burning in subtitles",
                cwd=str(CACHE_DIR),
                progress_cb=progress_cb, expected_duration=project.total_duration(),
            )
        finally:
            srt_local.unlink(missing_ok=True)
        return str(out)

    # ---- stage 4: audio ----------------------------------------------------------
    def mix_audio(self, project: Project, video_path: str) -> str:
        """Mix all audio tracks and attach them to the video (video stream-copied).

        Each track is trimmed so it can't run past the video's end, faded,
        volume-scaled, delayed to its start_time, then amixed together with a
        silent base track that pins the mix to exactly the video duration.
        """
        video_dur = self.probe_media(video_path)["duration"]
        if video_dur is None:
            raise RenderError(f"Could not determine duration of {video_path}.")

        cmd = [self.ffmpeg, "-y", "-i", video_path]
        filters: list[str] = []
        mix_labels: list[str] = []
        for k, track in enumerate(project.audio_tracks):
            asset = project.get_asset(track.asset_id)
            cmd += ["-i", asset.stored_path]
            src_dur = asset.metadata.get("duration") or video_dur
            avail = max(min(src_dur, video_dur - track.start_time), 0.05)
            chain = [
                f"atrim=0:{avail:.3f}",
                "aformat=sample_rates=44100:channel_layouts=stereo",
            ]
            if track.fade_in > 0:
                chain.append(f"afade=t=in:st=0:d={track.fade_in:.3f}")
            if track.fade_out > 0:
                st = max(avail - track.fade_out, 0.0)
                chain.append(f"afade=t=out:st={st:.3f}:d={track.fade_out:.3f}")
            chain.append(f"volume={track.volume:.3f}")
            if track.start_time > 0:
                chain.append(f"adelay={int(round(track.start_time * 1000))}:all=1")
            filters.append(f"[{k + 1}:a]{','.join(chain)}[a{k}]")
            mix_labels.append(f"[a{k}]")

        # Silent stereo base of exactly the video length keeps amix (with
        # duration=longest) pinned to the video duration.
        cmd += ["-f", "lavfi", "-t", f"{video_dur:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        base_idx = len(project.audio_tracks) + 1
        filters.append(
            f"[{base_idx}:a]{''.join(mix_labels)}amix=inputs={len(mix_labels) + 1}:"
            f"duration=longest:normalize=0[aout]"
        )
        out = self._tmp()
        self._run_to(
            out,
            cmd + ["-filter_complex", ";".join(filters),
                   "-map", "0:v", "-map", "[aout]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)],
            "Mixing audio tracks",
        )
        return str(out)

    # ---- orchestration -----------------------------------------------------------
    def render_final(self, project: Project, output_path: str,
                     progress=None, quality: str = "final") -> dict[str, Any]:
        if not project.scenes:
            raise RenderError("Project has no scenes. Use add_scene before rendering.")
        if quality not in QUALITY_PROFILES:
            raise RenderError(f"Unknown quality '{quality}'. Use one of: {', '.join(QUALITY_PROFILES)}.")

        def report(frac: float, msg: str) -> None:
            if progress is not None:
                progress(frac, msg)

        def sub_progress(base: float, span: float):
            """Fine-grained encode progress within a stage: msg=None events
            update the fraction without polluting the stage log."""
            if progress is None:
                return None
            return lambda f: progress(base + span * f, None)

        # Scene rendering dominates render time; weight it as 70% of the bar.
        n = len(project.scenes)
        rendered, cached = [], []
        clips: list[str] = []
        for i, scene in enumerate(project.scenes):
            asset = project.get_asset(scene.asset_id)
            # Check against the *recomputed* key: any edit to the scene, its
            # asset or the project settings must count as a re-render.
            was_cached = has_cached_clip(self._scene_key(project, scene, asset, quality))
            clips.append(self.render_scene(project, scene, asset, quality,
                                           progress_cb=sub_progress(0.7 * i / n, 0.7 / n)))
            (cached if was_cached else rendered).append(i)
            report(0.7 * (i + 1) / n,
                   f"Scene {i + 1}/{n} {'reused from cache' if was_cached else 'rendered'} "
                   f"(effect '{scene.effect}')")

        work = Path(self._join_clips(project, clips, quality,
                                     progress_cb=sub_progress(0.7, 0.1)))
        report(0.8, f"Joined {n} scene(s) with {len(project.transitions)} transition(s)")
        try:
            if project.subtitle_srt_path or project.subtitle_entries:
                subbed = Path(self.render_subtitles(project, str(work), quality,
                                                    progress_cb=sub_progress(0.8, 0.1)))
                work.unlink(missing_ok=True)
                work = subbed
                report(0.9, "Subtitles burned in")
            if project.audio_tracks:
                mixed = Path(self.mix_audio(project, str(work)))
                work.unlink(missing_ok=True)
                work = mixed
                report(0.95, f"Mixed {len(project.audio_tracks)} audio track(s)")

            # Verify the finished video BEFORE moving it: the temp file lives
            # in cache_store/, a location we fully control. Probing at the
            # destination instead used to fail spuriously when the target
            # folder was cloud-synced (OneDrive), antivirus-scanned, or the
            # path resolved oddly ("No such file or directory" right after a
            # successful write).
            meta = self.probe_media(str(work))

            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work), out)
            # Post-move sanity check only (no re-probe): tolerate brief
            # visibility delays from sync clients / AV scanners.
            for _ in range(10):
                if out.is_file() and out.stat().st_size > 0:
                    break
                time.sleep(0.2)
            else:
                raise RenderError(
                    f"Render finished and verified, but the file is not visible at "
                    f"{out} after moving. If that folder is cloud-synced (OneDrive/"
                    f"Dropbox) or aggressively scanned by antivirus, render to a "
                    f"local folder instead (omit output_path to use {out.parent})."
                )
            report(1.0, f"Wrote {out}")
        finally:
            work.unlink(missing_ok=True)

        return {
            "output_path": str(out),
            "quality": quality,
            "scenes_rendered": rendered,
            "scenes_from_cache": cached,
            "duration": meta.get("duration"),
            "resolution": f"{meta.get('width')}x{meta.get('height')}",
        }
