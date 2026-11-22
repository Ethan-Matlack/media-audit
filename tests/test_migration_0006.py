"""Migration 0006: absolute paths -> paths relative to the library."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from media_audit.db import apply_migrations, schema_version


def _old_db(path: Path) -> sqlite3.Connection:
    """A database at schema 5 (0.6.0), with absolute paths."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    pkg = resources.files("media_audit.db.migrations")
    old = {"0001", "0002", "0003", "0004", "0005"}
    for name in sorted(e.name for e in pkg.iterdir() if e.name[:4] in old):
        conn.executescript((pkg / name).read_text())
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '5') "
                 "ON CONFLICT (key) DO UPDATE SET value = '5'")  # fmt: skip
    conn.executescript("""
        INSERT INTO libraries (id, name, path, type, created_at) VALUES
            (1, 'tv', '/media/tv', 'tv', 'now'), (2, 'movies', '/media/movies', 'movies', 'now');
        INSERT INTO titles (id, library_id, kind, name, folder_path, updated_at) VALUES
            (1, 1, 'show', 'Show', '/media/tv/Show', 'now'),
            (2, 2, 'movie', 'Heat', '/media/movies/Heat (1995)', 'now'),
            (3, 2, 'movie', 'Stale', '/old/movies/Stale', 'now');
        INSERT INTO seasons (id, title_id, season_number, folder_path) VALUES
            (1, 1, 1, '/media/tv/Show/Season 01'), (2, 1, 2, NULL);
        INSERT INTO files (id, library_id, title_id, season_id, path, rel_path, filename, ext,
                           role, is_video, size_bytes, mtime_ns, first_seen, last_seen,
                           deleted_at) VALUES
            (10, 1, 1, 1, '/media/tv/Show/Season 01/e1.mkv', 'Show/Season 01/e1.mkv', 'e1.mkv',
             'mkv', 'main', 1, 100, 1, 'a', 'b', NULL),
            (11, 2, 2, NULL, '/media/movies/Heat (1995)/heat.mkv', 'Heat (1995)/heat.mkv',
             'heat.mkv', 'mkv', 'main', 1, 200, 1, 'a', 'z', NULL),
            -- the same file under the library's previous folder, left behind as deleted
            (12, 2, NULL, NULL, '/old/movies/Heat (1995)/heat.mkv', 'Heat (1995)/heat.mkv',
             'heat.mkv', 'mkv', 'main', 1, 200, 1, 'a', 'y', 'gone');
        INSERT INTO video_info (file_id, codec) VALUES (10, 'HEVC'), (11, 'AVC'), (12, 'AVC');
        INSERT INTO flag_ignores (key, fingerprint, code, created_at) VALUES
            ('duplicate_episode:tv:/media/tv/Show:S1E1', '10:100:1', 'duplicate_episode', 'x'),
            ('no_video_folder:movies:Old (1990)', '', 'no_video_folder', 'x'),
            ('cross_library:movies:/media/movies/Heat (1995)', 'tv:/media/tv/Show',
             'cross_library', 'x'),
            ('stray_file:10', '10:100:1', 'stray_file', 'x');
    """)
    return conn


def test_relative_paths_migration(tmp_path: Path) -> None:
    db = tmp_path / "media-audit.db"
    conn = _old_db(db)
    assert apply_migrations(conn) == ["0006_relative_paths.py"]
    assert schema_version(conn) == 6

    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)")]
    assert "path" not in cols and "rel_path" in cols
    # The live copy of the duplicated file survives with its analysis; the stale one goes.
    assert [r[0] for r in conn.execute("SELECT id FROM files ORDER BY id")] == [10, 11]
    assert [r[0] for r in conn.execute("SELECT file_id FROM video_info ORDER BY 1")] == [10, 11]

    titles = dict(conn.execute("SELECT id, rel_folder FROM titles").fetchall())
    assert titles == {1: "Show", 2: "Heat (1995)"}  # the stale title is dropped
    seasons = dict(conn.execute("SELECT id, rel_folder FROM seasons").fetchall())
    assert seasons == {1: "Show/Season 01", 2: None}

    ignores = {r["key"]: r["fingerprint"] for r in conn.execute("SELECT * FROM flag_ignores")}
    assert ignores == {
        "duplicate_episode:L1:Show:S1E1": "10:100:1",
        "no_video_folder:L2:Old (1990)": "",
        "cross_library:L2:Heat (1995)": "L1:Show",
        "stray_file:10": "10:100:1",
    }
    # The views still offer a full path, built from the library's folder.
    path = conn.execute("SELECT path FROM v_media WHERE file_id = 10").fetchone()[0]
    assert path == "/media/tv/Show/Season 01/e1.mkv"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert list(tmp_path.glob("media-audit.db.bak-0006-*"))  # backed up first
