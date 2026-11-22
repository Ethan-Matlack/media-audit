"""Remap a library to a renamed or moved folder, keeping its history and analysis.

Everything below a library is stored relative to its folder, but a renamed folder still
looks like a missing library to the scan (and a new folder like a new library, whose
files would all be read again). Remapping points the existing library at the new folder
(and name); the next scan finds every file where it expects it and only checks sizes and
dates.

Three parts:

* :func:`suggestions` notices a library whose folder is gone and finds the folder that
  holds the same files (by folder names, then by the sizes of a sample of files);
* :func:`plan_config_edit` changes the library in config.yml with a targeted text edit
  that keeps comments and layout, and checks the result by loading it again;
* :func:`apply_remap` rewrites the database (backed up first).

Media files are never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from media_audit.config import Config
from media_audit.db import get_meta, set_meta, transaction
from media_audit.settings import Change, ConfigStore, SettingsError, yaml_text

DISMISSED_KEY = "remap_dismissed"
SAMPLE_FILES = 60  # files whose sizes are compared; few, so array disks aren't all woken
MIN_FOLDER_SHARE = 0.5  # of the old library's title folders found in the candidate
MIN_FILE_SHARE = 0.8  # of the sampled files found there with the same size


def norm(path: str | os.PathLike[str]) -> str:
    """A folder path without a trailing slash ('/' stays '/')."""
    s = str(path)
    return s.rstrip("/") or "/"


# --------------------------------------------------------------------------- database


class RemapError(Exception):
    pass


@dataclass
class RemapResult:
    library: str
    old_path: str
    new_path: str
    files: int
    merged: list[str]  # libraries created for the new folder, merged into this one
    backup: Path | None


def backup_database(conn: sqlite3.Connection, db_path: Path, now: datetime) -> Path:
    target = db_path.with_name(f"{db_path.name}.bak-remap-{now.strftime('%Y%m%d-%H%M%S')}")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def check_remap(conn: sqlite3.Connection, old_name: str, new_path: str,
                new_name: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:  # fmt: skip
    """The library to remap, and the libraries to merge into it. Raises RemapError."""
    new_path = norm(new_path)
    old = conn.execute("SELECT * FROM libraries WHERE name = ?", (old_name,)).fetchone()
    if old is None:
        raise RemapError(f"there's no library called {old_name!r}")
    if not new_name.strip():
        raise RemapError("the library needs a name")
    if not new_path.startswith("/") or new_path == "/":
        raise RemapError("the new folder must be an absolute path, and not /")
    if not Path(new_path).is_dir():
        raise RemapError(f"{new_path} isn't a folder that media-audit can see")
    if new_path == norm(old["path"]) and new_name == old_name:
        raise RemapError("that's already this library's folder and name")
    merge, others = [], conn.execute("SELECT * FROM libraries WHERE id != ?", (old["id"],))
    for lib in others:
        path = norm(lib["path"])
        if path == new_path:
            merge.append(lib)  # scanned as a new library: its rows give way to the history
        elif lib["name"] == new_name:
            raise RemapError(f"another library is already called {new_name!r}")
        elif lib["enabled"] and (
            new_path.startswith(path + "/") or path.startswith(new_path + "/")
        ):
            raise RemapError(f"{new_path} overlaps library {lib['name']!r} ({path})")
    return old, merge


def apply_remap(
    conn: sqlite3.Connection,
    db_path: Path | None,
    old_name: str,
    new_path: str,
    new_name: str | None = None,
    *,
    now: datetime | None = None,
) -> RemapResult:
    """Rename/move a library in the database. Hold the scan lock while calling this."""
    new_name = (new_name or old_name).strip()
    new_path = norm(new_path)
    old, merge = check_remap(conn, old_name, new_path, new_name)
    now = now or datetime.now()
    backup = backup_database(conn, db_path, now) if db_path and str(db_path) != ":memory:" else None
    with transaction(conn):
        for lib in merge:
            conn.execute("DELETE FROM libraries WHERE id = ?", (lib["id"],))
        # Stored paths are relative to the library and Issues keys use its id, so moving
        # or renaming it is this one row.
        files = conn.execute(
            "SELECT count(*) FROM files WHERE library_id = ? AND deleted_at IS NULL", (old["id"],)
        ).fetchone()[0]
        conn.execute("UPDATE libraries SET name = ?, path = ?, enabled = 1 WHERE id = ?",
                     (new_name, new_path, old["id"]))  # fmt: skip
        dismissed = [d for d in _dismissed(conn) if not d.startswith(f"{old_name}|")]
        set_meta(conn, DISMISSED_KEY, json.dumps(dismissed))
    return RemapResult(new_name, norm(old["path"]), new_path, files,
                       [m["name"] for m in merge], backup)  # fmt: skip


# --------------------------------------------------------------------------- detection


@dataclass
class Comparison:
    """How much of a library's last-known contents is in a folder."""

    path: str
    folders_total: int
    folders_matched: int
    files_checked: int
    files_matched: int
    missing: list[str] = field(default_factory=list)  # title folders not found there
    extra: list[str] = field(default_factory=list)  # folders there that weren't before

    @property
    def folder_share(self) -> float:
        return self.folders_matched / self.folders_total if self.folders_total else 0.0

    @property
    def file_share(self) -> float:
        return self.files_matched / self.files_checked if self.files_checked else 0.0

    @property
    def likely(self) -> bool:
        return self.folder_share >= MIN_FOLDER_SHARE and self.file_share >= MIN_FILE_SHARE


@dataclass
class Suggestion:
    library: str
    old_path: str
    new_path: str
    name: str  # suggested name: the configured name of the new folder, else the old name
    comparison: Comparison


def _top_folders(conn: sqlite3.Connection, library_id: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT substr(rel_path, 1, instr(rel_path, '/') - 1) FROM files "
        "WHERE library_id = ? AND deleted_at IS NULL AND instr(rel_path, '/') > 0",
        (library_id,))}  # fmt: skip


def compare(conn: sqlite3.Connection, library_id: int, folder: str) -> Comparison:
    folder = norm(folder)
    before = _top_folders(conn, library_id)
    try:
        with os.scandir(folder) as it:
            now = {e.name for e in it if e.is_dir(follow_symlinks=False)
                   and not e.name.startswith(".")}  # fmt: skip
    except OSError:
        now = set()
    matched = before & now
    total = conn.execute("SELECT count(*) FROM files WHERE library_id = ? AND deleted_at IS NULL",
                         (library_id,)).fetchone()[0]  # fmt: skip
    step = max(1, total // SAMPLE_FILES)
    sample = conn.execute(
        "SELECT rel_path, size_bytes FROM (SELECT rel_path, size_bytes, row_number() OVER "
        "(ORDER BY rel_path) AS i FROM files WHERE library_id = ? AND deleted_at IS NULL) "
        "WHERE (i - 1) % ? = 0 LIMIT ?",
        (library_id, step, SAMPLE_FILES),
    ).fetchall()
    found = 0
    for r in sample:
        with contextlib.suppress(OSError):
            found += (Path(folder) / r["rel_path"]).stat().st_size == r["size_bytes"]
    return Comparison(folder, len(before), len(matched), len(sample), found,
                      sorted(before - now)[:10], sorted(now - before)[:10])  # fmt: skip


def _dismissed(conn: sqlite3.Connection) -> list[str]:
    try:
        return list(json.loads(get_meta(conn, DISMISSED_KEY) or "[]"))
    except ValueError:
        return []


def dismiss(conn: sqlite3.Connection, library: str, new_path: str) -> None:
    with transaction(conn):
        items = _dismissed(conn)
        items.append(f"{library}|{norm(new_path)}")
        set_meta(conn, DISMISSED_KEY, json.dumps(sorted(set(items))))


def _subfolders(folder: str) -> list[str]:
    try:
        with os.scandir(folder) as it:
            return [norm(e.path) for e in it if e.is_dir(follow_symlinks=False)
                    and not e.name.startswith(".")]  # fmt: skip
    except OSError:
        return []


def missing_libraries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Libraries with files on record whose folder is gone."""
    rows = conn.execute(
        "SELECT l.*, (SELECT count(*) FROM files f WHERE f.library_id = l.id "
        "AND f.deleted_at IS NULL) AS files FROM libraries l ORDER BY l.name"
    ).fetchall()
    return [r for r in rows if r["files"] and not Path(r["path"]).is_dir()]


def suggestions(conn: sqlite3.Connection, config: Config) -> list[Suggestion]:
    """For each library whose folder is gone, the folder that most likely replaced it."""
    out = []
    dismissed = set(_dismissed(conn))
    configured = {norm(lib.path): lib.name for lib in config.libraries}
    for lib in missing_libraries(conn):
        old_path = norm(lib["path"])
        candidates = {p for p in configured if p != old_path and Path(p).is_dir()}
        # Beside the old folder, and anywhere in the media folders (two levels down, for
        # layouts like /media/content2/movies where each content folder is a mount).
        candidates |= set(_subfolders(str(Path(old_path).parent)))
        for top in config.media_folders:
            for sub in _subfolders(top):
                candidates.add(sub)
                candidates |= set(_subfolders(sub))
        best: Comparison | None = None
        for path in sorted(candidates - {old_path}):
            if f"{lib['name']}|{path}" in dismissed:
                continue
            c = compare(conn, lib["id"], path)
            if c.likely and (best is None or (c.file_share, c.folder_share)
                             > (best.file_share, best.folder_share)):  # fmt: skip
                best = c
        if best is not None:
            out.append(Suggestion(lib["name"], old_path, best.path,
                                  configured.get(best.path, lib["name"]), best))  # fmt: skip
    return out


def library_rows(conn: sqlite3.Connection, config: Config) -> list[dict[str, Any]]:
    """Every library media-audit knows, with where it stands, for the Scans page."""
    configured = {lib.name: lib for lib in config.libraries}
    rows = []
    for r in conn.execute(
        "SELECT l.id, l.name, l.path, l.type, l.enabled, count(f.id) AS files, "
        "coalesce(sum(f.size_bytes), 0) AS bytes FROM libraries l "
        "LEFT JOIN files f ON f.library_id = l.id AND f.deleted_at IS NULL "
        "GROUP BY l.id ORDER BY l.enabled DESC, l.name"
    ):
        row = dict(r)
        row["host_path"] = config.host_path(r["path"])
        cfg = configured.pop(r["name"], None)
        if not Path(r["path"]).is_dir():
            row["status"] = "missing"
        elif cfg is None or not cfg.enabled:
            row["status"] = "not in config" if cfg is None else "disabled"
        else:
            row["status"] = "ok"
        rows.append(row)
    for lib in configured.values():  # in the config but never scanned
        rows.append({"id": None, "name": lib.name, "path": norm(lib.path), "type": lib.type,
                     "enabled": lib.enabled, "files": 0, "bytes": 0,
                     "host_path": config.host_path(lib.path),
                     "status": "ok" if Path(lib.path).is_dir() else "missing"})  # fmt: skip
    return rows


# --------------------------------------------------------------------------- config.yml


@dataclass
class ConfigEdit:
    path: Path | None
    old_text: str
    new_text: str | None  # None: can't be done; see ``reason`` and ``snippet``
    changed: bool
    reason: str = ""
    snippet: str = ""
    change: Change | None = None


def _snippet(name: str, path: str, lib_type: str) -> str:
    return yaml_text({"libraries": [{"name": name, "path": path, "type": lib_type}]})


def plan_config_edit(
    config_path: Path | None,
    old_name: str,
    new_name: str,
    new_path: str,
    lib_type: str,
    environ: dict[str, str] | None = None,
) -> ConfigEdit:
    """The config.yml change that makes the library ``new_name`` at ``new_path``, or why
    it can't be made (with the lines to change by hand)."""
    snippet = _snippet(new_name, norm(new_path), lib_type)
    try:
        store = ConfigStore(config_path, environ)
        change = store.remap_library(old_name, new_name, new_path, lib_type)
    except SettingsError as exc:
        old = (
            config_path.read_text(encoding="utf-8") if config_path and config_path.is_file() else ""
        )
        return ConfigEdit(config_path, old, None, False, str(exc), snippet)
    return ConfigEdit(config_path, change.old_text, change.new_text, change.changed, "",
                      snippet, change)  # fmt: skip


def write_config(edit: ConfigEdit, now: datetime) -> Path | None:
    """Save the edit, keeping a backup of the old file. Returns the backup's path."""
    if edit.change is None or edit.path is None or not edit.changed:
        return None
    return ConfigStore(edit.path).save(edit.change, now=now)
