"""Rules engine tests driven by stored MediaInfo JSON fixtures.

Synthetic fixtures model documented MediaInfo output. When a real capture with the
same stem is added under fixtures/mediainfo/real/, it takes precedence, so these
expectations double as the validation checklist for real files.
"""

from __future__ import annotations

from typing import Any

import pytest

from media_audit.rules.audio import channels_label, derive_audio
from media_audit.rules.subtitles import derive_subtitles, parse_sidecar
from media_audit.rules.video import derive_video, resolution_class
from tests.conftest import load_mediainfo

GB = 1_000_000_000

VIDEO_CASES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "hdr10_hevc_2160p_cropped_truehd_atmos",
        "mkv",
        dict(
            container="MKV",
            codec="HEVC",
            width=3840,
            height=1600,
            resolution_class="2160p",
            bit_depth=10,
            dynamic_range="HDR10",
            hdr_formats=["HDR10"],
            dv_profile=None,
            frame_rate=23.976,
            duration_s=7200.0,
        ),
    ),
    (
        "dv_p8_hdr10_eac3_atmos",
        "mkv",
        dict(
            dynamic_range="DV",
            hdr_formats=["DV", "HDR10"],
            dv_profile=8,
            dv_level=6,
            dv_compatibility="HDR10",
            dv_has_fallback=True,
        ),
    ),
    (
        "dv_p5_no_fallback",
        "mp4",
        dict(
            container="MP4",
            dynamic_range="DV",
            hdr_formats=["DV"],
            dv_profile=5,
            dv_compatibility=None,
            dv_has_fallback=False,
        ),
    ),
    (
        "dv_p7_fel_dtsx",
        "mkv",
        dict(
            dynamic_range="DV",
            hdr_formats=["DV", "HDR10"],
            dv_profile=7,
            dv_compatibility="HDR10",
            dv_has_fallback=True,
        ),
    ),
    ("hdr10plus", "mkv", dict(dynamic_range="HDR10+", hdr_formats=["HDR10+", "HDR10"])),
    (
        "dv_p8_hdr10plus",
        "mkv",
        dict(dynamic_range="DV", hdr_formats=["DV", "HDR10+", "HDR10"], dv_compatibility="HDR10"),
    ),
    ("hlg", "mkv", dict(dynamic_range="HLG", hdr_formats=["HLG"])),
    (
        "dv_p84_hlg",
        "mkv",
        dict(dynamic_range="DV", hdr_formats=["DV", "HLG"], dv_profile=8, dv_compatibility="HLG"),
    ),
    (
        "sdr_avc_1080p_mp4_aac",
        "mp4",
        dict(
            container="MP4",
            codec="AVC",
            resolution_class="1080p",
            dynamic_range="SDR",
            hdr_formats=[],
            bit_depth=8,
        ),
    ),
    (
        "avi_xvid_mp3_sd",
        "avi",
        dict(container="AVI", codec="MPEG-4 Visual", resolution_class="SD", dynamic_range="SDR"),
    ),
    (
        "dvd_mpeg2_ac3",
        "vob",
        dict(container="MPEG-PS", codec="MPEG-2", resolution_class="SD", scan_type="Interlaced"),
    ),
    ("vc1_1080p_legacy_dts_profile", "mkv", dict(codec="VC-1", resolution_class="1080p")),
    ("anamorphic_1440x1080_hdtv", "ts", dict(container="TS", resolution_class="1080p")),
]


@pytest.mark.parametrize(
    ("fixture", "ext", "expected"), VIDEO_CASES, ids=[c[0] for c in VIDEO_CASES]
)
def test_video(fixture: str, ext: str, expected: dict[str, Any]) -> None:
    v = derive_video(load_mediainfo(fixture), 30 * GB, ext).__dict__
    mismatches = {k: (v[k], e) for k, e in expected.items() if v[k] != e}
    assert not mismatches, f"(got, expected): {mismatches}"


def test_gb_per_hour_and_bitrate_fallback() -> None:
    raw = load_mediainfo("audio_only")  # 300 s, no OverallBitRate
    v = derive_video(raw, 1 * GB, "mkv")
    assert v.gb_per_hour == pytest.approx(12.0)
    assert v.overall_bitrate == int(GB * 8 / 300)
    assert v.codec is None and v.resolution_class is None


@pytest.mark.parametrize(
    ("w", "h", "cls"),
    [
        (3840, 2160, "2160p"), (3840, 1600, "2160p"), (4096, 1716, "2160p"),
        (1920, 1080, "1080p"), (1920, 800, "1080p"), (1916, 796, "1080p"),
        (1440, 1080, "1080p"), (1280, 720, "720p"), (1280, 536, "720p"), (960, 720, "720p"),
        (720, 576, "SD"), (720, 480, "SD"), (640, 360, "SD"), (7680, 4320, "4320p"),
        (None, None, None),
    ],
)  # fmt: skip
def test_resolution_class(w: int | None, h: int | None, cls: str | None) -> None:
    assert resolution_class(w, h) == cls


AUDIO_CASES: list[tuple[str, list[dict[str, Any]]]] = [
    ("hdr10_hevc_2160p_cropped_truehd_atmos", [
        dict(codec="TrueHD", commercial_name="TrueHD Atmos", channels=8, channels_label="7.1",
             is_object_audio=True, is_default=True, language="en"),
        dict(codec="AC-3", commercial_name="AC-3", channels_label="5.1", is_object_audio=False),
    ]),
    ("dv_p8_hdr10_eac3_atmos", [dict(codec="E-AC-3", commercial_name="E-AC-3 Atmos",
                                     is_object_audio=True)]),
    ("dv_p7_fel_dtsx", [dict(codec="DTS-HD MA", commercial_name="DTS:X", is_object_audio=True)]),
    ("hdr10plus", [dict(codec="E-AC-3", commercial_name="E-AC-3", is_object_audio=False)]),
    ("remux_1080p_avc_dtshd_ma_multisub", [
        dict(codec="DTS-HD MA", commercial_name="DTS-HD MA", channels_label="5.1"),
        dict(codec="AC-3", channels_label="2.0", title="Commentary"),
        dict(codec="DTS", commercial_name="DTS", language="de"),
    ]),
    ("avi_xvid_mp3_sd", [dict(codec="MP3", language="und", channels_label="2.0")]),
    ("dvd_mpeg2_ac3", [dict(codec="AC-3", language="und")]),
    ("vc1_1080p_legacy_dts_profile", [dict(codec="DTS-HD MA", commercial_name="DTS:X")]),
    ("hlg", [dict(codec="AAC", channels_label="2.0")]),
]  # fmt: skip


@pytest.mark.parametrize(("fixture", "expected"), AUDIO_CASES, ids=[c[0] for c in AUDIO_CASES])
def test_audio(fixture: str, expected: list[dict[str, Any]]) -> None:
    tracks = derive_audio(load_mediainfo(fixture))
    assert len(tracks) == len(expected)
    for track, exp in zip(tracks, expected, strict=True):
        got = track.__dict__
        mismatches = {k: (got[k], v) for k, v in exp.items() if got[k] != v}
        assert not mismatches, f"track {track.idx} (got, expected): {mismatches}"


@pytest.mark.parametrize(
    ("ch", "layout", "label"),
    [(8, "L R C LFE Ls Rs Lb Rb", "7.1"), (6, None, "5.1"), (2, "L R", "2.0"),
     (1, "C", "1.0"), (None, None, None), (3, "L R LFE", "2.1")],
)  # fmt: skip
def test_channels_label(ch: int | None, layout: str | None, label: str | None) -> None:
    assert channels_label(ch, layout) == label


def test_subtitles_embedded() -> None:
    subs = derive_subtitles(load_mediainfo("remux_1080p_avc_dtshd_ma_multisub"))
    assert [s.language for s in subs] == ["en", "de", "fr", "und"]
    assert all(s.format == "PGS" for s in subs)
    assert subs[0].is_default

    subs = derive_subtitles(load_mediainfo("hdr10_hevc_2160p_cropped_truehd_atmos"))
    assert [s.is_forced for s in subs] == [False, True]

    subs = derive_subtitles(load_mediainfo("dv_p8_hdr10_eac3_atmos"))
    assert [s.format for s in subs] == ["SRT", "SRT"]
    assert [s.is_hearing_impaired for s in subs] == [False, True]

    subs = derive_subtitles(load_mediainfo("sdr_avc_1080p_mp4_aac"))
    assert subs[0].format == "mov_text"
    assert derive_subtitles(load_mediainfo("dvd_mpeg2_ac3"))[0].format == "VobSub"


@pytest.mark.parametrize(
    ("video", "sidecar", "expected"),
    [
        ("Movie (2001)", "Movie (2001).en.srt", dict(language="en", format="SRT")),
        ("Movie (2001)", "Movie (2001).eng.forced.srt", dict(language="en", is_forced=True)),
        ("Movie (2001)", "Movie (2001).de.sdh.ass", dict(language="de", is_hearing_impaired=True,
                                                        format="ASS")),
        ("Movie (2001)", "Movie (2001).srt", dict(language="und")),
        ("Movie (2001)", "Movie (2001).pt-BR.srt", dict(language="pt")),
        ("Movie (2001)", "Movie (2001).English.srt", dict(language="en")),
        ("Movie (2001)", "Movie (2001).en.Director Commentary.srt",
         dict(language="en", title="Director Commentary")),
        ("Movie (2001)", "Movie (2001).sup", dict(format="PGS")),
        ("Movie (2001)", "Other Movie.en.srt", None),
        ("Movie (2001)", "Movie (2001) Extended.en.srt", None),
        ("Movie (2001)", "Movie (2001).en.txt", None),
    ],
)  # fmt: skip
def test_parse_sidecar(video: str, sidecar: str, expected: dict[str, Any] | None) -> None:
    track = parse_sidecar(video, sidecar)
    if expected is None:
        assert track is None
        return
    assert track is not None and track.is_external
    for k, v in expected.items():
        assert getattr(track, k) == v, k
