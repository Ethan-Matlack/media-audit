"""Remap a library to a renamed or moved folder, keeping its history and analysis.

media-audit stores absolute paths (files, title and season folders), so a library whose
folder is renamed would otherwise look like a missing library plus a brand-new one,
and everything in it would be read again. Remapping rewrites the stored paths from the
old folder to the new one; the next scan then finds every file where it expects it and
only checks sizes and dates.

Three parts:

* :func:`suggestions` notices a library whose folder is gone and finds the folder that
  holds the same files (by folder names, then by the sizes of a sample of files);
* :func:`plan_config_edit` changes the library in config.yml with a targeted text edit
  that keeps comments and layout, and checks the result by loading it again;
* :func:`apply_remap` rewrites the database (backed up first).

:func:`rewrite_root` is also used by scans whenever a library's configured folder
changes, so editing the path by hand gets the same treatment.

Media files are never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from media_audit.config import Config, ConfigError, load_config
from media_audit.db import get_meta, set_meta, transaction

DISMISSED_KEY = "remap_dismissed"
SAMPLE_FILES = 60  # files whose sizes are compared; few, so array disks aren't all woken
MIN_FOLDER_SHARE = 0.5  # of the old library's title folders found in the candidate
MIN_FILE_SHARE = 0.8  # of the sampled files found there with the same size


def norm(path: str | os.PathLike[str]) -> str:
    """A folder path without a trailing slash ('/' stays '/')."""
    s = str(path)
    return s.rstrip("/") or "/"


# --------------------------------------------------------------------------- database


def rewrite_root(conn: sqlite3.Connection, library_id: int, old_root: str, new_root: str,
                 old_name: str | None = None, new_name: str | None = None) -> int:  # fmt: skip
    """Point stored paths under ``old_root`` at ``new_root``. Call inside a transaction.

    Rewrites files, title and season folders, and the Ignore list's keys (which name the
    library and, for some checks, a folder). Returns how many files moved.
    """
    old_root, new_root = norm(old_root), norm(new_root)
    if old_root == new_root and old_name == new_name:
        return 0
    n = len(old_root) + 1
    prefix = old_root + "/"
    moved = conn.execute(
        "UPDATE files SET path = ? || substr(path, ?) WHERE library_id = ? "
        "AND substr(path, 1, ?) = ?",
        (new_root, n, library_id, n, prefix),
    ).rowcount
    conn.execute(
        "UPDATE titles SET folder_path = ? || substr(folder_path, ?) WHERE library_id = ? "
        "AND substr(folder_path, 1, ?) = ?",
        (new_root, n, library_id, n, prefix),
    )
    conn.execute(
        "UPDATE seasons SET folder_path = ? || substr(folder_path, ?) "
        "WHERE title_id IN (SELECT id FROM titles WHERE library_id = ?) "
        "AND substr(folder_path, 1, ?) = ?",
        (new_root, n, library_id, n, prefix),
    )
    _rewrite_ignores(conn, old_root, new_root, old_name or "", new_name or old_name or "")
    return moved


def _rewrite_ignores(conn: sqlite3.Connection, old_root: str, new_root: str,
                     old_name: str, new_name: str) -> None:  # fmt: skip
    def fix(s: str, is_key: bool) -> str:
        if old_name and new_name != old_name:
            s = s.replace(f"{old_name}:{old_root}/", f"{new_name}:{new_root}/")
            if is_key:  # "<check>:<library>:<where>"
                parts = s.split(":", 2)
                if len(parts) == 3 and parts[1] == old_name:
                    s = f"{parts[0]}:{new_name}:{parts[2]}"
        return s.replace(old_root + "/", new_root + "/")

    rows = conn.execute("SELECT key, fingerprint, code, created_at FROM flag_ignores").fetchall()
    for r in rows:
        key, fp = fix(r["key"], True), fix(r["fingerprint"] or "", False)
        if (key, fp) == (r["key"], r["fingerprint"] or ""):
            continue
        conn.execute("DELETE FROM flag_ignores WHERE key = ?", (r["key"],))
        conn.execute(
            "INSERT OR REPLACE INTO flag_ignores (key, fingerprint, code, created_at) "
            "VALUES (?, ?, ?, ?)",
            (key, fp, r["code"], r["created_at"]),
        )


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
        files = rewrite_root(conn, old["id"], old["path"], new_path, old_name, new_name)
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
        parent = Path(old_path).parent
        try:
            with os.scandir(parent) as it:
                candidates |= {norm(e.path) for e in it if e.is_dir(follow_symlinks=False)
                               and not e.name.startswith(".")}  # fmt: skip
        except OSError:
            pass
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
                     "status": "ok" if Path(lib.path).is_dir() else "missing"})  # fmt: skip
    return rows


# --------------------------------------------------------------------------- config.yml


@dataclass
class ConfigEdit:
    path: Path | None
    old_text: str
    new_text: str | None  # None: couldn't be done safely; see ``snippet``
    changed: bool
    reason: str = ""
    snippet: str = ""


def _scalar(value: str) -> str:
    """``value`` as a YAML scalar that reads back as exactly that string."""
    if re.fullmatch(r"[\w./()\[\]@+-][\w ./()\[\]@+-]*", value) and value == value.strip():
        try:
            if yaml.safe_load(value) == value:
                return value
        except yaml.YAMLError:
            pass
    return json.dumps(value)  # double-quoted YAML


def _snippet(name: str, path: str, lib_type: str) -> str:
    return (f"libraries:\n  - name: {_scalar(name)}\n    path: {_scalar(path)}\n"
            f"    type: {lib_type}")  # fmt: skip


def _library_nodes(text: str) -> list[yaml.MappingNode] | None:
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        return None
    for key, value in root.value:
        if isinstance(key, yaml.ScalarNode) and key.value == "libraries":
            if not isinstance(value, yaml.SequenceNode):
                return None
            return [n for n in value.value if isinstance(n, yaml.MappingNode)]
    return None


def _field(node: yaml.MappingNode, name: str) -> yaml.ScalarNode | None:
    for key, value in node.value:
        if isinstance(key, yaml.ScalarNode) and key.value == name:
            return value if isinstance(value, yaml.ScalarNode) else None
    return None


def _last_line(node: yaml.Node, lines: list[str]) -> int:
    """The last line of a sequence item: PyYAML ends a block item where the next token
    starts, and trailing blank or comment lines belong to whatever follows."""
    end = node.end_mark
    line = end.line if lines[end.line : end.line + 1] and lines[end.line][: end.column].strip() \
        else end.line - 1  # fmt: skip
    line = min(line, len(lines) - 1)
    while line > node.start_mark.line and (not lines[line].strip()
                                           or lines[line].lstrip().startswith("#")):  # fmt: skip
        line -= 1
    return line


def plan_config_edit(
    config_path: Path | None,
    old_name: str,
    new_name: str,
    new_path: str,
    lib_type: str,
    environ: dict[str, str] | None = None,
) -> ConfigEdit:
    """Change config.yml so the library is ``new_name`` at ``new_path``, or explain why not.

    Edits only the values involved (comments and layout stay), removes a second entry
    for the same folder if one was added, and checks the result by loading it.
    """
    new_path = norm(new_path)
    snippet = _snippet(new_name, new_path, lib_type)

    def refuse(reason: str, text: str = "") -> ConfigEdit:
        return ConfigEdit(config_path, text, None, False, reason, snippet)

    if config_path is None or not config_path.is_file():
        return refuse("there's no config file to edit")
    text = config_path.read_text(encoding="utf-8")
    try:
        nodes = _library_nodes(text)
    except yaml.YAMLError as exc:
        return refuse(f"config.yml doesn't parse: {exc}", text)
    if nodes is None:
        return refuse("config.yml has no libraries list in a form that can be edited", text)

    by_name = next((n for n in nodes if (s := _field(n, "name")) and s.value == old_name), None)
    by_path = next((n for n in nodes if (s := _field(n, "path")) and norm(s.value) == new_path
                    and n is not by_name), None)  # fmt: skip
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)

    def set_value(node: yaml.MappingNode, name: str, value: str) -> bool:
        scalar = _field(node, name)
        if scalar is None:
            return False
        if scalar.value != value:
            edits.append((scalar.start_mark.index, scalar.end_mark.index, _scalar(value)))
        return True

    lines = text.splitlines(keepends=True)
    if by_name is not None and by_path is not None:
        # The new folder was added as another library: drop the old entry, rename the new.
        start = by_name.start_mark
        dash = lines[start.line][: start.column]
        end_line = _last_line(by_name, lines)
        if not re.fullmatch(r"\s*-\s*", dash) or any(
            re.match(r"\s*-", lines[i]) and not lines[i].lstrip().startswith("#")
            for i in range(start.line + 1, end_line + 1)
        ):
            return refuse("the old library's entry can't be removed automatically", text)
        line_start = sum(len(x) for x in lines[: start.line])
        line_end = sum(len(x) for x in lines[: end_line + 1])
        edits.append((line_start, line_end, ""))
        if not set_value(by_path, "name", new_name):
            return refuse("the new folder's entry has no name to change", text)
    elif by_name is not None:
        if not (set_value(by_name, "path", new_path) and set_value(by_name, "name", new_name)):
            return refuse("the library's entry has no name or path to change", text)
    elif by_path is not None:
        if not set_value(by_path, "name", new_name):
            return refuse("the new folder's entry has no name to change", text)
    else:
        # Neither is in the config: add an entry after the last library.
        if not nodes or nodes[-1].flow_style:
            return refuse("there's no library entry to add this one next to", text)
        last = nodes[-1]
        indent = " " * last.start_mark.column
        dash = lines[last.start_mark.line][: last.start_mark.column]
        if not re.fullmatch(r"\s*-\s*", dash):
            return refuse("the libraries list isn't in a form that can be extended", text)
        end_line = _last_line(last, lines)
        at = sum(len(x) for x in lines[: end_line + 1])
        lead = dash
        entry = (f"{lead}name: {_scalar(new_name)}\n{indent}path: {_scalar(new_path)}\n"
                 f"{indent}type: {lib_type}\n")  # fmt: skip
        if at and not text[:at].endswith("\n"):
            entry = "\n" + entry
        edits.append((at, at, entry))

    new_text = text
    for start_i, end_i, repl in sorted(edits, reverse=True):
        new_text = new_text[:start_i] + repl + new_text[end_i:]
    if new_text == text:
        return ConfigEdit(config_path, text, text, False)

    # Check the result the way media-audit will read it (environment overrides included).
    with tempfile.NamedTemporaryFile("w", suffix=".yml", dir=config_path.parent, delete=False,
                                     encoding="utf-8") as tmp:  # fmt: skip
        tmp.write(new_text)
    try:
        cfg = load_config(Path(tmp.name), environ=environ)
    except ConfigError as exc:
        return refuse(f"the edited config wouldn't load: {exc}", text)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    libs = {lib.name: norm(lib.path) for lib in cfg.libraries}
    if libs.get(new_name) != new_path or (old_name != new_name and old_name in libs) or \
            sum(1 for p in libs.values() if p == new_path) != 1:  # fmt: skip
        return refuse("libraries are also set elsewhere (e.g. MEDIA_AUDIT_LIBRARIES), so "
                      "editing config.yml wouldn't take effect", text)  # fmt: skip
    return ConfigEdit(config_path, text, new_text, True)


def write_config(edit: ConfigEdit, now: datetime) -> Path | None:
    """Save the edit (keeping a backup of the old file). Returns the backup's path."""
    if not edit.changed or edit.new_text is None or edit.path is None:
        return None
    backup = edit.path.with_name(f"{edit.path.name}.bak-{now.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(edit.old_text, encoding="utf-8")
    tmp = edit.path.with_name(edit.path.name + ".tmp")
    tmp.write_text(edit.new_text, encoding="utf-8")
    tmp.replace(edit.path)
    return backup
