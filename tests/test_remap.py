"""Remapping a library to a renamed/moved folder: database, detection, config.yml."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from media_audit import remap
from media_audit.config import load_config
from media_audit.db import connect
from media_audit.pipeline import run_scan
from tests.conftest import FakeAnalyzer

MOVIES = {
    "Heat (1995)/Heat (1995).mkv": 2000,
    "Heat (1995)/Heat (1995).en.srt": 10,
    "Alien (1979)/Alien (1979).mkv": 1500,
    "Old Movie (1990)/notes.txt": 7,  # a stray file, so there's an issue to ignore
}
FOUR_K = {  # same title folders, different files: must not look like the same library
    "Heat (1995)/Heat (1995) [2160p].mkv": 8000,
    "Alien (1979)/Alien (1979) [2160p].mkv": 7000,
    "Old Movie (1990)/Old Movie (1990) [2160p].mkv": 6000,
}


def _write_config(path: Path, db: Path, libs: list[tuple[str, Path, str]]) -> None:
    lines = [f"database: {db}", "# my libraries", "libraries:"]
    for name, p, kind in libs:
        lines += [f"  - name: {name}  # keep this comment", f"    path: {p}", f"    type: {kind}"]
    lines += ["", "scan:", "  schedule: off", ""]
    path.write_text("\n".join(lines))


@pytest.fixture
def lib(make_tree: Callable[[str, dict[str, int | str]], Path], tmp_path: Path) -> dict[str, Any]:
    media = tmp_path / "media"
    movies = make_tree("media/movies", dict(MOVIES))
    four_k = make_tree("media/movies-4k", dict(FOUR_K))
    cfg_path = tmp_path / "config.yml"
    db = tmp_path / "db" / "media-audit.db"
    _write_config(cfg_path, db, [("movies", movies, "movies"), ("movies-4k", four_k, "movies")])
    cfg = load_config(cfg_path, environ={})
    conn = connect(db)
    analyzer = FakeAnalyzer({})
    run_scan(conn, cfg, analyzer=analyzer)
    key = conn.execute(
        "SELECT key, fingerprint, code FROM flags WHERE code = 'stray_file'"
    ).fetchone()
    conn.execute(
        "INSERT INTO flag_ignores (key, fingerprint, code, created_at) VALUES (?, ?, ?, 'x')",
        (key["key"], key["fingerprint"], key["code"]),
    )
    conn.commit()
    return {"conn": conn, "cfg_path": cfg_path, "db": db, "media": media, "movies": movies,
            "four_k": four_k, "analyzer": analyzer}  # fmt: skip


def _paths(conn: Any, name: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT f.path FROM files f JOIN libraries l ON l.id = f.library_id "
        "WHERE l.name = ? AND f.deleted_at IS NULL", (name,))}  # fmt: skip


def test_rename_detected_and_remapped(lib: dict[str, Any]) -> None:
    conn, media = lib["conn"], lib["media"]
    lib["movies"].rename(media / "movies2")
    cfg = load_config(lib["cfg_path"], environ={})

    (s,) = remap.suggestions(conn, cfg)
    assert (s.library, s.old_path, s.new_path, s.name) == (
        "movies",
        str(lib["movies"]),
        str(media / "movies2"),
        "movies",
    )
    assert s.comparison.folders_matched == 3 and s.comparison.file_share == 1.0
    # movies-4k has the same title folders but not the same files: not suggested.
    assert remap.compare(conn, 1, str(lib["four_k"])).likely is False

    edit = remap.plan_config_edit(lib["cfg_path"], "movies", "movies2", s.new_path, "movies",
                                  environ={})  # fmt: skip
    assert edit.changed and "# keep this comment" in edit.new_text
    assert "# my libraries" in edit.new_text
    remap.write_config(edit, datetime(2026, 11, 22))
    result = remap.apply_remap(conn, lib["db"], "movies", s.new_path, "movies2")
    assert result.files == 4 and result.backup is not None and result.backup.is_file()
    assert all(p.startswith(f"{media}/movies2/") for p in _paths(conn, "movies2"))
    assert conn.execute("SELECT count(*) FROM libraries WHERE name = 'movies'").fetchone()[0] == 0

    # The next scan finds everything where it expects it: nothing re-read, nothing new.
    before = sum(lib["analyzer"].calls.values())
    summary = run_scan(conn, load_config(lib["cfg_path"], environ={}), analyzer=lib["analyzer"])
    assert (summary.new, summary.deleted, summary.status) == (0, 0, "ok")
    assert sum(lib["analyzer"].calls.values()) == before
    # The ignored stray file is still ignored, under the new library name.
    key = conn.execute("SELECT key FROM flag_ignores").fetchone()[0]
    assert conn.execute("SELECT 1 FROM flags f JOIN flag_ignores i ON i.key = f.key "
                        "AND i.fingerprint = f.fingerprint").fetchone(), key  # fmt: skip
    assert remap.suggestions(conn, load_config(lib["cfg_path"], environ={})) == []


def test_new_folder_already_scanned_as_a_new_library(lib: dict[str, Any]) -> None:
    """Folder renamed *and* config pointed at it under a new name, then scanned."""
    conn, media = lib["conn"], lib["media"]
    new = media / "films"
    lib["movies"].rename(new)
    _write_config(lib["cfg_path"], lib["db"],
                  [("films", new, "movies"), ("movies-4k", lib["four_k"], "movies")])  # fmt: skip
    run_scan(conn, load_config(lib["cfg_path"], environ={}), analyzer=lib["analyzer"])
    assert _paths(conn, "films") and _paths(conn, "movies")  # duplicated history

    (s,) = remap.suggestions(conn, load_config(lib["cfg_path"], environ={}))
    assert (s.library, s.name) == ("movies", "films")  # suggests the configured name
    edit = remap.plan_config_edit(lib["cfg_path"], "movies", "films", str(new), "movies",
                                  environ={})  # fmt: skip
    assert not edit.changed and edit.new_text is not None  # config already right
    result = remap.apply_remap(conn, lib["db"], "movies", str(new), "films")
    assert result.merged == ["films"]
    old_id = conn.execute("SELECT id FROM libraries WHERE name = 'films'").fetchone()[0]
    assert old_id == 1  # the original library (and its history) kept the name


def test_same_name_new_path_is_handled_by_a_scan(lib: dict[str, Any]) -> None:
    """Editing the path by hand: the scan rewrites stored paths instead of re-reading."""
    conn, media = lib["conn"], lib["media"]
    lib["movies"].rename(media / "moved")
    libs = [("movies", media / "moved", "movies"), ("movies-4k", lib["four_k"], "movies")]
    _write_config(lib["cfg_path"], lib["db"], libs)
    before = sum(lib["analyzer"].calls.values())
    summary = run_scan(conn, load_config(lib["cfg_path"], environ={}), analyzer=lib["analyzer"])
    assert (summary.new, summary.deleted, summary.moved) == (0, 0, 0)
    assert sum(lib["analyzer"].calls.values()) == before
    assert all(p.startswith(f"{media}/moved/") for p in _paths(conn, "movies"))


def test_dismiss_and_refusals(lib: dict[str, Any]) -> None:
    conn, media = lib["conn"], lib["media"]
    lib["movies"].rename(media / "movies2")
    cfg = load_config(lib["cfg_path"], environ={})
    remap.dismiss(conn, "movies", str(media / "movies2"))
    assert remap.suggestions(conn, cfg) == []
    with pytest.raises(remap.RemapError, match="isn't a folder"):
        remap.apply_remap(conn, None, "movies", str(media / "nope"))
    with pytest.raises(remap.RemapError, match="already called"):
        remap.apply_remap(conn, None, "movies", str(media / "movies2"), "movies-4k")
    with pytest.raises(remap.RemapError, match="overlaps"):
        remap.apply_remap(conn, None, "movies", str(lib["four_k"] / "Heat (1995)"))
    with pytest.raises(remap.RemapError, match="no library"):
        remap.apply_remap(conn, None, "nope", str(media / "movies2"))


# --------------------------------------------------------------------------- config edits

BLOCK = """\
database: /config/media-audit.db
libraries:
  # the films
  - name: movies   # main library
    path: /media/movies
    type: movies
  - name: tv
    path: "/media/tv"
    type: tv

scan:
  schedule: off
"""
FLOW = """\
libraries:
  - {name: movies, path: /media/movies, type: movies}
  - {name: tv, path: /media/tv, type: tv}
"""


def _edit(tmp_path: Path, text: str, *args: str, environ: dict[str, str] | None = None) -> Any:
    (tmp_path / "media" / "films").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.yml"
    cfg.write_text(text)
    return remap.plan_config_edit(cfg, *args, environ=environ or {})


def _libs(text: str) -> list[tuple[str, str]]:
    return [(x["name"], x["path"]) for x in yaml.safe_load(text)["libraries"]]


@pytest.mark.parametrize("text", [BLOCK, FLOW])
def test_config_rename_and_move(tmp_path: Path, text: str) -> None:
    edit = _edit(tmp_path, text, "movies", "films", "/media/films", "movies")
    assert edit.changed, edit.reason
    assert _libs(edit.new_text) == [("films", "/media/films"), ("tv", "/media/tv")]
    assert edit.new_text.count("\n") == text.count("\n")  # only values changed
    if text is BLOCK:
        assert "# main library" in edit.new_text and "# the films" in edit.new_text


def test_config_drops_the_duplicate_entry(tmp_path: Path) -> None:
    films = "  - name: films\n    path: /media/films\n    type: movies\n"
    text = BLOCK.replace("\nscan:", films + "\nscan:")
    edit = _edit(tmp_path, text, "movies", "movies", "/media/films", "movies")
    assert edit.changed, edit.reason
    assert _libs(edit.new_text) == [("tv", "/media/tv"), ("movies", "/media/films")]
    assert "# the films" in edit.new_text and "scan:" in edit.new_text


def test_config_adds_an_entry_when_neither_is_there(tmp_path: Path) -> None:
    edit = _edit(tmp_path, BLOCK, "gone", "films", "/media/films", "movies")
    assert edit.changed, edit.reason
    assert _libs(edit.new_text)[-1] == ("films", "/media/films")


def test_config_quotes_awkward_values(tmp_path: Path) -> None:
    (tmp_path / "media" / "it's: here").mkdir(parents=True)
    edit = _edit(tmp_path, BLOCK, "movies", "true", "/media/it's: here", "movies")
    assert edit.changed, edit.reason
    assert _libs(edit.new_text)[0] == ("true", "/media/it's: here")


def test_config_env_override_falls_back_to_a_snippet(tmp_path: Path) -> None:
    env = {"MEDIA_AUDIT_LIBRARIES": "[{name: movies, path: /media/movies, type: movies}]"}
    edit = _edit(tmp_path, BLOCK, "movies", "films", "/media/films", "movies", environ=env)
    assert edit.new_text is None and "MEDIA_AUDIT_LIBRARIES" in edit.reason
    assert "name: films" in edit.snippet and "path: /media/films" in edit.snippet


def test_write_config_keeps_a_backup(tmp_path: Path) -> None:
    edit = _edit(tmp_path, BLOCK, "movies", "films", "/media/films", "movies")
    backup = remap.write_config(edit, datetime(2026, 11, 22, 9, 0))
    assert backup is not None and backup.read_text() == BLOCK
    assert (tmp_path / "config.yml").read_text() == edit.new_text


# --------------------------------------------------------------------------- web


def test_remap_in_the_web_ui(lib: dict[str, Any]) -> None:
    from starlette.testclient import TestClient

    from media_audit.web.app import create_app
    from media_audit.web.service import ScanService

    conn, media = lib["conn"], lib["media"]
    lib["movies"].rename(media / "movies2")
    service = ScanService(lib["cfg_path"], analyzer_factory=lambda cfg: lib["analyzer"])
    client = TestClient(create_app(service))

    scans = client.get("/scans").text
    assert "isn't at" in scans and f"<code>{media}/movies2</code> looks like the same" in scans
    assert "folder missing" in scans and "Remap…" in scans

    review = client.get("/libraries/remap", params={"library": "movies",
                                                    "path": str(media / "movies2"),
                                                    "name": "movies2"}).text  # fmt: skip
    assert "Looks like the same library." in review and "3 of 3" in review
    assert 'class="del">-  - name: movies' in review and 'class="add">+  - name: movies2' in review

    r = client.post("/libraries/remap", data={"library": "movies", "path": str(media / "movies2"),
                                              "name": "movies2", "action": "remap"})  # fmt: skip
    assert r.status_code == 200 and "Library remapped" in r.text, r.text
    service.wait(30)
    assert "name: movies2" in lib["cfg_path"].read_text()
    assert list(lib["cfg_path"].parent.glob("config.yml.bak-*"))
    last = service.status()["scan"]["last"]
    assert last["status"] == "ok" and last["new"] == 0 and last["analyzed"] == 0
    assert "isn't at" not in client.get("/scans").text

    # Refused across sites, and errors are shown on the review page.
    r = client.post("/libraries/remap", data={"library": "movies2", "path": "/nope",
                                              "action": "remap"},
                    headers={"origin": "http://evil.example"})  # fmt: skip
    assert r.status_code == 403
    r = client.post("/libraries/remap", data={"library": "movies2", "path": "/nope",
                                              "action": "remap"})  # fmt: skip
    assert "isn&#39;t a folder" in r.text
    del conn


def test_dismiss_in_the_web_ui(lib: dict[str, Any]) -> None:
    from starlette.testclient import TestClient

    from media_audit.web.app import create_app
    from media_audit.web.service import ScanService

    media = lib["media"]
    lib["movies"].rename(media / "movies2")
    client = TestClient(create_app(ScanService(lib["cfg_path"])))
    form = {"library": "movies", "path": str(media / "movies2"), "action": "dismiss"}
    r = client.post("/libraries/remap", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert "isn't at" not in client.get("/scans").text


def test_remap_from_the_cli(lib: dict[str, Any], capsys: Any) -> None:
    from media_audit.cli import main

    media = lib["media"]
    lib["movies"].rename(media / "movies2")
    base = ["--config", str(lib["cfg_path"]), "remap-library", "movies",
            "--path", str(media / "movies2"), "--name", "movies2"]  # fmt: skip
    assert main([*base, "--dry-run"]) == 0
    assert "3 of 3 title folders" in capsys.readouterr().out
    assert "name: movies\n" in lib["cfg_path"].read_text().replace("  # keep this comment", "")
    assert main(base) == 0
    assert "movies2 now points at" in capsys.readouterr().out
    assert "name: movies2" in lib["cfg_path"].read_text()
