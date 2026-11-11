"""File analyzers. Each backend returns a MediaInfo-JSON-shaped document.

The rules engine only understands MediaInfo's JSON shape. A future ffprobe backend
should translate its output into that shape (see ``rules/mi.py``) rather than
teaching the rules a second format.
"""

from __future__ import annotations

from typing import Any, Protocol


class AnalyzerError(Exception):
    """The file couldn't be analyzed (unreadable, truncated, not media...)."""


class AnalyzerUnavailable(Exception):
    """The backend's native dependency isn't installed."""


class Analyzer(Protocol):
    name: str

    def version(self) -> str:
        """Backend name and version, stored per file (e.g. ``mediainfo 24.12``)."""
        ...

    def analyze(self, path: str) -> dict[str, Any]:
        """Return a MediaInfo ``--Output=JSON`` shaped document for ``path``."""
        ...


def get_analyzer(name: str = "mediainfo", **options: Any) -> Analyzer:
    if name == "mediainfo":
        from media_audit.analyzer.mediainfo import MediaInfoAnalyzer

        return MediaInfoAnalyzer(**options)
    raise ValueError(f"unknown analyzer backend: {name}")
