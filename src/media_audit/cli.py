"""Command-line interface: ``media-audit serve | scan | rederive | stats | check-config | ...``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from media_audit import stats as stats_mod
from media_audit.build import display_version
from media_audit.config import (
    AUTO_INIT_ENV,
    CONFIG_ENV,
    MEDIA_ROOT_ENV,
    Config,
    ConfigError,
    find_config_path,
    load_config,
)

log = logging.getLogger("media_audit")


def _human_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1000 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1000
    return f"{value:.2f} PB"


def _load(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config)
    if args.db:
        cfg.database = args.db
    return cfg


def _open_db(cfg: Config) -> Any:
    from media_audit.db import connect

    return connect(cfg.database)


# ------------------------------------------------------------------------- commands


def cmd_scan(args: argparse.Namespace) -> int:
    from media_audit.analyzer import AnalyzerUnavailable, get_analyzer
    from media_audit.pipeline import ProgressEvent, ScanLocked, run_scan, scan_lock
    from media_audit.scanner import ScanError

    cfg = _load(args)
    analyzer = None
    if not args.no_analyze:
        try:
            analyzer = get_analyzer(
                "mediainfo",
                parse_speed=cfg.scan.parse_speed,
                retry_parse_speed=cfg.scan.retry_parse_speed,
                retry_timeout=cfg.scan.retry_timeout,
            )
        except AnalyzerUnavailable as exc:
            print(f"error: {exc}\n(use --no-analyze to only index files)", file=sys.stderr)
            return 2
    if args.workers:
        cfg.scan.workers = args.workers

    def progress(event: ProgressEvent) -> None:
        if not args.quiet:
            print(event.message, file=sys.stderr, flush=True)

    try:
        with scan_lock(cfg.database):
            conn = _open_db(cfg)
            summary = run_scan(
                conn, cfg, analyzer=analyzer, library_names=args.library or None,
                allow_empty=args.allow_empty, retry_errors=args.retry_errors, progress=progress,
            )  # fmt: skip
    except ScanLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"scan #{summary.scan_id} {summary.status} in {summary.elapsed_s}s: "
        f"{summary.seen} files seen, {summary.new} new, {summary.changed} changed, "
        f"{summary.moved} moved, {summary.deleted} deleted, {summary.analyzed} analyzed, "
        f"{len(summary.errors)} errors"
    )
    for err in summary.errors[:20]:
        print(f"  ! {err}")
    if len(summary.errors) > 20:
        print(f"  ... and {len(summary.errors) - 20} more (see the scans table)")
    return 0 if summary.status == "ok" else 1


def cmd_rederive(args: argparse.Namespace) -> int:
    from media_audit.pipeline import ScanLocked, rederive, scan_lock

    cfg = _load(args)
    try:
        with scan_lock(cfg.database):
            count = rederive(_open_db(cfg), cfg)
    except ScanLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"re-derived {count} files from stored data (no media files were read)")
    return 0


def _print_table(
    title: str, rows: list[dict[str, Any]], total_files: int, with_bytes: bool = True
) -> None:
    if not rows:
        return
    print(f"\n{title}")
    width = max(len(str(r["value"])) for r in rows)
    width = max(width, 8)
    for r in rows:
        pct = (100 * r["files"] / total_files) if total_files else 0
        line = f"  {r['value']!s:<{width}}  {r['files']:>7}  {pct:5.1f}%"
        if with_bytes and "bytes" in r:
            line += f"  {_human_bytes(r['bytes']):>10}"
            if r.get("avg_bytes") is not None:
                line += f"  avg {_human_bytes(r['avg_bytes'])}"
        print(line)


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if not cfg.database.exists():
        print(f"error: database not found: {cfg.database} (run a scan first)", file=sys.stderr)
        return 2
    conn = _open_db(cfg)
    report = stats_mod.full_report(conn, args.library)
    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
        return 0
    t = report["totals"]
    print(f"Titles:        {t['titles']} ({t['distinct_titles']} distinct after linking)")
    print(f"Files:         {t['files']} ({t['main_videos']} main videos)")
    print(f"Size:          {_human_bytes(t['bytes'])} (unique on disk: "
          f"{_human_bytes(t['unique_bytes'])})")  # fmt: skip
    if t["not_analyzed"] or t["analyze_errors"]:
        print(f"Not analyzed:  {t['not_analyzed']}   Analyze errors: {t['analyze_errors']}")
    n = t["main_videos"]
    _print_table("Resolution", report["resolution"], n)
    _print_table("Video codec", report["video_codec"], n)
    _print_table("Dynamic range", report["dynamic_range"], n)
    _print_table("Container", report["container"], n)
    _print_table("Source", report["source"], n)
    _print_table("Audio format (files with at least one)", report["audio_format"], n, False)
    _print_table("Audio language", report["audio_language"], n, False)
    _print_table("Subtitle language", report["subtitle_language"], n, False)
    _print_table("Release group (top 25)", report["release_group"], n)
    if report["largest_titles"]:
        print("\nLargest titles")
        for r in report["largest_titles"]:
            year = f" ({r['year']})" if r["year"] else ""
            print(f"  {_human_bytes(r['bytes']):>10}  [{r['library']}] {r['name']}{year}")
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    cfg = _load(args)
    ok = True
    print(f"database: {cfg.database}")
    paths = []
    for lib in cfg.libraries:
        exists = lib.path.is_dir()
        state = "ok" if exists else "MISSING"
        ok = ok and (exists or not lib.enabled)
        flag = "" if lib.enabled else " (disabled)"
        print(f"library {lib.name!r} [{lib.type}]{flag}: {lib.path} ... {state}")
        paths.append((lib.name, lib.path.resolve()))
    for name_a, a in paths:
        for name_b, b in paths:
            if name_a < name_b and (a == b or a in b.parents or b in a.parents):
                print(f"error: libraries {name_a!r} and {name_b!r} overlap ({a}, {b})")
                ok = False
    try:
        from media_audit.analyzer import AnalyzerUnavailable, get_analyzer

        get_analyzer("mediainfo")
        print("libmediainfo: ok")
    except AnalyzerUnavailable as exc:
        print(f"libmediainfo: MISSING ({exc})")
        ok = False
    if not cfg.libraries:
        print("warning: no libraries configured")
    return 0 if ok else 1


DEFAULT_MEDIA_ROOT = Path("/media")


def _init_target(args: argparse.Namespace) -> Path:
    if args.config:
        return Path(args.config)
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else Path("config.yml")


def _generate_config(target: Path, media_root: Path, force: bool) -> int:
    from media_audit.init_config import write_default_config

    try:
        text, candidates = write_default_config(
            target, media_root, target.parent / "media-audit.db", force=force
        )
    except FileExistsError:
        print(f"error: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    libs = [c for c in candidates if c.type]
    print(f"Wrote {target} with {len(libs)} libraries detected in {media_root}:")
    for c in candidates:
        print(f"  {c.name:<24} {c.type or '(skipped: not recognized as movies/tv)'}")
    print(f"\n{text}")
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    return _generate_config(_init_target(args), args.media_root, args.force)


def cmd_serve(args: argparse.Namespace) -> int:
    from media_audit.web.server import serve

    if args.host:
        os.environ["MEDIA_AUDIT_WEB__HOST"] = args.host
    if args.port:
        os.environ["MEDIA_AUDIT_WEB__PORT"] = str(args.port)
    cfg = _load(args)  # fail fast on a broken config
    return serve(cfg, config_path=args.config, db_override=args.db,
                 first_start=bool(getattr(args, "first_start", False)))  # fmt: skip


def _auto_init(args: argparse.Namespace) -> int | None:
    """Docker first start: create a config from /media for review.

    One-shot commands stop there; ``serve`` keeps running (without scanning) so the web
    UI can say a config was just created.
    """
    if os.environ.get(AUTO_INIT_ENV) != "1" or args.command == "init-config":
        return None
    found = find_config_path()
    if args.config or (found is not None and found.is_file()):
        return None
    target = _init_target(args)
    media_root = Path(os.environ.get(MEDIA_ROOT_ENV) or DEFAULT_MEDIA_ROOT)
    print(f"No config found at {target}; creating one (first start).")
    rc = _generate_config(target, media_root, force=False)
    if rc == 0 and args.command == "serve":
        print(
            "Review the library list above (edit the file if needed), then use Scan now "
            "in the web UI. Nothing is scanned until then or the next scheduled scan."
        )
        args.first_start = True
        return None
    if rc == 0:
        print(
            "Review the library list and types above (edit the file if needed), then start "
            "the container again to scan. Nothing was scanned this time."
        )
    return rc


# ------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-audit", description="Scan a media library and report what's in it."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {display_version()}")
    parser.add_argument("-c", "--config", type=Path, help="config file (default: "
                        "$MEDIA_AUDIT_CONFIG, ./config.yml, /config/config.yml)")  # fmt: skip
    parser.add_argument("--db", type=Path, help="override the database path")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web UI and scheduled scans (the Docker default)")
    p.add_argument("--host", help="override web.host (default 0.0.0.0)")
    p.add_argument("--port", type=int, help="override web.port (default 8484)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("scan", help="walk libraries, analyze new/changed files, update the DB")
    p.add_argument("-l", "--library", action="append", help="only this library (repeatable)")
    p.add_argument("--no-analyze", action="store_true", help="index files without MediaInfo")
    p.add_argument("--retry-errors", action="store_true",
                   help="re-analyze files that previously failed")  # fmt: skip
    p.add_argument("--allow-empty", action="store_true",
                   help="allow an empty library root to mark all its files deleted")  # fmt: skip
    p.add_argument("-w", "--workers", type=int, help="override scan.workers")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("rederive", help="re-apply rules to stored data without reading media")
    p.set_defaults(func=cmd_rederive)

    p = sub.add_parser("stats", help="print library statistics")
    p.add_argument("-l", "--library", help="only this library")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("check-config", help="validate config, library paths, and libmediainfo")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser(
        "init-config", help="write a starter config by detecting libraries under a media root"
    )
    p.add_argument("--media-root", type=Path, default=DEFAULT_MEDIA_ROOT,
                   help="folder whose subfolders are the libraries (default: /media)")  # fmt: skip
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_init_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # serve is a long-running service: log scans to the container log by default.
    base = logging.INFO if args.command == "serve" else logging.WARNING
    level = max(logging.DEBUG, base - 10 * min(args.verbose, 2))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "serve":
        # Container start: make it obvious in the log. One-off commands (stats --json,
        # scan) stay quiet so their output can be piped.
        from media_audit.splash import render

        print(render(), flush=True)
    try:
        initialized = _auto_init(args)
        if initialized is not None:
            return initialized
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
