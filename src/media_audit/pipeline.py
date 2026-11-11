"""Scan orchestration: walk -> analyze -> derive -> titles -> sizes -> snapshot."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from media_audit import stats as stats_mod
from media_audit.analyzer import Analyzer, AnalyzerError
from media_audit.config import Config, LibraryConfig
from media_audit.db import transaction, utcnow
from media_audit.scanner import ScanError, sync_library
from media_audit.store import derive_pending, ensure_rules_version, invalidate_all
from media_audit.titles import link_titles, resolve_library

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressEvent:
    """Where a running scan is. ``done``/``total`` are set while analyzing."""

    phase: str  # walking | analyzing | deriving
    message: str
    library: str | None = None
    done: int | None = None
    total: int | None = None
    path: str | None = None


ProgressFn = Callable[[ProgressEvent], None]


class ScanLocked(Exception):
    pass


class ScanCancelled(Exception):
    """The scan was asked to stop (e.g. the container is shutting down)."""


@dataclass
class ScanSummary:
    scan_id: int
    status: str = "running"
    seen: int = 0
    new: int = 0
    changed: int = 0
    moved: int = 0
    deleted: int = 0
    analyzed: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


@contextmanager
def scan_lock(db_path: Path) -> Iterator[None]:
    """Process-level lock so a scheduled scan and a manual one can't overlap."""
    lock_path = Path(f"{db_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScanLocked(f"another scan is running (lock: {lock_path})") from exc
        yield
    finally:
        os.close(fd)


def sync_libraries(conn: sqlite3.Connection, config: Config, now: str) -> dict[str, int]:
    """Mirror configured libraries into the DB. Returns name -> id.

    Libraries removed from the config are disabled, not deleted, so their history stays.
    """
    with transaction(conn):
        for lib in config.libraries:
            conn.execute(
                "INSERT INTO libraries (name, path, type, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET path = excluded.path, "
                "type = excluded.type, enabled = excluded.enabled",
                (lib.name, str(lib.path), lib.type, int(lib.enabled), now),
            )
        names = [lib.name for lib in config.libraries]
        conn.execute(
            f"UPDATE libraries SET enabled = 0 WHERE name NOT IN ({','.join('?' * len(names))})",
            names,
        )
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM libraries")}


def _store_analysis(
    conn: sqlite3.Connection,
    file_id: int,
    raw: dict[str, Any] | None,
    analyzer: str,
    error: str | None,
    now: str,
) -> None:
    has_video = bool(raw) and any(
        t.get("@type") == "Video" for t in (raw or {}).get("media", {}).get("track", [])
    )
    if raw is not None and not has_video and error is None:
        error = "no video stream found"
    conn.execute(
        "UPDATE files SET analyzed_at = ?, analyzer = ?, analyze_error = ?, mediainfo_raw = ?, "
        "derived_at = NULL WHERE id = ?",
        (now, analyzer, error, json.dumps(raw, separators=(",", ":")) if raw else None, file_id),
    )


def analyze_pending(
    conn: sqlite3.Connection,
    analyzer: Analyzer,
    *,
    library_ids: list[int],
    workers: int,
    progress: ProgressFn | None = None,
    retry_errors: bool = False,
    cancel: threading.Event | None = None,
) -> tuple[int, list[str]]:
    """Run the analyzer on every live video file that hasn't been analyzed yet.

    When ``cancel`` is set, no new files are started; finished results are still saved
    and :class:`ScanCancelled` is raised. The next scan picks up the rest.
    """
    sql = (
        "SELECT id, path, size_bytes, mtime_ns FROM files WHERE deleted_at IS NULL "
        f"AND is_video = 1 AND library_id IN ({','.join('?' * len(library_ids))}) AND "
        + ("(analyzed_at IS NULL OR analyze_error IS NOT NULL)" if retry_errors
           else "analyzed_at IS NULL")
        + " ORDER BY path"
    )  # fmt: skip
    rows = conn.execute(sql, library_ids).fetchall()
    total = len(rows)
    if not total:
        return 0, []
    errors: list[str] = []
    done = 0
    pending_writes: list[tuple[int, dict[str, Any] | None, str | None]] = []
    last_commit = time.monotonic()

    def run(path: str) -> dict[str, Any]:
        return analyzer.analyze(path)

    def flush() -> None:
        nonlocal last_commit
        if not pending_writes:
            return
        now = utcnow()
        with transaction(conn):
            for fid, raw, err in pending_writes:
                _store_analysis(conn, fid, raw, analyzer.version(), err, now)
        pending_writes.clear()
        last_commit = time.monotonic()

    if progress:
        progress(ProgressEvent("analyzing", f"analyzing {total} files", done=0, total=total))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="analyze") as pool:
        it = iter(rows)
        in_flight: dict[Future[dict[str, Any]], sqlite3.Row] = {}

        def submit_next() -> None:
            if cancel is not None and cancel.is_set():
                return
            row = next(it, None)
            if row is not None:
                in_flight[pool.submit(run, row["path"])] = row

        for _ in range(workers * 2):  # bounded queue: don't stat/open everything at once
            submit_next()
        while in_flight:
            finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in finished:
                row = in_flight.pop(fut)
                submit_next()
                done += 1
                try:
                    raw: dict[str, Any] | None = fut.result()
                    err = None
                except AnalyzerError as exc:
                    raw, err = None, str(exc)
                except OSError as exc:
                    raw, err = None, f"read error: {exc}"
                # Skip files modified while being read; the next scan picks them up.
                try:
                    st = Path(row["path"]).stat()
                    if st.st_size != row["size_bytes"] or st.st_mtime_ns != row["mtime_ns"]:
                        log.info("changed during analysis, deferring: %s", row["path"])
                        continue
                except OSError:
                    continue
                if err:
                    errors.append(f"{row['path']}: {err}")
                pending_writes.append((row["id"], raw, err))
                if progress:
                    progress(ProgressEvent("analyzing", f"analyzed {done}/{total}: {row['path']}",
                                           done=done, total=total, path=row["path"]))  # fmt: skip
            if len(pending_writes) >= 25 or time.monotonic() - last_commit > 10:
                flush()
    flush()
    if cancel is not None and cancel.is_set():
        raise ScanCancelled(f"stopped after analyzing {done} of {total} files")
    return done, errors


def run_scan(
    conn: sqlite3.Connection,
    config: Config,
    *,
    analyzer: Analyzer | None,
    library_names: list[str] | None = None,
    allow_empty: bool = False,
    retry_errors: bool = False,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> ScanSummary:
    """Run one scan. Call it while holding :func:`scan_lock`."""
    started = time.monotonic()
    now = utcnow()
    ids = sync_libraries(conn, config, now)
    libs: list[LibraryConfig] = [
        lib
        for lib in config.libraries
        if lib.enabled and (library_names is None or lib.name in library_names)
    ]
    if library_names:
        unknown = set(library_names) - {lib.name for lib in config.libraries}
        if unknown:
            raise ScanError(f"unknown library: {', '.join(sorted(unknown))}")
    if not libs:
        raise ScanError("no enabled libraries to scan (check the config)")

    if ensure_rules_version(conn):
        log.info("derivation rules changed; re-deriving stored data")

    # We hold the scan lock, so any scan still marked running died without finishing
    # (container killed, power loss). Its work so far is kept; record how it ended.
    with transaction(conn):
        conn.execute(
            "UPDATE scans SET status = 'interrupted', finished_at = coalesce(finished_at, ?), "
            "message = 'did not finish (process stopped)' || coalesce(char(10) || message, '') "
            "WHERE status = 'running'",
            (now,),
        )
    cur = conn.execute(
        "INSERT INTO scans (started_at, libraries) VALUES (?, ?)",
        (now, ",".join(lib.name for lib in libs)),
    )
    summary = ScanSummary(scan_id=int(cur.lastrowid or 0))
    scanned_ids: list[int] = []
    try:
        for lib in libs:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("stopped while walking libraries")
            if progress:
                progress(ProgressEvent("walking", f"walking {lib.name}: {lib.path}",
                                       library=lib.name))  # fmt: skip
            try:
                s = sync_library(
                    conn, ids[lib.name], lib.path, ignore=config.scan.ignore,
                    follow_symlinks=config.scan.follow_symlinks, now=now, allow_empty=allow_empty,
                )  # fmt: skip
            except ScanError as exc:
                summary.errors.append(f"{lib.name}: {exc}")
                log.error("%s: %s", lib.name, exc)
                continue
            scanned_ids.append(ids[lib.name])
            summary.seen += s.seen
            summary.new += s.new
            summary.changed += s.changed
            summary.moved += s.moved
            summary.deleted += s.deleted
            summary.errors.extend(f"{lib.name}: {e}" for e in s.errors)

        if scanned_ids and analyzer is not None:
            analyzed, errs = analyze_pending(
                conn, analyzer, library_ids=scanned_ids, workers=config.scan.workers,
                progress=progress, retry_errors=retry_errors, cancel=cancel,
            )  # fmt: skip
            summary.analyzed = analyzed
            summary.errors.extend(errs)

        if scanned_ids:
            if progress:
                progress(ProgressEvent("deriving", "deriving fields and rebuilding titles"))
            derive_pending(conn, utcnow(), scanned_ids)
            for lib in libs:
                if ids[lib.name] in scanned_ids:
                    resolve_library(conn, ids[lib.name], str(lib.path), lib.type, utcnow())
            link_titles(conn, config.linking)
        summary.status = "ok" if len(scanned_ids) == len(libs) else "failed"
    except (KeyboardInterrupt, ScanCancelled) as exc:
        summary.status = "interrupted"
        if isinstance(exc, ScanCancelled):
            summary.errors.append(str(exc))
        raise
    except Exception as exc:
        summary.status = "failed"
        summary.errors.append(f"scan failed: {exc}")
        raise
    finally:
        summary.elapsed_s = round(time.monotonic() - started, 2)
        finished = utcnow()
        with transaction(conn):
            conn.execute(
                "UPDATE scans SET finished_at = ?, status = ?, files_seen = ?, files_new = ?, "
                "files_changed = ?, files_moved = ?, files_deleted = ?, files_analyzed = ?, "
                "errors = ?, message = ? WHERE id = ?",
                (finished, summary.status, summary.seen, summary.new, summary.changed,
                 summary.moved, summary.deleted, summary.analyzed, len(summary.errors),
                 "\n".join(summary.errors[:200]) or None, summary.scan_id),
            )  # fmt: skip
            if summary.status == "ok":
                conn.execute(
                    "INSERT INTO scan_snapshots (scan_id, created_at, data) VALUES (?, ?, ?)",
                    (summary.scan_id, finished,
                     json.dumps(stats_mod.snapshot(conn), separators=(",", ":"))),
                )  # fmt: skip
    return summary


def rederive(conn: sqlite3.Connection, config: Config) -> int:
    """Re-apply all rules to stored data without reading any media files."""
    now = utcnow()
    ids = sync_libraries(conn, config, now)
    invalidate_all(conn)
    count = derive_pending(conn, now)
    for lib in config.libraries:
        if lib.name in ids:
            resolve_library(conn, ids[lib.name], str(lib.path), lib.type, utcnow())
    link_titles(conn, config.linking)
    return count
