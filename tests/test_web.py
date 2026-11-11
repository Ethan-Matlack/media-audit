"""The ``serve`` command: scan service, scheduler, and web pages."""

from __future__ import annotations

import csv
import io
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from media_audit.config import Config
from media_audit.db import connect
from media_audit.web.app import create_app, duration, growth_chart, human_bytes, query_string
from media_audit.web.service import ScanService
from tests.conftest import FakeAnalyzer

MOVIES = {
    "Heat (1995)/Heat (1995) {tmdb-949} [Bluray-1080p]-GRP.mkv": 2000,
    "Heat (1995)/Heat (1995) {tmdb-949} [Bluray-2160p][HDR10]-GRP.mkv": 4000,
    "Heat (1995)/Heat (1995).en.srt": 10,
    "Alien (1979)/Alien (1979) [WEBDL-1080p].mkv": 1500,
    "Broken (2001)/Broken corrupt (2001).mkv": 100,
}
TV = {
    "Show (2010)/Season 01/Show (2010) - S01E01 - One [WEBDL-1080p].mkv": 800,
    "Show (2010)/Season 01/Show (2010) - S01E02 - Two [WEBDL-1080p].mkv": 800,
    "Show (2010)/Season 02/Show (2010) - S02E01 - Three [WEBDL-1080p].mkv": 900,
}
MAPPING = {"2160p": "hdr10_hevc_2160p_cropped_truehd_atmos"}


@pytest.fixture
def config_file(make_tree: Callable[[str, dict[str, int | str]], Path], tmp_path: Path) -> Path:
    movies = make_tree("movies", dict(MOVIES))
    tv = make_tree("tv", dict(TV))
    path = tmp_path / "config.yml"
    path.write_text(
        f"database: {tmp_path / 'db' / 'media-audit.db'}\n"
        "libraries:\n"
        f"  - {{name: movies, path: {movies}, type: movies}}\n"
        f"  - {{name: tv, path: {tv}, type: tv}}\n"
        "scan:\n  schedule: off\n"
    )
    return path


def _service(config_file: Path, analyzer: Any = None, **kw: Any) -> ScanService:
    analyzer = analyzer or FakeAnalyzer(MAPPING)
    return ScanService(config_file, analyzer_factory=lambda cfg: analyzer, **kw)


def _scan(service: ScanService) -> dict[str, Any]:
    started, reason = service.start_scan()
    assert started, reason
    service.wait(30)
    last = service.status()["scan"]["last"]
    assert last is not None
    return last


# --------------------------------------------------------------------------- service


def test_service_scan_and_rescan(config_file: Path) -> None:
    service = _service(config_file)
    first = _scan(service)
    assert first["status"] == "ok" and first["analyzed"] == 7 and first["error_count"] == 1
    assert not service.running
    again = _scan(service)
    assert again["status"] == "ok" and again["analyzed"] == 0


class BlockingAnalyzer(FakeAnalyzer):
    """Holds every analyze() call until released, so a scan can be stopped midway."""

    def __init__(self) -> None:
        super().__init__(MAPPING)
        self.release = threading.Event()
        self.entered = threading.Event()

    def analyze(self, path: str) -> dict[str, Any]:
        self.entered.set()
        self.release.wait(10)
        return super().analyze(path)


def test_stop_scan_keeps_progress(config_file: Path) -> None:
    analyzer = BlockingAnalyzer()
    service = _service(config_file, analyzer)
    assert service.start_scan()[0]
    assert analyzer.entered.wait(10)
    assert service.start_scan() == (False, "a scan is already running")
    status = service.status()["scan"]
    assert status["running"] and status["phase"] == "analyzing"
    assert service.stop_scan()
    analyzer.release.set()
    service.wait(30)
    last = service.status()["scan"]["last"]
    assert last["status"] == "interrupted"

    cfg = service.config()
    conn = connect(cfg.database)
    row = conn.execute("SELECT status, files_analyzed FROM scans ORDER BY id DESC").fetchone()
    assert row["status"] == "interrupted"
    done = conn.execute("SELECT count(*) FROM files WHERE analyzed_at IS NOT NULL").fetchone()[0]
    assert 0 < done < 7  # files already being read were saved; the rest were not started
    # The next scan picks up where the stopped one left off.
    resumed = _scan(service)
    assert resumed["status"] == "ok" and resumed["analyzed"] == 7 - done


def test_stale_running_scan_is_marked_interrupted(config_file: Path) -> None:
    service = _service(config_file)
    _scan(service)
    conn = connect(service.config().database)
    conn.execute("UPDATE scans SET status = 'running', finished_at = NULL")  # killed mid-scan
    _scan(service)
    rows = conn.execute("SELECT status, message FROM scans ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["interrupted", "ok"]
    assert "did not finish" in rows[0]["message"]


def test_scheduler(config_file: Path) -> None:
    now = [datetime(2026, 11, 10, 2, 59)]
    service = _service(config_file, clock=lambda: now[0])
    service.tick()
    assert service.status()["schedule"]["next_run"] is None  # schedule: off

    config_file.write_text(config_file.read_text().replace("schedule: off", "schedule: 0 3 * * *"))
    service._cfg.loaded_at = 0  # don't wait for the mtime check
    service.tick()
    assert service.status()["schedule"]["next_run"] == "2026-11-10T03:00"
    assert not service.running
    now[0] = datetime(2026, 11, 10, 3, 0, 20)
    service.tick()
    service.wait(30)
    last = service.status()["scan"]["last"]
    assert last["trigger"] == "schedule" and last["status"] == "ok"
    assert service.status()["schedule"]["next_run"] == "2026-11-11T03:00"

    config_file.write_text(config_file.read_text().replace("0 3 * * *", "not cron"))
    service._cfg.loaded_at = 0
    service.tick()
    assert "5 fields" in (service.config_error() or "")
    assert service.status()["schedule"]["next_run"] == "2026-11-11T03:00"  # last good config


def test_config_error_reported(tmp_path: Path) -> None:
    bad = tmp_path / "config.yml"
    bad.write_text("libraries: [oops\n")
    service = ScanService(bad)
    assert "invalid YAML" in (service.config_error() or "")
    last = _scan(service)
    assert last["status"] == "failed" and "invalid YAML" in last["errors"][0]


# --------------------------------------------------------------------------- pages


@pytest.fixture
def client(config_file: Path) -> TestClient:
    service = _service(config_file)
    _scan(service)
    _scan(service)  # a second snapshot so the growth chart draws
    return TestClient(create_app(service))


def test_pages_render(client: TestClient) -> None:
    urls = [
        "/", "/?library=tv", "/titles", "/titles?badge=4k&sort=name&dir=asc", "/files",
        "/files?role=all", "/files?errors=1&role=video", "/scans", "/files?group=(none)",
        "/files?resolution=2160p&dr=HDR10&audio=TrueHD+Atmos",
    ]  # fmt: skip
    for url in urls:
        r = client.get(url)
        assert r.status_code == 200, url
        assert "Traceback" not in r.text
    home = client.get("/").text
    assert "Storage by library" in home and "Library size over time" in home
    assert 'class="series-line"' in home


def test_titles_and_detail(client: TestClient) -> None:
    page = client.get("/titles?q=Heat").text
    assert "Heat" in page and "Alien" not in page and "4K" in page
    four_k = client.get("/titles?badge=4k").text
    assert "Heat" in four_k and "Alien" not in four_k
    tid = int(page.split('href="/titles/')[1].split('"')[0])
    detail = client.get(f"/titles/{tid}").text
    assert "TrueHD Atmos" in detail and "TMDB 949" in detail and "Heat (1995).en.srt" in detail
    show = client.get("/titles?q=Show").text
    sid = int(show.split('href="/titles/')[1].split('"')[0])
    detail = client.get(f"/titles/{sid}").text
    assert "Season 1" in detail and "S02E01" in detail
    assert client.get("/titles/999999").status_code == 404


def test_files_filters_and_csv(client: TestClient) -> None:
    assert "7 files" in client.get("/files").text  # main videos
    assert "1 files" in client.get("/files?resolution=2160p").text
    assert "1 files" in client.get("/files?errors=1&role=video").text
    assert "corrupt" in client.get("/files?errors=1&role=video").text
    r = client.get("/files.csv?library=movies")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 4 and {row["library"] for row in rows} == {"movies"}
    uhd = next(row for row in rows if row["resolution_class"] == "2160p")
    assert "TrueHD Atmos" in uhd["audio"]


def test_scan_now_endpoint(config_file: Path) -> None:
    service = _service(config_file)
    client = TestClient(create_app(service))
    assert "Nothing scanned yet" in client.get("/").text
    r = client.post("/scans/run", headers={"accept": "application/json"})
    assert r.status_code == 202 and r.json()["started"]
    service.wait(30)
    assert client.get("/api/status").json()["scan"]["last"]["status"] == "ok"
    assert "Storage by library" in client.get("/").text
    # A form post from another site is refused (there's no login).
    r = client.post("/scans/run", headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/scans/run", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/scans"
    service.wait(30)
    assert client.get("/healthz").json()["ok"]


def test_no_database_yet(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(f"database: {tmp_path / 'none.db'}\nlibraries: []\n")
    client = TestClient(create_app(ScanService(cfg)))
    for url in ["/", "/titles", "/files", "/scans"]:
        assert client.get(url).status_code == 200
    assert "Nothing scanned yet" in client.get("/").text


# --------------------------------------------------------------------------- helpers


def test_formatting_helpers() -> None:
    assert human_bytes(31_610_000_000_000, 2) == "31.61 TB"
    assert human_bytes(512) == "512 B"
    assert duration(45) == "45 s" and duration(3300) == "55 min" and duration(5400) == "1 h 30 min"
    assert query_string({"a": "1", "page": "2"}, page=None, sort="size") == "?a=1&sort=size"


def test_growth_chart_labels_stay_distinct() -> None:
    pts = [{"scan_id": i, "at": f"2026-11-10T0{i}:00:00Z", "bytes": b, "files": 10}
           for i, b in enumerate([31_614e9, 31_609e9, 31_606e9], start=1)]  # fmt: skip
    chart = growth_chart(pts)
    labels = [c["label"] for c in chart["coords"]]
    assert len(set(labels)) == 3
    assert len({t["label"] for t in chart["ticks"]}) == 4
    # A 0.03% change is drawn nearly flat, not as a cliff.
    ys = [c["y"] for c in chart["coords"]]
    assert max(ys) - min(ys) < 10
    assert not growth_chart(pts[:1])["ready"]


def test_serve_first_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_tree: Callable[..., Path]
) -> None:
    from media_audit import cli
    from media_audit.web import server

    root = make_tree("media", {"movies/A (2000)/A (2000).mkv": 10})
    target = tmp_path / "config" / "config.yml"
    monkeypatch.setenv("MEDIA_AUDIT_AUTO_INIT", "1")
    monkeypatch.setenv("MEDIA_AUDIT_CONFIG", str(target))
    monkeypatch.setenv("MEDIA_AUDIT_MEDIA_ROOT", str(root))
    calls: list[dict[str, Any]] = []

    def fake_serve(cfg: Config, **kw: Any) -> int:
        calls.append({"cfg": cfg, **kw})
        return 0

    monkeypatch.setattr(server, "serve", fake_serve)
    assert cli.main(["serve", "--port", "9123"]) == 0
    assert target.is_file()
    assert calls[0]["first_start"] is True and calls[0]["cfg"].web.port == 9123
    assert [lib.name for lib in calls[0]["cfg"].libraries] == ["movies"]
    # Later starts: config exists, so no first-start banner.
    assert cli.main(["serve"]) == 0
    assert calls[1]["first_start"] is False


def test_progress_logging_is_throttled(config_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="media_audit")
    service = _service(config_file)
    t = time.monotonic()
    _scan(service)
    assert time.monotonic() - t < 30
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("walking movies") for m in msgs)
    assert any(m.startswith("scan finished: ok") for m in msgs)
    # Not one line per file: the phase change, then at most one line per 30 s.
    assert sum(m.startswith("analyzed ") for m in msgs) <= 2


def test_episode_averages(
    make_tree: Callable[[str, dict[str, int | str]], Path], tmp_path: Path
) -> None:
    from media_audit.web import queries

    tv = make_tree("tv2", {
        "Show (2010)/Season 01/Show (2010) - S01E01 - A.mkv": 1000,
        "Show (2010)/Season 01/Show (2010) - S01E02 - B [WEBDL-720p].mkv": 1000,
        "Show (2010)/Season 01/Show (2010) - S01E02 - B [Bluray-1080p].mkv": 3000,  # duplicate
        "Show (2010)/Season 01/Show (2010) - S01E03-E04 - C.mkv": 2000,  # two episodes
        "Show (2010)/Season 01/Show (2010) - S01E01 - A.en.srt": 500,  # not counted
        "Show (2010)/Specials/Show (2010) - S00E01 - Special.mkv": 9000,
    })  # fmt: skip
    cfg_file = tmp_path / "tv2.yml"
    cfg_file.write_text(
        f"database: {tmp_path / 'tv2.db'}\n"
        f"libraries:\n  - {{name: tv, path: {tv}, type: tv}}\nscan:\n  schedule: off\n"
    )
    service = _service(cfg_file)
    _scan(service)
    conn = connect(service.config().database)
    tid = conn.execute("SELECT id FROM titles WHERE name = 'Show'").fetchone()[0]

    stats = queries.show_episode_stats(conn, tid)
    s1 = stats["seasons"][1]
    assert (s1["episodes"], s1["bytes"], s1["dup_episodes"], s1["extra_files"]) == (4, 7000, 1, 1)
    assert s1["avg_bytes"] == 1750
    assert stats["specials"]["episodes"] == 1 and stats["specials"]["avg_bytes"] == 9000
    assert stats["show"]["avg_bytes"] == 1750  # specials left out
    # Every file has the same running time; a duplicate doesn't make an episode longer.
    one = conn.execute(
        "SELECT duration_s FROM video_info JOIN files ON files.id = file_id "
        "WHERE filename LIKE '%S01E01%' AND is_video = 1"
    ).fetchone()[0]
    assert stats["show"]["avg_runtime_s"] == pytest.approx(one)

    listed = queries.titles(conn, {"kind": "show", "sort": "avg_episode"}).rows[0]
    assert listed["avg_episode_bytes"] == 1750 and listed["dup_episodes"] == 1

    client = TestClient(create_app(service))
    page = client.get(f"/titles/{tid}").text
    assert "Avg episode size" in page and "specials not included" in page
    assert "1 with extra copies" in page and "not in show averages" in page
    assert "How the averages are worked out" in page
    listing = client.get("/titles?sort=avg_episode").text
    assert "excluding specials" in listing and 'class="dup"' in listing


def test_splash_only_for_serve(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from media_audit import cli
    from media_audit.web import server

    monkeypatch.setattr(server, "serve", lambda cfg, **kw: 0)
    assert cli.main(["-c", str(config_file), "serve"]) == 0
    assert "m e d i a - a u d i t" in capsys.readouterr().out
    assert cli.main(["-c", str(config_file), "stats", "--json"]) in (0, 2)
    assert "m e d i a - a u d i t" not in capsys.readouterr().out


def test_page_cache_reused_until_data_changes(client: TestClient) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    client.get("/titles")
    misses = db.misses
    for _ in range(3):
        assert client.get("/titles").status_code == 200
    assert db.misses == misses and db.hits >= 3  # served from the cache

    # A write from another connection (e.g. `docker exec ... media-audit scan`) clears it.
    service = client.app.state.service  # type: ignore[attr-defined]
    other = connect(service.config().database)
    other.execute("UPDATE titles SET name = 'Heat (renamed)' WHERE name = 'Heat'")
    assert "Heat (renamed)" in client.get("/titles?q=Heat").text
    assert db.misses > misses


def test_warm_fills_default_views(config_file: Path) -> None:
    service = _service(config_file)
    _scan(service)
    app = create_app(service)
    app.state.warm()
    misses = app.state.db.misses
    client = TestClient(app)
    for url in ["/", "/titles", "/files"]:
        assert client.get(url).status_code == 200
    assert app.state.db.misses == misses  # every default view was already computed
