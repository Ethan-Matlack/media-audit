"""Scanner + pipeline tests on small generated trees (dummy bytes, fake analyzer)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from media_audit.config import Config
from media_audit.db import connect
from media_audit.pipeline import rederive, run_scan
from media_audit.scanner import ScanError
from tests.conftest import FakeAnalyzer

MOVIE_4K = (
    "Harry Potter and the Philosophers Stone (2001)/"
    "Harry Potter and the Philosophers Stone (2001) {tmdb-671} [Bluray-2160p][HDR10][DTS-X 7.1]"
    "[x265]-TERMiNAL.mkv"
)
MOVIE_HD = (
    "Harry Potter and the Philosopher's Stone (2001)/"
    "Harry Potter and the Philosophers Stone (2001) {tmdb-671} {edition-Theatrical} "
    "[WEBDL-1080p][EAC3 7.1][h265].mkv"
)

MAPPING = {"[DV]": "dv_p5_no_fallback", "2160p": "hdr10_hevc_2160p_cropped_truehd_atmos"}


def _scan(conn: Any, cfg: Config, analyzer: FakeAnalyzer | None, **kw: Any) -> Any:
    return run_scan(conn, cfg, analyzer=analyzer, **kw)


@pytest.fixture
def setup(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> tuple[Path, Path, Path, Config]:
    movies = make_tree("movies", {
        MOVIE_HD: 3000,
        "Harry Potter and the Philosopher's Stone (2001)/movie.nfo": 100,
        "Harry Potter and the Philosopher's Stone (2001)/poster.jpg": 500,
        "Harry Potter and the Philosopher's Stone (2001)/"
        "Harry Potter and the Philosophers Stone (2001) {tmdb-671} {edition-Theatrical} "
        "[WEBDL-1080p][EAC3 7.1][h265].en.forced.srt": 10,
        "Heat (1995)/Heat (1995) [Bluray-1080p].mkv": 2000,
        "Heat (1995)/Heat (1995) [Bluray-2160p][DV].mkv": 4000,
        "Heat (1995)/Featurettes/Making of.mkv": 300,
        "Heat (1995)/Sample/heat-sample.mkv": 50,
        "Harry Potter and the Philosopher's Stone (2001)/Subs/2_English.srt": 5,
        "Loose Movie (1999).avi": 700,
        "Loose Movie (1999).nfo": 7,
        "@eaDir/thumb.jpg": 1,
    })  # fmt: skip
    movies4k = make_tree("movies-4k", {MOVIE_4K: 9000})
    tv = make_tree("tv", {
        "Breaking Bad (2008) {tvdb-81189}/tvshow.nfo": 10,
        "Breaking Bad (2008) {tvdb-81189}/Season 01/Breaking Bad (2008) - S01E01 - Pilot "
        "[Bluray-1080p].mkv": 1000,
        "Breaking Bad (2008) {tvdb-81189}/Season 01/Breaking Bad (2008) - S01E02 - Cat's in "
        "the Bag [Bluray-1080p].mkv": 1000,
        "Breaking Bad (2008) {tvdb-81189}/Season 01/season01-poster.jpg": 20,
        "Breaking Bad (2008) {tvdb-81189}/Season 02/Breaking Bad (2008) - S02E01 - Seven "
        "Thirty-Seven [Bluray-1080p].mkv": 1100,
        "Breaking Bad (2008) {tvdb-81189}/Specials/Breaking Bad (2008) - S00E01 - Good Cop "
        "Bad Cop.mkv": 200,
        "Loose Show/Loose.Show.S01E03.720p.HDTV.x264-GRP.mkv": 400,
    })  # fmt: skip
    cfg = make_config(movies=(movies, "movies"), **{"movies-4k": (movies4k, "movies")},
                      tv=(tv, "tv"))  # fmt: skip
    return movies, movies4k, tv, cfg


def _titles(conn: Any) -> dict[tuple[str, str], Any]:
    return {(r["library"], r["name"]): r for r in conn.execute("SELECT * FROM v_titles")}


def test_full_scan(setup: tuple[Path, Path, Path, Config]) -> None:
    _, _, _, cfg = setup
    conn = connect(cfg.database)
    analyzer = FakeAnalyzer(MAPPING)
    s = _scan(conn, cfg, analyzer)
    assert s.status == "ok", s.errors
    assert s.seen == 19  # @eaDir ignored
    assert s.new == 19
    # Every video (main, extra, sample) is analyzed exactly once.
    assert s.analyzed == 12
    assert all(n == 1 for n in analyzer.calls.values())

    titles = _titles(conn)
    hp = titles[("movies", "Harry Potter and the Philosopher's Stone")]
    assert hp["year"] == 2001 and hp["tmdb_id"] == "671"
    assert hp["file_count"] == 5 and hp["version_count"] == 1
    assert hp["folder_size_bytes"] == 3615
    assert hp["media_size_bytes"] == 3000 and hp["other_size_bytes"] == 615

    heat = titles[("movies", "Heat")]
    assert heat["version_count"] == 2  # HD + 4K in one folder; extras/samples not counted
    assert heat["file_count"] == 4
    assert heat["has_4k"] == 1 and heat["has_dv"] == 1

    loose = titles[("movies", "Loose Movie")]
    assert loose["file_count"] == 2 and loose["year"] == 1999

    # Cross-library linking by tmdb id, even though folder names differ ("Philosopher's").
    hp4k = titles[("movies-4k", "Harry Potter and the Philosophers Stone")]
    assert hp4k["group_key"] == hp["group_key"] == "movie:tmdb:671"
    assert hp4k["has_hdr"] == 1 and hp4k["has_object_audio"] == 1
    assert heat["group_key"].startswith("title:")

    # TV: seasons from folders, specials = season 0, loose show folder uses SxxEyy.
    bb = titles[("tv", "Breaking Bad")]
    assert bb["tvdb_id"] == "81189" and bb["season_count"] == 3
    seasons = {
        r["season_number"]: r
        for r in conn.execute("SELECT * FROM seasons WHERE title_id = ?", (bb["title_id"],))
    }
    assert seasons[1]["episode_count"] == 2 and seasons[1]["folder_size_bytes"] == 2020
    assert seasons[0]["episode_count"] == 1
    loose_show = titles[("tv", "Loose Show")]
    (season,) = conn.execute("SELECT * FROM seasons WHERE title_id = ?",
                             (loose_show["title_id"],)).fetchall()  # fmt: skip
    assert season["season_number"] == 1 and season["folder_path"] is None
    ep = conn.execute(
        "SELECT season_number, episode_number FROM files WHERE filename LIKE '%S01E02%'"
    ).fetchone()
    assert tuple(ep) == (1, 2)

    # Sidecars: same-stem subtitle and Subs/ folder subtitle become external tracks.
    ext = conn.execute(
        "SELECT f.filename, s.language, s.is_forced FROM subtitle_tracks s "
        "JOIN files f ON f.id = s.file_id WHERE s.is_external = 1 ORDER BY f.filename"
    ).fetchall()
    assert [(r["language"], r["is_forced"]) for r in ext] == [("en", 1), ("en", 0)]

    # Derived data reached the DB.
    v = conn.execute("SELECT * FROM v_media WHERE library = 'movies-4k'").fetchone()
    assert v["resolution_class"] == "2160p" and v["dynamic_range"] == "HDR10"
    assert v["release_group"] == "TERMiNAL" and "TrueHD Atmos 7.1 [en]" in v["audio"]
    dv = conn.execute("SELECT * FROM v_media WHERE filename LIKE '%[DV]%'").fetchone()
    assert dv["dv_profile"] == 5 and dv["dv_has_fallback"] == 0

    snap = conn.execute("SELECT data FROM scan_snapshots").fetchone()
    assert json.loads(snap["data"])["totals"]["files"] == 19


def test_rescan_is_incremental(setup: tuple[Path, Path, Path, Config]) -> None:
    movies, _, _, cfg = setup
    conn = connect(cfg.database)
    analyzer = FakeAnalyzer(MAPPING)
    _scan(conn, cfg, analyzer)
    first_calls = sum(analyzer.calls.values())

    s = _scan(conn, cfg, analyzer)
    assert (s.new, s.changed, s.moved, s.deleted, s.analyzed) == (0, 0, 0, 0, 0)
    assert sum(analyzer.calls.values()) == first_calls

    # Modify one file, add one, delete one, rename one.
    heat_hd = movies / "Heat (1995)/Heat (1995) [Bluray-1080p].mkv"
    heat_hd.write_bytes(b"\1" * 2500)
    (movies / "New (2020)").mkdir()
    (movies / "New (2020)/New (2020).mkv").write_bytes(b"\0" * 10)
    (movies / "Loose Movie (1999).nfo").unlink()
    old = movies / "Heat (1995)/Heat (1995) [Bluray-2160p][DV].mkv"
    new = movies / "Heat (1995)/Heat (1995) {tmdb-949} [Bluray-2160p][DV][HEVC].mkv"
    old_id = conn.execute("SELECT id FROM files WHERE path = ?", (str(old),)).fetchone()[0]
    old.rename(new)

    s = _scan(conn, cfg, analyzer)
    assert (s.new, s.changed, s.moved, s.deleted) == (1, 1, 1, 1)
    assert s.analyzed == 2  # the modified file and the new one; the moved file isn't re-read
    assert analyzer.calls[str(new)] == 0

    moved = conn.execute("SELECT * FROM files WHERE id = ?", (old_id,)).fetchone()
    assert moved["path"] == str(new) and moved["mediainfo_raw"] is not None
    assert json.loads(moved["filename_parsed"])["tmdb_id"] == "949"
    assert conn.execute("SELECT dv_profile FROM video_info WHERE file_id = ?",
                        (old_id,)).fetchone()[0] == 5  # fmt: skip
    heat = _titles(conn)[("movies", "Heat")]
    assert heat["tmdb_id"] == "949"  # picked up from the renamed file

    gone = conn.execute(
        "SELECT deleted_at FROM files WHERE filename = 'Loose Movie (1999).nfo'"
    ).fetchone()
    assert gone["deleted_at"] is not None

    # The file comes back unchanged: revived without re-analysis.
    (movies / "Loose Movie (1999).nfo").write_bytes(b"\0" * 7)
    s = _scan(conn, cfg, analyzer)
    assert s.new == 0 and s.analyzed == 0


def test_hardlinks_counted_once(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> None:
    lib = make_tree("m", {"A (2000)/A (2000).mkv": 5000, "A (2000)/a.nfo": 10})
    os.link(lib / "A (2000)/A (2000).mkv", lib / "A (2000)/A (2000) copy.mkv")
    cfg = make_config(m=(lib, "movies"))
    conn = connect(cfg.database)
    _scan(conn, cfg, None)
    t = conn.execute("SELECT * FROM titles").fetchone()
    assert t["folder_size_bytes"] == 10010
    assert t["folder_unique_bytes"] == 5010


def test_empty_root_refuses_mass_delete(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> None:
    lib = make_tree("m", {"A (2000)/A (2000).mkv": 10})
    cfg = make_config(m=(lib, "movies"))
    conn = connect(cfg.database)
    _scan(conn, cfg, None)
    (lib / "A (2000)/A (2000).mkv").unlink()
    (lib / "A (2000)").rmdir()
    s = _scan(conn, cfg, None)
    assert s.status == "failed" and "refusing" in s.errors[0]
    assert conn.execute("SELECT count(*) FROM files WHERE deleted_at IS NULL").fetchone()[0] == 1
    s = _scan(conn, cfg, None, allow_empty=True)
    assert s.status == "ok" and s.deleted == 1


def test_missing_root(make_config: Callable[..., Config], tmp_path: Path) -> None:
    cfg = make_config(m=(tmp_path / "nope", "movies"))
    conn = connect(cfg.database)
    s = _scan(conn, cfg, None)
    assert s.status == "failed" and "not a directory" in s.errors[0]
    with pytest.raises(ScanError, match="unknown library"):
        _scan(conn, cfg, None, library_names=["other"])


def test_analyze_errors_recorded_and_retried(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> None:
    lib = make_tree("m", {"A (2000)/A corrupt.mkv": 10, "B (2001)/B.mkv": 10})
    cfg = make_config(m=(lib, "movies"))
    conn = connect(cfg.database)
    analyzer = FakeAnalyzer(default="audio_only")
    s = _scan(conn, cfg, analyzer)
    assert len(s.errors) == 1 and "no tracks" in s.errors[0]
    rows = {r["filename"]: r["analyze_error"] for r in conn.execute("SELECT * FROM files")}
    assert "no tracks" in rows["A corrupt.mkv"]
    assert rows["B.mkv"] == "no video stream found"
    assert _scan(conn, cfg, analyzer).analyzed == 0
    assert _scan(conn, cfg, analyzer, retry_errors=True).analyzed == 2


def test_rederive_without_reading(setup: tuple[Path, Path, Path, Config]) -> None:
    _, _, _, cfg = setup
    conn = connect(cfg.database)
    analyzer = FakeAnalyzer(MAPPING)
    _scan(conn, cfg, analyzer)
    conn.execute("DELETE FROM video_info")
    conn.execute("UPDATE titles SET group_key = NULL")
    calls = sum(analyzer.calls.values())
    count = rederive(conn, cfg)
    assert count == 19
    assert conn.execute("SELECT count(*) FROM video_info").fetchone()[0] == 12
    assert (
        conn.execute("SELECT count(*) FROM titles WHERE group_key IS NOT NULL").fetchone()[0] == 2
    )
    assert sum(analyzer.calls.values()) == calls


def test_linking_options(setup: tuple[Path, Path, Path, Config]) -> None:
    _, _, _, cfg = setup
    conn = connect(cfg.database)
    cfg.linking.by_id = False
    _scan(conn, cfg, None)
    # Names differ by an apostrophe; normalized name+year still links them.
    keys = {r["group_key"] for r in conn.execute(
        "SELECT group_key FROM titles WHERE name LIKE 'Harry Potter%'")}  # fmt: skip
    assert len(keys) == 1 and next(iter(keys)).startswith("movie:name:")
    cfg.linking.by_name_year = False
    rederive(conn, cfg)
    assert (
        conn.execute("SELECT count(*) FROM titles WHERE group_key IS NOT NULL").fetchone()[0] == 0
    )


def test_name_year_never_joins_conflicting_ids(tmp_path: Path) -> None:
    from media_audit.config import LinkingConfig
    from media_audit.titles import link_titles

    conn = connect(tmp_path / "x.db")
    conn.execute(
        "INSERT INTO libraries (id, name, path, type, created_at) "
        "VALUES (1, 'movies', '/m', 'movies', 'now'), (2, 'm4k', '/k', 'movies', 'now')"
    )
    rows = [  # (id, library, name, tmdb_id)
        (1, 1, "Lincoln", "72331"),   # really Abraham Lincoln Vampire Hunter, misfiled
        (2, 1, "Lincoln", "72976"),
        (3, 2, "Lincoln", None),      # no ID: can join either; joins the first it meets
        (4, 2, "Lincoln", "72976"),
        (5, 1, "Heat", None),
        (6, 2, "Heat", None),
    ]  # fmt: skip
    conn.executemany(
        "INSERT INTO titles (id, library_id, kind, name, year, tmdb_id, folder_path, updated_at) "
        "VALUES (?, ?, 'movie', ?, 2012, ?, 'f' || ?, 'now')",
        [(*r, r[0]) for r in rows],
    )
    link_titles(conn, LinkingConfig())
    key = {r["id"]: r["group_key"] for r in conn.execute("SELECT id, group_key FROM titles")}
    assert key[2] == key[4] == "movie:tmdb:72976"
    assert key[1] != key[2]
    assert key[5] == key[6] is not None and key[5].startswith("movie:name:")


def test_cli_end_to_end(setup: tuple[Path, Path, Path, Config], tmp_path: Path,
                        capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from media_audit.cli import main

    movies, _, tv, cfg = setup
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        f"database: {cfg.database}\nlibraries:\n"
        f"  - {{name: movies, path: '{movies}', type: movies}}\n"
        f"  - {{name: tv, path: '{tv}', type: tv}}\n"
    )
    assert main(["-c", str(cfg_file), "scan", "--no-analyze", "-q"]) == 0
    out = capsys.readouterr().out
    assert "18 files seen" in out and "0 analyzed" in out
    assert main(["-c", str(cfg_file), "stats"]) == 0
    out = capsys.readouterr().out
    assert "Breaking Bad" in out and "Largest titles" in out
    assert main(["-c", str(cfg_file), "stats", "--json", "-l", "tv"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["totals"]["main_videos"] == 5
    assert main(["-c", str(cfg_file), "rederive"]) == 0
