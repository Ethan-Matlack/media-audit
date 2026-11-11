"""Starter config generation (`init-config` and Docker first start)."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_audit.cli import main
from media_audit.config import load_config
from media_audit.init_config import guess_type, write_default_config


@pytest.mark.parametrize(
    ("name", "kind"),
    [("movies", "movies"), ("Movies 4K", "movies"), ("movies-4k", "movies"),
     ("4kmovies", "movies"), ("Films", "movies"), ("movies-anime", "movies"),
     ("tv", "tv"), ("TV Shows", "tv"), ("tvshows", "tv"), ("anime", "tv"),
     ("series", "tv"), ("kids-tv", "tv"), ("books", None), ("music", None),
     ("music-unsorted", None), ("photos", None), ("downloads", None)],
)  # fmt: skip
def test_guess_type(name: str, kind: str | None) -> None:
    assert guess_type(name) == kind


def _media(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "media"
    for n in names:
        (root / n).mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "#recycle").mkdir()
    (root / "loose.txt").write_text("x")
    return root


def test_generated_config_matches_server_layout(tmp_path: Path) -> None:
    # The author's share: content/{anime,books,movies,movies-anime,music,...,tv}
    root = _media(tmp_path, "anime", "books", "movies", "movies-anime", "music",
                  "music-unsorted", "photos", "tv")  # fmt: skip
    target = tmp_path / "config" / "config.yml"
    text, _ = write_default_config(target, root, tmp_path / "config" / "media-audit.db")
    cfg = load_config(target, environ={})
    assert [(lib.name, lib.type) for lib in cfg.libraries] == [
        ("anime", "tv"),
        ("movies", "movies"),
        ("movies-anime", "movies"),
        ("tv", "tv"),
    ]
    assert cfg.libraries[0].path == root / "anime"
    assert cfg.database == tmp_path / "config" / "media-audit.db"
    assert cfg.scan.parse_speed == 0 and cfg.scan.retry_timeout == 60
    assert cfg.scan.schedule == "0 3 * * *" and cfg.web.port == 8484
    assert "#   path: " + str(root / "books") in text  # unrecognized: commented out
    assert ".hidden" not in text and "#recycle" not in text
    with pytest.raises(FileExistsError):
        write_default_config(target, root, tmp_path / "x.db")


def test_generated_config_with_nothing_recognized(tmp_path: Path) -> None:
    root = _media(tmp_path, "photos")
    target = tmp_path / "c.yml"
    write_default_config(target, root, tmp_path / "db.sqlite")
    assert load_config(target, environ={}).libraries == []
    write_default_config(target, tmp_path / "missing", tmp_path / "db.sqlite", force=True)
    assert load_config(target, environ={}).libraries == []


def test_odd_folder_names_round_trip(tmp_path: Path) -> None:
    root = _media(tmp_path, "Movies: 'Kids' #2", "TV [UHD]")
    target = tmp_path / "c.yml"
    write_default_config(target, root, tmp_path / "db.sqlite")
    names = {lib.name for lib in load_config(target, environ={}).libraries}
    assert names == {"Movies: 'Kids' #2", "TV [UHD]"}


def test_docker_first_start_creates_config_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _media(tmp_path, "movies", "tv")
    target = tmp_path / "config" / "config.yml"
    monkeypatch.setenv("MEDIA_AUDIT_AUTO_INIT", "1")
    monkeypatch.setenv("MEDIA_AUDIT_CONFIG", str(target))
    monkeypatch.setenv("MEDIA_AUDIT_MEDIA_ROOT", str(root))
    monkeypatch.chdir(tmp_path)

    assert main(["scan"]) == 0
    out = capsys.readouterr().out
    assert "creating one (first start)" in out and "Nothing was scanned" in out
    assert target.is_file()
    assert not (tmp_path / "config" / "media-audit.db").exists()

    # Second start: config exists, so the scan really runs.
    assert main(["scan", "--no-analyze", "-q"]) == 0
    assert "scan #1 ok" in capsys.readouterr().out


def test_no_auto_init_outside_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_AUDIT_AUTO_INIT", raising=False)
    monkeypatch.setenv("MEDIA_AUDIT_CONFIG", str(tmp_path / "nope.yml"))
    assert main(["stats"]) == 2  # config error, nothing written
    assert not (tmp_path / "nope.yml").exists()


def test_init_config_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _media(tmp_path, "movies")
    target = tmp_path / "out.yml"
    assert main(["-c", str(target), "init-config", "--media-root", str(root)]) == 0
    assert "1 libraries detected" in capsys.readouterr().out
    assert main(["-c", str(target), "init-config", "--media-root", str(root)]) == 1
    assert main(["-c", str(target), "init-config", "--media-root", str(root), "--force"]) == 0
