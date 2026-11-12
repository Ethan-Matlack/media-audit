"""Classify files by role from their path alone (no file reads)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

VIDEO_EXTENSIONS = frozenset(
    {
        "mkv", "mp4", "m4v", "avi", "wmv", "mov", "ts", "m2ts", "mts", "mpg", "mpeg",
        "vob", "flv", "webm", "divx", "ogm", "ogv", "asf", "rm", "rmvb", "3gp", "iso",
    }
)  # fmt: skip
# Where cleanup scripts move files (inside each library folder). Never scanned.
QUARANTINE_DIR = ".media-audit-quarantine"
SUBTITLE_EXTENSIONS = frozenset({"srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "smi"})

# Folder names Plex/Jellyfin/Emby treat as local extras (compared case-insensitively).
EXTRAS_FOLDERS = frozenset(
    {
        "extras", "extra", "featurettes", "featurette", "behind the scenes", "deleted scenes",
        "interviews", "scenes", "shorts", "trailers", "trailer", "other", "others",
        "bonus", "bonus features", "special features", "clips", "backdrops", "theme-music",
        "samples", "sample",
    }
)  # fmt: skip
SAMPLE_FOLDERS = frozenset({"sample", "samples"})

# Plex extras suffix: "Movie (2001)-trailer.mkv", also "-behindthescenes" etc.
_EXTRA_SUFFIX = re.compile(
    r"[-_. ](trailer|featurette|behindthescenes|deleted|interview|scene|short|other)$", re.I
)
_SAMPLE_NAME = re.compile(r"(?:^|[-_. \[(])sample(?:$|[-_. \])])", re.I)

# "Season 01", "Season 1", "S01", "Series 2", and the specials folder.
_SEASON_FOLDER = re.compile(
    r"^(?:season|series|staffel|saison|temporada)[ ._-]*(\d{1,4})$|^s(\d{1,4})$", re.I
)
SPECIALS_FOLDERS = frozenset({"specials", "special", "season 0", "season 00"})


def extension(filename: str) -> str:
    dot = filename.rfind(".")
    if dot <= 0:
        return ""
    return filename[dot + 1 :].lower()


def is_video(ext: str) -> bool:
    return ext in VIDEO_EXTENSIONS


def season_folder_number(name: str) -> int | None:
    """Season number for a season folder name, or ``None`` if it isn't one."""
    low = name.strip().lower()
    if low in SPECIALS_FOLDERS:
        return 0
    m = _SEASON_FOLDER.match(low)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def classify_role(rel_path: str) -> str:
    """Role for a file given its path relative to the library root.

    ``main`` (feature/episode video), ``extra``, ``sample``, ``subtitle`` or ``other``.
    """
    p = PurePosixPath(rel_path)
    ext = extension(p.name)
    stem = p.name[: -(len(ext) + 1)] if ext else p.name
    dirs = [d.lower() for d in p.parts[:-1]]

    if ext in SUBTITLE_EXTENSIONS:
        return "subtitle"
    if not is_video(ext):
        return "other"
    if any(d in SAMPLE_FOLDERS for d in dirs) or _SAMPLE_NAME.search(stem):
        return "sample"
    # Only folders *below* the title folder count as extras folders; a library
    # literally named "Other" shouldn't turn everything into extras.
    if any(d in EXTRAS_FOLDERS for d in dirs[1:]):
        return "extra"
    if _EXTRA_SUFFIX.search(stem):
        return "extra"
    return "main"


JUNK_EXTENSIONS = frozenset({"txt", "exe", "url", "lnk", "bat", "cmd", "scr", "htm", "html",
                             "nzb", "torrent", "rar", "zip", "7z", "par2", "sfv", "md5", "!qb",
                             "part", "tmp"})  # fmt: skip
