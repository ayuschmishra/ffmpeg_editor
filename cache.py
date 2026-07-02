"""Scene render cache: key hashing, lookup, invalidation.

A scene's cache key is a hash of everything that affects its rendered pixels:
the asset's content checksum, the effect name + params, the duration, the
project resolution/fps, and a renderer version number. If any of those change,
the key changes and the old cached clip is simply never looked up again.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from presets import RENDER_VERSION
from project_state import CACHE_DIR


def scene_cache_key(asset_checksum: str, effect: str, effect_params: dict,
                    duration: float, width: int, height: int, fps: int,
                    quality: str = "final") -> str:
    payload = json.dumps(
        {
            "v": RENDER_VERSION,
            "asset": asset_checksum,
            "effect": effect,
            "params": effect_params or {},
            "duration": round(float(duration), 4),
            "w": width,
            "h": height,
            "fps": fps,
            "quality": quality,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def cached_clip_path(cache_key: str) -> Path:
    return CACHE_DIR / f"scene_{cache_key}.mp4"


def has_cached_clip(cache_key: str) -> bool:
    p = cached_clip_path(cache_key)
    return p.is_file() and p.stat().st_size > 0


def invalidate(cache_key: str) -> bool:
    """Delete a cached clip. Returns True if a file was removed."""
    p = cached_clip_path(cache_key)
    if p.exists():
        p.unlink()
        return True
    return False


def cache_stats() -> dict:
    clips = list(CACHE_DIR.glob("scene_*.mp4"))
    return {"clips": len(clips), "bytes": sum(c.stat().st_size for c in clips)}
