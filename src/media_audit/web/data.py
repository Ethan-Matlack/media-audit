"""Read-only database access for the web UI: pooled connections and a page-data cache.

The data only changes when something writes to the database (a scan, ``rederive``),
so page data is computed once and reused until then. Changes are detected with
SQLite's ``PRAGMA data_version``, which catches writes from any process, including a
``docker exec ... media-audit scan``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")

POOL_SIZE = 4
CACHE_ENTRIES = 128
CACHE_KIB = 32 * 1024  # SQLite page cache per pooled connection
MMAP_BYTES = 256 * 1024 * 1024  # read through the OS page cache instead of read() calls


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(f"PRAGMA cache_size = -{CACHE_KIB}")
    conn.execute(f"PRAGMA mmap_size = {MMAP_BYTES}")
    return conn


class ReadDB:
    """A small pool of read-only connections plus a cache of computed page data."""

    def __init__(self, pool_size: int = POOL_SIZE, cache_entries: int = CACHE_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._idle: list[sqlite3.Connection] = []
        self._pool_size = pool_size
        self._watcher: sqlite3.Connection | None = None
        self._version: int | None = None
        self._cache: OrderedDict[Hashable, Any] = OrderedDict()
        self._cache_entries = cache_entries
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ connections

    def _switch(self, path: Path) -> None:
        """Called with the lock held when the database path changes (config edit)."""
        for conn in self._idle:
            conn.close()
        if self._watcher is not None:
            self._watcher.close()
        self._idle, self._watcher, self._version = [], None, None
        self._cache.clear()
        self._path = path

    @contextmanager
    def connection(self, path: Path | None) -> Iterator[sqlite3.Connection | None]:
        """A pooled read-only connection, or None when there's no database yet."""
        if path is None or not path.exists():
            yield None
            return
        with self._lock:
            if path != self._path:
                self._switch(path)
            conn = self._idle.pop() if self._idle else None
        if conn is None:
            conn = _open(path)
        try:
            yield conn
        finally:
            with self._lock:
                keep = path == self._path and len(self._idle) < self._pool_size
                if keep:
                    self._idle.append(conn)
            if not keep:
                conn.close()

    # ------------------------------------------------------------------ cache

    def _check_version(self) -> None:
        """Clear the cache if anything was written to the database since the last check.

        Called with the lock held. ``data_version`` changes whenever another connection
        commits, so a scan in progress keeps invalidating (pages show its progress).
        """
        if self._path is None:
            return
        try:
            if self._watcher is None:
                self._watcher = _open(self._path)
            version = int(self._watcher.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error as exc:
            log.debug("data_version check failed: %s", exc)
            self._cache.clear()
            return
        if version != self._version:
            self._version = version
            self._cache.clear()

    def cached(self, key: Hashable, compute: Callable[[], T]) -> T:
        """Return the cached value for ``key``, computing it if the data has changed."""
        with self._lock:
            self._check_version()
            if key in self._cache:
                self._cache.move_to_end(key)
                self.hits += 1
                return self._cache[key]  # type: ignore[no-any-return]
        value = compute()
        with self._lock:
            self.misses += 1
            self._cache[key] = value
            while len(self._cache) > self._cache_entries:
                self._cache.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def close(self) -> None:
        with self._lock:
            self._switch(Path())
            self._path = None
