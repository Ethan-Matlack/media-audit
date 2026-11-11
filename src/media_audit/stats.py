"""Library statistics queries. Phase 1 has the core breakdowns; Phase 2 adds the rest."""

from __future__ import annotations

import sqlite3
from typing import Any

# Breakdowns over live main-role video files: name -> SQL expression on v_media.
_DIMENSIONS: dict[str, str] = {
    "resolution": "coalesce(resolution_class, '?')",
    "video_codec": "coalesce(video_codec, '?')",
    "dynamic_range": "coalesce(dynamic_range, '?')",
    "container": "coalesce(container, upper(ext))",
    "source": "coalesce(source, '?')",
}


def _where(library: str | None) -> tuple[str, list[Any]]:
    clause = "role = 'main'"
    params: list[Any] = []
    if library:
        clause += " AND library = ?"
        params.append(library)
    return clause, params


def totals(conn: sqlite3.Connection, library: str | None = None) -> dict[str, Any]:
    lib_clause = "AND l.name = ?" if library else ""
    params = [library] if library else []
    row = conn.execute(
        f"""
        SELECT count(*) AS files,
               coalesce(sum(f.size_bytes), 0) AS bytes,
               coalesce(sum(f.is_video AND f.role = 'main'), 0) AS main_videos,
               coalesce(sum(f.analyze_error IS NOT NULL), 0) AS analyze_errors,
               coalesce(sum(f.is_video AND f.analyzed_at IS NULL), 0) AS not_analyzed
        FROM files f JOIN libraries l ON l.id = f.library_id
        WHERE f.deleted_at IS NULL {lib_clause}
        """,
        params,
    ).fetchone()
    unique = conn.execute(
        f"""
        SELECT coalesce(sum(size), 0) FROM (
            SELECT max(f.size_bytes) AS size FROM files f JOIN libraries l ON l.id = f.library_id
            WHERE f.deleted_at IS NULL {lib_clause}
            GROUP BY CASE WHEN f.inode IS NULL THEN 'f' || f.id ELSE f.dev || ':' || f.inode END
        )
        """,
        params,
    ).fetchone()[0]
    titles = conn.execute(
        "SELECT count(*), count(DISTINCT coalesce(t.group_key, 'title:' || t.id)) FROM titles t "
        f"JOIN libraries l ON l.id = t.library_id WHERE 1 = 1 {lib_clause}",
        params,
    ).fetchone()
    return {
        "titles": titles[0],
        "distinct_titles": titles[1],
        "files": row["files"],
        "main_videos": row["main_videos"],
        "bytes": row["bytes"],
        "unique_bytes": unique,
        "analyze_errors": row["analyze_errors"],
        "not_analyzed": row["not_analyzed"],
    }


def breakdown(
    conn: sqlite3.Connection, dimension: str, library: str | None = None
) -> list[dict[str, Any]]:
    expr = _DIMENSIONS[dimension]
    where, params = _where(library)
    rows = conn.execute(
        f"SELECT {expr} AS value, count(*) AS files, coalesce(sum(size_bytes), 0) AS bytes, "
        f"avg(size_bytes) AS avg_bytes FROM v_media WHERE {where} "
        "GROUP BY value ORDER BY files DESC, value",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def audio_breakdown(
    conn: sqlite3.Connection, column: str, library: str | None = None
) -> list[dict[str, Any]]:
    """Count main files having at least one audio track with each value."""
    assert column in {"commercial_name", "language"}
    lib = "AND l.name = ?" if library else ""
    rows = conn.execute(
        f"""
        SELECT coalesce(a.{column}, '?') AS value, count(DISTINCT f.id) AS files
        FROM audio_tracks a JOIN files f ON f.id = a.file_id
        JOIN libraries l ON l.id = f.library_id
        WHERE f.deleted_at IS NULL AND f.role = 'main' {lib}
        GROUP BY value ORDER BY files DESC, value
        """,
        [library] if library else [],
    ).fetchall()
    return [dict(r) for r in rows]


def subtitle_language_breakdown(
    conn: sqlite3.Connection, library: str | None = None
) -> list[dict[str, Any]]:
    lib = "AND l.name = ?" if library else ""
    rows = conn.execute(
        f"""
        SELECT s.language AS value, count(DISTINCT f.id) AS files
        FROM subtitle_tracks s JOIN files f ON f.id = s.file_id
        JOIN libraries l ON l.id = f.library_id
        WHERE f.deleted_at IS NULL AND f.role = 'main' {lib}
        GROUP BY value ORDER BY files DESC, value
        """,
        [library] if library else [],
    ).fetchall()
    return [dict(r) for r in rows]


def release_group_breakdown(
    conn: sqlite3.Connection, library: str | None = None, limit: int = 25
) -> list[dict[str, Any]]:
    where, params = _where(library)
    rows = conn.execute(
        f"SELECT coalesce(release_group, '(none)') AS value, count(*) AS files, "
        f"coalesce(sum(size_bytes), 0) AS bytes FROM v_media WHERE {where} "
        "GROUP BY value ORDER BY files DESC, value LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [dict(r) for r in rows]


def largest_titles(
    conn: sqlite3.Connection, library: str | None = None, limit: int = 15
) -> list[dict[str, Any]]:
    lib = "WHERE library = ?" if library else ""
    rows = conn.execute(
        f"SELECT library, name, year, version_count, folder_size_bytes AS bytes FROM v_titles "
        f"{lib} ORDER BY folder_size_bytes DESC LIMIT ?",
        [*([library] if library else []), limit],
    ).fetchall()
    return [dict(r) for r in rows]


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compact aggregate document stored once per successful scan (growth charts)."""
    per_library = {}
    for (name,) in conn.execute("SELECT name FROM libraries WHERE enabled = 1"):
        per_library[name] = totals(conn, name)
    return {
        "totals": totals(conn),
        "libraries": per_library,
        "breakdowns": {
            dim: {r["value"]: [r["files"], r["bytes"]] for r in breakdown(conn, dim)}
            for dim in _DIMENSIONS
        },
    }


def full_report(conn: sqlite3.Connection, library: str | None = None) -> dict[str, Any]:
    return {
        "totals": totals(conn, library),
        **{dim: breakdown(conn, dim, library) for dim in _DIMENSIONS},
        "audio_format": audio_breakdown(conn, "commercial_name", library),
        "audio_language": audio_breakdown(conn, "language", library),
        "subtitle_language": subtitle_language_breakdown(conn, library),
        "release_group": release_group_breakdown(conn, library),
        "largest_titles": largest_titles(conn, library),
    }
