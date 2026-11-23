"""The Files page's Chart view: grouping, measures, split, minimum group size, links."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.datastructures import ImmutableMultiDict
from starlette.testclient import TestClient

from media_audit.config import Config
from media_audit.db import connect
from media_audit.pipeline import run_scan
from media_audit.web import charts
from media_audit.web.app import create_app
from media_audit.web.service import ScanService
from tests.conftest import FakeAnalyzer

# folder -> (release group, resolution, dv profile, GB/h, audio languages)
MOVIES: dict[str, tuple[str, str, int | None, float, list[str]]] = {
    "A (2001)": ("FLUX", "2160p", 7, 30.0, ["en"]),
    "B (2002)": ("FLUX", "2160p", 8, 20.0, ["en", "ja"]),
    "C (2003)": ("FLUX", "2160p", 8, 10.0, ["en"]),
    "D (2004)": ("NTb", "1080p", None, 4.0, ["en"]),
    "E (2005)": ("NTb", "1080p", None, 6.0, ["ja"]),
    "F (1999)": ("Solo", "1080p", None, 50.0, ["en"]),  # one file: a thin group
}


@pytest.fixture
def db(make_tree: Callable[[str, dict[str, int | str]], Path],
       make_config: Callable[..., Config]) -> tuple[Any, Config]:  # fmt: skip
    files = {f"{name}/{name}.mkv": 1000 * (i + 1) for i, name in enumerate(MOVIES)}
    cfg = make_config(movies=(make_tree("movies", files), "movies"))
    conn = connect(cfg.database)
    run_scan(conn, cfg, analyzer=FakeAnalyzer({}))
    conn.execute("DELETE FROM audio_tracks")
    for fid, rel in conn.execute("SELECT id, rel_path FROM files").fetchall():
        group, res, dv, gbh, langs = MOVIES[rel.split("/")[0]]
        conn.execute("UPDATE video_info SET resolution_class = ?, dv_profile = ?, "
                     "gb_per_hour = ?, overall_bitrate = ? WHERE file_id = ?",
                     (res, dv, gbh, gbh * 2.2e6, fid))  # fmt: skip
        conn.execute("UPDATE release_info SET release_group = ? WHERE file_id = ?", (group, fid))
        for i, lang in enumerate(langs):
            conn.execute("INSERT INTO audio_tracks (file_id, idx, language, codec) "
                         "VALUES (?, ?, ?, 'AAC')", (fid, i, lang))  # fmt: skip
    conn.commit()
    return conn, cfg


def _chart(conn: Any, query: str) -> dict[str, Any]:
    q = ImmutableMultiDict([tuple(p.split("=", 1)) for p in query.split("&") if p])
    return charts.chart_data(conn, q)


def _bars(data: dict[str, Any]) -> dict[str, Any]:
    return {g["label"]: (round(g["amount"], 2), g["n"]) for g in data["groups"]}


def test_counts_and_shares(db: tuple[Any, Config]) -> None:
    conn, _ = db
    assert _bars(_chart(conn, "dim=resolution&measure=files")) == {
        "2160p": (3, 3), "1080p": (3, 3)}  # fmt: skip
    share = _chart(conn, "dim=group&measure=share")
    assert _bars(share) == {"FLUX": (50.0, 3), "NTb": (33.33, 2), "Solo": (16.67, 1)}
    # Filters apply: only 2160p, by DV profile.
    dv = _chart(conn, "resolution=2160p&dim=dv&measure=share")
    assert _bars(dv) == {"P8": (66.67, 2), "P7": (33.33, 1)}
    assert dv["groups"][0]["link"] == "/files?resolution=2160p&dv=P8"


def test_share_split_is_a_breakdown_of_each_bar(db: tuple[Any, Config]) -> None:
    conn, _ = db
    data = _chart(conn, "dim=resolution&measure=share&split=dv")
    uhd = next(g for g in data["groups"] if g["value"] == "2160p")
    assert uhd["amount"] == 100.0
    parts = {s["label"]: (round(s["amount"], 2), s["n"]) for s in uhd["segments"]}
    assert parts == {"P7": (33.33, 1), "P8": (66.67, 2)}
    hd = next(g for g in data["groups"] if g["value"] == "1080p")
    assert [(s["label"], s["amount"]) for s in hd["segments"]] == [("(no DV)", 100.0)]
    assert [entry["label"] for entry in data["legend"]] == ["P8", "P7", "(no DV)"]
    # A part of a bar links to both values.
    p7 = next(s for s in uhd["segments"] if s["label"] == "P7")
    assert p7["link"] == "/files?resolution=2160p&dv=P7"


def test_medians_skip_thin_groups(db: tuple[Any, Config]) -> None:
    conn, _ = db
    data = _chart(conn, "dim=group&measure=gbh_median")  # default minimum: 5 files
    assert data["groups"] == [] and data["hidden_small"] == 3
    data = _chart(conn, "dim=group&measure=gbh_median&min=2")
    assert _bars(data) == {"FLUX": (20.0, 3), "NTb": (5.0, 2)}  # Solo (1 file) left out
    assert data["hidden_small"] == 1
    avg = _chart(conn, "dim=group&measure=gbh_avg&min=1")
    assert _bars(avg)["Solo"] == (50.0, 1) and next(iter(_bars(avg))) == "Solo"
    # Split with a median: one sub-row per value, each held to the minimum too.
    split = _chart(conn, "dim=group&measure=gbh_median&min=1&split=resolution")
    flux = next(g for g in split["groups"] if g["value"] == "FLUX")
    assert [(s["label"], s["amount"]) for s in flux["segments"]] == [("2160p", 20.0)]


def test_track_dimensions_count_a_file_in_each(db: tuple[Any, Config]) -> None:
    conn, _ = db
    data = _chart(conn, "dim=alang&measure=files")
    assert _bars(data) == {"en": (5, 5), "ja": (2, 2)}  # B has both
    assert data["units"] == 6


def test_count_each_title_once(db: tuple[Any, Config]) -> None:
    conn, _ = db
    per_file = _chart(conn, "dim=resolution&measure=files&per=file")
    per_title = _chart(conn, "dim=resolution&measure=files&per=title")
    assert _bars(per_file) == _bars(per_title)  # one file per movie here
    assert per_title["unit_word"] == "title"


def test_sort_top_and_other(db: tuple[Any, Config], monkeypatch: Any) -> None:
    conn, _ = db
    by_name = _chart(conn, "dim=group&measure=files&csort=name")
    assert [g["label"] for g in by_name["groups"]] == ["FLUX", "NTb", "Solo"]
    monkeypatch.setattr(charts, "DEFAULT_TOP", 2)
    data = _chart(conn, "dim=group&measure=size")
    assert len(data["groups"]) == 2 and data["other"]["label"] == "Other (1 more)"
    assert len(_chart(conn, "dim=group&measure=size&top=all")["groups"]) == 3


def test_links_for_every_dimension(db: tuple[Any, Config]) -> None:
    conn, _ = db
    decade = _chart(conn, "dim=decade&measure=files")
    assert [g["label"] for g in decade["groups"]] == ["2000s", "1990s"]  # newest first
    link = decade["groups"][1]["link"]
    assert link.startswith("/files?year=1990&year=1991") and link.endswith("year=1999")
    files = _file_counter(db)
    assert files(link)["count"] == 1  # F (1999)
    remux = _chart(conn, "dim=remux&measure=files")
    assert remux["groups"][0]["link"] is None  # "Not remux" has no filter


def _file_counter(db: tuple[Any, Config]) -> Callable[[str], dict[str, Any]]:
    from media_audit.web import queries

    conn, _ = db

    def get(link: str) -> dict[str, Any]:
        qs = link.split("?", 1)[1]
        q = ImmutableMultiDict([tuple(p.split("=", 1)) for p in qs.split("&")])
        return {"count": queries.files(conn, q).total}

    return get


def test_chart_page_and_csv(db: tuple[Any, Config], tmp_path: Path) -> None:
    _, cfg = db
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    client = TestClient(create_app(ScanService(cfg_file)))
    page = client.get("/files?by=chart&dim=resolution&measure=share&split=dv").text
    assert 'aria-current="true">Chart</a>' in page and 'class="chart-legend"' in page
    assert "Each bar is 100% of its group." in page and "P7 33%" in page
    assert '<select name="dim" form="file-filters">' in page
    # Filters still apply, and the filter bar is still there.
    page = client.get("/files?by=chart&dim=group&measure=gbh_median&min=2").text
    assert "20.00 GB/h" in page and "5.00 GB/h" in page and "Solo" not in page.split(
        '<ol class="chart">')[1]  # fmt: skip
    rows = list(csv.reader(io.StringIO(
        client.get("/files.csv?by=chart&dim=resolution&measure=share&split=dv").text)))  # fmt: skip
    assert rows[0] == ["Resolution", "Dolby Vision profile", "Share", "files"]
    assert ["2160p", "P8", "66.667", "2"] in rows


def test_overview_links_to_charts(db: tuple[Any, Config], tmp_path: Path) -> None:
    _, cfg = db
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    client = TestClient(create_app(ScanService(cfg_file)))
    from html import unescape

    page = unescape(client.get("/?library=movies").text)
    assert 'href="/files?by=chart&dim=group&library=movies"' in page
    assert page.count('class="chart-link small"') == 9
    assert client.get("/files?by=chart&dim=group&library=movies").status_code == 200
