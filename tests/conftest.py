from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from media_audit.config import Config, LibraryConfig
from media_audit.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
MEDIAINFO_DIR = FIXTURES / "mediainfo"


def load_mediainfo(name: str) -> dict[str, Any]:
    """Load a MediaInfo fixture by stem: real/ first, then generated/, then synthetic/."""
    for sub in ("real", "generated", "synthetic"):
        path = MEDIAINFO_DIR / sub / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text())  # type: ignore[no-any-return]
    raise FileNotFoundError(name)


class FakeAnalyzer:
    """Returns fixture JSON chosen by a substring of the filename; counts calls."""

    name = "fake"

    def __init__(self, mapping: dict[str, str] | None = None,
                 default: str = "sdr_avc_1080p_mp4_aac") -> None:  # fmt: skip
        self.mapping = mapping or {}
        self.default = default
        self.calls: Counter[str] = Counter()

    def version(self) -> str:
        return "fake 1.0"

    def analyze(self, path: str) -> dict[str, Any]:
        from media_audit.analyzer import AnalyzerError

        self.calls[path] += 1
        name = Path(path).name
        if "corrupt" in name:
            raise AnalyzerError("MediaInfo found no tracks (unreadable or not a media file)")
        for needle, fixture in self.mapping.items():
            if needle in name:
                return load_mediainfo(fixture)
        return load_mediainfo(self.default)


@pytest.fixture
def make_tree(tmp_path: Path) -> Callable[[str, dict[str, int | str]], Path]:
    """Create files under tmp_path/<root>. Values: size in bytes (int) or content (str)."""

    def make(root: str, files: dict[str, int | str]) -> Path:
        base = tmp_path / root
        base.mkdir(parents=True, exist_ok=True)
        for rel, spec in files.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(spec, int):
                p.write_bytes(b"\0" * spec)
            else:
                p.write_text(spec)
        return base

    return make


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Config]:
    def make(**libraries: tuple[Path, str]) -> Config:
        return Config(
            database=tmp_path / "db" / "media-audit.db",
            libraries=[
                LibraryConfig(name=name, path=path, type=kind)  # type: ignore[arg-type]
                for name, (path, kind) in libraries.items()
            ],
        )

    return make


@pytest.fixture
def mem_db() -> Any:
    conn = connect(":memory:")
    yield conn
    conn.close()
