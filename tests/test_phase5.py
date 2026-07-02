"""Phase 5 test: subtitles (SRT + manual entries) and audio mixing with fades.

Verifies with ffprobe/ffmpeg signal analysis, not just "file exists":
  - subtitle burn-in actually changes pixels in the subtitle region
  - the mixed audio stream exists, spans the video, and fades change loudness
"""
import re
import subprocess
from pathlib import Path

from util import check, make_test_image, make_test_audio, make_test_video, video_summary, MEDIA_DIR

from ffmpeg_renderer import FFmpegRenderer
from project_state import (AudioTrack, OUTPUT_DIR, ProjectStore, Scene,
                           SubtitleEntry, Transition, _new_id)


def mean_volume(path: Path, start: float, dur: float) -> float:
    """Measure mean loudness (dB) of a time slice via the volumedetect filter."""
    proc = subprocess.run(
        ["ffmpeg", "-ss", str(start), "-t", str(dur), "-i", str(path),
         "-map", "0:a", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    if not m:
        raise RuntimeError(f"volumedetect gave no reading:\n{proc.stderr[-800:]}")
    return float(m.group(1))


def frame_signature(path: Path, t: float, crop: str) -> bytes:
    """Raw bytes of a cropped region of the frame at time t."""
    proc = subprocess.run(
        ["ffmpeg", "-ss", str(t), "-i", str(path), "-frames:v", "1",
         "-vf", f"crop={crop}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True)
    return proc.stdout


def main() -> None:
    print("Phase 5 test: subtitles + audio")
    img_r = make_test_image("red", "red", "RED")
    img_b = make_test_image("blue", "blue", "BLUE")
    music = make_test_audio("tone", seconds=10.0, freq=220)
    voice = make_test_audio("voice", seconds=2.0, freq=880)

    store = ProjectStore()
    renderer = FFmpegRenderer()
    project = store.create("phase5_test", 960, 540, 30)
    pid = project.project_id

    a_r = store.import_asset(pid, str(img_r), renderer.probe_media(str(img_r)))
    a_b = store.import_asset(pid, str(img_b), renderer.probe_media(str(img_b)))
    a_music = store.import_asset(pid, str(music), renderer.probe_media(str(music)))
    a_voice = store.import_asset(pid, str(voice), renderer.probe_media(str(voice)))
    check(a_music.media_type == "audio" and a_music.metadata["has_audio"], "audio asset probed")

    project.scenes = [
        Scene(scene_id=_new_id("scene"), asset_id=a_r.asset_id, duration=3.0, effect="zoom_in"),
        Scene(scene_id=_new_id("scene"), asset_id=a_b.asset_id, duration=3.0, effect="zoom_out"),
    ]
    project.transitions = [Transition(0, 1, "crossfade", 1.0)]  # total: 5s

    # ---- manual subtitle entries -------------------------------------------
    project.subtitle_entries = [
        SubtitleEntry("Hello from scene one", 0.5, 2.0),
        SubtitleEntry("And now scene two", 3.0, 4.5),
    ]
    out = OUTPUT_DIR / "phase5_test.mp4"
    report = renderer.render_final(project, str(out))
    check(Path(report["output_path"]).is_file(), "render with subtitles + transition succeeds")
    check(abs(video_summary(out)["duration"] - 5.0) < 0.3, "duration correct (5s)")

    # Subtitle burn-in check: bottom strip at t=1.0 (subtitle showing) must
    # differ from t=2.5 (no subtitle). Same scene region otherwise.
    bottom = "960:100:0:440"
    check(frame_signature(out, 1.0, bottom) != frame_signature(out, 2.4, bottom),
          "subtitle text visibly burned into bottom of frame")

    # ---- SRT file path -----------------------------------------------------------
    srt = MEDIA_DIR / "test.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nSRT subtitle line\n", encoding="utf-8")
    project.subtitle_entries = []
    project.subtitle_srt_path = str(srt)
    renderer.render_final(project, str(out))
    check(frame_signature(out, 1.0, bottom) != frame_signature(out, 2.4, bottom),
          "SRT file subtitles burned in")
    project.subtitle_srt_path = None

    # ---- audio mixing -------------------------------------------------------------
    project.audio_tracks = [
        AudioTrack(asset_id=a_music.asset_id, track_type="music", start_time=0.0,
                   volume=1.0, fade_in=1.5, fade_out=1.5),
        AudioTrack(asset_id=a_voice.asset_id, track_type="narration", start_time=2.5,
                   volume=1.0),
    ]
    report = renderer.render_final(project, str(out))
    s = video_summary(out)
    check(s["has_audio"], "output has an audio stream")
    check(abs(s["duration"] - 5.0) < 0.3, f"audio does not extend video duration (got {s['duration']})")

    quiet_start = mean_volume(out, 0.0, 0.4)     # inside 1.5s fade-in
    loud_middle = mean_volume(out, 1.8, 0.6)     # music at full volume
    check(loud_middle - quiet_start > 6,
          f"fade-in audible: start {quiet_start} dB vs middle {loud_middle} dB")
    narration_zone = mean_volume(out, 2.6, 1.5)  # music + narration overlap
    check(narration_zone >= loud_middle - 1,
          f"narration mixes on top of music ({narration_zone} dB vs {loud_middle} dB)")

    # Scene clips stayed cached through subtitle/audio changes.
    check(report["scenes_from_cache"] == [0, 1],
          "subtitle/audio changes never invalidate scene caches")

    print("Phase 5 test: ALL PASSED")


if __name__ == "__main__":
    main()
