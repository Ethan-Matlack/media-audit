"""Files page filters: several values per filter, "is not", and live counts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.datastructures import ImmutableMultiDict
from starlette.testclient import TestClient

from media_audit.config import Config
from media_audit.db import connect
from media_audit.pipeline import run_scan
from media_audit.web import queries
from media_audit.web.app import create_app, query_string
from media_audit.web.service import ScanService
from tests.conftest import FakeAnalyzer

ANIME = {
    "Jp Only/Jp Only - S01E01.mkv": 1000,  # audio: ja
    "Dual/Dual - S01E01.mkv": 1000,  # audio: ja + en
    "Unknown/Unknown - S01E01.mkv": 1000,  # audio: und (no language tag)
    "Silent/Silent - S01E01.mkv": 1000,  # no audio tracks at all
}
MOVIES = {"Heat (1995)/Heat (1995) [1080p].mkv": 2000}  # audio: en
AUDIO = {"Jp Only": ["ja"], "Dual": ["ja", "en"], "Unknown": ["und"], "Silent": [],
         "Heat (1995)": ["en"]}  # fmt: skip


@pytest.fixture
def db(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> tuple[Any, Config]:
    cfg = make_config(anime=(make_tree("anime", dict(ANIME)), "tv"),
                      movies=(make_tree("movies", dict(MOVIES)), "movies"))  # fmt: skip
    conn = connect(cfg.database)
    run_scan(conn, cfg, analyzer=FakeAnalyzer({}))
    # Set each file's audio tracks explicitly, so every case below is spelled out.
    conn.execute("DELETE FROM audio_tracks")
    for fid, rel in conn.execute("SELECT id, rel_path FROM files WHERE is_video = 1").fetchall():
        for i, lang in enumerate(AUDIO[rel.split("/")[0]]):
            conn.execute("INSERT INTO audio_tracks (file_id, idx, language, codec) "
                         "VALUES (?, ?, ?, 'AAC')", (fid, i, lang))  # fmt: skip
    conn.execute("UPDATE video_info SET resolution_class = NULL WHERE file_id IN "
                 "(SELECT id FROM files WHERE rel_path LIKE 'Silent/%')")  # fmt: skip
    conn.commit()
    return conn, cfg


def _names(conn: Any, query: str) -> set[str]:
    q = ImmutableMultiDict([tuple(p.split("=", 1)) for p in query.split("&") if p])
    return {r["rel_path"].split("/")[0] for r in queries.files(conn, q).rows}


def test_not_english_audio(db: tuple[Any, Config]) -> None:
    conn, _ = db
    # The example from the request: anime with no English audio track. A dual-audio file
    # has one, so it's out; a file with an untagged track or no audio at all is in.
    assert _names(conn, "library=anime&alang=en&alang_not=1") == {"Jp Only", "Unknown", "Silent"}
    assert _names(conn, "alang=en") == {"Dual", "Heat (1995)"}
    assert _names(conn, "alang=en&alang=ja") == {"Jp Only", "Dual", "Heat (1995)"}  # either
    assert _names(conn, "alang=en&alang=ja&alang_not=1") == {"Unknown", "Silent"}  # neither
    assert _names(conn, "alang=und") == {"Unknown"}  # "(unknown)" can be picked


def test_is_not_keeps_missing_values(db: tuple[Any, Config]) -> None:
    conn, _ = db
    everything = {"Jp Only", "Dual", "Unknown", "Silent", "Heat (1995)"}
    res = {r["value"] for r in queries.facet_counts(conn, {})["resolution"]}
    some = sorted(res - {queries.NONE})[0]
    shown = _names(conn, f"resolution={some}&resolution_not=1")
    assert "Silent" in shown  # its resolution is unknown, so it's "not <some>"
    assert _names(conn, "resolution=(none)") == {"Silent"}
    assert _names(conn, "resolution=(none)&resolution_not=1") == everything - {"Silent"}
    assert _names(conn, "library=anime&library=movies") == everything
    assert _names(conn, "library=movies&library_not=1") == everything - {"Heat (1995)"}


def test_path_does_not_contain(db: tuple[Any, Config]) -> None:
    conn, _ = db
    assert _names(conn, "q=S01E01&q_not=1") == {"Heat (1995)"}


def test_live_counts_follow_the_other_filters(db: tuple[Any, Config]) -> None:
    conn, _ = db
    q = ImmutableMultiDict([("library", "anime"), ("alang", "en"), ("alang_not", "1")])
    facets = queries.facet_counts(conn, q)
    langs = {r["value"]: (r["count"], r["selected"], r["label"]) for r in facets["alang"]}
    # Counted within anime only, and not narrowed by the language filter itself.
    assert langs == {"ja": (2, False, "ja"), "en": (1, True, "en"),
                     "und": (1, False, "(unknown)")}  # fmt: skip
    libs = {r["value"]: r["count"] for r in facets["library"]}
    assert libs == {"anime": 3}  # the library list counts files without English audio
    # A selected value that no longer matches anything stays listed (so it can be unticked).
    q = ImmutableMultiDict([("library", "movies"), ("slang", "fr")])
    assert any(r["value"] == "fr" and r["count"] == 0 and r["selected"]
               for r in queries.facet_counts(conn, q)["slang"])  # fmt: skip


def test_query_string_keeps_repeated_values() -> None:
    q = ImmutableMultiDict([("alang", "en"), ("alang", "ja"), ("page", "2")])
    assert query_string(q, page=None) == "?alang=en&alang=ja"
    assert query_string(q, alang=None) == "?page=2"
    assert query_string(q, alang=["fr"]) == "?page=2&alang=fr"
    assert query_string({"a": "1"}, b="2") == "?a=1&b=2"


def test_files_page_multi_select(db: tuple[Any, Config], tmp_path: Path) -> None:
    _, cfg = db
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    client = TestClient(create_app(ScanService(cfg_file)))
    page = client.get("/files?library=anime&alang=en&alang_not=1").text
    assert 'class="result-count"><strong>3</strong>' in page
    assert 'name="alang" value="en" checked' in page
    assert 'name="alang_not" value="1" checked' in page
    assert "Audio language: not en" in page  # the active-filter chip
    # Removing the chip drops both the values and the switch, and keeps the rest.
    chip = page.split('class="chip" href="')[2].split('"')[0].replace("&amp;", "&")
    assert chip == "?library=anime"
    # Sorting and paging links keep every selected value.
    two = client.get("/files?alang=en&alang=ja").text
    assert "alang=en&amp;alang=ja" in two.split("<thead>")[1].split("</thead>")[0]
    csv = client.get("/files.csv?alang=en&alang=ja").text
    assert len(csv.strip().splitlines()) == 1 + 3


def test_pager_first_and_last(db: tuple[Any, Config], tmp_path: Path, monkeypatch: Any) -> None:
    import re

    _, cfg = db
    monkeypatch.setattr(queries, "PAGE_SIZE", 2)  # 5 files -> 3 pages
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    client = TestClient(create_app(ScanService(cfg_file)))

    def links(url: str) -> dict[str, str]:
        parts = client.get(url).text.split('<nav class="pager"')
        nav = parts[1].split("</nav>")[0] if len(parts) > 1 else ""
        return {rel: href.replace("&amp;", "&")
                for href, rel in re.findall(r'href="([^"]*)" rel="(\w+)"', nav)}  # fmt: skip

    assert links("/files?alang=ja") == {}  # 2 files: one page, no pager
    first = links("/files")
    assert first == {"next": "?page=2", "last": "?page=3"}  # First/Prev shown, not links
    middle = links("/files?library=anime&library=movies&page=2")
    assert middle == {"first": "?library=anime&library=movies",
                      "prev": "?library=anime&library=movies&page=1",
                      "next": "?library=anime&library=movies&page=3",
                      "last": "?library=anime&library=movies&page=3"}  # fmt: skip
    assert set(links("/files?page=3")) == {"first", "prev"}


# --------------------------------------------------------------------------- grouping

GROUP_TV = {  # file -> audio languages
    "Show A/Season 01/Show A - S01E01.mkv": ["ja"],
    "Show A/Season 01/Show A - S01E02.mkv": ["ja"],
    "Show A/Season 01/Show A - S01E03.mkv": ["ja"],  # the whole season pack lacks English
    "Show A/Season 02/Show A - S02E01.mkv": ["ja", "en"],
    "Show A/Season 02/Show A - S02E02.mkv": ["ja"],  # one odd episode
    "Show A/Specials/Show A - S00E01.mkv": ["ja"],
    "Show B/Season 01/Show B - S01E01.mkv": ["en"],
}
GROUP_MOVIES = {"Movie (2000)/Movie (2000).mkv": ["ja"]}


@pytest.fixture
def grouped(
    make_tree: Callable[[str, dict[str, int | str]], Path], make_config: Callable[..., Config]
) -> tuple[Any, Config]:
    cfg = make_config(
        tv=(make_tree("tv", dict.fromkeys(GROUP_TV, 1000)), "tv"),
        movies=(make_tree("movies", dict.fromkeys(GROUP_MOVIES, 3000)), "movies"),
    )
    conn = connect(cfg.database)
    run_scan(conn, cfg, analyzer=FakeAnalyzer({}))
    conn.execute("DELETE FROM audio_tracks")
    audio = {**GROUP_TV, **GROUP_MOVIES}
    for fid, rel in conn.execute("SELECT id, rel_path FROM files WHERE is_video = 1").fetchall():
        for i, lang in enumerate(audio[rel]):
            conn.execute("INSERT INTO audio_tracks (file_id, idx, language, codec) "
                         "VALUES (?, ?, ?, 'AAC')", (fid, i, lang))  # fmt: skip
    conn.commit()
    return conn, cfg


def _groups(conn: Any, query: str) -> dict[tuple[str, Any], tuple[int, int]]:
    q = ImmutableMultiDict([tuple(p.split("=", 1)) for p in query.split("&") if p])
    data = queries.file_groups(conn, q)
    return {(r["title_name"], r["season"]): (r["n"], r["of"]) for r in data["rows"]}


def test_group_by_season_and_title(grouped: tuple[Any, Config]) -> None:
    conn, _ = grouped
    no_en = "alang=en&alang_not=1"
    # "N of M": M is every file in that season, so 3 of 3 = the whole season pack.
    assert _groups(conn, f"{no_en}&by=season") == {
        ("Show A", 1): (3, 3), ("Show A", 2): (1, 2), ("Show A", 0): (1, 1),
        ("Movie", None): (1, 1),
    }  # fmt: skip
    assert _groups(conn, f"{no_en}&by=title") == {("Show A", None): (5, 6),
                                                      ("Movie", None): (1, 1)}  # fmt: skip
    data = queries.file_groups(conn, ImmutableMultiDict([("alang", "en"), ("alang_not", "1"),
                                                         ("by", "season")]))  # fmt: skip
    assert (data["files"], data["total"], data["titles"]) == (6, 4, 2)
    # Largest first by default (the movie is 3 KB, Show A S01 is 3 x 1 KB).
    assert [(r["title_name"], r["season"]) for r in data["rows"]][:1] == [("Movie", None)]
    s2 = next(r for r in data["rows"] if r["season"] == 2)
    assert s2["audio_languages"] == ["ja"]  # only the matching file's languages


def test_group_drill_down(grouped: tuple[Any, Config]) -> None:
    conn, _ = grouped
    tid = conn.execute("SELECT id FROM titles WHERE name = 'Show A'").fetchone()[0]

    def names(query: str) -> list[str]:
        q = ImmutableMultiDict([tuple(p.split("=", 1)) for p in query.split("&") if p])
        return sorted(r["filename"] for r in queries.files(conn, q).rows)

    # "Files →" on Show A / Season 2 lists exactly that group's matching files.
    assert names(f"alang=en&alang_not=1&title={tid}&season=2") == ["Show A - S02E02.mkv"]
    assert len(names(f"title={tid}&season=2")) == 2
    assert names(f"title={tid}&season=0") == ["Show A - S00E01.mkv"]
    assert len(names(f"title={tid}")) == 6
    assert queries.title_label(conn, str(tid), "2") == "Show A · Season 2"
    assert queries.title_label(conn, str(tid), "0") == "Show A · Specials"


def test_grouped_page_and_csv(grouped: tuple[Any, Config], tmp_path: Path) -> None:
    import csv
    import io

    conn, cfg = grouped
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    client = TestClient(create_app(ScanService(cfg_file)))
    page = client.get("/files?alang=en&alang_not=1&by=season").text
    assert "<strong>6</strong> files" in page and "in 4 seasons and movies (2 titles)" in page
    assert "<strong>3</strong> of 3" in page and "<strong>1</strong> of 2" in page
    assert ">Specials<" in page and ">Movie<" in page
    tid = conn.execute("SELECT id FROM titles WHERE name = 'Show A'").fetchone()[0]
    drill = f"/files?alang=en&amp;alang_not=1&amp;title={tid}&amp;season=2"
    assert f'href="{drill}"' in page  # "Files →" keeps the filters, drops the grouping
    detail = client.get(drill.replace("&amp;", "&")).text
    assert "In Show A · Season 2" in detail and 'name="season" value="2"' in detail

    rows = list(csv.DictReader(io.StringIO(
        client.get("/files.csv?alang=en&alang_not=1&by=season").text)))  # fmt: skip
    assert len(rows) == 4
    s1 = next(r for r in rows if r["title"] == "Show A" and r["season"] == "1")
    assert (s1["matching_files"], s1["files_in_group"], s1["audio_languages"]) == ("3", "3", "ja")


def test_grouping_and_release_group_filter_coexist(grouped: tuple[Any, Config]) -> None:
    conn, _ = grouped
    # "group" is the release-group filter; grouping is "by". Both at once must work.
    both = _groups(conn, "by=season&group=(none)")
    assert both == _groups(conn, "by=season") and len(both) == 5
