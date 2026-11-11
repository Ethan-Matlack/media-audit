"""Rules vs. *real* MediaInfo 24.12 output for small ffmpeg encodes.

Regenerate with ``scripts/generate_fixtures.sh``. These pin field names/values that
the synthetic fixtures only assume.
"""

from __future__ import annotations

from typing import Any

import pytest

from media_audit.rules.derive import derive
from tests.conftest import load_mediainfo

CASES: list[tuple[str, str, dict[str, Any], list[tuple[Any, ...]], list[tuple[Any, ...]]]] = [
    # stem, ext, video fields, audio (codec, commercial, channels, lang, default),
    # subtitles (format, lang, forced)
    ("gen_hdr10_hevc_eac3_srt", "mkv",
     dict(container="MKV", codec="HEVC", resolution_class="2160p", bit_depth=10,
          dynamic_range="HDR10", hdr_formats=["HDR10"]),
     [("E-AC-3", "E-AC-3", "5.1", "en", True)], [("SRT", "de", True)]),
    ("gen_hlg_hevc_ac3", "mkv", dict(dynamic_range="HLG", hdr_formats=["HLG"]),
     [("AC-3", "AC-3", "2.0", "und", False)], []),
    ("gen_av1_opus", "mkv", dict(codec="AV1", resolution_class="1080p", dynamic_range="SDR"),
     [("Opus", "Opus", "2.0", "ja", False)], []),
    ("gen_vp9_720p", "webm", dict(container="WebM", codec="VP9", resolution_class="720p"),
     [("Opus", "Opus", "1.0", "und", False)], []),
    ("gen_h264_aac_movtext", "mp4", dict(container="MP4", codec="AVC"),
     [("AAC", "AAC", "2.0", "en", True)], [("mov_text", "und", False)]),
    ("gen_h264_dts_core", "mkv", dict(codec="AVC"), [("DTS", "DTS", "5.1", "und", False)], []),
    ("gen_h264_truehd", "mkv", dict(codec="AVC"),
     [("TrueHD", "TrueHD", "5.1", "und", False)], []),
    ("gen_mpeg2_mp2", "mpg", dict(container="MPEG-PS", codec="MPEG-2", resolution_class="SD"),
     [("MP2", "MP2", "1.0", "und", False)], []),
]  # fmt: skip


@pytest.mark.parametrize(("stem", "ext", "video", "audio", "subs"), CASES,
                         ids=[c[0] for c in CASES])  # fmt: skip
def test_generated(stem: str, ext: str, video: dict[str, Any], audio: list[tuple[Any, ...]],
                   subs: list[tuple[Any, ...]]) -> None:  # fmt: skip
    d = derive(f"{stem}.{ext}", ext, 1_000_000, load_mediainfo(stem))
    assert d.video is not None
    got = d.video.__dict__
    assert {k: got[k] for k in video} == video
    assert [(a.codec, a.commercial_name, a.channels_label, a.language, a.is_default)
            for a in d.audio] == audio  # fmt: skip
    assert [(s.format, s.language, s.is_forced) for s in d.subtitles] == subs
