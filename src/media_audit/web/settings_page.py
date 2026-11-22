"""The Settings page: everything in config.yml, changed from the browser.

Every change goes through :class:`media_audit.settings.ConfigStore` (checked, backed up,
environment-locked settings refused), so the page, the CLI and hand edits stay in step.
The running service re-reads config.yml when it changes; this page also refreshes the
schedule straight away and recomputes issues when their settings change.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from media_audit import mounts, remap
from media_audit.config import Config, ConfigError
from media_audit.db import connect
from media_audit.init_config import guess_type
from media_audit.rules.flags import CHECKS
from media_audit.settings import ConfigStore, SettingsError
from media_audit.web import settings_form as sf
from media_audit.web.service import ScanService

log = logging.getLogger(__name__)

LIST_KEYS = ("libraries", "media_folders", "path_mappings")


def _norm(path: str) -> str:
    return path.strip().rstrip("/") or "/"


def found_folders(cfg: Config, limit: int = 40) -> list[dict[str, Any]]:
    """Folders in the media folders (and mounts under them) that aren't libraries yet."""
    libs = [_norm(str(lib.path)) for lib in cfg.libraries]
    tops = list(dict.fromkeys([*cfg.media_folders, *mounts.mounts_under(cfg.media_folders)]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for top in tops:
        try:
            entries = sorted(os.scandir(top), key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False) or e.name.startswith((".", "@", "#", "$")):
                continue
            path = _norm(e.path)
            if path in seen or path in tops:
                continue
            seen.add(path)
            # Skip folders that are a library, inside one, or hold one.
            if any(path == p or path.startswith(p + "/") or p.startswith(path + "/")
                   for p in libs):  # fmt: skip
                continue
            out.append({"name": e.name, "path": path, "type": guess_type(e.name)})
    out.sort(key=lambda f: (f["type"] is None, f["path"].lower()))
    return out[:limit]


def browse(cfg: Config, path: str | None) -> dict[str, Any]:
    """Subfolders of ``path``, which must be inside one of the media folders."""
    roots = [os.path.realpath(f) for f in cfg.media_folders]
    if not path:
        folders = [{"name": f, "path": f, "type": None} for f in cfg.media_folders
                   if Path(f).is_dir()]  # fmt: skip
        return {"path": None, "parent": None, "folders": folders}
    real = os.path.realpath(path)
    if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots):
        raise SettingsError("only folders inside the media folders can be browsed")
    try:
        entries = sorted(os.scandir(real), key=lambda e: e.name.lower())
    except OSError as exc:
        raise SettingsError(f"can't open {path}: {exc.strerror or exc}") from exc
    libs = {_norm(str(lib.path)): lib.name for lib in cfg.libraries}
    folders = []
    for e in entries:
        if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
            child = _norm(str(Path(path) / e.name))
            folders.append({"name": e.name, "path": child, "type": guess_type(e.name),
                            "library": libs.get(child)})  # fmt: skip
    parent = (
        None if _norm(path) in [_norm(f) for f in cfg.media_folders] else str(Path(path).parent)
    )
    return {"path": _norm(path), "parent": parent, "folders": folders}


def _file_locations(config_path: Path | None, cfg: Config) -> list[dict[str, str | None]]:
    """The config file and database as full paths inside the container, and on the server
    when the app-data folder's server path (cleanup.host_config_path) is known."""
    host_root = cfg.cleanup.host_config_path
    out = []
    for label, path in (("Config file", config_path), ("Database", cfg.database)):
        if path is None:
            continue
        full = Path(path).absolute()  # not resolve(): keep the path as mounted, not symlink targets
        host = None
        if host_root:
            try:
                host = f"{host_root}/{full.relative_to('/config')}"
            except ValueError:
                host = None
        out.append({"label": label, "path": str(full), "host": host})
    return out


def settings_routes(
    service: ScanService,
    db: Any,
    render: Callable[..., Response],
    same_origin: Callable[[Request], bool],
    db_path: Callable[[], Path | None],
) -> list[Route]:
    def store() -> ConfigStore:
        return ConfigStore(service.config_path)

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        cfg = service.config()
        st = store()
        keys = [f.key for s in sf.SECTIONS for f in s.fields] + list(LIST_KEYS)
        locked = {k: v for k in keys if (v := st.locked(k))}
        form: dict[str, list[str]] = extra.pop("form", {})
        failed: str | None = extra.get("error_section")

        def value(f: sf.Field) -> Any:
            if failed and form and any(f in s.fields for s in sf.SECTIONS if s.key == failed):
                vals = form.get(f.key, [])
                if f.kind == "bool":
                    return (vals[-1] if vals else "") in {"1", "on"}
                if f.kind == "checks":
                    return None
                return vals[-1] if vals else ""
            return sf.display(cfg, f)

        with db.connection(db_path()) as conn:
            libraries = remap.library_rows(conn, cfg) if conn else []
        if not libraries:
            libraries = [
                {"id": None, "name": lib.name, "path": str(lib.path), "type": lib.type,
                 "enabled": lib.enabled, "files": 0, "bytes": 0, "status": "ok",
                 "host_path": cfg.host_path(lib.path)}
                for lib in cfg.libraries
            ]  # fmt: skip
        configured = {lib.name for lib in cfg.libraries}
        for lib in libraries:
            lib["configured"] = lib["name"] in configured
            lib["off"] = set(cfg.flags.disabled_by_library.get(lib["name"], []))
        explicit = [(m.container, m.host) for m in cfg.path_mappings]
        legacy = cfg.cleanup.legacy_mapping()
        covered = [m.container for m in cfg.mappings()]
        suggested = [p for p in mounts.mounts_under(cfg.media_folders)
                     if not any(p == c or p.startswith(c + "/") for c in covered)]  # fmt: skip
        q = request.query_params
        return {
            "cfg": cfg, "sections": sf.SECTIONS, "value": value, "locked": locked,
            "check_groups": sf.check_groups(cfg), "all_checks": list(CHECKS.values()),
            "libraries": libraries,
            "found": found_folders(cfg), "mappings": explicit,
            # The mapping from cleanup.host_media_path: shown as a locked row, unless a
            # mapping in the list for the same folder takes its place.
            "legacy_mapping": legacy if legacy and all(c != legacy.container
                                                       for c, _ in explicit) else None,
            "legacy_source": st.locked("cleanup.host_media_path"),
            "suggested_mounts": suggested, "in_docker": Path("/.dockerenv").exists(),
            "saved": q.get("saved"), "message": q.get("msg"), "backup": q.get("backup"),
            "tabs": sf.TABS, "tab_of": sf.TAB_OF,
            "active_tab": sf.tab_for(extra.get("error_section"), q.get("tab"), q.get("saved")),
            "rescan": q.get("rescan") == "1",
            "restart": q.get("restart") == "1",
            "files": _file_locations(service.config_path, cfg),
            **extra,
        }  # fmt: skip

    def page(request: Request, status_code: int = 200, **extra: Any) -> Response:
        try:
            ctx = context(request, **extra)
        except (ConfigError, SettingsError) as exc:
            return render(request, "settings.html", status_code=status_code,
                          broken=str(exc))  # fmt: skip
        with db.connection(db_path()) as conn:
            return render(request, "settings.html", status_code=status_code, _conn=conn, **ctx)

    async def settings_get(request: Request) -> Response:
        return await run_in_threadpool(page, request)

    async def browse_get(request: Request) -> Response:
        try:
            data = await run_in_threadpool(browse, service.config(),
                                           request.query_params.get("path"))  # fmt: skip
        except (SettingsError, ConfigError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(data)

    def after_save(keys: set[str]) -> None:
        """Bring the running service in line with the saved config."""
        db.clear()
        try:
            service.tick()  # the schedule shows the new next run straight away
        except Exception:
            log.exception("schedule refresh after a settings change failed")
        if not keys & {"flags", "languages", "linking"} or service.running:
            return
        from media_audit.flags import compute_flags
        from media_audit.pipeline import ScanLocked, scan_lock
        from media_audit.titles import link_titles

        try:
            cfg = service.config()
            with scan_lock(cfg.database):
                conn = connect(cfg.database)
                try:
                    if "linking" in keys:
                        link_titles(conn, cfg.linking)
                    compute_flags(conn, cfg)
                finally:
                    conn.close()
        except ScanLocked:
            pass  # a scan is starting; it recomputes issues itself
        except Exception:
            log.exception("recomputing issues after a settings change failed")
        db.clear()

    def done(anchor: str, message: str, backup: Path | None, *, rescan: bool = False,
             restart: bool = False) -> Response:  # fmt: skip
        params = {"tab": sf.tab_for(anchor), "saved": anchor, "msg": message}
        if backup is not None:
            params["backup"] = backup.name
        if rescan:
            params["rescan"] = "1"
        if restart:
            params["restart"] = "1"
        return RedirectResponse(f"/settings?{urlencode(params)}#{anchor}", status_code=303)

    def save(change: Any) -> Path | None:
        path = db_path()
        conn: sqlite3.Connection | None = None
        if change.renames and path is not None and path.exists():
            conn = connect(path, migrate=False)
        try:
            return store().save(change, conn=conn)
        finally:
            if conn is not None:
                conn.close()

    async def read_form(request: Request) -> dict[str, list[str]]:
        body = (await request.body()).decode("utf-8", "replace")
        return parse_qs(body, keep_blank_values=True)

    # ------------------------------------------------------------------ sections
    def _section_post(request: Request, key: str, form: dict[str, list[str]]) -> Response:
        section = sf.SECTION_KEYS.get(key)
        if section is None:
            return PlainTextResponse("no such section", status_code=404)
        try:
            cfg = service.config()
            st = store()
            locked = {f.key for f in section.fields if st.locked(f.key)}
            changes = sf.parse_section(section, form, cfg, locked)
            if not changes:
                return done(key, "Nothing changed.", None)
            change = st.set_values(changes, f"{section.title}: saved")
            backup = save(change)
        except (sf.FormError, SettingsError, ConfigError) as exc:
            return page(request, 400, error_section=key, error=str(exc), form=form)
        after_save({k.split(".")[0] for k in changes})
        labels = [f.label for f in section.fields if f.key in changes]
        restart = any(f.restart for f in section.fields if f.key in changes)
        return done(key, f"Saved: {', '.join(labels)}.", backup, restart=restart)

    async def section_post(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("cross-origin request refused", status_code=403)
        form = await read_form(request)
        return await run_in_threadpool(_section_post, request, request.path_params["key"], form)

    # ------------------------------------------------------------------ libraries
    def _library_post(request: Request, form: dict[str, list[str]]) -> Response:
        def one(name: str) -> str:
            vals = form.get(name, [])
            return vals[-1].strip() if vals else ""

        action, name = one("action"), one("name")
        try:
            st = store()
            if action == "add":
                path, lib_type = _norm(one("path")) if one("path") else "", one("type")
                if not name or not path or lib_type not in {"movies", "tv"}:
                    raise SettingsError("a library needs a name, a folder and a type")
                backup = save(st.add_library(name, path, lib_type))
                after_save({"libraries"})
                return done("libraries", f"Added library {name}. Scan to read it.", backup,
                            rescan=True)  # fmt: skip
            if action == "remove":
                if one("confirm") != "1":
                    raise SettingsError(f"tick the box to confirm removing {name}")
                backup = save(st.remove_library(name))
                after_save({"libraries"})
                return done("libraries", f"Removed library {name}. Its history is kept but "
                            "hidden; add it again to bring it back.", backup)  # fmt: skip
            if action == "update":
                new_name = one("new_name") or name
                new_path: str | None = _norm(one("path")) if one("path") else None
                new_type: str | None = one("type") or None
                enabled = one("enabled") in {"1", "on"}
                off = [c for c in form.get("off", []) if c in CHECKS]
                change = st.update_library(name, new_name=new_name, path=new_path,
                                           lib_type=new_type, enabled=enabled)  # fmt: skip
                cfg = service.config()
                per_lib = {k: list(v) for k, v in cfg.flags.disabled_by_library.items()
                           if k not in {name, new_name}}  # fmt: skip
                if off:
                    per_lib[new_name] = off
                if per_lib != cfg.flags.disabled_by_library:
                    # One change for both: apply the override on top of the library edit.
                    change = _chain(st, change, lambda s: s.set_values(
                        {"flags.disabled_by_library": per_lib or None}))  # fmt: skip
                if not change.changed:
                    return done("libraries", "Nothing changed.", None)
                backup = save(change)
                after_save({"libraries", "flags"})
                moved = new_path is not None and new_path != _norm(
                    next((str(lib.path) for lib in cfg.libraries if lib.name == name), "")
                )
                return done("libraries", f"Saved library {new_name}."
                            + (" Scan to check it at its new folder." if moved else ""),
                            backup, rescan=moved)  # fmt: skip
        except (SettingsError, ConfigError) as exc:
            return page(request, 400, error_section="libraries", error=str(exc),
                        library_form=form, open_library=name)  # fmt: skip
        return PlainTextResponse("unknown action", status_code=400)

    async def library_post(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("cross-origin request refused", status_code=403)
        form = await read_form(request)
        return await run_in_threadpool(_library_post, request, form)

    # ------------------------------------------------------------------ folders + mappings
    def _folders_post(request: Request, form: dict[str, list[str]]) -> Response:
        folders = [_norm(f) for f in (form.get("media_folders", [""])[-1]).splitlines()
                   if f.strip()]  # fmt: skip
        pairs = list(zip(form.get("map_container", []), form.get("map_host", []), strict=False))
        mappings = [(_norm(c), _norm(h)) for c, h in pairs if c.strip() or h.strip()]
        try:
            for c, h in mappings:
                if not c.strip("/") or not h.strip("/"):
                    raise SettingsError("each path mapping needs both folders")
            st = store()
            cfg = service.config()
            backup = None
            if not st.locked("media_folders") and folders != cfg.media_folders:
                if not folders:
                    raise SettingsError("keep at least one media folder")
                backup = save(st.set_media_folders(folders))
            current = [(m.container, m.host) for m in cfg.path_mappings]
            if not st.locked("path_mappings") and mappings != current:
                backup = save(st.set_path_mappings(mappings)) or backup
        except (SettingsError, ConfigError) as exc:
            return page(request, 400, error_section="folders", error=str(exc))
        after_save({"media_folders"})
        return done("folders", "Saved media folders and path mappings."
                    if backup else "Nothing changed.", backup)  # fmt: skip

    async def folders_post(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("cross-origin request refused", status_code=403)
        form = await read_form(request)
        return await run_in_threadpool(_folders_post, request, form)

    return [
        Route("/settings", settings_get),
        Route("/settings/browse", browse_get),
        Route("/settings/section/{key}", section_post, methods=["POST"]),
        Route("/settings/libraries", library_post, methods=["POST"]),
        Route("/settings/folders", folders_post, methods=["POST"]),
    ]


def _chain(store: ConfigStore, first: Any, then: Callable[[ConfigStore], Any]) -> Any:
    """Apply ``then`` on top of ``first`` (not yet saved) and return one combined change."""
    import tempfile

    from media_audit.settings import Change

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / store.path.name
        staged.write_text(first.new_text, encoding="utf-8")
        # Validation loads the staged file relative to the real config's folder; the
        # database path in it is absolute in practice, so the copy behaves the same.
        second = then(ConfigStore(staged, store.environ))
        return Change(store.path, first.old_text, second.new_text,
                      first.summary, first.renames)  # fmt: skip
