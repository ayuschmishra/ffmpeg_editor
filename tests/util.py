"""Shared test helpers: generate test media with FFmpeg, probe outputs."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent
MEDIA_DIR = TESTS_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(REPO_DIR))  # make core modules importable from tests/


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def make_test_image(name: str, color: str, label: str, size: str = "1600x900") -> Path:
    """Solid-color PNG with a big label, generated via ffmpeg lavfi."""
    out = MEDIA_DIR / f"{name}.png"
    if not out.exists():
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c={color}:s={size}",
             "-vf", f"drawtext=text='{label}':fontsize=120:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
             "-frames:v", "1", str(out)])
    return out


def make_test_video(name: str, seconds: float = 4.0, size: str = "1280x720") -> Path:
    """Short testsrc video clip with audio tone."""
    out = MEDIA_DIR / f"{name}.mp4"
    if not out.exists():
        run(["ffmpeg", "-y",
             "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration={seconds}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)])
    return out


def make_test_audio(name: str, seconds: float = 10.0, freq: int = 220) -> Path:
    out = MEDIA_DIR / f"{name}.mp3"
    if not out.exists():
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"sine=frequency={freq}:duration={seconds}",
             "-c:a", "libmp3lame", str(out)])
    return out


def probe(path: str | Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr[-500:]}")
    return json.loads(proc.stdout)


def video_summary(path: str | Path) -> dict:
    data = probe(path)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    num, den = v["r_frame_rate"].split("/")
    return {
        "width": v["width"], "height": v["height"],
        "fps": round(int(num) / int(den), 2),
        "duration": round(float(data["format"]["duration"]), 2),
        "has_audio": a is not None,
    }


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS: {msg}")
