"""The Issues checks: what each one means, and the pure logic behind the trickier ones.

Database access lives in :mod:`media_audit.flags`; this module has no I/O so the rules
can be tested table-style.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning", "info"]
SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}

# Bump when checks change so a running container recomputes issues on start.
FLAGS_VERSION = 5


@dataclass(frozen=True)
class Check:
    code: str
    category: str
    severity: Severity
    heading: str  # group heading on the Issues page
    explain: str  # one or two sentences: why it matters / how it's decided


CATEGORIES: dict[str, str] = {
    "problems": "Problems",
    "duplicates": "Duplicates",
    "suspicious": "Suspicious",
    "naming": "Naming",
    "notes": "Notes",
}

_CHECKS = [
    # ---- problems
    Check("unreadable", "problems", "error", "Unreadable or not a video",
          "MediaInfo couldn't read a video stream from these files. They may be damaged, "
          "incomplete, or not really video files."),
    Check("disc_image", "problems", "error", "Disc images instead of video files",
          "Disc images (.iso/.img) usually aren't playable by media servers and can't be "
          "analyzed. Remux them to MKV."),
    Check("no_audio", "problems", "warning", "Videos without an audio track",
          "MediaInfo found video but no audio. Usually a bad remux or an incomplete file."),
    Check("stray_file", "problems", "warning", "Stray files in a library",
          "Files that aren't video, subtitles, artwork or metadata: partial downloads, "
          "leftover archives, text files. Expected types are set in "
          "flags.allowed_extensions; anything inside an extras folder (Extras, "
          "Featurettes, Commentaries…) counts as bonus material. An .nfo file counts "
          "only if media servers read it: tvshow.nfo, season.nfo, movie.nfo, or named "
          "after a video next to it; others are release info or leftovers."),
    Check("no_video_folder", "problems", "warning", "Folders with no videos left",
          "A movie, show or season folder that only holds artwork, metadata, subtitles or "
          "other leftovers. Usually what's left after the video was deleted, moved or "
          "renamed into another folder."),
    Check("empty_folder", "problems", "warning", "Empty folders",
          "Folders with nothing in them (files matched by scan.ignore don't count). Often "
          "left behind by a rename. Only the outermost empty folder is listed."),
    # ---- duplicates
    Check("split_season", "duplicates", "warning", "Seasons split across folders",
          "Episodes of one season in two or more folders of a show, e.g. Season 1 and "
          "Season 01, or Specials and Season 00: usually a rename that only moved some "
          "episodes. A leftover folder with no videos is listed under Empty folders or "
          "Folders with no videos left instead. Duplicate episodes between the folders "
          "are listed under Same episode stored more than once."),
    Check("duplicate_episode", "duplicates", "warning", "Same episode stored more than once",
          "An episode with several files (two qualities, or a leftover copy). Recoverable "
          "space assumes you keep only the largest file."),
    Check("multi_version_movie", "duplicates", "info", "Movies with more than one version",
          "Several main video files in one movie folder: different qualities or editions. "
          "Often deliberate; ignore the ones you mean to keep."),
    Check("cross_library", "duplicates", "info", "Same title in more than one library",
          "Linked by ID or by name and year. Expected for e.g. a separate 4K library."),
    Check("orphan_subtitle", "duplicates", "warning", "Subtitle files with no matching video",
          "Usually left behind when the video was renamed or replaced, so the player no "
          "longer picks them up."),
    # ---- suspicious
    Check("short_runtime", "suspicious", "warning",
          "Episodes much shorter than the rest of their season",
          "Shorter than flags.short_runtime of the season's median running time. Often a "
          "truncated download or the wrong file. Specials aren't compared."),
    Check("resolution_mismatch", "suspicious", "warning",
          "Filename claims a different resolution",
          "The resolution in the name (e.g. 1080p) doesn't match the video itself: "
          "usually an upscale or a mislabelled release."),
    Check("bitrate_outlier", "suspicious", "info", "Bitrate far from its peers",
          "GB per hour well below or above the rest of the season (episodes) or other "
          "movies at the same resolution. Low can mean a poor encode; high is often just a "
          "better source."),
    # ---- naming
    Check("missing_episodes", "naming", "info", "Gaps in episode numbering",
          "Episode numbers missing between the first and last episode present. Counted "
          "from E01, or from the first episode when a season starts past E03 (absolute "
          "anime numbering). Episodes after the last one present can't be detected."),
    Check("unnumbered_episode", "naming", "warning", "Episodes without an episode number",
          "The filename has no SxxEyy (or absolute) episode number, so the episode can't "
          "be placed or counted."),
    Check("episode_outside_season", "naming", "info", "Episodes outside their season folder",
          "An episode directly in the show folder while the show's other episodes are in "
          "season folders: usually a botched import or rename. Shows without season "
          "folders aren't flagged. Sonarr's rename (Organize) moves it back."),
    Check("no_ids", "naming", "info", "Titles without a TMDB/TVDB/IMDb ID",
          "No {tmdb-…}, {tvdb-…} or {imdb-…} in the folder or file names. Linking across "
          "libraries falls back to name and year."),
    # ---- notes
    Check("mixed_resolution_season", "notes", "info", "Seasons with mixed resolutions",
          "Episodes in one season at different resolutions, e.g. some 1080p and some 720p."),
    Check("legacy_codec", "notes", "info", "Older video codecs (upgrade candidates)",
          "MPEG-4 Part 2 (Xvid/DivX), MPEG-2, MPEG-1 or VC-1: large for their quality and "
          "less widely supported."),
    Check("no_preferred_audio", "notes", "info", "No audio in your preferred language",
          "None of the audio tracks is in languages.preferred_audio. Tracks with an "
          "unknown language count as a match."),
]  # fmt: skip
CHECKS: dict[str, Check] = {c.code: c for c in _CHECKS}

LEGACY_CODECS = {"MPEG-4 Visual": "MPEG-4 Part 2 (Xvid/DivX)", "MPEG-2": "MPEG-2",
                 "MPEG-1": "MPEG-1", "VC-1": "VC-1"}  # fmt: skip
DISC_IMAGE_EXTS = {"iso", "img"}
PARTIAL_EXTS = {"part", "parts", "partial", "crdownload", "!qb", "tmp"}

# Resolution named in a filename -> the class the video should have.
_TAG_CLASS = {
    "4320p": "4320p", "2160p": "2160p", "4k": "2160p", "uhd": "2160p",
    "1080p": "1080p", "1080i": "1080p", "720p": "720p",
    "576p": "SD", "576i": "SD", "480p": "SD", "480i": "SD", "360p": "SD", "sd": "SD",
}  # fmt: skip
_CLASS_RANK = {"SD": 0, "720p": 1, "1080p": 2, "2160p": 3, "4320p": 4}


def resolution_mismatch(tag: str | None, actual: str | None) -> str | None:
    """'upscale'/'downscale' when a filename's resolution disagrees with the video."""
    if not tag or not actual:
        return None
    expected = _TAG_CLASS.get(tag.lower())
    if expected is None or expected == actual or actual not in _CLASS_RANK:
        return None
    return "upscale" if _CLASS_RANK[expected] > _CLASS_RANK[actual] else "downscale"


def missing_episodes(spans: Iterable[tuple[int, int]]) -> list[int]:
    """Episode numbers missing from a season, given (first, last) of each file."""
    present: set[int] = set()
    for first, last in spans:
        present.update(range(first, max(first, last) + 1))
    if not present:
        return []
    lo, hi = min(present), max(present)
    start = 1 if lo <= 3 else lo  # starting past E03: absolute numbering, not a gap
    return [n for n in range(start, hi + 1) if n not in present]


def median(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def ranges(numbers: Sequence[int], limit: int = 12) -> str:
    """[1, 2, 3, 7, 9, 10] -> 'E01-E03, E07, E09-E10 (with en dashes)'."""
    out: list[str] = []
    nums = sorted(numbers)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"E{nums[i]:02d}" if i == j else f"E{nums[i]:02d}–E{nums[j]:02d}")
        i = j + 1
    if len(out) > limit:
        return ", ".join(out[:limit]) + f" and {len(out) - limit} more"
    return ", ".join(out)
