"""Renderer abstract interface — the backend-agnostic seam.

server.py and project_state.py only ever talk to this contract. FFmpegRenderer
(ffmpeg_renderer.py) is v1's only implementation; a future backend would
subclass Renderer without touching the MCP tool layer or project state.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from project_state import Asset, Project, Scene, Transition


class RenderError(Exception):
    """Raised when a rendering step fails; message should be actionable."""


class Renderer(ABC):
    @abstractmethod
    def probe_media(self, path: str) -> dict[str, Any]:
        """Return metadata for a media file: media_type, width, height,
        duration (seconds, None for still images), has_audio."""

    @abstractmethod
    def render_scene(self, project: Project, scene: Scene, asset: Asset,
                     quality: str = "final") -> str:
        """Render one scene to a standalone clip (using the cache when the
        scene's cache key already has a rendered clip). Returns the clip path
        and updates scene.cache_key / scene.cached_clip_path."""

    @abstractmethod
    def render_transition(self, project: Project, clip_a: str, clip_b: str,
                          transition: Transition, a_duration: float,
                          quality: str = "final") -> str:
        """Join two clips with a transition applied across their boundary.
        Returns the combined clip path."""

    @abstractmethod
    def render_subtitles(self, project: Project, video_path: str,
                         quality: str = "final") -> str:
        """Burn/attach the project's subtitles onto a video. Returns new path."""

    @abstractmethod
    def mix_audio(self, project: Project, video_path: str) -> str:
        """Mix the project's audio tracks and attach them to the video.
        Returns the final path."""

    @abstractmethod
    def render_final(self, project: Project, output_path: str,
                     progress: "ProgressCallback | None" = None,
                     quality: str = "final") -> dict[str, Any]:
        """Orchestrate the full staged pipeline: scenes (cached) -> transitions
        -> subtitles -> audio -> output file. Returns a report dict with at
        least: output_path, scenes_rendered, scenes_from_cache, duration.

        `progress`, when given, is called as progress(fraction, message) after
        each pipeline stage (fraction in [0, 1]); it may raise to abort the
        render between stages (cooperative cancellation).
        `quality` is "final" or "draft" (fast half-resolution preview)."""


# progress(fraction_complete, human_readable_message)
ProgressCallback = "Callable[[float, str], None]"
