"""Background scans for ``serve``: one scan thread at a time, live progress, schedule.

The scan itself is :func:`media_audit.pipeline.run_scan`, run under the same file lock
the CLI uses, so a ``docker exec ... media-audit scan`` and a scheduled scan can never
overlap. The config file is re-read before every scan (and checked by the scheduler),
so edits to ``config.yml`` apply without restarting the container.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from media_audit.analyzer import Analyzer, AnalyzerUnavailable, get_analyzer
from media_audit.config import Config, ConfigError, find_config_path, load_config
from media_audit.cron import CronError, parse_cron
from media_audit.db import connect
from media_audit.pipeline import (
    ProgressEvent,
    ScanCancelled,
    ScanLocked,
    run_scan,
    scan_lock,
)
from media_audit.scanner import ScanError

log = logging.getLogger(__name__)

AnalyzerFactory = Callable[[Config], Analyzer | None]


def default_analyzer(cfg: Config) -> Analyzer:
    return get_analyzer(
        "mediainfo",
        parse_speed=cfg.scan.parse_speed,
        retry_parse_speed=cfg.scan.retry_parse_speed,
        retry_timeout=cfg.scan.retry_timeout,
    )


@dataclass
class ScanState:
    running: bool = False
    trigger: str | None = None  # manual | schedule
    started: float | None = None  # epoch seconds
    phase: str | None = None
    message: str | None = None
    library: str | None = None
    done: int | None = None
    total: int | None = None
    path: str | None = None
    stopping: bool = False
    # Outcome of the last scan this process ran (the scans table has the full history).
    last: dict[str, Any] | None = None


@dataclass
class ScheduleState:
    expr: str | None = None
    next_run: datetime | None = None
    error: str | None = None


@dataclass
class _ConfigCache:
    mtime: float | None = None
    config: Config | None = None
    error: str | None = None
    loaded_at: float = field(default=0.0)


class ScanService:
    def __init__(
        self,
        config_path: Path | None = None,
        *,
        db_override: Path | None = None,
        analyzer_factory: AnalyzerFactory = default_analyzer,
        clock: Callable[[], datetime] = datetime.now,
        first_start: bool = False,
    ) -> None:
        self._config_path = find_config_path(config_path)
        self._db_override = db_override
        self._analyzer_factory = analyzer_factory
        self._clock = clock
        self.first_start = first_start
        self._lock = threading.Lock()
        self._state = ScanState()
        self._schedule = ScheduleState()
        self._cfg = _ConfigCache()
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None
        self._last_log = 0.0
        self.on_finish: list[Callable[[], None]] = []  # e.g. warm the web page cache

    # ------------------------------------------------------------------ config

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    def config(self) -> Config:
        """The current config, re-read when the file changes. Raises ConfigError."""
        path = self._config_path
        try:
            mtime = path.stat().st_mtime if path is not None else None
        except OSError:
            mtime = None
        cache = self._cfg
        stale = cache.config is None and cache.error is None
        if stale or mtime != cache.mtime or time.monotonic() - cache.loaded_at > 60:
            try:
                cfg = load_config(path)
                if self._db_override is not None:
                    cfg.database = self._db_override
                self._cfg = _ConfigCache(mtime, cfg, None, time.monotonic())
            except ConfigError as exc:
                # Keep serving the last good config, but report the problem.
                self._cfg = _ConfigCache(mtime, cache.config, str(exc), time.monotonic())
        if self._cfg.config is None:
            raise ConfigError(self._cfg.error or "no config")
        return self._cfg.config

    def config_error(self) -> str | None:
        try:
            self.config()
        except ConfigError as exc:
            return str(exc)
        return self._cfg.error

    # ------------------------------------------------------------------ scans

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = asdict(self._state)
            sched = asdict(self._schedule)
        if state["started"]:
            state["elapsed_s"] = round(time.time() - state["started"], 1)
        nxt = sched["next_run"]
        sched["next_run"] = nxt.isoformat(timespec="minutes") if nxt else None
        return {"scan": state, "schedule": sched}

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state.running

    def start_scan(self, trigger: str = "manual") -> tuple[bool, str]:
        """Start a scan in the background. Returns (started, reason)."""
        with self._lock:
            if self._state.running:
                return False, "a scan is already running"
            if self._stop.is_set():
                return False, "shutting down"
            self._cancel.clear()
            self._state = ScanState(
                running=True,
                trigger=trigger,
                started=time.time(),
                phase="starting",
                message="starting",
                last=self._state.last,
            )
            self._scan_thread = threading.Thread(
                target=self._run, args=(trigger,), name="scan", daemon=True
            )
            self._scan_thread.start()
        log.info("scan started (%s)", trigger)
        return True, "started"

    def stop_scan(self) -> bool:
        with self._lock:
            if not self._state.running:
                return False
            self._state.stopping = True
        self._cancel.set()
        log.info("scan stop requested; finishing files already being read")
        return True

    def wait(self, timeout: float | None = None) -> None:
        thread = self._scan_thread
        if thread is not None:
            thread.join(timeout)

    def _progress(self, event: ProgressEvent) -> None:
        with self._lock:
            s = self._state
            if event.phase != s.phase or (event.library and event.library != s.library):
                log.info("%s", event.message)
            s.phase, s.message, s.library = event.phase, event.message, event.library or s.library
            s.done, s.total, s.path = event.done, event.total, event.path
        now = time.monotonic()
        if event.phase == "analyzing" and now - self._last_log > 30:
            self._last_log = now
            log.info("analyzed %s/%s files", event.done, event.total)

    def _finish(self, result: dict[str, Any]) -> None:
        with self._lock:
            started = self._state.started
            if started:
                result.setdefault("elapsed_s", round(time.time() - started, 1))
            result["finished"] = time.time()
            self._state = ScanState(last=result)
        if result.get("status") == "ok":
            self.first_start = False
        for hook in self.on_finish:
            try:
                hook()
            except Exception:
                log.exception("scan-finished hook failed")

    def _run(self, trigger: str) -> None:
        result: dict[str, Any] = {"trigger": trigger, "status": "failed", "errors": []}
        try:
            cfg = self.config()
            analyzer = self._analyzer_factory(cfg)
            with scan_lock(cfg.database):
                conn = connect(cfg.database)
                try:
                    summary = run_scan(
                        conn, cfg, analyzer=analyzer, progress=self._progress, cancel=self._cancel
                    )
                finally:
                    conn.close()
            result.update(
                scan_id=summary.scan_id,
                status=summary.status,
                seen=summary.seen,
                new=summary.new,
                changed=summary.changed,
                moved=summary.moved,
                deleted=summary.deleted,
                analyzed=summary.analyzed,
                errors=summary.errors[:20],
                error_count=len(summary.errors),
                elapsed_s=summary.elapsed_s,
            )
        except ScanCancelled as exc:
            result.update(status="interrupted", errors=[str(exc)])
        except ScanLocked:
            result.update(status="skipped", errors=["another scan (e.g. from the CLI) is running"])
        except (ConfigError, ScanError, AnalyzerUnavailable) as exc:
            result.update(errors=[str(exc)])
        except Exception as exc:  # keep the service alive; the scans table has the details
            log.exception("scan failed")
            result.update(errors=[f"{type(exc).__name__}: {exc}"])
        log.info(
            "scan finished: %s %s",
            result["status"],
            "; ".join(result["errors"][:3]) if result["errors"] else "",
        )
        self._finish(result)

    # ------------------------------------------------------------------ schedule

    def tick(self) -> None:
        """One scheduler step: refresh the schedule and start a scan if one is due."""
        now = self._clock()
        try:
            expr = self.config().scan.schedule
        except ConfigError as exc:
            with self._lock:
                self._schedule.error = str(exc)
            return
        with self._lock:
            sched = self._schedule
            if expr != sched.expr:
                sched.expr, sched.error, sched.next_run = expr, None, None
                if expr:
                    try:
                        sched.next_run = parse_cron(expr).next_after(now)
                    except CronError as exc:
                        sched.error = str(exc)
                if sched.next_run:
                    log.info(
                        "scheduled scans: %r, next at %s",
                        expr,
                        sched.next_run.isoformat(timespec="minutes"),
                    )
            due = sched.next_run is not None and now >= sched.next_run
            if due and expr:
                sched.next_run = parse_cron(expr).next_after(now)
        if due:
            started, reason = self.start_scan("schedule")
            if not started:
                log.info("scheduled scan skipped: %s", reason)

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler error")
            self._stop.wait(20)

    def start(self) -> None:
        self._scheduler = threading.Thread(
            target=self._scheduler_loop, name="scheduler", daemon=True
        )
        self._scheduler.start()

    def shutdown(self, timeout: float = 8.0) -> None:
        """Stop scheduling and ask a running scan to stop (its progress is kept)."""
        self._stop.set()
        if self.stop_scan():
            self.wait(timeout)
