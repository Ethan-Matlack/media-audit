"""The settings service: every config change goes through it (CLI, remap, web UI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from media_audit.config import load_config
from media_audit.db import connect
from media_audit.settings import ConfigStore, LockedError, SettingsError, env_key

CONFIG = """\
# media-audit config (hand-written, with comments)
database: {db}

libraries:
  # the main ones
  - name: movies   # films
    path: {movies}
    type: movies
  - name: tv
    path: "{tv}"
    type: tv

scan:
  schedule: off   # no schedule while testing
"""


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    for d in ("media/movies", "media/tv", "media/anime", "archive/movies"):
        (tmp_path / d).mkdir(parents=True)
    cfg = tmp_path / "config.yml"
    cfg.write_text(CONFIG.format(db=tmp_path / "m.db", movies=tmp_path / "media/movies",
                                 tv=tmp_path / "media/tv"))  # fmt: skip
    return ConfigStore(cfg, environ={})


def _libs(store: ConfigStore) -> dict[str, Any]:
    return {lib.name: (str(lib.path), lib.type, lib.enabled) for lib in store.config().libraries}


def test_add_update_remove_library(store: ConfigStore, tmp_path: Path) -> None:
    anime = str(tmp_path / "media/anime")
    change = store.add_library("anime", anime, "tv")
    assert change.changed and any(line.startswith("+  - name: anime") for line in change.diff())
    assert store.path.read_text() == change.old_text  # nothing written yet
    backup = store.save(change)
    assert backup is not None and backup.read_text() == change.old_text
    text = store.path.read_text()
    assert "# films" in text and "# the main ones" in text and "# no schedule" in text
    assert _libs(store)["anime"] == (anime, "tv", True)

    store.save(store.update_library("anime", enabled=False))
    assert _libs(store)["anime"][2] is False
    store.save(store.update_library("anime", enabled=True, lib_type="movies"))
    assert _libs(store)["anime"] == (anime, "movies", True)
    assert "enabled" not in store.path.read_text()  # back to the default, not "enabled: true"

    store.save(store.remove_library("anime"))
    assert "anime" not in _libs(store) and "# films" in store.path.read_text()


def test_refusals(store: ConfigStore, tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="already a library called"):
        store.add_library("tv", str(tmp_path / "media/anime"), "tv")
    with pytest.raises(SettingsError, match="isn't a folder"):
        store.add_library("x", str(tmp_path / "nope"), "tv")
    with pytest.raises(SettingsError, match="overlap"):
        store.add_library("x", str(tmp_path / "media"), "movies")
    with pytest.raises(SettingsError, match="no library called"):
        store.update_library("nope", enabled=False)
    with pytest.raises(SettingsError, match="already a library called"):
        store.update_library("tv", new_name="movies")


def test_save_refuses_when_the_file_changed_meanwhile(store: ConfigStore) -> None:
    change = store.update_library("tv", enabled=False)
    store.path.write_text(store.path.read_text() + "\n# edited by hand\n")
    with pytest.raises(SettingsError, match="changed since"):
        store.save(change)


def test_rename_keeps_the_library_history(store: ConfigStore, tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    conn.execute(
        "INSERT INTO libraries (id, name, path, type, created_at) "
        "VALUES (7, 'tv', '/x', 'tv', 'now'), (8, 'shows', '/y', 'tv', 'now')"
    )
    # "shows" exists in the database but has no files: it gives way.
    store.save(store.update_library("tv", new_name="shows"), conn=conn)
    assert dict(conn.execute("SELECT id, name FROM libraries").fetchall()) == {7: "shows"}
    assert "shows" in _libs(store)

    # A name that has history of its own is refused, and the config isn't touched.
    conn.execute("INSERT INTO libraries (id, name, path, type, created_at) "
                 "VALUES (9, 'films', '/z', 'movies', 'now')")  # fmt: skip
    conn.execute("INSERT INTO files (library_id, rel_path, filename, ext, role, size_bytes, "
                 "mtime_ns, first_seen, last_seen) VALUES (9, 'a', 'a', '', 'other', 1, 1, "
                 "'n', 'n')")  # fmt: skip
    before = store.path.read_text()
    with pytest.raises(SettingsError, match="already has history"):
        store.save(store.update_library("movies", new_name="films"), conn=conn)
    assert store.path.read_text() == before


def test_environment_variables_lock_settings(tmp_path: Path, store: ConfigStore) -> None:
    env = {"MEDIA_AUDIT_SCAN__WORKERS": "4", "MEDIA_AUDIT_LIBRARIES": "[]",
           "MEDIA_AUDIT_CONFIG": "/x", "MEDIA_AUDIT_WEB__PORT": ""}  # fmt: skip
    locked = ConfigStore(store.path, environ=env)
    assert locked.locked("scan.workers") == "MEDIA_AUDIT_SCAN__WORKERS"
    assert locked.locked("scan") == "MEDIA_AUDIT_SCAN__WORKERS"  # part of the section
    assert locked.locked("scan.schedule") is None
    assert locked.locked("web.port") is None  # empty values don't count
    with pytest.raises(LockedError, match="MEDIA_AUDIT_LIBRARIES"):
        locked.add_library("anime", str(tmp_path / "media/anime"), "tv")
    with pytest.raises(LockedError):
        locked.set_value("scan.workers", 2)
    assert env_key("MEDIA_AUDIT_CONFIG") is None and env_key("PATH") is None


def test_set_value_and_lists(store: ConfigStore, tmp_path: Path) -> None:
    store.save(store.set_value("scan.workers", 3))
    store.save(store.set_value("web.port", 9000))  # creates the section
    cfg = store.config()
    assert (cfg.scan.workers, cfg.web.port) == (3, 9000)
    assert "# no schedule while testing" in store.path.read_text()
    store.save(store.set_value("web.port", None))
    assert store.config().web.port == 8484
    with pytest.raises(SettingsError):
        store.set_value("scan.workers", "lots")  # rejected by the schema

    media, archive = str(tmp_path / "media"), str(tmp_path / "archive")
    store.save(store.set_media_folders([media, archive]))
    store.save(store.set_path_mappings([(media, "/mnt/user/media"), (archive, "/mnt/disks/a")]))
    cfg = store.config()
    assert cfg.media_folders == [media, archive]
    assert cfg.host_path(f"{archive}/movies") == "/mnt/disks/a/movies"
    with pytest.raises(SettingsError, match="isn't a folder"):
        store.set_media_folders([str(tmp_path / "nope")])
    with pytest.raises(SettingsError, match="more than once"):
        store.set_path_mappings([(media, "/a"), (media, "/b")])


def test_untouched_config_is_not_rewritten(store: ConfigStore) -> None:
    change = store.update_library("tv", lib_type="tv")
    assert not change.changed and store.save(change) is None
    assert load_config(store.path, environ={}).libraries[1].name == "tv"


def test_cli(store: ConfigStore, tmp_path: Path, capsys: Any) -> None:
    from media_audit.cli import main

    base = ["--config", str(store.path)]
    anime = str(tmp_path / "media/anime")
    assert main([*base, "library", "add", "anime", "--path", anime, "--type", "tv",
                 "--dry-run"]) == 0  # fmt: skip
    assert "+  - name: anime" in capsys.readouterr().out and "anime" not in _libs(store)
    assert main([*base, "library", "add", "anime", "--path", anime, "--type", "tv"]) == 0
    assert main([*base, "library", "set", "anime", "--rename", "japanese", "--enabled", "off"]) == 0
    assert _libs(store)["japanese"] == (anime, "tv", False)
    assert main([*base, "library", "add", "x", "--path", "/nope", "--type", "tv"]) == 2
    assert "isn't a folder" in capsys.readouterr().err
    assert main([*base, "path-mapping", "add", str(tmp_path / "media"), "--host", "/mnt/m"]) == 0
    capsys.readouterr()
    assert main([*base, "library", "list"]) == 0
    assert "(server: /mnt/m/movies)" in capsys.readouterr().out
    assert main([*base, "path-mapping", "remove", str(tmp_path / "media")]) == 0
    assert store.config().path_mappings == []


def test_mount_table(tmp_path: Path) -> None:
    from media_audit import mounts

    info = tmp_path / "mountinfo"
    info.write_text(
        "22 1 0:21 / / rw - overlay overlay rw\n"
        "30 22 0:40 /content1 /media/content1 ro - fuse.shfs shfs ro\n"
        "31 22 0:40 /c2 /media/my\\040archive ro - fuse.shfs shfs ro\n"
        "32 22 8:1 /appdata /config rw - xfs /dev/sdb1 rw\n"
    )
    assert mounts.mount_points(info) == ["/", "/config", "/media/content1", "/media/my archive"]
    assert mounts.mounts_under(["/media"], info) == ["/media/content1", "/media/my archive"]
    assert mounts.mount_of("/media/content1/movies", info) == "/media/content1"
    assert mounts.mount_of("/media/content2/movies", info) == "/"
    assert mounts.mount_points(tmp_path / "missing") == []
