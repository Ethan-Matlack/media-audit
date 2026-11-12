"""Compute the Issues list from what's already in the database (no media files are read).

Runs at the end of every scan and ``rederive``, and when a container starts with new
check logic or changed ``flags:`` settings. The ``flags`` table is rebuilt from scratch
each time; ``flag_ignores`` (the user's choices) is never touched here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from media_audit.classify import EXTRAS_FOLDERS, season_folder_number
from media_audit.config import Config
from media_audit.db import get_meta, set_meta, transaction, utcnow
from media_audit.rules.flags import (
    CHECKS,
    DISC_IMAGE_EXTS,
    FLAGS_VERSION,
    LEGACY_CODECS,
    PARTIAL_EXTS,
    median,
    missing_episodes,
    ranges,
    resolution_mismatch,
)

log = logging.getLogger(__name__)


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in {"B", "KB", "MB"} else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def _minutes(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60:02d} min"


@dataclass
class Flag:
    code: str
    key: str
    fingerprint: str
    library_id: int
    message: str
    title_id: int | None = None
    file_id: int | None = None
    season_number: int | None = None
    data: dict[str, Any] | None = None
    bytes_recoverable: int | None = None


@dataclass
class _Collector:
    config: Config
    flags: list[Flag] = field(default_factory=list)

    def add(self, library: str, flag: Flag) -> None:
        if flag.code in CHECKS and self.config.flags.enabled(flag.code, library):
            self.flags.append(flag)


def _fp(f: sqlite3.Row | dict[str, Any]) -> str:
    return f"{f['id']}:{f['size_bytes']}:{f['mtime_ns']}"


def _file_summary(f: sqlite3.Row) -> dict[str, Any]:
    return {"id": f["id"], "path": f["rel_path"], "size": f["size_bytes"],
            "resolution": f["resolution_class"], "codec": f["codec"],
            "gbh": f["gb_per_hour"]}  # fmt: skip


def _version_list(files: Iterable[sqlite3.Row]) -> str:
    parts = [f"{f['resolution_class'] or '?'} {_size(f['size_bytes'])}" for f in files]
    return ", ".join(parts)


_FILES_SQL = """
    SELECT f.id, f.library_id, l.name AS library, l.type AS library_type, f.title_id,
           t.kind, t.folder_path, t.name AS title_name, f.season_number, f.episode_number,
           f.episode_number_end, f.rel_path, f.filename, f.ext, f.role, f.is_video,
           f.size_bytes, f.mtime_ns, f.analyzed_at, f.analyze_error, f.parent_file_id,
           v.duration_s, v.gb_per_hour, v.resolution_class, v.width, v.height, v.codec,
           v.container, r.resolution_tag,
           (SELECT count(*) FROM audio_tracks a WHERE a.file_id = f.id) AS audio_count,
           (SELECT group_concat(DISTINCT a.language) FROM audio_tracks a
             WHERE a.file_id = f.id) AS audio_languages
    FROM files f
    JOIN libraries l ON l.id = f.library_id AND l.enabled = 1
    LEFT JOIN titles t ON t.id = f.title_id
    LEFT JOIN video_info v ON v.file_id = f.id
    LEFT JOIN release_info r ON r.file_id = f.id
    WHERE f.deleted_at IS NULL
"""


def collect(conn: sqlite3.Connection, config: Config) -> list[Flag]:
    """Run every enabled check. Returns the flags without writing anything."""
    fc = config.flags
    out = _Collector(config)
    files = conn.execute(_FILES_SQL).fetchall()
    mains = [f for f in files if f["is_video"] and f["role"] == "main"]
    analyzed = [f for f in mains if f["analyzed_at"] and not f["analyze_error"]]

    # ---------------------------------------------------------------- problems
    for f in files:
        lib = f["library"]
        ext = (f["ext"] or "").lower()
        if ext in DISC_IMAGE_EXTS:
            out.add(lib, Flag("disc_image", f"disc_image:{f['id']}", _fp(f), f["library_id"],
                              f"Disc image (.{ext}), {_size(f['size_bytes'])}",
                              f["title_id"], f["id"], f["season_number"]))  # fmt: skip
        elif f["is_video"] and f["analyze_error"]:
            out.add(lib, Flag("unreadable", f"unreadable:{f['id']}", _fp(f), f["library_id"],
                              f"{f['analyze_error'][:1].upper()}{f['analyze_error'][1:]} "
                              f"· {_size(f['size_bytes'])}",
                              f["title_id"], f["id"], f["season_number"]))  # fmt: skip
        if (f["role"] == "other" and ext not in fc.allowed_extensions
                and not _in_extras_folder(f["rel_path"])):  # fmt: skip
            if ext in PARTIAL_EXTS or ext.endswith("part"):
                what = f"Partial or unfinished download (.{ext})"
            elif not ext:
                what = "File without an extension"
            else:
                what = f"Not a media file (.{ext})"
            out.add(lib, Flag("stray_file", f"stray_file:{f['id']}", _fp(f), f["library_id"],
                              f"{what}, {_size(f['size_bytes'])}", f["title_id"], f["id"],
                              f["season_number"]))  # fmt: skip
        if f["role"] == "subtitle" and f["parent_file_id"] is None:
            out.add(lib, Flag("orphan_subtitle", f"orphan_subtitle:{f['id']}", _fp(f),
                              f["library_id"], "No video with a matching name in this folder",
                              f["title_id"], f["id"], f["season_number"]))  # fmt: skip
    for f in analyzed:
        if f["audio_count"] == 0:
            out.add(f["library"], Flag("no_audio", f"no_audio:{f['id']}", _fp(f),
                                       f["library_id"], "Video with no audio track",
                                       f["title_id"], f["id"], f["season_number"]))  # fmt: skip

    reported = _folder_checks(conn, files, out)
    if fc.strict_nfo and "nfo" in fc.allowed_extensions:
        _stray_nfo(files, reported, out)

    # ---------------------------------------------------------------- duplicates
    episodes: dict[tuple[int, int, int], list[sqlite3.Row]] = defaultdict(list)
    movie_versions: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for f in mains:
        if f["title_id"] is None:
            continue
        if f["kind"] == "show" and f["episode_number"] is not None:
            episodes[(f["title_id"], f["season_number"] or 0, f["episode_number"])].append(f)
        elif f["kind"] == "movie":
            movie_versions[f["title_id"]].append(f)
    for (_, season, ep), group in episodes.items():
        if len(group) < 2:
            continue
        first = group[0]
        group.sort(key=lambda g: g["size_bytes"], reverse=True)
        sizes = [g["size_bytes"] for g in group]
        out.add(first["library"], Flag(
            "duplicate_episode",
            f"duplicate_episode:{first['library']}:{first['folder_path']}:S{season}E{ep}",
            ",".join(sorted(_fp(g) for g in group)), first["library_id"],
            f"S{season:02d}E{ep:02d}: {len(group)} files ({_version_list(group)})",
            first["title_id"], None, season, {"files": [_file_summary(g) for g in group]},
            sum(sizes) - max(sizes)))  # fmt: skip
    for group in movie_versions.values():
        if len(group) < 2:
            continue
        first = group[0]
        group.sort(key=lambda g: g["size_bytes"], reverse=True)
        sizes = [g["size_bytes"] for g in group]
        out.add(first["library"], Flag(
            "multi_version_movie", f"multi_version_movie:{first['library']}:{first['folder_path']}",
            ",".join(sorted(_fp(g) for g in group)), first["library_id"],
            f"{len(group)} versions ({_version_list(group)})", first["title_id"], None, None,
            {"files": [_file_summary(g) for g in group]}, sum(sizes) - max(sizes)))  # fmt: skip

    titles = conn.execute(
        "SELECT t.id, t.library_id, l.name AS library, t.kind, t.name, t.year, t.folder_path, "
        "t.group_key, t.tmdb_id, t.imdb_id, t.tvdb_id FROM titles t "
        "JOIN libraries l ON l.id = t.library_id AND l.enabled = 1"
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for t in titles:
        if t["group_key"]:
            groups[t["group_key"]].append(t)
    for members in groups.values():
        if len({m["library_id"] for m in members}) < 2:
            continue
        for t in members:
            others = [m for m in members if m["library_id"] != t["library_id"]]
            if not others:
                continue
            where = ", ".join(sorted({f"{o['library']}" for o in others}))
            out.add(t["library"], Flag(
                "cross_library", f"cross_library:{t['library']}:{t['folder_path']}",
                ",".join(sorted(f"{o['library']}:{o['folder_path']}" for o in others)),
                t["library_id"], f"Also in {where}", t["id"],
                data={"titles": [{"id": o["id"], "library": o["library"], "name": o["name"],
                                  "year": o["year"]} for o in others]}))  # fmt: skip

    # ---------------------------------------------------------------- suspicious
    seasons: dict[tuple[int, int | None], list[sqlite3.Row]] = defaultdict(list)
    for f in analyzed:
        if f["kind"] == "show" and f["season_number"] != 0 and f["title_id"] is not None:
            seasons[(f["title_id"], f["season_number"])].append(f)
    for eps in seasons.values():
        timed = [e for e in eps if (e["duration_s"] or 0) > 0]
        if len(timed) < fc.min_peers:
            continue
        med_len = median([e["duration_s"] for e in timed])
        rates = [e for e in timed if (e["gb_per_hour"] or 0) > 0]
        med_rate = median([e["gb_per_hour"] for e in rates]) if len(rates) >= fc.min_peers else 0
        for e in timed:
            label = _episode_label(e)
            if e["duration_s"] < fc.short_runtime * med_len:
                out.add(e["library"], Flag(
                    "short_runtime", f"short_runtime:{e['id']}", _fp(e), e["library_id"],
                    f"{label}{_minutes(e['duration_s'])} long; the season's episodes run "
                    f"{_minutes(med_len)} (median)", e["title_id"], e["id"],
                    e["season_number"]))  # fmt: skip
            rate = e["gb_per_hour"] or 0
            if med_rate and rate and (rate < fc.bitrate_low * med_rate
                                      or rate > fc.bitrate_high * med_rate):  # fmt: skip
                word = "low" if rate < med_rate else "high"
                out.add(e["library"], Flag(
                    "bitrate_outlier", f"bitrate_outlier:{e['id']}", _fp(e), e["library_id"],
                    f"{label}{rate:.2f} GB/h ({word}); the rest of the season is around "
                    f"{med_rate:.2f} GB/h", e["title_id"], e["id"], e["season_number"],
                    {"direction": word}))  # fmt: skip

    by_res: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for f in analyzed:
        if f["kind"] == "movie" and (f["gb_per_hour"] or 0) > 0 and f["resolution_class"]:
            by_res[f["resolution_class"]].append(f)
    for res, movies in by_res.items():
        if len(movies) < fc.min_peers:
            continue
        med_rate = median([m["gb_per_hour"] for m in movies])
        for m in movies:
            if m["gb_per_hour"] < fc.movie_bitrate_low * med_rate:
                out.add(m["library"], Flag(
                    "bitrate_outlier", f"bitrate_outlier:{m['id']}", _fp(m), m["library_id"],
                    f"{m['gb_per_hour']:.2f} GB/h (low); {res} movies are around "
                    f"{med_rate:.2f} GB/h", m["title_id"], m["id"], None,
                    {"direction": "low"}))  # fmt: skip

    for f in analyzed:
        kind = resolution_mismatch(f["resolution_tag"], f["resolution_class"])
        if kind is None:
            continue
        what = "an upscale or mislabelled" if kind == "upscale" else "better than labelled"
        out.add(f["library"], Flag(
            "resolution_mismatch", f"resolution_mismatch:{f['id']}", _fp(f), f["library_id"],
            f"{_episode_label(f)}Named {f['resolution_tag']} but the video is "
            f"{f['width']}×{f['height']} ({f['resolution_class']}): {what}",
            f["title_id"], f["id"], f["season_number"], {"direction": kind}))  # fmt: skip

    # ---------------------------------------------------------------- naming
    spans: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    season_rows: dict[tuple[int, int], sqlite3.Row] = {}
    for f in mains:
        if f["kind"] != "show" or f["title_id"] is None:
            continue
        if f["episode_number"] is None:
            out.add(f["library"], Flag(
                "unnumbered_episode", f"unnumbered_episode:{f['id']}", _fp(f), f["library_id"],
                "No episode number in the filename", f["title_id"], f["id"],
                f["season_number"]))  # fmt: skip
        elif f["season_number"]:
            key = (f["title_id"], f["season_number"])
            spans[key].append((f["episode_number"], f["episode_number_end"] or 0))
            season_rows.setdefault(key, f)
    for key, sp in spans.items():
        missing = missing_episodes(sp)
        if not missing:
            continue
        f = season_rows[key]
        present = sorted({a for a, _ in sp})
        out.add(f["library"], Flag(
            "missing_episodes",
            f"missing_episodes:{f['library']}:{f['folder_path']}:S{f['season_number']}",
            ",".join(map(str, present)), f["library_id"],
            f"Season {f['season_number']}: missing {ranges(missing)} "
            f"({len(missing)} episode{'s' if len(missing) != 1 else ''})",
            f["title_id"], None, f["season_number"], {"missing": missing}))  # fmt: skip
    season_dirs: dict[tuple[int, str], dict[int, str]] = defaultdict(dict)
    for f in mains:
        parts = f["rel_path"].split("/")
        if f["kind"] == "show" and len(parts) >= 3:
            n = season_folder_number(parts[1])
            if n is not None:
                season_dirs[(f["library_id"], parts[0])].setdefault(n, parts[1])
    for f in mains:
        parts = f["rel_path"].split("/")
        if f["kind"] != "show" or len(parts) != 2 or f["title_id"] is None:
            continue
        dirs = season_dirs.get((f["library_id"], parts[0]))
        if not dirs:
            continue  # no season folders at all: a flat layout, which is fine
        n = f["season_number"]
        where = (f"{dirs[n]} has the rest of that season" if n in dirs
                 else "the show's other episodes are in season folders")  # fmt: skip
        out.add(f["library"], Flag(
            "episode_outside_season", f"episode_outside_season:{f['id']}", _fp(f),
            f["library_id"], f"{_episode_label(f)}In the show folder, not a season folder; "
            f"{where}", f["title_id"], f["id"], n))  # fmt: skip
    for t in titles:
        if not (t["tmdb_id"] or t["imdb_id"] or t["tvdb_id"]):
            out.add(t["library"], Flag(
                "no_ids", f"no_ids:{t['library']}:{t['folder_path']}", "", t["library_id"],
                "No ID in the folder or file names", t["id"]))  # fmt: skip

    # ---------------------------------------------------------------- notes
    for eps in seasons.values():
        classes = Counter(e["resolution_class"] for e in eps if e["resolution_class"])
        if len(classes) < 2:
            continue
        e = eps[0]
        mix = ", ".join(f"{c} ×{n}" for c, n in classes.most_common())
        out.add(e["library"], Flag(
            "mixed_resolution_season",
            f"mixed_resolution_season:{e['library']}:{e['folder_path']}:S{e['season_number']}",
            mix, e["library_id"], f"Season {e['season_number']}: {mix}", e["title_id"], None,
            e["season_number"]))  # fmt: skip
    preferred = set(config.languages.preferred_audio)
    for f in analyzed:
        if f["codec"] in LEGACY_CODECS:
            out.add(f["library"], Flag(
                "legacy_codec", f"legacy_codec:{f['id']}", _fp(f), f["library_id"],
                f"{_episode_label(f)}{LEGACY_CODECS[f['codec']]} in "
                f"{f['container'] or f['ext'].upper()}, {_size(f['size_bytes'])}",
                f["title_id"], f["id"], f["season_number"]))  # fmt: skip
        langs = set((f["audio_languages"] or "").split(",")) - {""}
        if preferred and langs and not (langs & (preferred | {"und"})):
            out.add(f["library"], Flag(
                "no_preferred_audio", f"no_preferred_audio:{f['id']}", _fp(f),
                f["library_id"],
                f"{_episode_label(f)}Audio: {', '.join(sorted(langs))} "
                f"(preferred: {', '.join(sorted(preferred))})",
                f["title_id"], f["id"], f["season_number"]))  # fmt: skip
    return out.flags


def _season_name(n: int) -> str:
    return "Specials" if n == 0 else f"Season {n}"


def _folder_summary(rows: list[sqlite3.Row], show: bool) -> str:
    """'10 episodes, 8.1 GB' / 'no videos: nfo, jpg x2, 15 KB' / 'empty'."""
    if not rows:
        return "empty"
    size = _size(sum(r["size_bytes"] for r in rows))
    videos = sum(1 for r in rows if r["is_video"] and r["role"] == "main")
    if videos:
        word = "episode" if show else "video"
        return f"{videos} {word}{'s' if videos != 1 else ''}, {size}"
    if any(r["is_video"] for r in rows):
        return f"only extras or samples, {size}"
    exts = Counter((r["ext"] or "no extension") for r in rows)
    kinds = ", ".join(e if n == 1 else f"{e} ×{n}" for e, n in exts.most_common(6))
    if len(exts) > 6:
        kinds += ", …"
    return f"no videos: {kinds}, {size}"


def _folder_checks(
    conn: sqlite3.Connection, files: list[sqlite3.Row], out: _Collector
) -> dict[int, list[str]]:
    """Leftover folders: seasons split across folders, folders without videos, empty ones.

    Returns the folders reported as having no videos (library id -> folders), so their
    files aren't reported again one by one.

    Folders are relative to the library root: ``Show`` (a title folder) and
    ``Show/Season 01`` (a season folder, in shows only).
    """
    libs = {r["id"]: r for r in conn.execute(
        "SELECT id, name, type, path FROM libraries WHERE enabled = 1")}  # fmt: skip
    title_ids: dict[tuple[int, str], int] = {}
    for r in conn.execute("SELECT id, library_id, folder_path FROM titles"):
        lib = libs.get(r["library_id"])
        root = lib["path"].rstrip("/") + "/" if lib else None
        if root and r["folder_path"].startswith(root):
            title_ids[(r["library_id"], r["folder_path"][len(root) :])] = r["id"]

    def is_show(lib_id: int) -> bool:
        return bool(libs[lib_id]["type"] != "movies")

    tops: dict[tuple[int, str], list[sqlite3.Row]] = defaultdict(list)
    # (library, show folder, season number) -> season folder name -> files
    seasons: dict[tuple[int, str, int], dict[str, list[sqlite3.Row]]] = defaultdict(dict)
    for f in files:
        parts = f["rel_path"].split("/")
        if len(parts) < 2:
            continue  # loose file in the library root
        tops[(f["library_id"], parts[0])].append(f)
        if len(parts) >= 3 and is_show(f["library_id"]):
            n = season_folder_number(parts[1])
            if n is not None:
                seasons[(f["library_id"], parts[0], n)].setdefault(parts[1], []).append(f)

    empties = [(r["library_id"], r["rel_path"]) for r in conn.execute(
        "SELECT e.library_id, e.rel_path FROM empty_folders e "
        "JOIN libraries l ON l.id = e.library_id AND l.enabled = 1 ORDER BY 1, 2")]  # fmt: skip
    for lib_id, rel in empties:
        parts = rel.split("/")
        if len(parts) == 2 and is_show(lib_id):
            n = season_folder_number(parts[1])
            if n is not None:
                seasons[(lib_id, parts[0], n)].setdefault(parts[1], [])

    # A title folder with no videos at all is reported once, not again per season.
    videoless: set[tuple[int, str]] = set()
    reported: dict[int, list[str]] = defaultdict(list)
    for (lib_id, top), rows in sorted(tops.items()):
        if any(r["is_video"] for r in rows):
            continue
        videoless.add((lib_id, top))
        reported[lib_id].append(top)
        lib = libs[lib_id]
        out.add(lib["name"], Flag(
            "no_video_folder", f"no_video_folder:{lib['name']}:{top}",
            ",".join(sorted(_fp(r) for r in rows)), lib_id,
            f"{'Show' if is_show(lib_id) else 'Movie'} folder with "
            f"{_folder_summary(rows, is_show(lib_id))}",
            title_ids.get((lib_id, top)),
            data={"folder": top, "files": [{"path": r["rel_path"], "size": r["size_bytes"]}
                                           for r in rows[:20]]}))  # fmt: skip

    # A season in several folders is only "split" when more than one of them has videos.
    # A leftover beside the real folder (empty, or only junk) is reported on its own as an
    # empty folder / folder with no videos, noting which folder has the season's videos.
    twin_note: dict[tuple[int, str], str] = {}  # empty leftover -> note for its message
    for (lib_id, top, n), folders in sorted(seasons.items()):
        if (lib_id, top) in videoless:
            continue
        lib = libs[lib_id]
        with_videos = sorted((d for d in folders if any(r["is_video"] for r in folders[d])),
                             key=lambda d: (-len(folders[d]), d))  # fmt: skip
        if len(with_videos) > 1:
            listing = "; ".join(f"{d} ({_folder_summary(folders[d], True)})" for d in with_videos)
            out.add(lib["name"], Flag(
                "split_season", f"split_season:{lib['name']}:{top}:S{n}",
                ",".join(f"{d}:{len(folders[d])}:{sum(r['size_bytes'] for r in folders[d])}"
                         for d in sorted(with_videos)), lib_id,
                f"{_season_name(n)} is in {len(with_videos)} folders: {listing}",
                title_ids.get((lib_id, top)), None, n,
                {"folders": [{"path": f"{top}/{d}",
                              "summary": _folder_summary(folders[d], True)}
                             for d in with_videos]}))  # fmt: skip
        note = ""
        if with_videos and len(folders) > len(with_videos):
            verb = "has" if len(with_videos) == 1 else "have"
            note = f"; {' and '.join(with_videos)} {verb} this season's videos"
        for name in sorted(folders):
            rows = folders[name]
            if name in with_videos:
                continue
            path = f"{top}/{name}"
            if not rows:  # empty: reported with the other empty folders below
                twin_note[(lib_id, path)] = note
                continue
            reported[lib_id].append(path)
            out.add(lib["name"], Flag(
                "no_video_folder", f"no_video_folder:{lib['name']}:{path}",
                ",".join(sorted(_fp(r) for r in rows)), lib_id,
                f"{_season_name(n)} folder with {_folder_summary(rows, True)}{note}",
                title_ids.get((lib_id, top)), None, n,
                {"folder": path,
                 "files": [{"path": r["rel_path"], "size": r["size_bytes"]}
                           for r in rows[:20]]}))  # fmt: skip

    for lib_id, rel in empties:
        lib = libs[lib_id]
        top = rel.split("/")[0]
        what = (
            ("Empty show folder" if is_show(lib_id) else "Empty movie folder")
            if "/" not in rel
            else "Empty folder"
        )
        what += twin_note.get((lib_id, rel), "")
        out.add(lib["name"], Flag(
            "empty_folder", f"empty_folder:{lib['name']}:{rel}", "", lib_id, what,
            title_ids.get((lib_id, top)), data={"folder": rel}))  # fmt: skip
    return reported


# .nfo files media servers read: these names, or the name of a video next to them.
_METADATA_NFO = frozenset({"tvshow", "season", "movie"})


def _stray_nfo(files: list[sqlite3.Row], reported: dict[int, list[str]], out: _Collector) -> None:
    """.nfo files that aren't media-server metadata: scene release info, or the metadata
    of a video that has since moved or been renamed."""
    stems: dict[tuple[int, str], set[str]] = defaultdict(set)
    for f in files:
        if f["is_video"]:
            folder, _, name = f["rel_path"].rpartition("/")
            stems[(f["library_id"], folder)].add(name.rsplit(".", 1)[0].casefold())
    for f in files:
        if (f["ext"] or "").lower() != "nfo" or _in_extras_folder(f["rel_path"]):
            continue
        folder, _, name = f["rel_path"].rpartition("/")
        stem = name[: -len(".nfo")].casefold()
        if stem in _METADATA_NFO or stem in stems.get((f["library_id"], folder), ()):
            continue
        if any(
            folder == d or folder.startswith(d + "/") for d in reported.get(f["library_id"], [])
        ):
            continue  # already listed as a folder with no videos left
        out.add(f["library"], Flag(
            "stray_file", f"stray_file:{f['id']}", _fp(f), f["library_id"],
            "Not media-server metadata (.nfo): release info, or left over from a moved or "
            f"renamed video, {_size(f['size_bytes'])}", f["title_id"], f["id"],
            f["season_number"], {"kind": "nfo"}))  # fmt: skip


def _in_extras_folder(rel_path: str) -> bool:
    """Bonus material (e.g. Extras/Commentaries/*.mp3) isn't a stray file."""
    folders = rel_path.lower().split("/")[1:-1]  # below the title folder, like classify
    return any(d in EXTRAS_FOLDERS or d in {"commentaries", "commentary"} for d in folders)


def _episode_label(f: sqlite3.Row) -> str:
    s, e = f["season_number"], f["episode_number"]
    if f["kind"] == "show" and s is not None and e is not None:
        return f"S{s:02d}E{e:02d}: "
    return ""


def _settings_hash(config: Config) -> str:
    settings = {"flags": config.flags.model_dump(),
                "preferred_audio": config.languages.preferred_audio}  # fmt: skip
    return hashlib.sha1(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]


def compute_flags(conn: sqlite3.Connection, config: Config) -> int:
    """Rebuild the ``flags`` table. Returns how many issues were found."""
    flags = collect(conn, config)
    now = utcnow()
    seen: set[str] = set()
    rows = []
    for fl in flags:
        if fl.key in seen:  # e.g. one file hit by both the episode and movie checks
            continue
        seen.add(fl.key)
        rows.append((
            fl.code, CHECKS[fl.code].severity, fl.key, fl.fingerprint, fl.library_id,
            fl.title_id, fl.file_id, fl.season_number, fl.message,
            json.dumps(fl.data, separators=(",", ":")) if fl.data else None,
            fl.bytes_recoverable, now,
        ))  # fmt: skip
    with transaction(conn):
        conn.execute("DELETE FROM flags")
        conn.executemany(
            "INSERT INTO flags (code, severity, key, fingerprint, library_id, title_id, file_id, "
            "season_number, message, data, bytes_recoverable, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        set_meta(conn, "flags_version", str(FLAGS_VERSION))
        set_meta(conn, "flags_settings", _settings_hash(config))
    log.info("issues: %d found", len(rows))
    return len(rows)


def ensure_flags(conn: sqlite3.Connection, config: Config) -> bool:
    """Recompute issues if the checks or their settings changed since they were stored."""
    if not conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        return False
    if (get_meta(conn, "flags_version") == str(FLAGS_VERSION)
            and get_meta(conn, "flags_settings") == _settings_hash(config)):  # fmt: skip
        return False
    compute_flags(conn, config)
    return True
