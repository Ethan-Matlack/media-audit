"""SQLite access: connections, transactions, and schema migrations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    pass


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str, *, migrate: bool = True) -> sqlite3.Connection:
    """Open the database in autocommit mode (use :func:`transaction` for writes).

    The database should live on local disk, not on the network mount with the media:
    SQLite's locking is unreliable over NFS/SMB.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    if migrate:
        apply_migrations(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migrations() -> list[tuple[int, str, str]]:
    found = []
    pkg = resources.files("media_audit.db.migrations")
    for entry in pkg.iterdir():
        m = _MIGRATION_NAME.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration numbers must be contiguous from 0001, got {versions}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def latest_version() -> int:
    migrations = _migrations()
    return migrations[-1][0] if migrations else 0


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns names applied."""
    current = schema_version(conn)
    migrations = _migrations()
    if current > (migrations[-1][0] if migrations else 0):
        raise MigrationError(
            f"database schema version {current} is newer than this build supports; "
            "upgrade media-audit"
        )
    applied = []
    for version, name, sql in migrations:
        if version <= current:
            continue
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n;\n"
            "INSERT INTO meta (key, value) VALUES ('schema_version', "
            f"'{version}') ON CONFLICT (key) DO UPDATE SET value = excluded.value;\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"migration {name} failed: {exc}") from exc
        applied.append(name)
    return applied


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
