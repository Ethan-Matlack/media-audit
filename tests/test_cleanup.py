"""Cleanup scripts: what goes in them, and running them for real against a temp library."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from media_audit import cleanup
from media_audit.classify import QUARANTINE_DIR
from media_audit.config import CleanupConfig, Config
from media_audit.db import connect
from media_audit.pipeline import run_scan
from media_audit.web.app import create_app
from media_audit.web.service import ScanService
from tests.conftest import FakeAnalyzer

WEIRD = "It's $weird `x` (2001)"
TV: dict[str, int | str] = {
    "Show {tvdb-2}/Season 01/Show - S01E01 - A.mkv": 1000,
    "Show {tvdb-2}/Season 01/Show - S01E01 - A.en.srt": 10,
    "Show {tvdb-2}/Season 01/old name.en.srt": 5,  # orphan subtitle
    "Show {tvdb-2}/Season 01/notes.txt": 7,  # stray
    "Show {tvdb-2}/Season 3/poster.jpg": 30,  # season folder with no videos
    "Gone Show/tvshow.nfo": 5,  # whole show folder with no videos
    "Gone Show/Season 01/episode.nfo": 5,
    f"{WEIRD}/Season 01/{WEIRD} - S01E01.mkv": 900,
    f'{WEIRD}/Season 01/a\'b "c" $(touch PWNED) -rf.txt': 3,  # stray, nasty name
}
MOVIES: dict[str, int | str] = {
    "Kept (2001)/Kept (2001).mkv": 2000,
    "Old Movie (1990)/Old Movie (1990).nfo": 4,
}
EMPTY_DIRS = ["tv/Show {tvdb-2}/Season 1",  # empty half of a split season
              "tv/Gone Show/Season 02",  # inside a junk folder: handled with it
              f"tv/{WEIRD}/Empty 'q' $HOME",
              "movies/Blank (2000)/Sub/Deeper"]  # fmt: skip


@pytest.fixture
def lib(
    make_tree: Callable[[str, dict[str, int | str]], Path],
    make_config: Callable[..., Config],
    tmp_path: Path,
) -> tuple[Any, Config, Path]:
    tv = make_tree("lib/tv", dict(TV))
    movies = make_tree("lib/movies", dict(MOVIES))
    for d in EMPTY_DIRS:
        (tmp_path / "lib" / d).mkdir(parents=True)
    cfg = make_config(tv=(tv, "tv"), movies=(movies, "movies"))
    conn = connect(cfg.database)
    run_scan(conn, cfg, analyzer=FakeAnalyzer({}))
    return conn, cfg, tmp_path / "lib"


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) + ("/" if p.is_dir() else "") for p in root.rglob("*")}


def _script(conn: Any, cfg: Config, tmp_path: Path, **where: Any) -> Path:
    plan = cleanup.build_plan(conn, cfg, *where.get("filter", ("1 = 1", [])))
    now = datetime(2026, 11, 12, 15, 30, 0)
    return cleanup.save_script(cleanup.render_script(plan, now), tmp_path / "scripts",
                               cleanup.script_name(now))  # fmt: skip


def _run(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          cwd=cwd, stdin=subprocess.DEVNULL, check=False)  # fmt: skip


def test_plan_contents(lib: tuple[Any, Config, Path]) -> None:
    conn, cfg, _ = lib
    plan = cleanup.build_plan(conn, cfg)
    by_key = {s.key: s for s in plan.sections}
    assert (by_key["junk"].folders, by_key["junk"].files) == (3, 4)
    assert by_key["stray"].files == 2 and by_key["orphan"].files == 1
    empty = "\n".join(by_key["empty"].lines)
    assert "Season 1'" in empty and "Blank (2000)" in empty and "Empty " in empty
    assert "Gone Show/Season 02" not in empty  # removed along with its junk folder
    assert {name for name, _ in plan.roots} == {"tv", "movies"}
    assert not plan.unmapped and plan.skipped_paths == 0 and not plan.empty


def test_dry_run_changes_nothing(lib: tuple[Any, Config, Path], tmp_path: Path) -> None:
    conn, cfg, root = lib
    script = _script(conn, cfg, tmp_path)
    before = _tree(root)
    r = _run(script, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout and "would move" in r.stdout and "would remove" in r.stdout
    assert "  skip " not in r.stdout and "0 would be skipped" in r.stdout
    assert _tree(root) == before
    assert not list(tmp_path.rglob("PWNED")) and not (tmp_path / "scripts" / "PWNED").exists()
    assert not script.with_suffix(".log").exists()


def test_apply_quarantines_and_restore_puts_back(
    lib: tuple[Any, Config, Path], tmp_path: Path
) -> None:
    conn, cfg, root = lib
    script = _script(conn, cfg, tmp_path)
    before = _tree(root)
    r = _run(script, "--apply", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "READ THIS FIRST" in r.stdout and "0 skipped" in r.stdout, r.stdout
    q = root / "tv" / QUARANTINE_DIR / "20261112-153000"
    assert (q / "Show {tvdb-2}/Season 01/notes.txt").is_file()
    assert (q / f'{WEIRD}/Season 01/a\'b "c" $(touch PWNED) -rf.txt').is_file()
    assert (q / "Gone Show/tvshow.nfo").is_file()
    assert not (root / "tv/Gone Show").exists()
    assert not (root / "tv/Show {tvdb-2}/Season 1").exists()
    assert not (root / "tv/Show {tvdb-2}/Season 3").exists()
    assert not (root / "movies/Blank (2000)").exists()
    assert (root / "movies" / QUARANTINE_DIR / "20261112-153000/Old Movie (1990)").is_dir()
    # Videos and their subtitles are untouched.
    assert (root / "tv/Show {tvdb-2}/Season 01/Show - S01E01 - A.mkv").is_file()
    assert (root / "tv/Show {tvdb-2}/Season 01/Show - S01E01 - A.en.srt").is_file()
    assert not list(tmp_path.rglob("PWNED"))
    log = script.with_suffix(".log").read_text().splitlines()
    assert sum(line.startswith("  moved ") for line in log) == 7

    # A rescan doesn't look inside the quarantine, and the issues are gone.
    run_scan(conn, cfg, analyzer=FakeAnalyzer({}))
    left = {r["code"] for r in conn.execute("SELECT code FROM flags")}
    assert not left & {"stray_file", "orphan_subtitle", "no_video_folder", "empty_folder"}
    assert conn.execute("SELECT count(*) FROM files WHERE rel_path LIKE ?",
                        (f"{QUARANTINE_DIR}%",)).fetchone()[0] == 0  # fmt: skip

    restore = script.with_name(script.stem + "-restore.sh")
    r = _run(restore, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    after = {p for p in _tree(root) if not p.split("/")[1].startswith(QUARANTINE_DIR)}
    files_before = {p for p in before if not p.endswith("/")}
    assert files_before <= after  # every file is back where it was


def test_changed_and_new_things_are_skipped(lib: tuple[Any, Config, Path], tmp_path: Path) -> None:
    conn, cfg, root = lib
    script = _script(conn, cfg, tmp_path)
    (root / "tv/Show {tvdb-2}/Season 01/notes.txt").write_text("edited since the scan")
    (root / "tv/Show {tvdb-2}/Season 3/Show - S03E01.mkv").write_bytes(b"\0" * 10)
    (root / "movies/Blank (2000)/Sub/new.txt").write_text("x")
    r = _run(script, "--apply", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "notes.txt  (changed since the scan)" in r.stdout
    assert "Season 3/  (has a video now: Show - S03E01.mkv)" in r.stdout
    assert "Blank (2000)/  (not empty: Sub/new.txt)" in r.stdout
    assert (root / "tv/Show {tvdb-2}/Season 01/notes.txt").exists()
    assert (root / "tv/Show {tvdb-2}/Season 3/poster.jpg").exists()
    assert (root / "movies/Blank (2000)/Sub/new.txt").exists()


def test_symlinked_folder_is_not_followed(lib: tuple[Any, Config, Path], tmp_path: Path) -> None:
    conn, cfg, root = lib
    script = _script(conn, cfg, tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.nfo").write_text("x")
    shutil.rmtree(root / "tv/Show {tvdb-2}/Season 3")
    (root / "tv/Show {tvdb-2}/Season 3").symlink_to(outside)
    r = _run(script, "--apply", cwd=tmp_path)
    assert "Season 3/  (no longer there)" in r.stdout
    assert (outside / "keep.nfo").exists()


def test_refuses_when_library_folder_is_missing(
    lib: tuple[Any, Config, Path], tmp_path: Path
) -> None:
    conn, cfg, root = lib
    cfg.cleanup = CleanupConfig(host_media_path="/nonexistent/media", media_mount=str(root))
    script = _script(conn, cfg, tmp_path)
    before = _tree(root)
    r = _run(script, "--apply", cwd=tmp_path)
    assert r.returncode == 1 and "Stopped before doing anything" in r.stderr
    assert _tree(root) == before


def test_host_path_mapping() -> None:
    c = CleanupConfig(host_media_path="/mnt/user/media/", media_mount="/media")
    assert c.host_root("/media/tv") == "/mnt/user/media/tv"
    assert c.host_root("/media") == "/mnt/user/media"
    assert c.host_root("/mediaX/tv") is None and c.host_root("/data/tv") is None
    assert CleanupConfig().host_root("/srv/tv/") == "/srv/tv"
    with pytest.raises(ValueError, match="absolute"):
        CleanupConfig(host_media_path="mnt/user")
    with pytest.raises(ValueError, match="root"):
        CleanupConfig(host_media_path="/")
    assert CleanupConfig(host_media_path="").host_media_path is None


def test_unsafe_paths_are_refused() -> None:
    for bad in ["", "/etc/passwd", "../x", "a/../../b", "a/./b", f"{QUARANTINE_DIR}/x"]:
        assert not cleanup._safe_rel(bad), bad
    assert cleanup._safe_rel("Show/Season 1/it's $x.txt")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_script_passes_shellcheck(lib: tuple[Any, Config, Path], tmp_path: Path) -> None:
    conn, cfg, _ = lib
    script = _script(conn, cfg, tmp_path)
    r = subprocess.run(["shellcheck", "-s", "bash", str(script)], capture_output=True,
                       text=True, check=False)  # fmt: skip
    assert r.returncode == 0, r.stdout


# --------------------------------------------------------------------------- web


@pytest.fixture
def client(lib: tuple[Any, Config, Path], tmp_path: Path, monkeypatch: Any) -> TestClient:
    _, cfg, _ = lib
    monkeypatch.setattr(cleanup, "in_docker", lambda: False)
    cfg_file = tmp_path / "web.yml"
    cfg_file.write_text(f"database: {cfg.database}\nlibraries: []\nscan:\n  schedule: off\n")
    return TestClient(create_app(ScanService(cfg_file)))


def _digest(page: str) -> str:
    return page.split('name="digest" value="')[1].split('"')[0]


def test_cleanup_page_and_save(client: TestClient, lib: tuple[Any, Config, Path]) -> None:
    _, cfg, _ = lib
    issues = client.get("/issues").text
    assert 'href="/issues/cleanup' in issues
    page = client.get("/issues/cleanup").text
    assert "Read this before you use a cleanup script" in page
    assert "Folders with no videos left" in page and "Empty folders" in page
    assert "remove_empty" in page  # the preview shows the script itself
    digest = _digest(page)

    r = client.post("/issues/cleanup", data={"digest": digest, "action": "save"})
    assert "Tick the box" in r.text
    r = client.post("/issues/cleanup", data={"digest": "stale", "ack": "1", "action": "save"})
    assert "issues changed since this page was opened" in r.text
    r = client.post("/issues/cleanup", data={"digest": digest, "ack": "1", "action": "save"})
    assert r.status_code == 200 and "Cleanup script saved" in r.text
    saved = list((cfg.database.parent / "cleanup").glob("cleanup-*.sh"))
    assert len(saved) == 1 and os.access(saved[0], os.X_OK)
    assert f"bash {saved[0]}" in r.text  # outside Docker: the real path

    dl = client.get(f"/issues/cleanup/saved/{saved[0].name}")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
    assert dl.text == saved[0].read_text()
    assert client.get("/issues/cleanup/saved/..%2Fmedia-audit.db").status_code == 404
    assert client.get("/issues/cleanup/saved/notes.sh").status_code == 404
    assert saved[0].name in client.get("/issues/cleanup").text  # listed under saved scripts


def test_cleanup_download_and_filters(client: TestClient) -> None:
    page = client.get("/issues/cleanup?code=empty_folder").text
    assert "Empty folders" in page and "Folders with no videos left" not in page
    r = client.post("/issues/cleanup?code=empty_folder",
                    data={"digest": _digest(page), "ack": "1", "action": "download"})  # fmt: skip
    assert r.headers["content-type"].startswith("text/x-shellscript")
    assert "attachment" in r.headers["content-disposition"] and "quarantine 1" not in r.text
    assert client.post("/issues/cleanup", data={"ack": "1"},
                       headers={"origin": "http://evil.example"}).status_code == 403  # fmt: skip


def test_ignored_issues_are_left_out(client: TestClient, lib: tuple[Any, Config, Path]) -> None:
    conn, _, _ = lib
    key = conn.execute("SELECT key FROM flags WHERE code = 'stray_file' AND message "
                       "LIKE '%txt%' ORDER BY id LIMIT 1").fetchone()[0]  # fmt: skip
    assert client.post(f"/issues/ignore?key={key}",
                       headers={"accept": "application/json"}).status_code == 200  # fmt: skip
    page = client.get("/issues/cleanup?code=stray_file").text
    assert page.count("quarantine 1 ") + page.count("quarantine 2 ") == 1


def test_needs_host_path_in_docker(client: TestClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(cleanup, "in_docker", lambda: True)
    page = client.get("/issues/cleanup").text
    assert "Set where your media is on the host first" in page
    assert 'value="save" class="button primary" disabled' in page
