# ruff: noqa: E501  (the view definitions are copied verbatim from 0001)
"""Store paths relative to their library, so a library's folder can move freely.

Before: files.path, titles.folder_path and seasons.folder_path held absolute paths
(/media/movies/Heat (1995)/...), and Issues keys named the library and sometimes its
absolute folder. After:

* files: no ``path`` column; unique per (library_id, rel_path). The full path is the
  library's folder + rel_path, built only when a file is read.
* titles.rel_folder / seasons.rel_folder: relative to the library ("Heat (1995)",
  "Show/Season 01").
* Issues keys name the library by id ("duplicate_episode:L3:Show:S1E2"), so renaming or
  moving a library keeps ignored issues.

The database is copied to ``<db>.bak-0006-<time>`` first. Rebuilding ``files`` needs
foreign keys off, which SQLite only allows outside a transaction, so this is a Python
migration.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# files as it stands after migrations 0001-0005, minus ``path``.
FILES_COLUMNS = [
    "id", "library_id", "title_id", "season_id", "parent_file_id", "rel_path", "filename",
    "ext", "role", "is_video", "season_number", "episode_number", "episode_number_end",
    "size_bytes", "mtime_ns", "dev", "inode", "first_seen", "last_seen", "changed_at",
    "deleted_at", "analyzed_at", "analyzer", "analyze_error", "derived_at", "filename_parsed",
]  # fmt: skip

FILES_TABLE = """
CREATE TABLE files_new (
    id                  INTEGER PRIMARY KEY,
    library_id          INTEGER NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    title_id            INTEGER REFERENCES titles (id) ON DELETE SET NULL,
    season_id           INTEGER REFERENCES seasons (id) ON DELETE SET NULL,
    parent_file_id      INTEGER REFERENCES files (id) ON DELETE SET NULL,  -- sidecar -> video
    rel_path            TEXT NOT NULL,  -- relative to the library folder, '/'-separated
    filename            TEXT NOT NULL,
    ext                 TEXT NOT NULL,  -- lowercase, no dot; '' if none
    role                TEXT NOT NULL
                        CHECK (role IN ('main', 'extra', 'sample', 'subtitle', 'other')),
    is_video            INTEGER NOT NULL DEFAULT 0,
    season_number       INTEGER,
    episode_number      INTEGER,
    episode_number_end  INTEGER,        -- multi-episode files (S01E01-E03)
    size_bytes          INTEGER NOT NULL,
    mtime_ns            INTEGER NOT NULL,
    dev                 INTEGER,
    inode               INTEGER,        -- NULL/0 when the filesystem doesn't provide stable inodes
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    changed_at          TEXT,           -- last time size/mtime changed (needs re-analysis)
    deleted_at          TEXT,
    analyzed_at         TEXT,
    analyzer            TEXT,           -- e.g. 'mediainfo 24.12'
    analyze_error       TEXT,
    derived_at          TEXT,
    filename_parsed     TEXT,           -- JSON
    UNIQUE (library_id, rel_path)
)
"""

FILES_INDEXES = [
    "CREATE INDEX idx_files_library_active ON files (library_id, deleted_at)",
    "CREATE INDEX idx_files_title ON files (title_id)",
    "CREATE INDEX idx_files_season ON files (season_id)",
    "CREATE INDEX idx_files_inode ON files (inode, size_bytes, mtime_ns)",
    "CREATE INDEX idx_files_needs_analysis ON files (is_video, analyzed_at) "
    "WHERE deleted_at IS NULL",
]

# (table, column) pairs that point at files.id, cleaned up after duplicates are dropped.
FILE_CHILDREN = [
    ("video_info", "file_id"), ("audio_tracks", "file_id"), ("subtitle_tracks", "file_id"),
    ("subtitle_tracks", "external_file_id"), ("release_info", "file_id"),
    ("file_mediainfo", "file_id"), ("flags", "file_id"),
]  # fmt: skip

V_MEDIA = """
CREATE VIEW v_media AS
SELECT
    f.id AS file_id,
    l.name AS library,
    l.type AS library_type,
    t.name AS title,
    t.year AS year,
    f.season_number,
    f.episode_number,
    f.role,
    f.rel_path,
    f.filename,
    f.ext,
    f.size_bytes,
    round(f.size_bytes / 1e9, 2) AS size_gb,
    v.container,
    v.duration_s,
    v.gb_per_hour,
    v.overall_bitrate,
    v.codec AS video_codec,
    v.profile AS video_profile,
    v.bit_depth,
    v.width,
    v.height,
    v.resolution_class,
    v.frame_rate,
    v.dynamic_range,
    v.hdr_formats,
    v.dv_profile,
    v.dv_compatibility,
    v.dv_has_fallback,
    (SELECT group_concat(a.commercial_name || ' ' || coalesce(a.channels_label, '') || ' [' || a.language || ']', ', ')
       FROM audio_tracks a WHERE a.file_id = f.id) AS audio,
    (SELECT group_concat(DISTINCT s.language) FROM subtitle_tracks s WHERE s.file_id = f.id) AS subtitle_languages,
    r.release_group,
    r.source,
    r.edition,
    r.quality_tag,
    r.is_remux,
    f.analyze_error,
    l.path || '/' || f.rel_path AS path
FROM files f
JOIN libraries l ON l.id = f.library_id
LEFT JOIN titles t ON t.id = f.title_id
LEFT JOIN video_info v ON v.file_id = f.id
LEFT JOIN release_info r ON r.file_id = f.id
WHERE f.deleted_at IS NULL AND f.is_video = 1
"""

V_TITLES = """
CREATE VIEW v_titles AS
SELECT
    t.id AS title_id,
    l.name AS library,
    t.kind,
    t.name,
    t.year,
    t.tmdb_id,
    t.imdb_id,
    t.tvdb_id,
    coalesce(t.group_key, 'title:' || t.id) AS group_key,
    t.file_count,
    t.video_count,
    t.version_count,
    t.folder_size_bytes,
    round(t.folder_size_bytes / 1e9, 2) AS folder_size_gb,
    t.folder_unique_bytes,
    t.media_size_bytes,
    t.other_size_bytes,
    (SELECT count(*) FROM seasons s WHERE s.title_id = t.id) AS season_count,
    EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND v.resolution_class IN ('2160p', '4320p')) AS has_4k,
    EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND v.dynamic_range <> 'SDR') AS has_hdr,
    EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND v.dv_profile IS NOT NULL) AS has_dv,
    EXISTS (SELECT 1 FROM files f JOIN audio_tracks a ON a.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND a.is_object_audio = 1) AS has_object_audio,
    t.rel_folder,
    l.path || '/' || t.rel_folder AS folder_path
FROM titles t
JOIN libraries l ON l.id = t.library_id
"""


def _db_file(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _backup(conn: sqlite3.Connection) -> None:
    path = _db_file(conn)
    if path is None:  # in-memory
        return
    target = path.with_name(f"{path.name}.bak-0006-{datetime.now():%Y%m%d-%H%M%S}")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    log.info("database backed up to %s before moving to relative paths", target)
    from media_audit.backups import DB_BACKUPS, prune

    prune(path.parent, f"{path.name}.bak-0006-*", DB_BACKUPS)


def _rel(path: str | None, root: str) -> str | None:
    """``path`` relative to ``root``, or None when it isn't under it."""
    if path is None:
        return None
    if path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return None


def rewrite_key(key: str, libs: dict[str, tuple[int, str]]) -> str:
    """'<check>:<library name>:<abs or rel>…' -> '<check>:L<id>:<rel>…'. Others unchanged."""
    parts = key.split(":", 2)
    if len(parts) < 3 or parts[1] not in libs:
        return key
    lib_id, root = libs[parts[1]]
    rest = parts[2]
    if rest.startswith(root + "/"):
        rest = rest[len(root) + 1 :]
    return f"{parts[0]}:L{lib_id}:{rest}"


def rewrite_cross_fp(fp: str, libs: dict[str, tuple[int, str]]) -> str:
    """cross_library fingerprints: 'lib:/abs/folder,lib2:/abs2' -> 'L1:folder,L2:folder2'."""
    out = []
    for part in fp.split(","):
        name, sep, path = part.partition(":")
        if sep and name in libs:
            lib_id, root = libs[name]
            out.append(f"L{lib_id}:{_rel(path, root) or path}")
        else:
            out.append(part)
    return ",".join(sorted(out))


def migrate(conn: sqlite3.Connection, version: int) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)")]
    expected = [c for c in FILES_COLUMNS if c != "rel_path"]
    missing = [c for c in [*expected, "rel_path", "path"] if c not in cols]
    if missing:
        raise RuntimeError(f"unexpected files table (missing {missing})")

    _backup(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        libs = {r["name"]: (r["id"], r["path"].rstrip("/") or "/")
                for r in conn.execute("SELECT id, name, path FROM libraries")}  # fmt: skip
        roots = dict(libs.values())

        conn.execute("DROP VIEW IF EXISTS v_media")
        conn.execute("DROP VIEW IF EXISTS v_titles")

        # files: keep one row per (library, rel_path). Duplicates only exist when a
        # library's folder changed before 0.6 (old rows stayed behind, deleted); the live
        # or most recently seen row wins and the rest go with their analysis.
        conn.execute(FILES_TABLE)
        names = ", ".join(FILES_COLUMNS)
        conn.execute(
            f"INSERT OR IGNORE INTO files_new ({names}) SELECT {names} FROM files "
            "ORDER BY (deleted_at IS NULL) DESC, last_seen DESC, id DESC"
        )
        dropped = conn.execute("SELECT count(*) FROM files").fetchone()[0] - conn.execute(
            "SELECT count(*) FROM files_new").fetchone()[0]  # fmt: skip
        conn.execute("DROP TABLE files")
        conn.execute("ALTER TABLE files_new RENAME TO files")
        for sql in FILES_INDEXES:
            conn.execute(sql)
        if dropped:
            for table, column in FILE_CHILDREN:
                conn.execute(f"DELETE FROM {table} WHERE {column} IS NOT NULL "
                             f"AND {column} NOT IN (SELECT id FROM files)")  # fmt: skip
            conn.execute("UPDATE files SET parent_file_id = NULL WHERE parent_file_id "
                         "NOT IN (SELECT id FROM files)")  # fmt: skip
            log.info("dropped %d stale duplicate file rows", dropped)

        # titles / seasons: relative folders. A title not under its library's current
        # folder is stale (it would be rebuilt by the next scan): drop it.
        conn.execute("ALTER TABLE titles RENAME COLUMN folder_path TO rel_folder")
        conn.execute("ALTER TABLE seasons RENAME COLUMN folder_path TO rel_folder")
        stale = []
        for r in conn.execute("SELECT id, library_id, rel_folder FROM titles").fetchall():
            rel = _rel(r["rel_folder"], roots.get(r["library_id"], "\0"))
            if rel is None:
                stale.append(r["id"])
            else:
                conn.execute("UPDATE titles SET rel_folder = ? WHERE id = ?", (rel, r["id"]))
        for tid in stale:
            conn.execute("UPDATE files SET title_id = NULL, season_id = NULL WHERE title_id = ?",
                         (tid,))  # fmt: skip
            conn.execute("DELETE FROM seasons WHERE title_id = ?", (tid,))
            conn.execute("DELETE FROM flags WHERE title_id = ?", (tid,))
            conn.execute("DELETE FROM titles WHERE id = ?", (tid,))
        for r in conn.execute(
            "SELECT s.id, s.rel_folder, t.library_id FROM seasons s JOIN titles t "
            "ON t.id = s.title_id WHERE s.rel_folder IS NOT NULL"
        ).fetchall():
            rel = _rel(r["rel_folder"], roots.get(r["library_id"], "\0"))
            conn.execute("UPDATE seasons SET rel_folder = ? WHERE id = ?", (rel, r["id"]))

        # Issues: keys name the library by id. The flags table itself is rebuilt below.
        for r in conn.execute("SELECT key, fingerprint, code, created_at FROM flag_ignores"
                              ).fetchall():  # fmt: skip
            key = rewrite_key(r["key"], libs)
            fp = r["fingerprint"] or ""
            if r["code"] == "cross_library":
                fp = rewrite_cross_fp(fp, libs)
            if (key, fp) != (r["key"], r["fingerprint"] or ""):
                conn.execute("DELETE FROM flag_ignores WHERE key = ?", (r["key"],))
                conn.execute("INSERT OR REPLACE INTO flag_ignores (key, fingerprint, code, "
                             "created_at) VALUES (?, ?, ?, ?)",
                             (key, fp, r["code"], r["created_at"]))  # fmt: skip
        conn.execute("DELETE FROM flags")
        conn.execute("DELETE FROM meta WHERE key IN ('flags_version', 'flags_settings')")

        conn.execute(V_MEDIA)
        conn.execute(V_TITLES)

        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"foreign key check failed: {[tuple(p) for p in problems[:5]]}")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(version),))  # fmt: skip
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
