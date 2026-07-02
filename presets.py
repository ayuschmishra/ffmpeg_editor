"""Effect & transition preset definitions.

FFmpeg-specific: this module is consumed ONLY by ffmpeg_renderer.py. Nothing in
server.py or project_state.py may import filter strings from here — they only
see preset *names* via list_effect_presets()/list_transition_presets().

Effect builders return a -vf filter chain that converts one input (image or
video) into a clip at exactly {w}x{h}, {fps} fps, yuv420p.
"""
from __future__ import annotations

from typing import Callable

# Bump to invalidate every cached scene clip after renderer logic changes.
RENDER_VERSION = 2


def _frames(duration: float, fps: int) -> int:
    return max(int(round(duration * fps)), 1)


def _zoompan(w: int, h: int, fps: int, duration: float, z: str, x: str, y: str) -> str:
    """Shared zoompan chain. The input is upscaled 2x first so sub-pixel pans
    don't jitter, then zoompan animates and downsamples to the target size."""
    n = _frames(duration, fps)
    up_w, up_h = w * 2, h * 2
    return (
        f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={up_w}:{up_h},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps},"
        f"format=yuv420p,setsar=1"
    )


# Expression helpers. F = last frame index; zoompan's `on` is the output frame
# counter, so value(on)/F sweeps 0 -> 1 across the clip. No commas allowed in
# expressions (they would split the filter string).
def _f(duration: float, fps: int) -> int:
    return max(_frames(duration, fps) - 1, 1)


X_CENTER = "iw/2-(iw/zoom/2)"
Y_CENTER = "ih/2-(ih/zoom/2)"


def _fx_none(w: int, h: int, fps: int, duration: float, p: dict) -> str:
    # Static: letterbox to fit, no motion.
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1"
    )


def _fx_zoom_in(w: int, h: int, fps: int, duration: float, p: dict) -> str:
    zs = float(p.get("zoom_start", 1.0))
    ze = float(p.get("zoom_end", 1.25))
    z = f"{zs}+({ze}-{zs})*on/{_f(duration, fps)}"
    return _zoompan(w, h, fps, duration, z, X_CENTER, Y_CENTER)


def _fx_zoom_out(w: int, h: int, fps: int, duration: float, p: dict) -> str:
    zs = float(p.get("zoom_start", 1.25))
    ze = float(p.get("zoom_end", 1.0))
    z = f"{zs}+({ze}-{zs})*on/{_f(duration, fps)}"
    return _zoompan(w, h, fps, duration, z, X_CENTER, Y_CENTER)


def _fx_pan_left(w: int, h: int, fps: int, duration: float, p: dict) -> str:
    zoom = float(p.get("zoom", 1.2))
    x = f"(iw-iw/zoom)*(1-on/{_f(duration, fps)})"  # view slides right -> left
    return _zoompan(w, h, fps, duration, str(zoom), x, Y_CENTER)


def _fx_pan_right(w: int, h: int, fps: int, duration: float, p: dict) -> str:
    zoom = float(p.get("zoom", 1.2))
    x = f"(iw-iw/zoom)*(on/{_f(duration, fps)})"    # view slides left -> right
    return _zoompan(w, h, fps, duration, str(zoom), x, Y_CENTER)


def _fx_ken_burns(w: int, h: int, fps: int, duration: float, p: dict) -> str:
    zs = float(p.get("zoom_start", 1.0))
    ze = float(p.get("zoom_end", 1.3))
    F = _f(duration, fps)
    z = f"{zs}+({ze}-{zs})*on/{F}"
    x = f"(iw-iw/zoom)*(on/{F})"                    # diagonal drift while zooming
    y = f"(ih-ih/zoom)*(on/{F})"
    return _zoompan(w, h, fps, duration, z, x, y)


def _fx_custom(w: int, h: int, fps: int, duration: float, p: dict) -> str:
    flt = p.get("filter")
    if not flt or not isinstance(flt, str):
        raise ValueError(
            "Effect 'custom_filter' requires effect_params={'filter': '<ffmpeg -vf chain>'}. "
            f"The chain must end at {w}x{h} yuv420p."
        )
    return flt


EFFECTS: dict[str, dict] = {
    "none": {
        "description": "Static clip, letterboxed to fit the project resolution. Works on images and videos.",
        "params": {},
        "media": ["image", "video"],
        "builder": _fx_none,
    },
    "zoom_in": {
        "description": "Slow zoom in on the image (Ken Burns style, centered).",
        "params": {"zoom_start": 1.0, "zoom_end": 1.25},
        "media": ["image"],
        "builder": _fx_zoom_in,
    },
    "zoom_out": {
        "description": "Slow zoom out from the image (centered).",
        "params": {"zoom_start": 1.25, "zoom_end": 1.0},
        "media": ["image"],
        "builder": _fx_zoom_out,
    },
    "pan_left": {
        "description": "Pan across the image from right to left at a fixed zoom.",
        "params": {"zoom": 1.2},
        "media": ["image"],
        "builder": _fx_pan_left,
    },
    "pan_right": {
        "description": "Pan across the image from left to right at a fixed zoom.",
        "params": {"zoom": 1.2},
        "media": ["image"],
        "builder": _fx_pan_right,
    },
    "ken_burns": {
        "description": "Classic Ken Burns: zoom in with a diagonal drift.",
        "params": {"zoom_start": 1.0, "zoom_end": 1.3},
        "media": ["image"],
        "builder": _fx_ken_burns,
    },
    "custom_filter": {
        "description": (
            "Escape hatch: raw FFmpeg -vf chain via effect_params={'filter': '...'}. "
            "The chain must produce frames at the project resolution, yuv420p."
        ),
        "params": {"filter": "<required>"},
        "media": ["image", "video"],
        "builder": _fx_custom,
    },
}

# Friendly transition name -> xfade transition= value.
TRANSITIONS: dict[str, dict] = {
    "crossfade":   {"xfade": "fade",       "description": "Classic crossfade/dissolve."},
    "fade_black":  {"xfade": "fadeblack",  "description": "Fade to black, then into the next scene."},
    "fade_white":  {"xfade": "fadewhite",  "description": "Fade to white, then into the next scene."},
    "slide_left":  {"xfade": "slideleft",  "description": "Next scene slides in from the right."},
    "slide_right": {"xfade": "slideright", "description": "Next scene slides in from the left."},
    "slide_up":    {"xfade": "slideup",    "description": "Next scene slides in from the bottom."},
    "slide_down":  {"xfade": "slidedown",  "description": "Next scene slides in from the top."},
    "wipe_left":   {"xfade": "wipeleft",   "description": "Wipe reveal, right to left."},
    "wipe_right":  {"xfade": "wiperight",  "description": "Wipe reveal, left to right."},
    "wipe_up":     {"xfade": "wipeup",     "description": "Wipe reveal, bottom to top."},
    "wipe_down":   {"xfade": "wipedown",   "description": "Wipe reveal, top to bottom."},
    "pixelize":    {"xfade": "pixelize",   "description": "Pixelated dissolve."},
    "circle_open": {"xfade": "circleopen", "description": "Circular iris opening onto the next scene."},
    "dissolve":    {"xfade": "dissolve",   "description": "Noise dissolve."},
}


def list_effect_presets() -> list[dict]:
    return [
        {"name": name, "description": e["description"], "default_params": e["params"], "media_types": e["media"]}
        for name, e in EFFECTS.items()
    ]


def list_transition_presets() -> list[dict]:
    return [{"name": name, "description": t["description"]} for name, t in TRANSITIONS.items()]


def build_effect_filter(effect: str, media_type: str, w: int, h: int, fps: int,
                        duration: float, params: dict) -> str:
    preset = EFFECTS.get(effect)
    if preset is None:
        raise ValueError(f"Unknown effect '{effect}'. Available: {', '.join(EFFECTS)}.")
    if media_type not in preset["media"]:
        raise ValueError(
            f"Effect '{effect}' supports {preset['media']} assets, not '{media_type}'. "
            f"For video assets use effect 'none' (or 'custom_filter')."
        )
    builder: Callable = preset["builder"]
    return builder(w, h, fps, duration, params or {})
