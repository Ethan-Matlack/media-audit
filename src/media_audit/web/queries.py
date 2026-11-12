"""Read-only queries behind the web pages."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from media_audit import stats as stats_mod

PAGE_SIZE = 50


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def libraries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT id, name, type, path, enabled FROM libraries ORDER BY enabled DESC, name"
        )
    )


def has_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None


# --------------------------------------------------------------------------- overview


def overview(conn: sqlite3.Connection, library: str | None) -> dict[str, Any]:
    lib = "AND l.name = ?" if library else ""
    params = [library] if library else []
    per_library = _rows(
        conn.execute(
            f"""
        SELECT l.name, l.type, count(*) AS files,
               coalesce(sum(f.is_video AND f.role = 'main'), 0) AS videos,
               coalesce(sum(f.size_bytes), 0) AS bytes,
               (SELECT count(*) FROM titles t WHERE t.library_id = l.id) AS titles
        FROM files f JOIN libraries l ON l.id = f.library_id
        WHERE f.deleted_at IS NULL {lib}
        GROUP BY l.id ORDER BY bytes DESC
        """,
            params,
        )
    )
    badges = conn.execute(
        f"""
        SELECT coalesce(sum(has_4k), 0) AS uhd, coalesce(sum(has_hdr), 0) AS hdr,
               coalesce(sum(has_dv), 0) AS dv, coalesce(sum(has_object_audio), 0) AS atmos
        FROM v_titles {"WHERE library = ?" if library else ""}
        """,
        params,
    ).fetchone()
    report = {
        "totals": stats_mod.totals(conn, library),
        "per_library": per_library,
        "badges": dict(badges),
        "resolution": stats_mod.breakdown(conn, "resolution", library),
        "dynamic_range": stats_mod.breakdown(conn, "dynamic_range", library),
        "video_codec": stats_mod.breakdown(conn, "video_codec", library),
        "container": stats_mod.breakdown(conn, "container", library),
        "source": stats_mod.breakdown(conn, "source", library),
        "audio_format": stats_mod.audio_breakdown(conn, "commercial_name", library)[:12],
        "audio_language": stats_mod.audio_breakdown(conn, "language", library)[:10],
        "subtitle_language": stats_mod.subtitle_language_breakdown(conn, library)[:10],
        "release_group": stats_mod.release_group_breakdown(conn, library, limit=12),
        "largest_titles": _rows(
            conn.execute(
                f"SELECT title_id, library, kind, name, year, folder_size_bytes AS bytes, "
                f"video_count FROM v_titles {'WHERE library = ?' if library else ''} "
                "ORDER BY folder_size_bytes DESC LIMIT 12",
                params,
            )
        ),
        "growth": growth(conn, library),
    }
    return report


def growth(conn: sqlite3.Connection, library: str | None) -> list[dict[str, Any]]:
    """Size and file count after each successful scan, from the stored snapshots."""
    points = []
    for r in conn.execute("SELECT scan_id, created_at, data FROM scan_snapshots ORDER BY scan_id"):
        data = json.loads(r["data"])
        totals = data.get("libraries", {}).get(library) if library else data.get("totals")
        if not totals:
            continue
        points.append(
            {
                "scan_id": r["scan_id"],
                "at": r["created_at"],
                "bytes": totals.get("bytes", 0),
                "files": totals.get("files", 0),
            }
        )
    return points


# --------------------------------------------------------------------------- episodes


def _episode_stats_sql(where: str = "1 = 1") -> str:
    """Per (show, season): episodes, episode-file bytes, runtime and duplicates.

    Only main video files count (no subtitles/artwork/extras). A file covering
    S01E01-E03 counts as three episodes. An episode with several files counts once,
    but every file's bytes count, so ``bytes / episodes`` is what each episode costs
    on disk. ``season`` is -1 when the season is unknown.
    """
    return f"""
        WITH eps AS (
            SELECT f.title_id, coalesce(f.season_number, -1) AS season, f.size_bytes,
                   v.duration_s, coalesce(f.episode_number, -f.id) AS ep,
                   CASE WHEN f.episode_number_end > f.episode_number
                        THEN f.episode_number_end - f.episode_number + 1 ELSE 1 END AS span
            FROM files f
            JOIN titles t ON t.id = f.title_id AND t.kind = 'show'
            LEFT JOIN video_info v ON v.file_id = f.id
            WHERE f.deleted_at IS NULL AND f.role = 'main' AND f.is_video = 1 AND {where}
        ),
        per_ep AS (
            SELECT title_id, season, ep, max(span) AS span, count(*) AS files
            FROM eps GROUP BY title_id, season, ep
        ),
        ep_agg AS (
            SELECT title_id, season, sum(span) AS episodes,
                   sum(files > 1) AS dup_episodes, sum(files - 1) AS extra_files
            FROM per_ep GROUP BY title_id, season
        ),
        file_agg AS (
            SELECT title_id, season, count(*) AS files, sum(size_bytes) AS bytes,
                   sum(CASE WHEN duration_s > 0 THEN duration_s END) AS seconds,
                   sum(duration_s > 0) AS timed_files,
                   sum(CASE WHEN duration_s > 0 THEN size_bytes END) AS timed_bytes
            FROM eps GROUP BY title_id, season
        )
        SELECT f.title_id, f.season, f.files, f.bytes, f.seconds, f.timed_bytes, f.timed_files,
               e.episodes, e.dup_episodes, e.extra_files
        FROM file_agg f JOIN ep_agg e USING (title_id, season)
    """


def _averages(row: Mapping[str, Any]) -> dict[str, Any]:
    episodes = row["episodes"] or 0
    seconds = row["seconds"] or 0
    return {
        "episodes": episodes,
        "files": row["files"],
        "bytes": row["bytes"],
        "dup_episodes": row["dup_episodes"] or 0,
        "extra_files": row["extra_files"] or 0,
        "avg_bytes": row["bytes"] / episodes if episodes else None,
        # Weighted by runtime: total GB / total hours, not an average of per-file ratios.
        "gb_per_hour": (row["timed_bytes"] / 1e9) / (seconds / 3600) if seconds else None,
        # Per file: a second copy of an episode doesn't make the episode longer.
        "avg_runtime_s": seconds / row["timed_files"] if row["timed_files"] else None,
    }


_STAT_KEYS = ("files", "bytes", "seconds", "timed_bytes", "timed_files", "episodes",
              "dup_episodes", "extra_files")  # fmt: skip


def show_episode_stats(conn: sqlite3.Connection, title_id: int) -> dict[str, Any]:
    """Averages for the whole show (specials excluded), per season, and for specials."""
    rows = conn.execute(_episode_stats_sql("f.title_id = ?"), (title_id,)).fetchall()
    seasons = {(None if r["season"] == -1 else r["season"]): _averages(r) for r in rows}
    regular = [r for r in rows if r["season"] != 0]
    show = None
    if regular:
        show = _averages({k: sum((r[k] or 0) for r in regular) for k in _STAT_KEYS})
    return {"show": show, "seasons": seasons, "specials": seasons.get(0)}


# --------------------------------------------------------------------------- titles

TITLE_SORTS = {
    "name": "lower(t.name)",
    "year": "t.year",
    "size": "t.folder_size_bytes",
    "files": "t.video_count",
    "library": "l.name",
    "updated": "t.updated_at",
    "avg_episode": "ep.avg_bytes",
}
BADGES = {
    "4k": "has_4k",
    "hdr": "has_hdr",
    "dv": "has_dv",
    "atmos": "has_object_audio",
}


@dataclass
class Page:
    rows: list[dict[str, Any]]
    total: int
    page: int
    pages: int


def _page(total: int, page: int) -> tuple[int, int]:
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)
    return page, pages


def _int(value: str | None, default: int = 1) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def titles(conn: sqlite3.Connection, q: Mapping[str, str]) -> Page:
    where, params = ["1 = 1"], []
    if q.get("q"):
        where.append("t.name LIKE ?")
        params.append(f"%{q['q']}%")
    if q.get("library"):
        where.append("l.name = ?")
        params.append(q["library"])
    if q.get("kind") in {"movie", "show"}:
        where.append("t.kind = ?")
        params.append(q["kind"])
    badge_sql = {
        "has_4k": "EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id "
        "WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main' "
        "AND v.resolution_class IN ('2160p', '4320p'))",
        "has_hdr": "EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id "
        "WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main' "
        "AND v.dynamic_range <> 'SDR')",
        "has_dv": "EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id "
        "WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main' "
        "AND v.dv_profile IS NOT NULL)",
        "has_object_audio": "EXISTS (SELECT 1 FROM files f JOIN audio_tracks a ON "
        "a.file_id = f.id WHERE f.title_id = t.id AND f.deleted_at IS NULL "
        "AND f.role = 'main' AND a.is_object_audio = 1)",
    }
    if q.get("badge") in BADGES:
        where.append(badge_sql[BADGES[q["badge"]]])
    sql_where = " AND ".join(where)
    total = conn.execute(
        f"SELECT count(*) FROM titles t JOIN libraries l ON l.id = t.library_id WHERE {sql_where}",
        params,
    ).fetchone()[0]
    page, pages = _page(total, _int(q.get("page")))
    sort = TITLE_SORTS.get(q.get("sort", ""), TITLE_SORTS["size"])
    direction = "ASC" if q.get("dir") == "asc" else "DESC"
    rows = _rows(
        conn.execute(
            f"""
        SELECT t.id, t.kind, t.name, t.year, l.name AS library, t.video_count, t.version_count,
               t.folder_size_bytes AS bytes, t.group_key,
               (SELECT count(*) FROM seasons s WHERE s.title_id = t.id) AS seasons,
               ep.avg_bytes AS avg_episode_bytes, ep.episodes, ep.dup_episodes,
               (SELECT count(*) FROM flags fl LEFT JOIN flag_ignores ig
                  ON ig.key = fl.key AND ig.fingerprint = fl.fingerprint
                WHERE fl.title_id = t.id AND ig.key IS NULL
                  AND fl.severity IN ('error', 'warning')) AS open_issues,
               {", ".join(f"{sql} AS {name}" for name, sql in badge_sql.items())}
        FROM titles t JOIN libraries l ON l.id = t.library_id
        LEFT JOIN (
            SELECT title_id, sum(episodes) AS episodes, sum(dup_episodes) AS dup_episodes,
                   1.0 * sum(bytes) / sum(episodes) AS avg_bytes
            FROM ({_episode_stats_sql()}) WHERE season <> 0 GROUP BY title_id
        ) AS ep ON ep.title_id = t.id
        WHERE {sql_where}
        ORDER BY {sort} {direction} NULLS LAST, lower(t.name)
        LIMIT ? OFFSET ?
        """,
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
    )
    return Page(rows, total, page, pages)


def title_detail(conn: sqlite3.Connection, title_id: int) -> dict[str, Any] | None:
    title = conn.execute(
        "SELECT t.*, l.name AS library, l.type AS library_type FROM titles t "
        "JOIN libraries l ON l.id = t.library_id WHERE t.id = ?",
        (title_id,),
    ).fetchone()
    if title is None:
        return None
    linked = []
    if title["group_key"]:
        linked = _rows(
            conn.execute(
                "SELECT t.id, t.name, t.year, l.name AS library, t.folder_size_bytes AS bytes "
                "FROM titles t JOIN libraries l ON l.id = t.library_id "
                "WHERE t.group_key = ? AND t.id <> ? ORDER BY l.name",
                (title["group_key"], title_id),
            )
        )
    seasons = _rows(
        conn.execute(
            "SELECT season_number, episode_count, folder_size_bytes AS bytes, folder_path "
            "FROM seasons WHERE title_id = ? ORDER BY season_number IS NULL, season_number",
            (title_id,),
        )
    )
    files = _rows(
        conn.execute(
            f"""
        SELECT {FILE_COLUMNS}
        FROM files f
        JOIN libraries l ON l.id = f.library_id
        LEFT JOIN video_info v ON v.file_id = f.id
        LEFT JOIN release_info r ON r.file_id = f.id
        WHERE f.title_id = ? AND f.deleted_at IS NULL
        ORDER BY f.is_video DESC, f.role = 'main' DESC, f.season_number, f.episode_number,
                 f.rel_path
        """,
            (title_id,),
        )
    )
    attach_tracks(conn, files)
    by_season: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for f in files:
        by_season[f["season_number"]].append(f)
    episode_stats = show_episode_stats(conn, title_id) if title["kind"] == "show" else None
    for season in seasons:
        season["stats"] = (episode_stats or {}).get("seasons", {}).get(season["season_number"])
    return {
        "title": dict(title),
        "linked": linked,
        "seasons": seasons,
        "files": files,
        "by_season": dict(by_season),
        "episode_stats": episode_stats,
    }


# --------------------------------------------------------------------------- files

FILE_COLUMNS = """
    f.id, f.title_id, l.name AS library, f.rel_path, f.filename, f.ext, f.role, f.is_video,
    f.season_number, f.episode_number, f.episode_number_end, f.size_bytes, f.analyze_error,
    f.analyzed_at, v.container, v.duration_s, v.overall_bitrate, v.gb_per_hour,
    v.codec AS video_codec, v.profile AS video_profile, v.bit_depth, v.width, v.height,
    v.resolution_class, v.frame_rate, v.dynamic_range, v.dv_profile, v.dv_compatibility,
    r.release_group, r.source, r.edition, r.quality_tag, r.is_remux
"""

FILE_SORTS = {
    "path": "f.rel_path",
    "size": "f.size_bytes",
    "gbh": "v.gb_per_hour",
    "bitrate": "v.overall_bitrate",
    "duration": "v.duration_s",
    "resolution": "v.height",
    "codec": "v.codec",
    "group": "lower(r.release_group)",
    "library": "l.name",
}

# Filter name -> (SQL condition, how to turn the query value into parameters)
_EQ_FILTERS = {
    "library": "l.name = ?",
    "resolution": "v.resolution_class = ?",
    "codec": "v.codec = ?",
    "dr": "v.dynamic_range = ?",
    "group": "r.release_group = ?",
    "source": "r.source = ?",
    "container": "v.container = ?",
}


def _file_filters(q: Mapping[str, str]) -> tuple[str, list[Any]]:
    where = ["f.deleted_at IS NULL"]
    params: list[Any] = []
    role = q.get("role", "main")
    if role == "main":
        where.append("f.role = 'main' AND f.is_video = 1")
    elif role == "video":
        where.append("f.is_video = 1")
    elif role in {"extra", "sample", "subtitle", "other"}:
        where.append("f.role = ?")
        params.append(role)
    if q.get("q"):
        where.append("f.rel_path LIKE ?")
        params.append(f"%{q['q']}%")
    if q.get("folder"):
        # Everything in one folder (and its subfolders), matched as a literal prefix.
        folder = q["folder"].strip("/") + "/"
        where.append("substr(f.rel_path, 1, ?) = ?")
        params.extend([len(folder), folder])
    for key, cond in _EQ_FILTERS.items():
        if q.get(key):
            if q[key] == "(none)":
                where.append(cond.replace("= ?", "IS NULL"))
            else:
                where.append(cond)
                params.append(q[key])
    if q.get("audio"):
        where.append(
            "EXISTS (SELECT 1 FROM audio_tracks a WHERE a.file_id = f.id AND "
            "coalesce(a.commercial_name, a.codec) = ?)"
        )
        params.append(q["audio"])
    if q.get("alang"):
        where.append(
            "EXISTS (SELECT 1 FROM audio_tracks a WHERE a.file_id = f.id AND a.language = ?)"
        )
        params.append(q["alang"])
    if q.get("remux") == "1":
        where.append("r.is_remux = 1")
    if q.get("errors") == "1":
        where.append("f.analyze_error IS NOT NULL")
    lo, hi = _float(q.get("min_gb")), _float(q.get("max_gb"))
    if lo is not None:
        where.append("f.size_bytes >= ?")
        params.append(int(lo * 1e9))
    if hi is not None:
        where.append("f.size_bytes <= ?")
        params.append(int(hi * 1e9))
    return " AND ".join(where), params


_FILE_FROM = """
    FROM files f
    JOIN libraries l ON l.id = f.library_id
    LEFT JOIN video_info v ON v.file_id = f.id
    LEFT JOIN release_info r ON r.file_id = f.id
"""


def files(conn: sqlite3.Connection, q: Mapping[str, str]) -> Page:
    where, params = _file_filters(q)
    total = conn.execute(f"SELECT count(*) {_FILE_FROM} WHERE {where}", params).fetchone()[0]
    page, pages = _page(total, _int(q.get("page")))
    sort = FILE_SORTS.get(q.get("sort", ""), FILE_SORTS["size"])
    direction = "ASC" if q.get("dir") == "asc" else "DESC"
    rows = _rows(
        conn.execute(
            f"SELECT {FILE_COLUMNS} {_FILE_FROM} WHERE {where} "
            f"ORDER BY {sort} {direction} NULLS LAST, f.rel_path LIMIT ? OFFSET ?",
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
    )
    attach_tracks(conn, rows)
    return Page(rows, total, page, pages)


def file_filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    def distinct(sql: str) -> list[str]:
        return [str(r[0]) for r in conn.execute(sql) if r[0] is not None]

    live = "JOIN files f ON f.id = x.file_id WHERE f.deleted_at IS NULL"
    return {
        "resolution": distinct(
            f"SELECT DISTINCT x.resolution_class FROM video_info x {live} "
            "ORDER BY x.resolution_class"
        ),
        "codec": distinct(
            f"SELECT x.codec FROM video_info x {live} GROUP BY 1 ORDER BY count(*) DESC"
        ),
        "dr": distinct(
            f"SELECT x.dynamic_range FROM video_info x {live} GROUP BY 1 ORDER BY count(*) DESC"
        ),
        "container": distinct(
            f"SELECT x.container FROM video_info x {live} GROUP BY 1 ORDER BY count(*) DESC"
        ),
        "source": distinct(
            f"SELECT x.source FROM release_info x {live} GROUP BY 1 ORDER BY count(*) DESC"
        ),
        "audio": distinct(
            f"SELECT coalesce(x.commercial_name, x.codec) FROM audio_tracks x "
            f"{live} GROUP BY 1 ORDER BY count(DISTINCT x.file_id) DESC"
        ),
        "alang": distinct(
            f"SELECT x.language FROM audio_tracks x {live} GROUP BY 1 "
            "ORDER BY count(DISTINCT x.file_id) DESC"
        ),
        "group": distinct(
            f"SELECT x.release_group FROM release_info x {live} GROUP BY 1 "
            "ORDER BY count(*) DESC LIMIT 300"
        ),
    }


def attach_tracks(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Add ``audio`` and ``subs`` track lists to file rows (two queries, not 2 per row)."""
    if not rows:
        return
    ids = [r["id"] for r in rows]
    audio: dict[int, list[dict[str, Any]]] = defaultdict(list)
    subs: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start : chunk_start + 500]
        marks = ",".join("?" * len(chunk))
        for a in conn.execute(
            "SELECT file_id, idx, codec, commercial_name, channels_label, language, title, "
            "bitrate, is_default, is_forced, is_object_audio, object_audio_hint "
            f"FROM audio_tracks WHERE file_id IN ({marks}) ORDER BY file_id, idx",
            chunk,
        ):
            audio[a["file_id"]].append(dict(a))
        for s in conn.execute(
            "SELECT file_id, idx, format, language, title, is_forced, is_default, "
            "is_hearing_impaired, is_external FROM subtitle_tracks "
            f"WHERE file_id IN ({marks}) ORDER BY file_id, idx",
            chunk,
        ):
            subs[s["file_id"]].append(dict(s))
    for r in rows:
        r["audio"] = audio.get(r["id"], [])
        r["subs"] = subs.get(r["id"], [])


CSV_COLUMNS = [
    "library",
    "rel_path",
    "role",
    "size_bytes",
    "container",
    "duration_s",
    "overall_bitrate",
    "gb_per_hour",
    "video_codec",
    "video_profile",
    "bit_depth",
    "width",
    "height",
    "resolution_class",
    "frame_rate",
    "dynamic_range",
    "dv_profile",
    "audio",
    "audio_languages",
    "subtitle_languages",
    "release_group",
    "source",
    "edition",
    "quality_tag",
    "is_remux",
    "analyze_error",
]


def files_csv_rows(conn: sqlite3.Connection, q: Mapping[str, str]) -> Iterator[list[Any]]:
    where, params = _file_filters(q)
    sort = FILE_SORTS.get(q.get("sort", ""), FILE_SORTS["path"])
    direction = "DESC" if q.get("dir") == "desc" else "ASC"
    yield CSV_COLUMNS
    cur = conn.execute(
        f"""
        SELECT {FILE_COLUMNS},
          (SELECT group_concat(coalesce(a.commercial_name, a.codec) || ' ' ||
                               coalesce(a.channels_label, ''), ' | ')
             FROM audio_tracks a WHERE a.file_id = f.id) AS audio,
          (SELECT group_concat(DISTINCT a.language) FROM audio_tracks a
             WHERE a.file_id = f.id) AS audio_languages,
          (SELECT group_concat(DISTINCT s.language) FROM subtitle_tracks s
             WHERE s.file_id = f.id) AS subtitle_languages
        {_FILE_FROM} WHERE {where} ORDER BY {sort} {direction} NULLS LAST, f.rel_path
        """,
        params,
    )
    for row in cur:
        yield [row[c] for c in CSV_COLUMNS]


# --------------------------------------------------------------------------- scans


def scans(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT id, started_at, finished_at, status, libraries, files_seen, files_new, "
            "files_changed, files_moved, files_deleted, files_analyzed, errors, message, "
            "CAST(round((julianday(finished_at) - julianday(started_at)) * 86400) AS INTEGER) "
            "AS duration_s FROM scans ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )


def last_scan(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, started_at, finished_at, status FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- issues

ISSUE_GROUP_LIMIT = 100  # rows shown per check on the main Issues page
ISSUE_PAGE_SIZE = 200  # rows per page when looking at a single check

_ISSUE_SELECT = """
    SELECT fl.id, fl.code, fl.severity, fl.key, fl.title_id, fl.file_id, fl.season_number,
           fl.message, fl.data, fl.bytes_recoverable, l.name AS library,
           t.name AS title_name, t.year AS title_year, t.kind AS title_kind,
           f.rel_path AS file_path, f.size_bytes AS file_size,
           (ig.key IS NOT NULL) AS ignored
    FROM flags fl
    JOIN libraries l ON l.id = fl.library_id
    LEFT JOIN titles t ON t.id = fl.title_id
    LEFT JOIN files f ON f.id = fl.file_id
    LEFT JOIN flag_ignores ig ON ig.key = fl.key AND ig.fingerprint = fl.fingerprint
"""


def _issue_row(r: sqlite3.Row) -> dict[str, Any]:
    row = dict(r)
    row["data"] = json.loads(row["data"]) if row["data"] else None
    return row


def _issue_filters(q: Mapping[str, str]) -> tuple[str, list[Any]]:
    """WHERE clause for the library/severity/category/check/ignored filters."""
    from media_audit.rules.flags import CATEGORIES, CHECKS, SEVERITY_ORDER

    where, params = ["1 = 1"], []
    if q.get("library"):
        where.append("l.name = ?")
        params.append(q["library"])
    if q.get("severity") in SEVERITY_ORDER:
        where.append("fl.severity = ?")
        params.append(q["severity"])
    if q.get("category") in CATEGORIES:
        codes = [c for c, chk in CHECKS.items() if chk.category == q["category"]]
        where.append(f"fl.code IN ({','.join('?' * len(codes))})")
        params.extend(codes)
    if q.get("code") in CHECKS:
        where.append("fl.code = ?")
        params.append(q["code"])
    if q.get("ignored") != "1":
        where.append("ig.key IS NULL")
    return " AND ".join(where), params


def issues(conn: sqlite3.Connection, q: Mapping[str, str]) -> dict[str, Any]:
    from media_audit.rules.flags import CATEGORIES, CHECKS, SEVERITY_ORDER

    code = q.get("code") if q.get("code") in CHECKS else None
    sql_where, params = _issue_filters(q)

    # Tiles: counts for the filtered set, ignoring the category/code filters so the tiles
    # stay useful for switching between categories.
    tile_where, tile_params = ["1 = 1"], []
    if q.get("library"):
        tile_where.append("l.name = ?")
        tile_params.append(q["library"])
    tiles = {c: {"error": 0, "warning": 0, "info": 0, "total": 0} for c in CATEGORIES}
    ignored_total = 0
    for r in conn.execute(
        f"SELECT fl.code, fl.severity, (ig.key IS NOT NULL) AS ignored, count(*) AS n "
        f"FROM flags fl JOIN libraries l ON l.id = fl.library_id "
        f"LEFT JOIN flag_ignores ig ON ig.key = fl.key AND ig.fingerprint = fl.fingerprint "
        f"WHERE {' AND '.join(tile_where)} GROUP BY 1, 2, 3",
        tile_params,
    ):
        if r["ignored"]:
            ignored_total += r["n"]
            continue
        cat = CHECKS[r["code"]].category if r["code"] in CHECKS else "notes"
        tiles[cat][r["severity"]] += r["n"]
        tiles[cat]["total"] += r["n"]

    rows = [
        _issue_row(r)
        for r in conn.execute(
            f"{_ISSUE_SELECT} WHERE {sql_where} "
            "ORDER BY l.name, lower(coalesce(t.name, f.rel_path)), fl.season_number, fl.message",
            params,
        )
    ]
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_code[r["code"]].append(r)
    order = list(CHECKS)
    groups = []
    for c in sorted(by_code, key=lambda c: (SEVERITY_ORDER[CHECKS[c].severity],
                                            order.index(c) if c in order else 99)):  # fmt: skip
        items = by_code[c]
        total = len(items)
        page, pages = 1, 1
        if code:
            page, pages = _page_of(total, _int(q.get("page")), ISSUE_PAGE_SIZE)
            shown = items[(page - 1) * ISSUE_PAGE_SIZE : page * ISSUE_PAGE_SIZE]
        else:
            shown = items[:ISSUE_GROUP_LIMIT]
        groups.append({
            "check": CHECKS[c], "total": total, "rows": shown, "page": page, "pages": pages,
            "recoverable": sum(r["bytes_recoverable"] or 0 for r in items if not r["ignored"]),
            "ignored": sum(1 for r in items if r["ignored"]),
        })  # fmt: skip
    return {"tiles": tiles, "groups": groups, "ignored_total": ignored_total,
            "total": len(rows), "code": code, "categories": CATEGORIES}  # fmt: skip


def _page_of(total: int, page: int, size: int) -> tuple[int, int]:
    pages = max(1, -(-total // size))
    return min(max(1, page), pages), pages


def issues_csv_rows(conn: sqlite3.Connection, q: Mapping[str, str]) -> Iterator[list[Any]]:
    from media_audit.rules.flags import CHECKS

    where, params = _issue_filters(q)
    yield ["severity", "category", "check", "library", "title", "season", "file", "message",
           "recoverable_bytes", "ignored"]  # fmt: skip
    for r in conn.execute(
        f"{_ISSUE_SELECT} WHERE {where} ORDER BY fl.severity, fl.code, l.name, t.name", params
    ):
        chk = CHECKS.get(r["code"])
        yield [r["severity"], chk.category if chk else "", chk.heading if chk else r["code"],
               r["library"], r["title_name"] or "", r["season_number"] or "",
               r["file_path"] or "", r["message"], r["bytes_recoverable"] or "",
               "yes" if r["ignored"] else ""]  # fmt: skip


def open_issue_count(conn: sqlite3.Connection) -> int:
    """Errors and warnings that aren't ignored (the nav badge and overview tile)."""
    row = conn.execute(
        "SELECT count(*) FROM flags fl LEFT JOIN flag_ignores ig "
        "ON ig.key = fl.key AND ig.fingerprint = fl.fingerprint "
        "WHERE ig.key IS NULL AND fl.severity IN ('error', 'warning')"
    ).fetchone()
    return int(row[0])


def title_issues(conn: sqlite3.Connection, title_id: int) -> list[dict[str, Any]]:
    from media_audit.rules.flags import CHECKS, SEVERITY_ORDER

    rows = [_issue_row(r) for r in conn.execute(
        f"{_ISSUE_SELECT} WHERE fl.title_id = ? ORDER BY fl.season_number, fl.message",
        (title_id,))]  # fmt: skip
    rows.sort(key=lambda r: (r["ignored"], SEVERITY_ORDER.get(r["severity"], 9)))
    for r in rows:
        r["check"] = CHECKS.get(r["code"])
    return rows
