"""Helpers for reading MediaInfo ``--Output=JSON`` documents.

Shape (abridged)::

    {"creatingLibrary": {"name": "MediaInfoLib", "version": "24.12"},
     "media": {"@ref": "/path/file.mkv",
               "track": [{"@type": "General", "Format": "Matroska", ...},
                         {"@type": "Video", "Format": "HEVC", ...},
                         {"@type": "Audio", ...}, {"@type": "Text", ...}]}}

All values are strings. Non-standard fields live under a nested ``"extra"`` dict.
Multi-valued fields are joined with ``" / "``.
"""

from __future__ import annotations

import re
from typing import Any

Track = dict[str, Any]

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+(?:\.\d+)?")


def tracks(raw: dict[str, Any], kind: str) -> list[Track]:
    media = raw.get("media") or {}
    items = media.get("track") or []
    if isinstance(items, dict):  # a single track is sometimes not wrapped in a list
        items = [items]
    return [t for t in items if isinstance(t, dict) and t.get("@type") == kind]


def first_track(raw: dict[str, Any], kind: str) -> Track | None:
    found = tracks(raw, kind)
    return found[0] if found else None


def get(track: Track | None, *keys: str) -> str | None:
    """First non-empty value among ``keys``, checking the ``extra`` dict too."""
    if not track:
        return None
    raw_extra = track.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    for key in keys:
        for source in (track, extra):
            value = source.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def get_int(track: Track | None, *keys: str) -> int | None:
    value = get(track, *keys)
    if value is None:
        return None
    m = _INT.search(value.replace(" ", ""))
    return int(m.group(0)) if m else None


def get_float(track: Track | None, *keys: str) -> float | None:
    value = get(track, *keys)
    if value is None:
        return None
    m = _FLOAT.search(value)
    return float(m.group(0)) if m else None


def get_bool(track: Track | None, *keys: str) -> bool:
    value = get(track, *keys)
    return value is not None and value.lower() in {"yes", "true", "1"}


def split_multi(value: str | None) -> list[str]:
    """Split a ``" / "`` joined value, keeping positions (empty strings preserved)."""
    if value is None:
        return []
    return [part.strip() for part in value.split("/")]


def library_version(raw: dict[str, Any]) -> str | None:
    lib = raw.get("creatingLibrary") or {}
    version = lib.get("version")
    return str(version) if version else None
