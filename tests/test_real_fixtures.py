"""Rules and parser vs. MediaInfo captures from the real library (parse_speed 0 unless noted).

Captured 2026-11-10 with scripts/capture_fixture.py (MediaInfo 24.12). Expected values
were checked against each file's name, which Radarr/Sonarr wrote from their own
analysis, so a disagreement here is either a rules bug or a genuinely mislabeled file.
"""

from __future__ import annotations

from typing import Any

import pytest

from media_audit.rules.derive import derive
from tests.conftest import load_mediainfo

THOR = (
    "Thor Ragnarok (2017) {tmdb-284053} [Bluray-2160p Proper][HDR10][TrueHD Atmos 7.1]"
    "[x265]-IAMABLE.mkv"
)

# (fixture, filename, video, audio [(commercial_name, channels, language)],
#  subtitles (count, first few (format, language, forced))
CASES: list[tuple[str, str, dict[str, Any], list[tuple[Any, ...]], tuple[int, list[Any]]]] = [
    ("real_web_dv_hdr10plus_eac3_atmos",
     "Avatar Fire and Ash (2025) {tmdb-83533} [WEBDL-2160p][DV HDR10Plus][EAC3 Atmos 5.1]"
     "[h265]-BYNDR.mkv",
     dict(codec="HEVC", width=3840, height=2076, resolution_class="2160p", bit_depth=10,
          dynamic_range="DV", hdr_formats=["DV", "HDR10+", "HDR10"], dv_profile=8,
          dv_compatibility="HDR10", dv_has_fallback=True),
     [("E-AC-3 Atmos", "5.1", "en")], (3, [("SRT", "en", False)])),
    ("real_web_dv_hdr10_eac3",
     "Elf (2003) {tmdb-10719} [WEBDL-1080p][DV HDR10][EAC3 5.1][h265]-SQS.mkv",
     dict(resolution_class="1080p", hdr_formats=["DV", "HDR10"], dv_profile=8,
          dv_compatibility="HDR10"),
     [("E-AC-3", "5.1", "en")], (40, [("SRT", "en", False)])),
    # "DV HDR10" remuxes in this library are profile 8.1 (converted from P7), not P7 FEL.
    ("real_remux_dv_hdr10_truehd_atmos",
     "Spider-Man (2002) {tmdb-557} [Remux-2160p][DV HDR10][TrueHD Atmos 7.1][HEVC]-FraMeSToR.mkv",
     dict(resolution_class="2160p", hdr_formats=["DV", "HDR10"], dv_profile=8, dv_level=6),
     [("TrueHD Atmos", "7.1", "en"), ("AC-3", "5.1", "en"), ("AC-3", "2.0", "en"),
      ("AC-3", "2.0", "en"), ("AC-3", "2.0", "en")],
     (52, [("PGS", "en", False)])),
    ("real_remux_dv_hdr10_dtshd_ma",
     "The Dark Knight Rises (2012) {tmdb-49026} - [Remux-2160p][DTS-HD MA 5.1][DV HDR10]"
     "[HEVC]-F.mkv",
     dict(hdr_formats=["DV", "HDR10"], dv_profile=8),
     [("DTS-HD MA", "5.1", "en")], (38, [("PGS", "en", False)])),
    ("real_remux_hdr10_dtshd_ma",
     "Interstellar (2014) {tmdb-157336} - [Remux-2160p][DTS-HD MA 5.1][HDR10][HEVC]-FraMeSToR.mkv",
     dict(dynamic_range="HDR10", hdr_formats=["HDR10"], dv_profile=None),
     [("DTS-HD MA", "5.1", "en")], (4, [("PGS", "en", False), ("PGS", "en", False)])),
    ("real_bluray_hdr10_dtsx",
     "Harry Potter and the Chamber of Secrets (2002) {tmdb-672} {edition-Theatrical} "
     "[Bluray-2160p][HDR10][DTS-X 7.1][x265]-DEPTH.mkv",
     dict(width=3840, height=1600, resolution_class="2160p", hdr_formats=["HDR10"]),
     [("DTS:X", "7.1", "en")], (9, [("PGS", "en", False)])),
    ("real_bluray_hdr10plus_eac3",
     "Die Hard (1988) {tmdb-562} [Bluray-1080p][HDR10Plus][EAC3 5.1][x265]-SQS.mkv",
     dict(width=1920, height=802, resolution_class="1080p", dynamic_range="HDR10+",
          hdr_formats=["HDR10+", "HDR10"]),
     [("E-AC-3", "5.1", "en"), ("FLAC", "2.0", "en"), ("AC-3", "2.0", "en"),
      ("AC-3", "2.0", "en")],
     (51, [("SRT", "en", False), ("PGS", "en", False)])),
    # At parse_speed 0 this MKV doesn't expose TrueHD details in its headers: MediaInfo
    # reports bare "TrueHD", so Atmos is only a hint from the track title. The analyzer
    # retries such files deeper; the _speed005 capture is what that retry returns.
    ("real_bluray_hdr10_truehd_atmos",
     THOR,
     dict(width=3840, height=1608, hdr_formats=["HDR10"]),
     [("TrueHD (Atmos?)", "7.1", "en"), ("AC-3", "5.1", "en")], (11, [("SRT", "en", False)])),
    ("real_bluray_hdr10_truehd_atmos_speed005",
     THOR,
     dict(hdr_formats=["HDR10"]),
     [("TrueHD Atmos", "7.1", "en"), ("AC-3", "5.1", "en")], (11, [("SRT", "en", False)])),
    ("real_web_hdr10_eac3_atmos",
     "Lightyear (2022) {tmdb-718789} [WEBDL-2160p][HDR10][EAC3 Atmos 5.1][h265]-EVO.mkv",
     dict(dynamic_range="HDR10"), [("E-AC-3 Atmos", "5.1", "en")], (0, [])),
    # The AVI header claims 2 channels; the AC-3 stream is mono (acmod 1), as the name says.
    ("real_dvd_xvid_ac3_avi",
     "Jack Frost (1979) {tmdb-26538} [DVD][AC3 1.0][XviD]-BHRG.avi",
     dict(container="AVI", codec="MPEG-4 Visual", width=640, height=480, resolution_class="SD"),
     [("AC-3", "1.0", "und")], (0, [])),
    ("real_plain_avi", "Brigadoon (1954).avi",
     dict(container="AVI", codec="MPEG-4 Visual", resolution_class="SD", dynamic_range="SDR"),
     [("MP3", "2.0", "und")], (0, [])),
    ("real_yts_mp4_aac", "All of Me (1984) [Bluray-1080p][AAC 2.0][x264]-YTS.MX.mp4",
     dict(container="MP4", codec="AVC", width=1920, height=1040, resolution_class="1080p"),
     [("AAC", "2.0", "und")], (0, [])),
    ("real_tv_web_hevc_aac",
     "House (2004) - S05E01 - Dying Changes Everything [WEBDL-1080p][AAC 2.0][h265].mkv",
     dict(codec="HEVC", bit_depth=8, resolution_class="1080p"),
     [("AAC", "2.0", lang) for lang in ("de", "en", "es", "es", "fr", "it", "ja", "pt")],
     (27, [("SRT", "ko", False)])),
    ("real_anime_hevc_opus",
     "Code Geass - Lelouch of the Rebellion (2006) - S02E02 - Plan for Independent Japan "
     "[HDTV-1080p][Opus 2.0][x265]-Judas.mkv",
     dict(codec="HEVC", bit_depth=10),
     [("Opus", "2.0", "ja"), ("Opus", "2.0", "en")],
     (2, [("PGS", "en", False), ("PGS", "ja", False)])),
    ("real_anime_web_eac3_multisub",
     "Lord of Mysteries (2025) - S00E20 - 016 - The Marked Hunt 2 "
     "[WEBDL-1080p][8bit][h264][EAC3 2.0][ZH+EN]-AnoZu.mkv",
     dict(codec="AVC", width=2538, height=1080, resolution_class="1080p", frame_rate=25.0),
     [("E-AC-3", "2.0", "zh"), ("E-AC-3", "2.0", "en")],
     (16, [("ASS", "en", False), ("ASS", "en", True), ("SRT", "en", False)])),
    ("real_bluray_576p_10bit_ja",
     "Gintama (2006) - S03E21 - 120 - Japanese Restaurants Abroad Taste Pretty Much Like "
     "School Cafeteria Lunches Once Youve Taken a Dish You Cant Put It Back "
     "[Bluray-576p][10bit][x265][AAC 2.0][JA]-Anime Time.mkv",
     dict(codec="HEVC", width=760, height=576, resolution_class="SD", bit_depth=10),
     [("AAC", "2.0", "ja")], (1, [("ASS", "en", False)])),
]  # fmt: skip


@pytest.mark.parametrize(("fixture", "filename", "video", "audio", "subs"), CASES,
                         ids=[c[0] for c in CASES])  # fmt: skip
def test_real_capture(
    fixture: str,
    filename: str,
    video: dict[str, Any],
    audio: list[tuple[Any, ...]],
    subs: tuple[int, list[Any]],
) -> None:
    ext = filename.rsplit(".", 1)[1].lower()
    d = derive(filename, ext, 1_000_000_000, load_mediainfo(fixture))
    assert d.video is not None
    got = d.video.__dict__
    assert {k: got[k] for k in video} == video
    assert [(a.commercial_name, a.channels_label, a.language) for a in d.audio] == audio
    count, first = subs
    assert len(d.subtitles) == count
    assert [(s.format, s.language, s.is_forced) for s in d.subtitles[: len(first)]] == first


# Parser expectations for the same real names (Sonarr anime formats in particular).
PARSER_CASES: list[tuple[str, dict[str, Any]]] = [
    (CASES[15][1], dict(title="Lord of Mysteries", year=2025, season=0, episodes=[20],
                        absolute_episode=16, episode_title="The Marked Hunt 2", bit_depth=8,
                        languages=["zh", "en"], release_group="AnoZu")),
    (CASES[16][1], dict(title="Gintama", season=3, episodes=[21], absolute_episode=120,
                        bit_depth=10, languages=["ja"], release_group="Anime Time",
                        resolution="576p")),
    (CASES[14][1], dict(title="Code Geass - Lelouch of the Rebellion", season=2,
                        episodes=[2], absolute_episode=None, release_group="Judas",
                        episode_title="Plan for Independent Japan")),
    (CASES[5][1], dict(audio_object="DTS:X", audio_channels="7.1", edition="Theatrical",
                       release_group="DEPTH", languages=[])),
    (CASES[2][1], dict(hdr=["DV", "HDR10"], languages=[], release_group="FraMeSToR")),
    (CASES[12][1], dict(release_group="YTS.MX", source="BluRay")),
    (CASES[10][1], dict(source="DVD", audio_channels="1.0", video_codec="MPEG-4 Visual")),
]  # fmt: skip


@pytest.mark.parametrize(("name", "expected"), PARSER_CASES, ids=[c[0][:50] for c in PARSER_CASES])
def test_real_names(name: str, expected: dict[str, Any]) -> None:
    parsed = parse(name)
    mismatches = {k: (parsed[k], v) for k, v in expected.items() if parsed[k] != v}
    assert not mismatches, f"(got, expected): {mismatches}"


def parse(name: str) -> dict[str, Any]:
    from media_audit.parsing import parse_filename

    return parse_filename(name).to_dict()


def test_incomplete_truehd_is_flagged_not_claimed() -> None:
    from media_audit.rules.audio import derive_audio, needs_deeper_parse

    shallow = load_mediainfo("real_bluray_hdr10_truehd_atmos")
    assert needs_deeper_parse(shallow)
    truehd = derive_audio(shallow)[0]
    assert truehd.details_incomplete and truehd.object_audio_hint == "Atmos"
    assert not truehd.is_object_audio  # a hint, not a confirmed detection

    for parsed in ("real_bluray_hdr10_truehd_atmos_speed005", "real_remux_dv_hdr10_truehd_atmos"):
        raw = load_mediainfo(parsed)
        assert not needs_deeper_parse(raw)
        track = derive_audio(raw)[0]
        assert track.is_object_audio and not track.details_incomplete
        assert track.object_audio_hint is None
