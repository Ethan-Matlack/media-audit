"""Real libmediainfo on tiny ffmpeg-generated files. Skipped when either is missing.

Runs inside the Docker ``test`` target, which installs both.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from media_audit.analyzer import AnalyzerError, AnalyzerUnavailable, get_analyzer
from media_audit.rules.derive import derive


def _analyzer():  # type: ignore[no-untyped-def]
    try:
        return get_analyzer("mediainfo")
    except AnalyzerUnavailable as exc:
        pytest.skip(str(exc))


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _ffmpeg(out: Path, *args: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1920x800:rate=24:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         *args, str(out)],
        check=True,
    )  # fmt: skip
    return out


@pytest.mark.mediainfo
@needs_ffmpeg
def test_mkv_h264_aac(tmp_path: Path) -> None:
    analyzer = _analyzer()
    f = _ffmpeg(tmp_path / "Test (2020) [WEBDL-1080p][AAC 2.0][h264].mkv",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "2",
                "-metadata:s:a:0", "language=eng")  # fmt: skip
    raw = analyzer.analyze(str(f))
    assert analyzer.version().startswith("mediainfo ")
    d = derive(f.name, "mkv", f.stat().st_size, raw)
    assert d.video is not None
    assert d.video.container == "MKV"
    assert d.video.codec == "AVC"
    assert (d.video.width, d.video.height, d.video.resolution_class) == (1920, 800, "1080p")
    assert d.video.dynamic_range == "SDR"
    assert d.video.duration_s == pytest.approx(2.0, abs=0.2)
    assert [(a.codec, a.channels_label, a.language) for a in d.audio] == [("AAC", "2.0", "en")]
    assert d.parsed.quality_tag == "WEBDL-1080p"


@pytest.mark.mediainfo
@needs_ffmpeg
def test_mp4_mpeg4_mp3_avi(tmp_path: Path) -> None:
    analyzer = _analyzer()
    f = _ffmpeg(tmp_path / "old.avi", "-c:v", "mpeg4", "-vtag", "XVID", "-s", "720x304",
                "-c:a", "libmp3lame")  # fmt: skip
    d = derive(f.name, "avi", f.stat().st_size, analyzer.analyze(str(f)))
    assert d.video is not None
    assert (d.video.container, d.video.codec, d.video.resolution_class) == (
        "AVI",
        "MPEG-4 Visual",
        "SD",
    )
    assert d.audio[0].codec == "MP3"


@pytest.mark.mediainfo
def test_garbage_file_errors(tmp_path: Path) -> None:
    analyzer = _analyzer()
    f = tmp_path / "junk.mkv"
    f.write_bytes(b"not a video" * 10)
    try:
        raw = analyzer.analyze(str(f))
    except AnalyzerError:
        return
    # MediaInfo may still return a General track; there must be no video track.
    assert not any(t.get("@type") == "Video" for t in raw["media"]["track"])


@pytest.mark.mediainfo
@needs_ffmpeg
def test_deeper_parse_child_process(tmp_path: Path) -> None:
    """The real child-process path (python -m media_audit.analyzer._child) works."""
    analyzer = _analyzer()
    f = _ffmpeg(tmp_path / "a.mkv", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3")
    doc, info = analyzer._deeper_parse(str(f))
    assert info["status"] == "ok", info
    assert doc is not None and any(t["@type"] == "Video" for t in doc["media"]["track"])
