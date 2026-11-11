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
