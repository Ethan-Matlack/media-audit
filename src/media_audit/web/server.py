"""``media-audit serve``: web UI + scheduled scans in one long-running process."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from starlette.applications import Starlette

from media_audit.config import Config
from media_audit.db import connect
from media_audit.web.app import create_app
from media_audit.web.service import ScanService

log = logging.getLogger(__name__)


def build(
    config_path: Path | None, db_override: Path | None, first_start: bool = False
) -> tuple[Starlette, ScanService]:
    service = ScanService(config_path, db_override=db_override, first_start=first_start)
    app = create_app(service)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        service.start()
        threading.Thread(target=app.state.warm, name="warm-cache", daemon=True).start()
        try:
            yield
        finally:
            log.info("shutting down")
            service.shutdown()
            app.state.db.close()

    app.router.lifespan_context = lifespan
    return app, service


def serve(
    cfg: Config, *, config_path: Path | None, db_override: Path | None, first_start: bool = False
) -> int:
    # Create / migrate the database up front so the UI can open it read-only.
    connect(cfg.database).close()
    app, _ = build(config_path, db_override, first_start)
    log.info(
        "web UI on http://%s:%d (config: %s, database: %s)",
        cfg.web.host,
        cfg.web.port,
        config_path or "default",
        cfg.database,
    )
    uvicorn.run(
        app,
        host=cfg.web.host,
        port=cfg.web.port,
        log_level="warning",
        access_log=False,
        proxy_headers=True,
        timeout_graceful_shutdown=10,
    )
    return 0
