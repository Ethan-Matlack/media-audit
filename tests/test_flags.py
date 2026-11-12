"""The Issues checks: pure rules, the full check run, ignoring, and the page."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from media_audit.config import Config, FlagsConfig
from media_audit.db import connect
from media_audit.flags import collect, compute_flags, ensure_flags
from media_audit.pipeline import rederive, run_scan
from media_audit.rules.flags import CHECKS, missing_episodes, ranges, resolution_mismatch
from media_audit.web.app import create_app
from media_audit.web.service import ScanService
from tests.conftest import FakeAnalyzer

# --------------------------------------------------------------------------- pure rules


@pytest.mark.parametrize(
    ("tag", "actual", "expected"),
    [("1080p", "1080p", None), ("1080p", "SD", "upscale"), ("720p", "1080p", "downscale"),
     ("2160p", "1080p", "upscale"), ("576p", "SD", None), ("480i", "SD", None),
     ("4K", "2160p", None), (None, "1080p", None), ("1080p", None, None),
     ("weird", "1080p", None)],
)  # fmt: skip
def test_resolution_mismatch(tag: str | None, actual: str | None, expected: str | None) -> None:
    assert resolution_mismatch(tag, actual) == expected


@pytest.mark.parametrize(
    ("spans", "missing"),
    [([(1, 0), (2, 0), (3, 0)], []),
     ([(1, 0), (2, 0), (5, 0)], [3, 4]),
     ([(2, 0), (3, 0)], [1]),                    # starts at E02: E01 is missing
     ([(1, 3), (4, 0)], []),                     # S01E01-E03 covers 1-3
     ([(26, 0), (27, 0), (29, 0)], [28]),        # absolute numbering: no gap before 26
     ([], [])],
)  # fmt: skip
def test_missing_episodes(spans: list[tuple[int, int]], missing: list[int]) -> None:
    assert missing_episodes(spans) == missing


def test_ranges() -> None:
    assert ranges([1, 2, 3, 7, 9, 10]) == "E01–E03, E07, E09–E10"
    assert ranges(list(range(1, 60, 2)), limit=3).endswith("and 27 more")


def test_every_check_is_documented() -> None:
    for code, chk in CHECKS.items():
        assert chk.code == code and chk.heading and chk.explain
        assert chk.severity in {"error", "warning", "info"}


# --------------------------------------------------------------------------- full run

TV = {
    "Show (2010) {tvdb-1}/Season 01/Show (2010) - S01E01 - A [WEBDL-1080p].mkv": 1000,
    "Show (2010) {tvdb-1}/Season 01/Show (2010) - S01E02 - B [WEBDL-1080p].mkv": 1000,
    "Show (2010) {tvdb-1}/Season 01/Show (2010) - S01E02 - B [HDTV-720p].mkv": 400,  # dup
    "Show (2010) {tvdb-1}/Season 01/Show (2010) - S01E03 - C [WEBDL-1080p].mkv": 1000,
    "Show (2010) {tvdb-1}/Season 01/Show (2010) - S01E05 - E [WEBDL-1080p].mkv": 1000,  # E04 gap
    "Show (2010) {tvdb-1}/Season 01/Show (2010) - S01E06 - F [WEBDL-1080p].mkv": 1000,
    "Show (2010) {tvdb-1}/Season 01/old.en.srt": 5,  # orphan subtitle
    "Show (2010) {tvdb-1}/Season 01/~uTorrentPartFile_1.dat": 50,  # stray
    "Show (2010) {tvdb-1}/Extras/Commentaries/Commentary.mp3": 70,  # bonus: not stray
    "Show (2010) {tvdb-1}/Season 02/Show (2010) - S02E01 - Disc.iso": 900,
    "Show (2010) {tvdb-1}/Season 02/Show (2010) - Pilot unaired.mkv": 300,  # no number
    "No Ids Show/Season 01/No Ids Show - S01E01 - X.mkv": 300,
}
MOVIES = {
    "Heat (1995) {tmdb-949}/Heat (1995) {tmdb-949} [Bluray-1080p].mkv": 2000,
    "Heat (1995) {tmdb-949}/Heat (1995) {tmdb-949} [DVD-480p].mkv": 700,
    "Heat (1995) {tmdb-949}/Heat (1995) {tmdb-949} [Bluray-1080p].nfo": 3,  # allowed
    "Broken (2001)/Broken corrupt (2001).mkv": 10,
}


@pytest.fixture
def scanned(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> tuple[Any, Config]:
    tv = make_tree("tv", dict(TV))
    movies = make_tree("movies", dict(MOVIES))
    cfg = make_config(tv=(tv, "tv"), movies=(movies, "movies"))
    cfg.flags = FlagsConfig(min_peers=4)
    conn = connect(cfg.database)
    run_scan(conn, cfg, analyzer=FakeAnalyzer({"2160p": "hdr10_hevc_2160p_cropped_truehd_atmos"}))
    return conn, cfg


def _codes(conn: Any) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for r in conn.execute("SELECT * FROM flags ORDER BY id"):
        out.setdefault(r["code"], []).append(r)
    return out


def test_checks_on_a_scanned_library(scanned: tuple[Any, Config]) -> None:
    conn, _ = scanned
    flags = _codes(conn)
    assert flags["unreadable"][0]["message"].startswith("MediaInfo found no tracks")
    assert flags["unreadable"][0]["message"].endswith("· 10 B")
    assert len(flags["disc_image"]) == 1 and ".iso" in flags["disc_image"][0]["message"]
    assert "unreadable" not in {f["code"] for f in flags["disc_image"]}
    strays = [f["message"] for f in flags["stray_file"]]
    assert strays == ["Not a media file (.dat), 50 B"]  # .nfo allowed, extras mp3 skipped
    assert len(flags["orphan_subtitle"]) == 1

    dup = flags["duplicate_episode"][0]
    assert dup["season_number"] == 1 and dup["bytes_recoverable"] == 400
    assert dup["message"].startswith("S01E02: 2 files")
    movie = flags["multi_version_movie"][0]
    assert movie["bytes_recoverable"] == 700 and "2 versions" in movie["message"]

    assert "missing E04" in flags["missing_episodes"][0]["message"]
    assert len(flags["unnumbered_episode"]) == 1
    assert {CHECKS[c].severity for c in flags} >= {"error", "warning", "info"}
    no_ids = {r["message"] for r in flags["no_ids"]}
    assert no_ids == {"No ID in the folder or file names"}
    titles = {r["title_id"] for r in flags["no_ids"]}
    names = {conn.execute("SELECT name FROM titles WHERE id = ?", (t,)).fetchone()[0]
             for t in titles}  # fmt: skip
    assert "No Ids Show" in names and "Heat" not in names


def test_peer_comparisons(scanned: tuple[Any, Config]) -> None:
    conn, cfg = scanned
    # Make one episode much shorter and lower-bitrate than the rest of its season.
    fid = conn.execute("SELECT id FROM files WHERE filename LIKE '%S01E03%'").fetchone()[0]
    conn.execute("UPDATE video_info SET gb_per_hour = 1.5")  # tiny test files: ~0 GB/h
    conn.execute("UPDATE video_info SET duration_s = 120, gb_per_hour = 0.01 WHERE file_id = ?",
                 (fid,))  # fmt: skip
    compute_flags(conn, cfg)
    flags = _codes(conn)
    short = flags["short_runtime"]
    assert [r["file_id"] for r in short] == [fid] and "2 min long" in short[0]["message"]
    assert any(r["file_id"] == fid and "(low)" in r["message"] for r in flags["bitrate_outlier"])


def test_checks_can_be_switched_off(scanned: tuple[Any, Config]) -> None:
    conn, cfg = scanned
    cfg.flags.disabled = ["no_ids"]
    cfg.flags.disabled_by_library = {"movies": ["multi_version_movie", "unreadable"]}
    codes = {f.code for f in collect(conn, cfg)}
    assert "no_ids" not in codes and "multi_version_movie" not in codes
    assert "unreadable" not in codes and "duplicate_episode" in codes


def test_ensure_flags_recomputes_when_settings_change(scanned: tuple[Any, Config]) -> None:
    conn, cfg = scanned
    assert ensure_flags(conn, cfg) is False  # the scan already computed them
    cfg.flags.disabled = ["stray_file"]
    assert ensure_flags(conn, cfg) is True
    assert "stray_file" not in _codes(conn)
    assert rederive(conn, cfg) > 0  # rederive recomputes too
    assert "stray_file" not in _codes(conn)


# --------------------------------------------------------------------------- page + ignore


@pytest.fixture
def client(scanned: tuple[Any, Config], tmp_path: Path) -> TestClient:
    _, cfg = scanned
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    return TestClient(create_app(ScanService(cfg_file)))


def test_issues_page(client: TestClient) -> None:
    page = client.get("/issues").text
    assert "Unreadable or not a video" in page and "Same episode stored more than once" in page
    assert page.index("Unreadable or not a video") < page.index("Gaps in episode numbering")
    assert "400 B recoverable" in page
    assert ">Issues" in page and 'class="count"' in page  # nav badge
    dups = client.get("/issues?category=duplicates").text
    assert "Same episode stored more than once" in dups and "Unreadable" not in dups
    assert "Disc images" in client.get("/issues?severity=error").text
    one = client.get("/issues?code=missing_episodes").text
    assert "All issues" in one and "missing E04" in one
    csv_text = client.get("/issues.csv").text
    assert csv_text.startswith("severity,category,check") and "missing E04" in csv_text


def test_ignore_until_files_change(client: TestClient, scanned: tuple[Any, Config]) -> None:
    conn, cfg = scanned
    key = conn.execute("SELECT key FROM flags WHERE code = 'duplicate_episode'").fetchone()[0]
    r = client.post("/issues/ignore", params={"key": key}, headers={"accept": "application/json"})
    assert r.status_code == 200 and r.json()["changed"]
    assert "S01E02: 2 files" not in client.get("/issues").text
    assert "S01E02: 2 files" in client.get("/issues?ignored=1").text

    compute_flags(conn, cfg)  # a rescan keeps the ignore
    assert "S01E02: 2 files" not in client.get("/issues").text

    # One of the files changes: the ignore no longer applies.
    conn.execute("UPDATE files SET size_bytes = 401 WHERE filename LIKE '%S01E02%720p%'")
    compute_flags(conn, cfg)
    assert "S01E02: 2 files" in client.get("/issues").text

    r = client.post("/issues/unignore", params={"key": key, "next": "/issues"},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == "/issues"
    assert conn.execute("SELECT count(*) FROM flag_ignores").fetchone()[0] == 0
    r = client.post("/issues/ignore", params={"key": key, "next": "//evil.example"},
                    follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/issues"
    assert client.post("/issues/ignore", params={"key": key},
                       headers={"origin": "http://evil.example"}).status_code == 403  # fmt: skip


def test_issue_markers_elsewhere(client: TestClient, scanned: tuple[Any, Config]) -> None:
    conn, _ = scanned
    show = conn.execute("SELECT id FROM titles WHERE name = 'Show'").fetchone()[0]
    page = client.get(f"/titles/{show}").text
    assert 'id="issues"' in page and "Same episode stored more than once" in page
    assert "⚠" in client.get("/titles?q=Show").text
    assert "Issues</span>" in client.get("/").text


# --------------------------------------------------------------------------- folders


def test_empty_folders_keeps_only_the_topmost() -> None:
    from media_audit.scanner import empty_folders

    dirs = ["A", "A/Season 1", "B", "B/Season 01", "B/Season 01/Subs", "C", "C/x", "D"]
    files = ["B/Season 01/ep.mkv"]
    # C/x couldn't be listed: unknown contents, so neither it nor C is reported.
    assert empty_folders(dirs, files, ["C/x"]) == ["A", "B/Season 01/Subs", "D"]


FOLDER_TV = {
    "Show {tvdb-2}/Season 01/Show - S01E01 - A.mkv": 1000,
    "Show {tvdb-2}/Season 01/Show - S01E02 - B.mkv": 1000,
    "Show {tvdb-2}/Season 1/season.nfo": 3,  # left behind by a Season 1 -> Season 01 rename
    "Show {tvdb-2}/Season 1/folder.jpg": 20,
    "Show {tvdb-2}/Season 02/Show - S02E01 - C.mkv": 1000,
    "Show {tvdb-2}/Season 3/poster.jpg": 30,  # season folder with no videos
    "Show {tvdb-2}/tvshow.nfo": 5,
    "Gone Show/tvshow.nfo": 5,  # every video gone: one issue for the show, not per season
    "Gone Show/Season 01/episode.nfo": 5,
}
FOLDER_MOVIES = {
    "Kept (2001)/Kept (2001).mkv": 2000,
    "Old Movie (1990)/Old Movie (1990).nfo": 4,
    "Old Movie (1990)/poster.jpg": 40,
}


@pytest.fixture
def folders(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> tuple[Any, Config, Path]:
    tv = make_tree("tv", dict(FOLDER_TV))
    movies = make_tree("movies", dict(FOLDER_MOVIES))
    for d in ["Show {tvdb-2}/Season 2", "Show {tvdb-2}/Season 01/Subs",
              "Empty Show/Season 01"]:  # fmt: skip
        (tv / d).mkdir(parents=True)
    (tv / "Show {tvdb-2}/Season 2/.DS_Store").write_bytes(b"x")  # ignored: still empty
    (movies / "Blank (2000)").mkdir()
    cfg = make_config(tv=(tv, "tv"), movies=(movies, "movies"))
    conn = connect(cfg.database)
    run_scan(conn, cfg, analyzer=FakeAnalyzer({}))
    return conn, cfg, tv


def test_folder_checks(folders: tuple[Any, Config, Path]) -> None:
    conn, _, _ = folders
    flags = _codes(conn)
    show_id = conn.execute("SELECT id FROM titles WHERE name = 'Show'").fetchone()[0]

    split = {r["season_number"]: r for r in flags["split_season"]}
    assert set(split) == {1, 2}
    s1 = split[1]
    assert s1["message"] == ("Season 1 is in 2 folders: Season 01 (2 episodes, 2 KB); "
                             "Season 1 (no videos: jpg, nfo, 23 B)")  # fmt: skip
    assert s1["bytes_recoverable"] == 23 and s1["title_id"] == show_id
    assert "Season 2 (empty)" in split[2]["message"]
    assert split[2]["bytes_recoverable"] is None

    no_video = {json.loads(r["data"])["folder"]: r["message"] for r in flags["no_video_folder"]}
    assert no_video == {
        "Show {tvdb-2}/Season 3": "Season 3 folder with no videos: jpg, 30 B",
        "Gone Show": "Show folder with no videos: nfo ×2, 10 B",
        "Old Movie (1990)": "Movie folder with no videos: nfo, jpg, 44 B",
    }
    empty = {json.loads(r["data"])["folder"]: r["message"] for r in flags["empty_folder"]}
    # Season 2 is reported as part of the split season, not again as empty.
    assert empty == {
        "Show {tvdb-2}/Season 01/Subs": "Empty folder",
        "Empty Show": "Empty show folder",
        "Blank (2000)": "Empty movie folder",
    }


def test_folder_issues_clear_after_cleanup(folders: tuple[Any, Config, Path]) -> None:
    import shutil

    conn, cfg, tv = folders
    shutil.rmtree(tv / "Show {tvdb-2}/Season 1")
    shutil.rmtree(tv / "Show {tvdb-2}/Season 2")
    shutil.rmtree(tv / "Empty Show")
    run_scan(conn, cfg, analyzer=FakeAnalyzer({}))
    flags = _codes(conn)
    assert "split_season" not in flags
    assert "Empty Show" not in {json.loads(r["data"])["folder"] for r in flags["empty_folder"]}


def test_folder_issues_on_the_page(folders: tuple[Any, Config, Path], tmp_path: Path) -> None:
    _, cfg, _ = folders
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    client = TestClient(create_app(ScanService(cfg_file)))
    page = client.get("/issues").text
    assert "Seasons split across folders" in page and "Show {tvdb-2}/Season 1/" in page
    assert "Empty folders" in page and "Blank (2000)/" in page
    csv_text = client.get("/issues.csv").text
    assert "Old Movie (1990)/" in csv_text
