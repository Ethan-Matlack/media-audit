"""The Settings page: set up and change everything without touching config.yml."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from media_audit.config import load_config
from media_audit.web.app import create_app
from media_audit.web.service import ScanService
from tests.conftest import FakeAnalyzer


@pytest.fixture
def setup(make_tree: Callable[[str, dict[str, int | str]], Path], tmp_path: Path,
          monkeypatch: Any) -> dict[str, Any]:  # fmt: skip
    media = tmp_path / "media"
    make_tree("media/movies", {"Heat (1995)/Heat (1995).mkv": 2000,
                               "Heat (1995)/notes.txt": 5})  # fmt: skip
    make_tree("media/tv shows", {"Show/Season 01/Show - S01E01.mkv": 900})
    make_tree("media/anime", {"Jp/Season 01/Jp - S01E01.mkv": 900})
    make_tree("elsewhere", {"secret/file.mkv": 1})
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        f"# my config\ndatabase: {tmp_path / 'db' / 'media-audit.db'}\n"
        f"media_folders: [{media}]\n"
        "libraries:\n"
        f"  - name: movies  # main\n    path: {media / 'movies'}\n    type: movies\n"
        "scan:\n  schedule: off\n"
    )
    for k in [k for k in __import__("os").environ if k.startswith("MEDIA_AUDIT_")]:
        monkeypatch.delenv(k)
    service = ScanService(cfg, analyzer_factory=lambda c: FakeAnalyzer({}))
    return {"client": TestClient(create_app(service)), "cfg": cfg, "media": media,
            "service": service, "tmp": tmp_path}  # fmt: skip


def _post(client: TestClient, url: str, pairs: list[tuple[str, str]]) -> Any:
    """A form post with repeated fields, as a browser sends it."""
    from urllib.parse import urlencode

    return client.post(url, content=urlencode(pairs),
                       headers={"content-type": "application/x-www-form-urlencoded"})  # fmt: skip


def _config(setup: dict[str, Any]) -> Any:
    return load_config(setup["cfg"], environ={})


def test_page_renders_every_section(setup: dict[str, Any]) -> None:
    page = setup["client"].get("/settings").text
    for anchor in ["libraries", "folders", "scanning", "linking", "languages", "issues",
                   "cleanup", "web"]:  # fmt: skip
        assert f'id="{anchor}"' in page, anchor
    assert 'href="/settings" aria-current="page"' in page
    # Folders that aren't libraries yet, with a guessed type.
    found = page.split('<ul class="found">')[1].split("</ul>")[0]
    assert "tv shows" in found and "anime" in found and "movies</code>" not in found


def test_add_rename_and_remove_a_library(setup: dict[str, Any]) -> None:
    client, media = setup["client"], setup["media"]
    add = {"action": "add", "name": "tv", "path": str(media / "tv shows"), "type": "tv"}
    r = client.post("/settings/libraries", data=add)
    assert r.status_code == 200 and "Added library tv" in r.text and "Scan now" in r.text
    libs = {lib.name: lib for lib in _config(setup).libraries}
    assert str(libs["tv"].path) == str(media / "tv shows") and libs["tv"].type == "tv"
    assert "# main" in setup["cfg"].read_text()  # comments kept

    # Scan so the library has history, then rename it and switch a check off for it.
    service = setup["service"]
    service.start_scan()
    service.wait(30)
    r = _post(client, "/settings/libraries", [
        ("action", "update"), ("name", "tv"), ("new_name", "shows"),
        ("path", str(media / "tv shows")), ("type", "tv"), ("enabled", "1"),
        ("off", "no_ids"), ("off", "missing_episodes")])  # fmt: skip
    assert "Saved library shows" in r.text, r.text
    cfg = _config(setup)
    assert [lib.name for lib in cfg.libraries] == ["movies", "shows"]
    assert cfg.flags.disabled_by_library == {"shows": ["no_ids", "missing_episodes"]}
    assert len(list(setup["cfg"].parent.glob("config.yml.bak-*"))) == 2  # one per save
    db = cfg.database
    import sqlite3

    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM libraries")}
    assert names == {"movies", "shows"}  # the history moved with the rename

    r = client.post("/settings/libraries", data={"action": "remove", "name": "shows"})
    assert "tick the box" in r.text and r.status_code == 400
    r = client.post("/settings/libraries", data={"action": "remove", "name": "shows",
                                                 "confirm": "1"})  # fmt: skip
    assert "Removed library shows" in r.text
    cfg = _config(setup)
    assert [lib.name for lib in cfg.libraries] == ["movies"]
    assert cfg.flags.disabled_by_library == {}  # its overrides went with it
    assert "Add back" in client.get("/settings").text  # the history is still offered


def test_library_errors_are_shown(setup: dict[str, Any]) -> None:
    client, media = setup["client"], setup["media"]
    taken = {"action": "add", "name": "movies", "path": str(media / "anime"), "type": "tv"}
    r = client.post("/settings/libraries", data=taken)
    assert r.status_code == 400 and "already a library called" in r.text
    r = client.post("/settings/libraries", data={"action": "add", "name": "x",
                                                 "path": str(media), "type": "tv"})  # fmt: skip
    assert r.status_code == 400 and "overlap" in r.text


def test_scanning_and_other_sections(setup: dict[str, Any]) -> None:
    client = setup["client"]
    r = client.post("/settings/section/scanning", data={
        "scan.schedule": "0 4 * * *", "scan.workers": "3", "scan.parse_speed": "0",
        "scan.retry_parse_speed": "0.05", "scan.retry_timeout": "60",
        "scan.follow_symlinks": "0", "scan.ignore": "@eaDir\n*.part\n.DS_Store"})  # fmt: skip
    assert "Saved: Scan schedule, Files read at once, Ignore these names." in r.text, r.text
    assert "Next scheduled scan:" in r.text  # the scheduler picked it up straight away
    cfg = _config(setup)
    assert (cfg.scan.schedule, cfg.scan.workers, cfg.scan.ignore) == (
        "0 4 * * *",
        3,
        ["@eaDir", "*.part", ".DS_Store"],
    )
    assert "parse_speed" not in setup["cfg"].read_text()  # unchanged values aren't written

    r = client.post("/settings/section/scanning", data={"scan.schedule": "every day"})
    assert r.status_code == 400 and 'value="every day"' in r.text  # the entry is kept
    r = client.post("/settings/section/scanning", data={"scan.workers": "99"})
    assert r.status_code == 400 and "between 1 and 32" in r.text

    langs = {"languages.preferred_audio": "English, ja", "languages.preferred_subtitles": ""}
    client.post("/settings/section/languages", data=langs)
    assert _config(setup).languages.preferred_audio == ["en", "ja"]
    client.post("/settings/section/linking", data={"linking.by_id": "0",
                                                   "linking.by_name_year": "1"})  # fmt: skip
    assert _config(setup).linking.by_id is False
    r = client.post("/settings/section/web", data={"web.port": "9000", "web.host": "0.0.0.0"})
    assert "Restart the container" in r.text and _config(setup).web.port == 9000


def test_issue_checks(setup: dict[str, Any]) -> None:
    from media_audit.rules.flags import CHECKS

    client = setup["client"]
    on = [c for c in CHECKS if c not in {"no_ids", "legacy_codec"}]
    data = [("flags.disabled", c) for c in on] + [
        ("flags.min_peers", "5"), ("flags.short_runtime", "0.5"), ("flags.bitrate_low", "0.4"),
        ("flags.bitrate_high", "2.5"), ("flags.movie_bitrate_low", "0.25"),
        ("flags.allowed_extensions", "nfo, jpg, png"), ("flags.strict_nfo", "0"),
        ("flags.strict_nfo", "1")]  # fmt: skip
    r = _post(client, "/settings/section/issues", data)
    assert "Saved:" in r.text, r.text
    flags = _config(setup).flags
    assert sorted(flags.disabled) == ["legacy_codec", "no_ids"]
    assert (flags.min_peers, flags.short_runtime, flags.strict_nfo) == (5, 0.5, True)
    assert flags.allowed_extensions == ["nfo", "jpg", "png"]


def test_folders_and_mappings(setup: dict[str, Any]) -> None:
    client, media, tmp = setup["client"], setup["media"], setup["tmp"]
    other = tmp / "elsewhere"
    r = _post(client, "/settings/folders", [
        ("media_folders", f"{media}\n{other}"),
        ("map_container", str(media)), ("map_host", "/mnt/user/media"),
        ("map_container", ""), ("map_host", "")])  # fmt: skip
    assert "Saved media folders and path mappings." in r.text, r.text
    cfg = _config(setup)
    assert cfg.media_folders == [str(media), str(other)]
    assert cfg.host_path(media / "movies") == "/mnt/user/media/movies"
    r = client.post("/settings/folders", data={"media_folders": str(tmp / "nope")})
    assert r.status_code == 400 and "isn&#39;t a folder" in r.text
    half = [("media_folders", str(media)), ("map_container", str(media)), ("map_host", "")]
    r = _post(client, "/settings/folders", half)
    assert r.status_code == 400 and "needs both folders" in r.text


def test_browse_stays_inside_the_media_folders(setup: dict[str, Any]) -> None:
    client, media, tmp = setup["client"], setup["media"], setup["tmp"]
    top = client.get("/settings/browse").json()
    assert [f["path"] for f in top["folders"]] == [str(media)]
    inside = client.get("/settings/browse", params={"path": str(media)}).json()
    names = {f["name"]: f for f in inside["folders"]}
    assert names["movies"]["library"] == "movies" and names["tv shows"]["type"] == "tv"
    assert inside["parent"] is None  # a media folder is the top
    for bad in [str(tmp / "elsewhere"), str(media / ".." / "elsewhere"), "/etc"]:
        r = client.get("/settings/browse", params={"path": bad})
        assert r.status_code == 400 and "only folders inside" in r.json()["error"], bad
    (media / "sneaky").symlink_to(tmp / "elsewhere")
    r = client.get("/settings/browse", params={"path": str(media / "sneaky")})
    assert r.status_code == 400


def test_locked_settings_are_read_only(setup: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.setenv("MEDIA_AUDIT_SCAN__WORKERS", "4")
    client = setup["client"]
    page = client.get("/settings").text
    assert "Set by <code>MEDIA_AUDIT_SCAN__WORKERS</code>" in page
    field = page.split('name="scan.workers"')[1].split(">")[0]
    assert "disabled" in field
    # Saving the section leaves the locked value alone and saves the rest.
    r = client.post("/settings/section/scanning", data={"scan.schedule": "0 5 * * *",
                                                        "scan.workers": "1"})  # fmt: skip
    assert r.status_code == 200
    assert "workers" not in setup["cfg"].read_text()


def test_cross_site_posts_are_refused(setup: dict[str, Any]) -> None:
    client = setup["client"]
    evil = {"origin": "http://evil.example"}
    for url in ["/settings/section/scanning", "/settings/libraries", "/settings/folders"]:
        assert client.post(url, data={}, headers=evil).status_code == 403


def test_all_checks_can_be_switched_off(setup: dict[str, Any]) -> None:
    from media_audit.rules.flags import CHECKS

    r = _post(setup["client"], "/settings/section/issues", [("flags.disabled", "")])
    assert r.status_code == 200
    assert sorted(_config(setup).flags.disabled) == sorted(CHECKS)


def test_issue_changes_apply_straight_away(setup: dict[str, Any]) -> None:
    import sqlite3

    from media_audit.rules.flags import CHECKS

    service = setup["service"]
    service.start_scan()
    service.wait(30)
    db = _config(setup).database

    def codes() -> set[str]:
        return {r[0] for r in sqlite3.connect(db).execute("SELECT code FROM flags")}

    assert "stray_file" in codes()  # Heat (1995)/notes.txt
    on = [("flags.disabled", c) for c in CHECKS if c != "stray_file"]
    _post(setup["client"], "/settings/section/issues", on)
    assert "stray_file" not in codes()


def test_saving_a_section_unchanged_changes_nothing(setup: dict[str, Any]) -> None:
    """Whatever the page shows must be accepted back as is."""
    import re
    from html import unescape

    client = setup["client"]
    page = client.get("/settings").text
    before = setup["cfg"].read_text()
    for key in ["scanning", "linking", "languages", "issues", "cleanup", "web"]:
        form = page.split(f'action="/settings/section/{key}"')[1].split("</form>")[0]
        pairs = []
        for tag in re.findall(r"<(?:input|textarea)[^>]*>(?:[^<]*</textarea>)?", form):
            name = re.search(r'name="([^"]+)"', tag)
            if not name or "disabled" in tag:
                continue
            if 'type="checkbox"' in tag and "checked" not in tag:
                continue
            if tag.startswith("<textarea"):
                value = unescape(tag.split(">", 1)[1].rsplit("</textarea>", 1)[0])
            else:
                m = re.search(r'value="([^"]*)"', tag)
                value = unescape(m.group(1)) if m else ""
            pairs.append((name.group(1), value))
        r = _post(client, f"/settings/section/{key}", pairs)
        assert r.status_code == 200 and "Nothing changed." in r.text, (key, r.text[-2000:])
    assert setup["cfg"].read_text() == before


def test_container_mapping_shows_as_a_locked_row(setup: dict[str, Any], monkeypatch: Any) -> None:
    client, media = setup["client"], setup["media"]
    monkeypatch.setenv("MEDIA_AUDIT_CLEANUP__HOST_MEDIA_PATH", "/mnt/user/media/content")
    monkeypatch.setenv("MEDIA_AUDIT_CLEANUP__MEDIA_MOUNT", str(media))
    page = client.get("/settings").text
    folders = page.split('id="folders"')[1].split("</section>")[0]
    locked_row = folders.split('class="locked-row"')[1].split("</tr>")[0]
    assert f'value="{media}" disabled' in locked_row
    assert 'value="/mnt/user/media/content" disabled' in locked_row
    assert "MEDIA_AUDIT_CLEANUP__HOST_MEDIA_PATH" in folders and "Host media path" in folders
    assert (
        "Usually there&#39;s nothing to add" in folders
        or "Usually there's nothing to add" in folders
    )
    assert "data-add-mapping" in folders and "<template data-mapping-template>" in folders

    # Saving the section doesn't copy the container's mapping into config.yml.
    r = _post(client, "/settings/folders", [("media_folders", str(media)),
                                            ("map_container", ""), ("map_host", "")])  # fmt: skip
    assert r.status_code == 200 and "path_mappings" not in setup["cfg"].read_text()

    # A mapping of your own for the same folder replaces it (and the locked row goes).
    own = [("media_folders", str(media)), ("map_container", str(media)), ("map_host", "/mnt/other")]
    _post(client, "/settings/folders", own)
    folders = client.get("/settings").text.split('id="folders"')[1].split("</section>")[0]
    assert 'class="locked-row"' not in folders
    assert _config(setup).host_path(media / "movies") == "/mnt/other/movies"


def test_schedule_empty_or_off_turns_it_off(setup: dict[str, Any]) -> None:
    client = setup["client"]
    client.post("/settings/section/scanning", data={"scan.schedule": "0 4 * * *"})
    assert _config(setup).scan.schedule == "0 4 * * *"
    client.post("/settings/section/scanning", data={"scan.schedule": ""})
    assert _config(setup).scan.schedule is None
    client.post("/settings/section/scanning", data={"scan.schedule": "30 2 * * 1"})
    client.post("/settings/section/scanning", data={"scan.schedule": "OFF"})
    assert _config(setup).scan.schedule is None
    page = client.get("/settings").text
    assert "minute hour day-of-month month day-of-week" in page and "data-cron" not in page
