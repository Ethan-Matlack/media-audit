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
