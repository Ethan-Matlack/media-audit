"""Change the configuration safely: the one way the CLI, remap and the web UI edit it.

config.yml stays the source of truth, so it can still be edited by hand. Every change:

1. is made on a round-trip copy of the file (ruamel.yaml keeps comments, order, quoting
   and layout; untouched files come back byte-for-byte);
2. is checked by loading the result exactly as media-audit will (environment overrides
   included) plus checks the schema can't do (folders exist, libraries don't overlap);
3. is returned as a :class:`Change` (old/new text and a diff) without writing anything,
   so a page can show it before saving;
4. is saved by :meth:`ConfigStore.save`: the old file is kept as ``config.yml.bak-<time>``,
   the new one is written atomically, and library renames are applied to the database
   so a renamed library keeps its history.

Settings that come from ``MEDIA_AUDIT_*`` environment variables (e.g. the Unraid
template's) can't be changed here; :meth:`ConfigStore.locked` names the variable so a UI
can show the setting as read-only.
"""

from __future__ import annotations

import difflib
import io
import os
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from media_audit.config import (
    ENV_PREFIX,
    NON_CONFIG_ENV,
    Config,
    ConfigError,
    load_config,
)
from media_audit.db import transaction


class SettingsError(ValueError):
    """A change that can't be made; the message says why, for a person to read."""


class LockedError(SettingsError):
    """The setting comes from an environment variable."""


def norm(path: str | os.PathLike[str]) -> str:
    s = str(path).strip()
    return s.rstrip("/") or "/"


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap long lines
    y.indent(mapping=2, sequence=4, offset=2)  # "libraries:\n  - name: x" (the usual style)
    return y


def yaml_text(data: Any) -> str:
    """``data`` as YAML in the config's style (quotes only where needed)."""
    out = io.StringIO()
    _yaml().dump(data, out)
    return out.getvalue().rstrip("\n")


@dataclass
class Change:
    path: Path
    old_text: str
    new_text: str
    summary: str
    renames: list[tuple[str, str]] = field(default_factory=list)  # libraries, old -> new

    @property
    def changed(self) -> bool:
        return self.new_text != self.old_text

    def diff(self, context: int = 1) -> list[str]:
        return [line for line in difflib.unified_diff(
            self.old_text.splitlines(), self.new_text.splitlines(), lineterm="", n=context)
            if not line.startswith(("---", "+++"))]  # fmt: skip


def env_key(name: str) -> str | None:
    """MEDIA_AUDIT_SCAN__WORKERS -> 'scan.workers' (None for non-config variables)."""
    if not name.startswith(ENV_PREFIX) or name in NON_CONFIG_ENV:
        return None
    return name[len(ENV_PREFIX) :].lower().replace("__", ".")


class ConfigStore:
    def __init__(self, path: Path | None, environ: Mapping[str, str] | None = None) -> None:
        if path is None:
            raise SettingsError("there's no config file to change")
        self.path = Path(path)
        self.environ = dict(os.environ if environ is None else environ)

    # ------------------------------------------------------------------ reading
    def config(self) -> Config:
        return load_config(self.path, environ=self.environ)

    def locked(self, key: str) -> str | None:
        """The environment variable that sets ``key`` (or a section containing it)."""
        for name, value in self.environ.items():
            dotted = env_key(name)
            if dotted and value != "" and (key == dotted or key.startswith(dotted + ".")
                                           or dotted.startswith(key + ".")):  # fmt: skip
                return name
        return None

    def _load(self) -> tuple[str, CommentedMap]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SettingsError(f"can't read {self.path}: {exc.strerror or exc}") from exc
        try:
            doc = _yaml().load(text)
        except Exception as exc:  # ruamel raises several error types
            raise SettingsError(f"{self.path.name} doesn't parse: {exc}") from exc
        if doc is None:
            doc = CommentedMap()
        if not isinstance(doc, CommentedMap):
            raise SettingsError(f"{self.path.name} isn't a YAML mapping")
        return text, doc

    def _finish(self, old_text: str, doc: CommentedMap, summary: str,
                check: Callable[[Config], None] | None = None,
                renames: list[tuple[str, str]] | None = None) -> Change:  # fmt: skip
        out = io.StringIO()
        _yaml().dump(doc, out)
        new_text = out.getvalue()
        if new_text != old_text:
            cfg = self._validate(new_text)
            if check is not None:
                check(cfg)
        return Change(self.path, old_text, new_text, summary, renames or [])

    def _validate(self, text: str) -> Config:
        with tempfile.NamedTemporaryFile("w", suffix=".yml", dir=self.path.parent,
                                         delete=False, encoding="utf-8") as tmp:  # fmt: skip
            tmp.write(text)
        try:
            return load_config(Path(tmp.name), environ=self.environ)
        except ConfigError as exc:
            raise SettingsError(str(exc)) from exc
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def _require_unlocked(self, key: str) -> None:
        var = self.locked(key)
        if var:
            raise LockedError(f"{key} is set by the environment variable {var}; change it "
                              "there (e.g. in the container's settings)")  # fmt: skip

    # ------------------------------------------------------------------ libraries
    def _libraries(self, doc: CommentedMap) -> CommentedSeq:
        libs = doc.get("libraries")
        if libs is None:
            libs = CommentedSeq()
            doc["libraries"] = libs
        if not isinstance(libs, CommentedSeq):
            raise SettingsError("libraries in the config isn't a list")
        if not libs:
            libs.fa.set_block_style()
        return libs

    @staticmethod
    def _find(libs: CommentedSeq, *, name: str | None = None,
              path: str | None = None) -> int | None:  # fmt: skip
        for i, item in enumerate(libs):
            if not isinstance(item, CommentedMap):
                continue
            if name is not None and item.get("name") == name:
                return i
            if path is not None and item.get("path") is not None and norm(item["path"]) == path:
                return i
        return None

    @staticmethod
    def _entry(libs: CommentedSeq, name: str, path: str, lib_type: str,
               enabled: bool) -> CommentedMap:  # fmt: skip
        entry = CommentedMap(name=name, path=path, type=lib_type)
        if not enabled:
            entry["enabled"] = False
        if libs and isinstance(libs[-1], CommentedMap) and libs[-1].fa.flow_style():
            entry.fa.set_flow_style()  # match "- {name: ..., path: ...}" entries
        return entry

    def _check_libraries(self, must_exist: set[str]) -> Callable[[Config], None]:
        def check(cfg: Config) -> None:
            enabled = [lib for lib in cfg.libraries if lib.enabled]
            for lib in cfg.libraries:
                if lib.name in must_exist and not Path(lib.path).is_dir():
                    raise SettingsError(f"{norm(lib.path)} isn't a folder media-audit can see")
            for i, a in enumerate(enabled):
                for b in enabled[i + 1 :]:
                    pa, pb = norm(a.path), norm(b.path)
                    if pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/"):
                        raise SettingsError(f"libraries {a.name!r} ({pa}) and {b.name!r} ({pb}) "
                                            "overlap")  # fmt: skip

        return check

    def add_library(self, name: str, path: str, lib_type: str, enabled: bool = True) -> Change:
        self._require_unlocked("libraries")
        name, path = name.strip(), norm(path)
        text, doc = self._load()
        libs = self._libraries(doc)
        if self._find(libs, name=name) is not None:
            raise SettingsError(f"there's already a library called {name!r}")
        libs.append(self._entry(libs, name, path, lib_type, enabled))
        return self._finish(text, doc, f"Add library {name} ({path})",
                            self._check_libraries({name}))  # fmt: skip

    def update_library(
        self,
        name: str,
        *,
        new_name: str | None = None,
        path: str | None = None,
        lib_type: str | None = None,
        enabled: bool | None = None,
    ) -> Change:
        self._require_unlocked("libraries")
        text, doc = self._load()
        libs = self._libraries(doc)
        i = self._find(libs, name=name)
        if i is None:
            raise SettingsError(f"there's no library called {name!r} in the config")
        item = libs[i]
        new_name = (new_name or name).strip()
        if new_name != name:
            if self._find(libs, name=new_name) is not None:
                raise SettingsError(f"there's already a library called {new_name!r}")
            item["name"] = new_name
        if path is not None and norm(path) != norm(item.get("path", "")):
            item["path"] = norm(path)
        if lib_type is not None and lib_type != item.get("type"):
            item["type"] = lib_type
        if enabled is not None and enabled != item.get("enabled", True):
            if enabled:
                item.pop("enabled", None)
            else:
                item["enabled"] = False
        what = [
            ("renamed to " + new_name, new_name != name),
            ("moved to " + norm(path or ""), path is not None),
            (f"type {lib_type}", lib_type is not None),
            ("enabled" if enabled else "disabled", enabled is not None),
        ]
        changes = [w for w, on in what if on]
        must = {new_name} if path is not None or enabled else set()
        return self._finish(text, doc, f"Library {name}: {', '.join(changes) or 'no change'}",
                            self._check_libraries(must),
                            [(name, new_name)] if new_name != name else [])  # fmt: skip

    def remove_library(self, name: str) -> Change:
        self._require_unlocked("libraries")
        text, doc = self._load()
        libs = self._libraries(doc)
        i = self._find(libs, name=name)
        if i is None:
            raise SettingsError(f"there's no library called {name!r} in the config")
        del libs[i]
        return self._finish(text, doc, f"Remove library {name} (its history stays, disabled)")

    def remap_library(self, old_name: str, new_name: str, new_path: str,
                      lib_type: str) -> Change:  # fmt: skip
        """Make the config say: library ``new_name`` is at ``new_path``.

        Covers every state the config can be in after a folder rename: the old entry still
        there (changed in place), an entry already added for the new folder (the old one
        is removed and the new one takes the name), or neither (an entry is added).
        """
        self._require_unlocked("libraries")
        new_path = norm(new_path)
        text, doc = self._load()
        libs = self._libraries(doc)
        old_i = self._find(libs, name=old_name)
        new_i = self._find(libs, path=new_path)
        if new_i is not None and new_i == old_i:
            new_i = None
        clash = self._find(libs, name=new_name)
        if clash is not None and clash not in (old_i, new_i):
            raise SettingsError(f"there's already a library called {new_name!r}")
        if old_i is not None and new_i is not None:
            libs[new_i]["name"] = new_name
            del libs[old_i]
        elif old_i is not None:
            libs[old_i]["path"] = new_path
            libs[old_i]["name"] = new_name
        elif new_i is not None:
            libs[new_i]["name"] = new_name
        else:
            libs.append(self._entry(libs, new_name, new_path, lib_type, True))
        # The folder itself is checked by the caller (remap.check_remap) against what
        # media-audit can see; here only the config's own consistency.
        return self._finish(text, doc, f"Library {old_name} -> {new_name} at {new_path}",
                            self._check_libraries(set()))  # fmt: skip

    # ------------------------------------------------------------------ folders and mappings
    def set_media_folders(self, folders: list[str]) -> Change:
        self._require_unlocked("media_folders")
        text, doc = self._load()
        seq = CommentedSeq(norm(f) for f in folders)
        seq.fa.set_flow_style()
        doc["media_folders"] = seq

        def check(cfg: Config) -> None:
            for f in cfg.media_folders:
                if not Path(f).is_dir():
                    raise SettingsError(f"{f} isn't a folder media-audit can see")

        return self._finish(text, doc, "Media folders: " + ", ".join(folders), check)

    def set_path_mappings(self, mappings: list[tuple[str, str]]) -> Change:
        """Replace the path mappings: (folder in the container, same folder on the server)."""
        self._require_unlocked("path_mappings")
        text, doc = self._load()
        seq = CommentedSeq()
        seq.fa.set_block_style()
        for container, host in mappings:
            seq.append(CommentedMap(container=norm(container), host=norm(host)))
        doc["path_mappings"] = seq

        def check(cfg: Config) -> None:
            seen = [m.container for m in cfg.path_mappings]
            dupes = {c for c in seen if seen.count(c) > 1}
            if dupes:
                raise SettingsError(f"{', '.join(sorted(dupes))} is mapped more than once")

        return self._finish(text, doc, f"Path mappings ({len(mappings)})", check)

    # ------------------------------------------------------------------ any single value
    def set_value(self, key: str, value: Any) -> Change:
        """Set one setting by its dotted name, e.g. ``scan.schedule`` or ``web.port``.
        ``None`` removes it (back to the default)."""
        self._require_unlocked(key)
        text, doc = self._load()
        parts = key.split(".")
        node: Any = doc
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                if value is None:
                    return Change(self.path, text, text, f"{key}: unchanged")
                child = CommentedMap()
                node[part] = child
            if not isinstance(child, CommentedMap):
                raise SettingsError(f"{part} in the config isn't a section")
            node = child
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
        return self._finish(text, doc, f"{key}: {value!r}")

    # ------------------------------------------------------------------ saving
    def save(self, change: Change, *, conn: sqlite3.Connection | None = None,
             now: datetime | None = None) -> Path | None:  # fmt: skip
        """Write ``change`` (and apply its library renames to the database).

        Fails without writing if the file changed since ``change`` was made. Returns the
        backup of the old file, or None if nothing changed.
        """
        if not change.changed:
            return None
        current = self.path.read_text(encoding="utf-8")
        if current != change.old_text:
            raise SettingsError(f"{self.path.name} changed since this change was prepared; "
                                "start again")  # fmt: skip
        if conn is not None:
            for old, new in change.renames:
                _check_db_rename(conn, old, new)
        now = now or datetime.now()
        backup = self.path.with_name(f"{self.path.name}.bak-{now:%Y%m%d-%H%M%S}")
        backup.write_text(change.old_text, encoding="utf-8")
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(change.new_text, encoding="utf-8")
        tmp.replace(self.path)
        if conn is not None and change.renames:
            try:
                with transaction(conn):
                    for old, new in change.renames:
                        _rename_in_db(conn, old, new)
            except Exception:
                self.path.write_text(change.old_text, encoding="utf-8")
                raise
        return backup


def _check_db_rename(conn: sqlite3.Connection, old: str, new: str) -> None:
    taken = conn.execute(
        "SELECT l.id, (SELECT count(*) FROM files f WHERE f.library_id = l.id) AS files "
        "FROM libraries l WHERE l.name = ?", (new,)).fetchone()  # fmt: skip
    if taken is not None and taken["files"]:
        raise SettingsError(f"media-audit already has history for a library called {new!r}; "
                            "remove or rename that one first")  # fmt: skip
    del old


def _rename_in_db(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Keep a renamed library's history: rename its row (an empty one by that name goes)."""
    conn.execute("DELETE FROM libraries WHERE name = ? AND NOT EXISTS "
                 "(SELECT 1 FROM files f WHERE f.library_id = libraries.id)", (new,))  # fmt: skip
    conn.execute("UPDATE libraries SET name = ? WHERE name = ?", (new, old))
