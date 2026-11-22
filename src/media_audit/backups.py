"""Keep backups from piling up: only the newest few of each kind are kept.

Config saves (Settings page, CLI) keep the last 5 ``config.yml.bak-*``; the rarer
database backups (before a schema upgrade or a library remap) keep the last 2 of each.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_BACKUPS = 5
DB_BACKUPS = 2


def prune(folder: Path, pattern: str, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` files matching ``pattern`` in ``folder``.

    Newest by modification time (then name, for files written in the same instant).
    Returns what was deleted. Failures are logged, never raised: a backup that can't be
    removed must not break the save that made it.
    """
    try:
        files = [p for p in folder.glob(pattern) if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    removed = []
    for old in files[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError as exc:
            log.warning("couldn't remove old backup %s: %s", old, exc)
    return removed
