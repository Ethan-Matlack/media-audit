"""Languages, classification, config, and migrations."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_audit.classify import classify_role, season_folder_number
from media_audit.config import ConfigError, load_config
from media_audit.db import apply_migrations, connect, latest_version, schema_version
from media_audit.rules.languages import normalize_language

# --------------------------------------------------------------------------- languages


@pytest.mark.parametrize(
    ("raw", "code"),
    [("en", "en"), ("eng", "en"), ("EN-us", "en"), ("ger", "de"), ("deu", "de"),
     ("fre", "fr"), ("jpn", "ja"), ("zh-Hant", "zh"), ("chi", "zh"), ("", "und"),
     (None, "und"), ("und", "und"), ("Undetermined", "und"), ("English", "en"),
     ("mul", "mul"), ("zxx", "zxx"), ("tlh", "tlh"), ("pt_BR", "pt")],
)  # fmt: skip
def test_normalize_language(raw: str | None, code: str) -> None:
    assert normalize_language(raw) == code


# --------------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    ("rel", "role"),
    [
        ("Movie (2001)/Movie (2001).mkv", "main"),
        ("Movie (2001)/Movie (2001).en.srt", "subtitle"),
        ("Movie (2001)/movie.nfo", "other"),
        ("Movie (2001)/poster.jpg", "other"),
        ("Movie (2001)/Featurettes/Making Of.mkv", "extra"),
        ("Movie (2001)/Behind The Scenes/x.mp4", "extra"),
        ("Movie (2001)/Movie (2001)-trailer.mkv", "extra"),
        ("Movie (2001)/Sample/movie-sample.mkv", "sample"),
        ("Movie (2001)/movie.sample.mkv", "sample"),
        ("Movie (2001)/sample.mkv", "sample"),
        ("Show/Season 01/Show - S01E01.mkv", "main"),
        ("Show/Specials/Show - S00E01.mkv", "main"),
        ("Show/Extras/Bloopers.mkv", "extra"),
        ("Other/Other.mkv", "main"),  # title folder literally named "Other"
        ("Samples In Space (2001)/Samples In Space (2001).mkv", "main"),
    ],
)
def test_classify_role(rel: str, role: str) -> None:
    assert classify_role(rel) == role


@pytest.mark.parametrize(
    ("name", "num"),
    [("Season 01", 1), ("Season 1", 1), ("season 10", 10), ("S02", 2), ("Specials", 0),
     ("Series 3", 3), ("Staffel 4", 4), ("Featurettes", None), ("Season", None)],
)  # fmt: skip
def test_season_folder(name: str, num: int | None) -> None:
    assert season_folder_number(name) == num


# --------------------------------------------------------------------------- config


def test_config_load_and_env(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "database: data/test.db\n"
        "libraries:\n"
        "  - {name: movies, path: media/movies, type: movies}\n"
        "  - {name: tv, path: /abs/tv, type: tv, enabled: false}\n"
        "languages:\n  preferred_audio: [eng, ger]\n"
    )
    cfg = load_config(cfg_file, environ={"MEDIA_AUDIT_SCAN__WORKERS": "4",
                                         "MEDIA_AUDIT_LINKING__BY_NAME_YEAR": "false"})  # fmt: skip
    assert cfg.database == tmp_path / "data/test.db"
    assert cfg.libraries[0].path == tmp_path / "media/movies"
    assert cfg.libraries[1].path == Path("/abs/tv") and not cfg.libraries[1].enabled
    assert cfg.scan.workers == 4
    assert cfg.linking.by_name_year is False
    assert cfg.languages.preferred_audio == ["en", "de"]
    # Program-control variables set by the Docker image are not config keys.
    docker_env = {"MEDIA_AUDIT_AUTO_INIT": "1", "MEDIA_AUDIT_MEDIA_ROOT": "/media",
                  "MEDIA_AUDIT_CONFIG": str(cfg_file)}  # fmt: skip
    assert load_config(cfg_file, environ=docker_env).scan.workers == 2


def test_config_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("libraries:\n  - {name: a, path: /x, type: music}\n")
    with pytest.raises(ConfigError, match="type"):
        load_config(bad, environ={})
    bad.write_text(
        "libraries:\n  - {name: a, path: /x, type: tv}\n  - {name: a, path: /y, type: tv}\n"
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(bad, environ={})
    bad.write_text("nonsense_key: 1\n")
    with pytest.raises(ConfigError, match="nonsense_key"):
        load_config(bad, environ={})
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml", environ={})


def test_example_config_is_valid() -> None:
    example = Path(__file__).parent.parent / "config.example.yml"
    cfg = load_config(example, environ={})
    assert {lib.name for lib in cfg.libraries} >= {"movies", "movies-4k", "tv"}


def test_docker_config_is_valid() -> None:
    cfg = load_config(Path(__file__).parent.parent / "docker/config/config.yml", environ={})
    assert {(lib.name, lib.type) for lib in cfg.libraries} == {
        ("movies", "movies"),
        ("movies-anime", "movies"),
        ("tv", "tv"),
        ("anime", "tv"),
    }
    assert cfg.scan.parse_speed == 0 and cfg.scan.retry_timeout == 60
    assert cfg.scan.schedule == "0 3 * * *" and cfg.web.port == 8484


# --------------------------------------------------------------------------- migrations


def test_migrations_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = connect(db)
    assert schema_version(conn) == latest_version() >= 1
    assert apply_migrations(conn) == []
    conn.close()
    conn = connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"files", "titles", "seasons", "video_info", "audio_tracks", "subtitle_tracks",
            "release_info", "flags", "scans", "scan_snapshots", "meta"} <= tables  # fmt: skip
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_newer_schema_refused(tmp_path: Path) -> None:
    from media_audit.db import MigrationError

    conn = connect(tmp_path / "x.db")
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    with pytest.raises(MigrationError, match="newer"):
        apply_migrations(conn)


# --------------------------------------------------------------------------- version


def test_version_is_plain_semver() -> None:
    import re
    from importlib.metadata import version

    from media_audit import __version__

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert version("media-audit") == __version__  # pyproject reads it from __init__.py


def test_splash(monkeypatch: pytest.MonkeyPatch) -> None:
    from media_audit import __author__, __version__
    from media_audit.splash import render

    monkeypatch.setenv("HOST_CONTAINERNAME", "media-audit-test")
    text = render()
    assert "m e d i a - a u d i t" in text
    assert __version__ in text and __author__ in text and "media-audit-test" in text


def test_mediainfo_raw_moves_out_of_files(tmp_path: Path) -> None:
    """Migration 0003 on a database created before it: data kept, file compacted."""
    from importlib import resources

    from media_audit.db import _migrations

    db = tmp_path / "old.db"
    conn = connect(db, migrate=False)
    for version, _, sql in _migrations():
        if version > 2:
            break
        conn.executescript(f"BEGIN;\n{sql}\n;INSERT INTO meta VALUES ('schema_version', "
                           f"'{version}') ON CONFLICT (key) DO UPDATE SET value = excluded.value;"
                           "COMMIT;")  # fmt: skip
    conn.execute("INSERT INTO libraries VALUES (1, 'm', '/m', 'movies', 1, 'now')")
    big = '{"media": {"track": [{"@type": "Video", "pad": "' + "x" * 20000 + '"}]}}'
    conn.executemany(
        "INSERT INTO files (id, library_id, path, rel_path, filename, ext, role, is_video, "
        "size_bytes, mtime_ns, first_seen, last_seen, mediainfo_raw) "
        "VALUES (?, 1, ?, ?, ?, 'mkv', 'main', 1, 1, 1, 'now', 'now', ?)",
        [(i, f"/m/{i}.mkv", f"{i}.mkv", f"{i}.mkv", big if i % 2 else None) for i in range(1, 301)],
    )
    conn.close()

    conn = connect(db)
    assert schema_version(conn) == latest_version()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    assert "mediainfo_raw" not in cols
    assert conn.execute("SELECT count(*) FROM file_mediainfo").fetchone()[0] == 150
    assert conn.execute("SELECT raw FROM file_mediainfo WHERE file_id = 1").fetchone()[0] == big
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0  # compacted
    assert resources.files("media_audit.db.migrations").joinpath(
        "0003_split_mediainfo_raw.sql").is_file()  # fmt: skip


@pytest.mark.parametrize(
    ("channel", "sha", "shown", "label"),
    [(None, None, "{v}", None),
     ("release", "abc1234def", "{v}", "release · abc1234"),
     ("main", "abc1234def", "{v}", "main · abc1234"),
     ("dev", "abc1234def", "{v}-dev (abc1234)", "dev · abc1234"),
     ("dev", None, "{v}-dev", "dev")],
)  # fmt: skip
def test_build_labels(
    monkeypatch: pytest.MonkeyPatch, channel: str | None, sha: str | None, shown: str,
    label: str | None,
) -> None:  # fmt: skip
    from media_audit import __version__
    from media_audit.build import CHANNEL_ENV, SHA_ENV, build_label, display_version

    for env, value in ((CHANNEL_ENV, channel), (SHA_ENV, sha)):
        if value is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, value)
    assert display_version() == shown.format(v=__version__)
    assert build_label() == label


def test_build_env_is_not_config(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yml"
    cfg.write_text("libraries: []\n")
    env = {"MEDIA_AUDIT_BUILD_CHANNEL": "dev", "MEDIA_AUDIT_BUILD_SHA": "abc"}
    assert load_config(cfg, environ=env).libraries == []  # no "unknown key" error
