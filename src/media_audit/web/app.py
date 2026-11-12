"""The web UI: server-rendered pages over the SQLite database (read-only).

The only write is "Scan now", which asks the :class:`ScanService` to start a scan.
"""

from __future__ import annotations

import csv
import io
import logging
import shlex
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from media_audit import __version__, cleanup
from media_audit.build import asset_tag, build_label, channel, display_version
from media_audit.config import ConfigError
from media_audit.db import connect, transaction, utcnow
from media_audit.web import queries
from media_audit.web.data import ReadDB
from media_audit.web.service import ScanService

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- formatting


def human_bytes(n: float | None, digits: int = 1) -> str:
    if n is None:
        return "–"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1000 or unit == "PB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.{digits}f} {unit}"
        value /= 1000
    return f"{value:.{digits}f} PB"


def number(n: float | None) -> str:
    return "–" if n is None else f"{n:,.0f}"


def local_time(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """DB timestamps are UTC ('...Z'); show them in the container's time zone (TZ)."""
    if not value:
        return "–"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime(fmt)


def duration(seconds: float | None) -> str:
    """Elapsed time: '45 s', '12 min', '1 h 05 min'."""
    if seconds is None:
        return "–"
    s = round(seconds)
    if s < 90:
        return f"{s} s"
    m = s // 60
    if m < 90:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


def runtime(seconds: float | None) -> str:
    """Media running time: '1:52:03' or '42:10'."""
    if not seconds:
        return "–"
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def bitrate(bps: float | None) -> str:
    return "–" if not bps else f"{bps / 1e6:.1f} Mb/s"


def episode_code(f: Mapping[str, Any]) -> str:
    s, e, e2 = f.get("season_number"), f.get("episode_number"), f.get("episode_number_end")
    if s is None or e is None:
        return ""
    code = f"S{s:02d}E{e:02d}"
    return f"{code}-E{e2:02d}" if e2 and e2 != e else code


def audio_label(a: Mapping[str, Any]) -> str:
    name = a.get("commercial_name") or a.get("codec") or "?"
    if not a.get("is_object_audio") and a.get("object_audio_hint"):
        name += f" ({a['object_audio_hint']})"
    return f"{name} {a.get('channels_label') or ''}".strip()


def query_string(params: Mapping[str, str], **changes: Any) -> str:
    """The current query string with some keys changed (None removes a key)."""
    merged = {k: v for k, v in params.items() if v not in ("", None)}
    for key, value in changes.items():
        if value is None or value == "":
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    return ("?" + urlencode(merged)) if merged else "?"


def parent_folder(path: str) -> str:
    """'Show/Season 01/ep.mkv' -> 'Show/Season 01' ('' for a file in the library root)."""
    return path.rstrip("/").rpartition("/")[0]


def rel_folder(folder: str, base: str | None) -> str:
    """A folder as shown under a title: relative to the title folder when it's inside it.

    The title folder itself is shown as is, so a file directly in it still says where.
    """
    if base and folder.startswith(base + "/"):
        return folder[len(base) + 1 :]
    return folder


def folder_url(library: str, folder: str, **extra: Any) -> str:
    """The Files page listing everything in one folder (and below it)."""
    params = {"library": library, "folder": folder, "role": "all"}
    params.update({k: str(v) for k, v in extra.items() if v})
    return "/files?" + urlencode(params)


def _distinct_labels(values: list[float]) -> tuple[list[str], int]:
    """Byte labels with just enough decimals that neighbouring values differ."""
    for digits in range(1, 5):
        labels = [human_bytes(v, digits) for v in values]
        if len(set(labels)) == len(labels):
            return labels, digits
    return labels, 4


def growth_chart(
    points: list[dict[str, Any]], width: int = 1200, height: int = 240
) -> dict[str, Any]:
    """Geometry for the size-over-time line chart (one series: total bytes).

    The y range is at least 5% of the largest value, so a few GB of churn on a 30 TB
    library reads as the near-flat line it is rather than a cliff.
    """
    pad_l, pad_r, pad_t, pad_b = 72, 16, 14, 30
    if len(points) < 2:
        return {"points": points, "ready": False}
    times = [datetime.fromisoformat(p["at"].replace("Z", "+00:00")).timestamp() for p in points]
    t0, t1 = min(times), max(times)
    values = [float(p["bytes"]) for p in points]
    lo, hi = min(values), max(values)
    span = max(hi - lo, hi * 0.05, 1.0)
    mid = (hi + lo) / 2
    lo, hi = max(0.0, mid - span * 0.6), mid + span * 0.6
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    def x(t: float) -> float:
        return pad_l + (plot_w * (t - t0) / (t1 - t0) if t1 > t0 else plot_w / 2)

    def y(v: float) -> float:
        return pad_t + plot_h * (1 - (v - lo) / (hi - lo))

    tick_values = [lo + (hi - lo) * i / 3 for i in range(4)]
    tick_labels, _ = _distinct_labels(tick_values)
    point_labels, _ = _distinct_labels(values)
    coords = [
        {
            "x": round(x(t), 1),
            "y": round(y(v), 1),
            "label": label,
            "files": number(p["files"]),
            "at": local_time(p["at"]),
            "scan_id": p["scan_id"],
        }
        for t, v, label, p in zip(times, values, point_labels, points, strict=True)
    ]
    same_day = local_time(points[0]["at"], "%Y-%m-%d") == local_time(points[-1]["at"], "%Y-%m-%d")
    x_fmt = "%Y-%m-%d %H:%M" if same_day else "%Y-%m-%d"
    return {
        "ready": True,
        "width": width,
        "height": height,
        "path": "M" + " L".join(f"{c['x']},{c['y']}" for c in coords),
        "coords": coords,
        "ticks": [
            {"y": round(y(v), 1), "label": lbl}
            for v, lbl in zip(tick_values, tick_labels, strict=True)
        ],
        "x_first": local_time(points[0]["at"], x_fmt),
        "x_last": local_time(points[-1]["at"], x_fmt),
        "plot": {"left": pad_l, "right": width - pad_r, "top": pad_t, "bottom": height - pad_b},
    }


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("media_audit.web", "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        bytes=human_bytes,
        num=number,
        localtime=local_time,
        duration=duration,
        runtime=runtime,
        bitrate=bitrate,
        episode=episode_code,
        audio_label=audio_label,
        shell_quote=shlex.quote,
        parent_folder=parent_folder,
    )
    env.globals.update(qs=query_string, folder_url=folder_url, rel_folder=rel_folder,
                       version=display_version(), asset_tag=asset_tag(),
                       build=None if channel() == "dev" else build_label())  # fmt: skip
    return env


# --------------------------------------------------------------------------- app


def _key(name: str, params: Mapping[str, str], *extra: Any) -> tuple[Any, ...]:
    return (name, tuple(sorted(params.items())), *extra)


def create_app(service: ScanService) -> Starlette:
    env = _env()
    db = ReadDB()

    def db_path() -> Path | None:
        try:
            return service.config().database
        except ConfigError:
            return None

    def render(request: Request, template: str, status_code: int = 200, **ctx: Any) -> Response:
        conn = ctx.pop("_conn", None)
        libraries = db.cached(("libraries",), lambda: queries.libraries(conn)) if conn else []
        issue_count = (
            db.cached(("issue_count",), lambda: queries.open_issue_count(conn)) if conn else 0
        )
        context = {
            "request": request,
            "params": dict(request.query_params),
            "status": service.status(),
            "config_error": service.config_error(),
            "config_path": service.config_path,
            "first_start": service.first_start,
            "nav": template.split(".")[0],
            "libraries": libraries,
            "issue_count": issue_count,
            **ctx,
        }
        html = env.get_template(template).render(context)
        return HTMLResponse(html, status_code=status_code)

    def with_db(handler: Any) -> Any:
        """Run a page handler in a worker thread with a read-only DB connection."""

        def run(request: Request) -> Response:
            with db.connection(db_path()) as conn:
                if conn is None or not db.cached(("has_data",), lambda: queries.has_data(conn)):
                    return render(request, "empty.html", _conn=conn)
                return handler(request, conn)  # type: ignore[no-any-return]

        async def endpoint(request: Request) -> Response:
            return await run_in_threadpool(run, request)

        return endpoint

    def overview(request: Request, conn: sqlite3.Connection) -> Response:
        library = request.query_params.get("library") or None
        data = db.cached(("overview", library), lambda: queries.overview(conn, library))
        return render(
            request,
            "overview.html",
            _conn=conn,
            data=data,
            library=library,
            chart=growth_chart(data["growth"]),
        )

    def titles(request: Request, conn: sqlite3.Connection) -> Response:
        params = request.query_params
        page = db.cached(_key("titles", params), lambda: queries.titles(conn, params))
        return render(request, "titles.html", _conn=conn, page=page)

    def title(request: Request, conn: sqlite3.Connection) -> Response:
        tid = int(request.path_params["title_id"])
        detail = db.cached(("title", tid), lambda: queries.title_detail(conn, tid))
        if detail is None:
            return render(request, "notfound.html", status_code=404, _conn=conn)
        title_issues = db.cached(("title_issues", tid), lambda: queries.title_issues(conn, tid))
        return render(request, "title.html", _conn=conn, issues=title_issues, **detail)

    def issues(request: Request, conn: sqlite3.Connection) -> Response:
        params = request.query_params
        data = db.cached(_key("issues", params), lambda: queries.issues(conn, params))
        return render(request, "issues.html", _conn=conn, data=data)

    async def issues_csv(request: Request) -> Response:
        path = db_path()
        if path is None or not path.exists():
            return PlainTextResponse("no database yet", status_code=404)
        params = dict(request.query_params)

        def stream() -> Iterator[str]:
            buf = io.StringIO()
            writer = csv.writer(buf)
            with db.connection(path) as conn:
                assert conn is not None
                for row in queries.issues_csv_rows(conn, params):
                    writer.writerow(row)
            yield buf.getvalue()

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        return StreamingResponse(
            stream(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="media-audit-issues-{stamp}.csv"'
            },
        )

    def _set_ignored(key: str, ignore: bool) -> bool:
        path = db_path()
        if path is None or not path.exists():
            return False
        conn = connect(path, migrate=False)
        try:
            with transaction(conn):
                if not ignore:
                    return (
                        conn.execute("DELETE FROM flag_ignores WHERE key = ?", (key,)).rowcount > 0
                    )
                row = conn.execute(
                    "SELECT fingerprint, code FROM flags WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    return False
                conn.execute(
                    "INSERT INTO flag_ignores (key, fingerprint, code, created_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                    "fingerprint = excluded.fingerprint, created_at = excluded.created_at",
                    (key, row["fingerprint"], row["code"], utcnow()),
                )
                return True
        finally:
            conn.close()

    def _ignore_endpoint(ignore: bool) -> Any:
        async def endpoint(request: Request) -> Response:
            if not _same_origin(request):
                return PlainTextResponse("cross-origin request refused", status_code=403)
            key = request.query_params.get("key", "")
            changed = await run_in_threadpool(_set_ignored, key, ignore)
            if request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"ignored": ignore, "changed": changed},
                                    status_code=200 if changed or not ignore else 404)  # fmt: skip
            nxt = request.query_params.get("next", "/issues")
            return RedirectResponse(nxt if nxt.startswith("/") and not nxt.startswith("//")
                                    else "/issues", status_code=303)  # fmt: skip

        return endpoint

    def files(request: Request, conn: sqlite3.Connection) -> Response:
        params = request.query_params
        page = db.cached(_key("files", params), lambda: queries.files(conn, params))
        options = db.cached(("file_options",), lambda: queries.file_filter_options(conn))
        return render(request, "files.html", _conn=conn, page=page, options=options)

    async def files_csv(request: Request) -> Response:
        path = db_path()
        if path is None or not path.exists():
            return PlainTextResponse("no database yet", status_code=404)
        params = dict(request.query_params)

        def stream() -> Iterator[str]:
            buf = io.StringIO()
            writer = csv.writer(buf)
            with db.connection(path) as conn:
                assert conn is not None
                for i, row in enumerate(queries.files_csv_rows(conn, params)):
                    writer.writerow(row)
                    if i % 500 == 0:
                        yield buf.getvalue()
                        buf.seek(0)
                        buf.truncate()
            yield buf.getvalue()

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        return StreamingResponse(
            stream(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="media-audit-files-{stamp}.csv"'
            },
        )

    # ------------------------------------------------------------------ cleanup scripts
    cleanup_filters = ("library", "severity", "category", "code")

    def _cleanup_context(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
        cfg = service.config()
        filters = {k: v for k, v in request.query_params.items() if k in cleanup_filters and v}
        where, params = queries._issue_filters({**filters, "ignored": "1"})
        plan = cleanup.build_plan(conn, cfg, where, params)
        in_docker = cleanup.in_docker()
        return {
            "cfg": cfg, "plan": plan, "filters": filters, "in_docker": in_docker,
            "needs_host_path": in_docker and cfg.cleanup.host_media_path is None,
            "saved": cleanup.saved_scripts(cleanup.scripts_dir(cfg)),
            "disclaimer": cleanup.DISCLAIMER,
        }  # fmt: skip

    def _host_script_path(cfg: Any, in_docker: bool, name: str) -> str | None:
        path = cleanup.scripts_dir(cfg) / name
        if not in_docker:
            return str(path)
        host = cfg.cleanup.host_config_path
        try:
            return f"{host}/{path.relative_to('/config')}" if host else None
        except ValueError:
            return None

    def cleanup_page(request: Request, conn: sqlite3.Connection, **extra: Any) -> Response:
        try:
            ctx = _cleanup_context(request, conn)
        except ConfigError:
            return render(request, "cleanup.html", _conn=conn, plan=None)
        text = cleanup.render_script(ctx["plan"], datetime.now())
        return render(request, "cleanup.html", _conn=conn, script=text, **ctx, **extra)

    def _cleanup_post(request: Request, form: dict[str, str]) -> Response:
        with db.connection(db_path()) as conn:
            if conn is None:
                return PlainTextResponse("no database yet", status_code=404)
            try:
                ctx = _cleanup_context(request, conn)
            except ConfigError:
                return cleanup_page(request, conn)
            plan = ctx["plan"]
            if plan.empty or ctx["needs_host_path"]:
                return cleanup_page(request, conn)
            if form.get("digest") != plan.digest:
                return cleanup_page(request, conn, changed=True)
            if form.get("ack") != "1":
                return cleanup_page(request, conn, need_ack=True)
            now = datetime.now()
            name = cleanup.script_name(now)
            text = cleanup.render_script(plan, now)
            if form.get("action") == "download":
                return Response(text, media_type="text/x-shellscript; charset=utf-8",
                                headers={"Content-Disposition":
                                         f'attachment; filename="{name}"'})  # fmt: skip
            try:
                path = cleanup.save_script(text, cleanup.scripts_dir(ctx["cfg"]), name)
            except OSError as exc:
                return cleanup_page(request, conn, save_error=str(exc))
            log.info("cleanup script saved: %s", path)
            return render(request, "cleanup_saved.html", _conn=conn, name=name,
                          path=str(path), plan=plan,
                          host_path=_host_script_path(ctx["cfg"], ctx["in_docker"], name),
                          in_docker=ctx["in_docker"])  # fmt: skip

    async def cleanup_post(request: Request) -> Response:
        if not _same_origin(request):
            return PlainTextResponse("cross-origin request refused", status_code=403)
        body = (await request.body()).decode("utf-8", "replace")
        form = {k: v[-1] for k, v in parse_qs(body).items()}
        return await run_in_threadpool(_cleanup_post, request, form)

    async def cleanup_saved(request: Request) -> Response:
        import re

        name = request.path_params["name"]
        try:
            directory = cleanup.scripts_dir(service.config())
        except ConfigError:
            return PlainTextResponse("config problem", status_code=503)
        path = directory / name
        if not re.fullmatch(cleanup.SCRIPT_RE, name) or not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path, media_type="text/x-shellscript; charset=utf-8",
                            filename=name)  # fmt: skip

    def _scans(request: Request) -> Response:
        with db.connection(db_path()) as conn:
            history = db.cached(("scans",), lambda: queries.scans(conn)) if conn else []
            return render(request, "scans.html", _conn=conn, history=history)

    async def scans(request: Request) -> Response:
        return await run_in_threadpool(_scans, request)

    def _same_origin(request: Request) -> bool:
        # No login, so at least refuse cross-site form posts (another site can't start scans).
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            return True
        return urlsplit(origin).netloc == request.headers.get("host")

    async def scan_now(request: Request) -> Response:
        if not _same_origin(request):
            return PlainTextResponse("cross-origin request refused", status_code=403)
        started, reason = service.start_scan("manual")
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                {"started": started, "reason": reason}, status_code=202 if started else 409
            )
        return RedirectResponse("/scans", status_code=303)

    async def scan_stop(request: Request) -> Response:
        if not _same_origin(request):
            return PlainTextResponse("cross-origin request refused", status_code=403)
        stopped = service.stop_scan()
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"stopping": stopped})
        return RedirectResponse("/scans", status_code=303)

    async def api_status(request: Request) -> Response:
        return JSONResponse(service.status())

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__, "build": build_label()})

    static_dir = Path(str(resources.files("media_audit.web") / "static"))
    routes = [
        Route("/", with_db(overview)),
        Route("/titles", with_db(titles)),
        Route("/titles/{title_id:int}", with_db(title)),
        Route("/files", with_db(files)),
        Route("/files.csv", files_csv),
        Route("/issues", with_db(issues)),
        Route("/issues.csv", issues_csv),
        Route("/issues/ignore", _ignore_endpoint(True), methods=["POST"]),
        Route("/issues/unignore", _ignore_endpoint(False), methods=["POST"]),
        Route("/issues/cleanup", with_db(cleanup_page)),
        Route("/issues/cleanup", cleanup_post, methods=["POST"]),
        Route("/issues/cleanup/saved/{name}", cleanup_saved),
        Route("/scans", scans),
        Route("/scans/run", scan_now, methods=["POST"]),
        Route("/scans/stop", scan_stop, methods=["POST"]),
        Route("/api/status", api_status),
        Route("/healthz", healthz),
        Mount("/static", StaticFiles(directory=static_dir), name="static"),
    ]

    def warm() -> None:
        """Fill the cache for the default views so the first click after a scan is quick."""
        try:
            with db.connection(db_path()) as conn:
                if conn is None or not queries.has_data(conn):
                    return
                empty: dict[str, str] = {}
                db.cached(("libraries",), lambda: queries.libraries(conn))
                db.cached(("has_data",), lambda: True)
                db.cached(("overview", None), lambda: queries.overview(conn, None))
                db.cached(_key("titles", empty), lambda: queries.titles(conn, empty))
                db.cached(_key("files", empty), lambda: queries.files(conn, empty))
                db.cached(("file_options",), lambda: queries.file_filter_options(conn))
                db.cached(("issue_count",), lambda: queries.open_issue_count(conn))
                db.cached(_key("issues", empty), lambda: queries.issues(conn, empty))
        except Exception:  # only an optimisation; pages compute on demand anyway
            log.exception("cache warm-up failed")

    app = Starlette(routes=routes)
    app.state.service = service
    app.state.db = db
    app.state.warm = warm
    service.on_finish.append(lambda: threading.Thread(target=warm, daemon=True).start())
    return app
