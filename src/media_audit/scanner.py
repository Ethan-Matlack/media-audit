"""Walk library roots and sync what's on disk into the ``files`` table.

Only ``stat`` metadata is read here; file contents are never opened. Change
detection uses ``(size, mtime_ns)``. A file that disappears from one path and appears
at another with the same ``(inode, size, mtime_ns)`` is treated as a move (e.g. a
Radarr rename), keeping its analysis, so renames don't cause re-reads.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from media_audit import classify
from media_audit.db import transaction

log = logging.getLogger(__name__)

BATCH = 500


class ScanError(Exception):
    pass


@dataclass(frozen=True)
class FoundFile:
    path: str
    rel_path: str
    name: str
    size: int
    mtime_ns: int
    dev: int
    inode: int


@dataclass
class WalkResult:
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncStats:
    seen: int = 0
    new: int = 0
    changed: int = 0
    moved: int = 0
    deleted: int = 0
    revived: int = 0
    errors: list[str] = field(default_factory=list)


def _ignored(name: str, patterns: list[str]) -> bool:
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, p.casefold()) for p in patterns)


def walk(
    root: Path, ignore: list[str], follow_symlinks: bool, result: WalkResult
) -> Iterator[FoundFile]:
    """Yield every regular file under ``root`` (sorted, depth-first)."""
    root_str = str(root)
    stack = [root_str]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            result.errors.append(f"cannot list {current}: {exc.strerror or exc}")
            continue
        subdirs = []
        for entry in entries:
            if _ignored(entry.name, ignore):
                continue
            try:
                if entry.is_symlink() and not follow_symlinks:
                    continue
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    subdirs.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=follow_symlinks):
                    continue
                st = entry.stat(follow_symlinks=follow_symlinks)
            except OSError as exc:
                result.errors.append(f"cannot stat {entry.path}: {exc.strerror or exc}")
                continue
            rel = os.path.relpath(entry.path, root_str).replace(os.sep, "/")
            yield FoundFile(
                path=entry.path,
                rel_path=rel,
                name=entry.name,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                dev=st.st_dev,
                inode=st.st_ino,
            )
        # Reverse so the stack pops directories in sorted order.
        stack.extend(reversed(subdirs))


def _file_columns(f: FoundFile) -> dict[str, object]:
    ext = classify.extension(f.name)
    return {
        "path": f.path,
        "rel_path": f.rel_path,
        "filename": f.name,
        "ext": ext,
        "role": classify.classify_role(f.rel_path),
        "is_video": int(classify.is_video(ext)),
        "size_bytes": f.size,
        "mtime_ns": f.mtime_ns,
        "dev": f.dev,
        "inode": f.inode or None,
    }


def _clear_analysis(conn: sqlite3.Connection, file_id: int) -> None:
    for table in ("video_info", "audio_tracks", "release_info"):
        conn.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM subtitle_tracks WHERE file_id = ? AND is_external = 0", (file_id,))


def sync_library(
    conn: sqlite3.Connection,
    library_id: int,
    root: Path,
    *,
    ignore: list[str],
    follow_symlinks: bool,
    now: str,
    allow_empty: bool = False,
) -> SyncStats:
    """Walk ``root`` and reconcile the ``files`` table for one library."""
    stats = SyncStats()
    if not root.is_dir():
        raise ScanError(f"library root is not a directory (is the share mounted?): {root}")

    existing: dict[str, sqlite3.Row] = {
        row["path"]: row
        for row in conn.execute(
            "SELECT id, path, size_bytes, mtime_ns, inode, deleted_at FROM files "
            "WHERE library_id = ?",
            (library_id,),
        )
    }
    active_before = sum(1 for r in existing.values() if r["deleted_at"] is None)

    walk_result = WalkResult()
    seen_paths: set[str] = set()
    unchanged_ids: list[int] = []
    pending_new: list[FoundFile] = []

    def flush_unchanged() -> None:
        if unchanged_ids:
            conn.executemany(
                "UPDATE files SET last_seen = ? WHERE id = ?", [(now, i) for i in unchanged_ids]
            )
            unchanged_ids.clear()

    conn.execute("BEGIN IMMEDIATE")
    try:
        ops = 0
        for f in walk(root, ignore, follow_symlinks, walk_result):
            stats.seen += 1
            seen_paths.add(f.path)
            row = existing.get(f.path)
            if row is None:
                pending_new.append(f)
                continue
            same = row["size_bytes"] == f.size and row["mtime_ns"] == f.mtime_ns
            if same and row["deleted_at"] is None:
                unchanged_ids.append(row["id"])
                if len(unchanged_ids) >= BATCH:
                    flush_unchanged()
            else:
                cols = _file_columns(f)
                if row["deleted_at"] is not None:
                    stats.revived += 1
                if same:
                    conn.execute(
                        "UPDATE files SET deleted_at = NULL, last_seen = ?, dev = ?, inode = ? "
                        "WHERE id = ?",
                        (now, f.dev, f.inode or None, row["id"]),
                    )
                else:
                    stats.changed += 1
                    _clear_analysis(conn, row["id"])
                    conn.execute(
                        "UPDATE files SET size_bytes = ?, mtime_ns = ?, dev = ?, inode = ?, "
                        "role = ?, is_video = ?, last_seen = ?, changed_at = ?, "
                        "deleted_at = NULL, analyzed_at = NULL, analyzer = NULL, "
                        "analyze_error = NULL, derived_at = NULL, mediainfo_raw = NULL "
                        "WHERE id = ?",
                        (
                            f.size, f.mtime_ns, f.dev, f.inode or None, cols["role"],
                            cols["is_video"], now, now, row["id"],
                        ),
                    )  # fmt: skip
            ops += 1
            if ops % 5000 == 0:  # keep transactions bounded on huge libraries
                flush_unchanged()
                conn.execute("COMMIT")
                conn.execute("BEGIN IMMEDIATE")
        flush_unchanged()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    stats.errors.extend(walk_result.errors)

    missing = {
        path: row
        for path, row in existing.items()
        if row["deleted_at"] is None and path not in seen_paths
    }

    # Safety net: an unmounted share often looks like an empty directory. Never
    # mark a whole library deleted because of that.
    if stats.seen == 0 and active_before > 0 and not allow_empty:
        raise ScanError(
            f"{root} contains no files but the database has {active_before} for this "
            "library; refusing to mark them all deleted (is the share mounted?). "
            "Use --allow-empty to override."
        )

    by_identity: dict[tuple[int, int, int], sqlite3.Row] = {}
    for row in missing.values():
        if row["inode"]:
            by_identity.setdefault((row["inode"], row["size_bytes"], row["mtime_ns"]), row)

    with transaction(conn):
        for f in pending_new:
            cols = _file_columns(f)
            moved_from = by_identity.pop((f.inode, f.size, f.mtime_ns), None) if f.inode else None
            if moved_from is not None:
                stats.moved += 1
                missing.pop(moved_from["path"], None)
                conn.execute(
                    "UPDATE files SET path = ?, rel_path = ?, filename = ?, ext = ?, role = ?, "
                    "is_video = ?, dev = ?, last_seen = ?, derived_at = NULL, "
                    "filename_parsed = NULL, title_id = NULL, season_id = NULL WHERE id = ?",
                    (
                        cols["path"], cols["rel_path"], cols["filename"], cols["ext"],
                        cols["role"], cols["is_video"], cols["dev"], now, moved_from["id"],
                    ),
                )  # fmt: skip
                conn.execute("DELETE FROM release_info WHERE file_id = ?", (moved_from["id"],))
                continue
            stats.new += 1
            conn.execute(
                "INSERT INTO files (library_id, path, rel_path, filename, ext, role, is_video, "
                "size_bytes, mtime_ns, dev, inode, first_seen, last_seen, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    library_id, cols["path"], cols["rel_path"], cols["filename"], cols["ext"],
                    cols["role"], cols["is_video"], cols["size_bytes"], cols["mtime_ns"],
                    cols["dev"], cols["inode"], now, now, now,
                ),
            )  # fmt: skip

        for row in missing.values():
            stats.deleted += 1
            conn.execute("UPDATE files SET deleted_at = ? WHERE id = ?", (now, row["id"]))

    log.info(
        "library %s: %d seen, %d new, %d changed, %d moved, %d deleted, %d errors",
        root, stats.seen, stats.new, stats.changed, stats.moved, stats.deleted, len(stats.errors),
    )  # fmt: skip
    return stats
