"""Filename parser corpus. Add real-world names here whenever one parses badly."""

from __future__ import annotations

from typing import Any

import pytest

from media_audit.parsing import parse_filename

CASES: list[tuple[str, dict[str, Any]]] = [
    # --- Radarr "Plex-style" names from the author's library
    (
        "Harry Potter and the Philosophers Stone (2001) {tmdb-671} "
        "[Bluray-2160p][HDR10][DTS-X 7.1][x265]-TERMiNAL.mkv",
        dict(
            title="Harry Potter and the Philosophers Stone",
            year=2001,
            tmdb_id="671",
            quality_tag="Bluray-2160p",
            source="BluRay",
            resolution="2160p",
            hdr=["HDR10"],
            audio_object="DTS:X",
            audio_channels="7.1",
            audio_tag="DTS-X 7.1",
            video_codec="HEVC",
            video_codec_tag="x265",
            release_group="TERMiNAL",
            style="radarr",
            is_remux=False,
        ),
    ),
    (
        "Harry Potter and the Philosophers Stone (2001) {tmdb-671} {edition-Theatrical} "
        "[WEBDL-1080p][EAC3 7.1][h265].mkv",
        dict(
            title="Harry Potter and the Philosophers Stone",
            year=2001,
            edition="Theatrical",
            source="WEB-DL",
            resolution="1080p",
            audio_codecs=["E-AC-3"],
            audio_channels="7.1",
            video_codec="HEVC",
            video_codec_tag="h265",
            release_group=None,
            hdr=[],
        ),
    ),
    (
        "Blade Runner 2049 (2017) {imdb-tt1856101} [Remux-2160p][DV HDR10][TrueHD Atmos 7.1]"
        "[HEVC]-FraMeSToR.mkv",
        dict(
            title="Blade Runner 2049",
            year=2017,
            imdb_id="tt1856101",
            is_remux=True,
            source="BluRay",
            resolution="2160p",
            hdr=["DV", "HDR10"],
            audio_codecs=["TrueHD"],
            audio_object="Atmos",
            release_group="FraMeSToR",
        ),
    ),
    (
        "Dune Part Two (2024) {tmdb-693134} [WEBDL-2160p][DV HDR10Plus][DDP Atmos 5.1]"
        "[h265]-FLUX.mkv",
        dict(
            title="Dune Part Two",
            year=2024,
            hdr=["DV", "HDR10+"],
            audio_codecs=["E-AC-3"],
            audio_object="Atmos",
            audio_channels="5.1",
            release_group="FLUX",
        ),
    ),
    (
        "Alien (1979) {tmdb-348} {edition-Director's Cut} [Bluray-1080p Proper]"
        "[DTS-HD MA 5.1][x264]-W4NK3R.mkv",
        dict(
            title="Alien",
            year=1979,
            edition="Director's Cut",
            is_proper=True,
            audio_codecs=["DTS-HD MA"],
            audio_channels="5.1",
            release_group="W4NK3R",
        ),
    ),
    # Jellyfin-style ID tags
    ("Heat (1995) [tmdbid-949].mkv", dict(title="Heat", year=1995, tmdb_id="949")),
    # --- Sonarr
    (
        "Breaking Bad (2008) - S01E01 - Pilot [Bluray-1080p][DTS 5.1][x264]-SPARKS.mkv",
        dict(
            title="Breaking Bad",
            year=2008,
            season=1,
            episodes=[1],
            episode_title="Pilot",
            audio_codecs=["DTS"],
            release_group="SPARKS",
        ),
    ),
    (
        "The Office (US) (2005) - S02E01-E03 - The Dundies [WEBDL-1080p][AAC 2.0][h264].mkv",
        dict(title="The Office (US)", year=2005, season=2, episodes=[1, 2, 3]),
    ),
    ("Show - S01E01E02 - Two Parter.mkv", dict(title="Show", season=1, episodes=[1, 2])),
    (
        "Doctor Who (2005) {tvdb-78804} - S00E01.mkv",
        dict(title="Doctor Who", tvdb_id="78804", season=0, episodes=[1]),
    ),
    # --- Scene
    (
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos-FGT.mkv",
        dict(
            title="The Matrix",
            year=1999,
            resolution="2160p",
            source="BluRay",
            is_remux=True,
            hdr=["HDR"],
            video_codec="HEVC",
            audio_codecs=["TrueHD"],
            audio_object="Atmos",
            audio_channels="7.1",
            audio_tag="TrueHD 7.1 Atmos",
            release_group="FGT",
            style="scene",
        ),
    ),
    (
        "Movie.2020.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX.mkv",
        dict(
            title="Movie",
            year=2020,
            source="WEB-DL",
            audio_codecs=["E-AC-3"],
            audio_channels="5.1",
            audio_object="Atmos",
            video_codec="AVC",
            release_group="FLUX",
        ),
    ),
    (
        "Some.Show.S03E07.720p.HDTV.x264-KILLERS[rarbg].mkv",
        dict(
            title="Some Show",
            season=3,
            episodes=[7],
            resolution="720p",
            source="HDTV",
            release_group="KILLERS",
        ),
    ),
    (
        "Movie.Name.2019.REPACK.1080p.BluRay.x264-GROUP.mkv",
        dict(title="Movie Name", year=2019, is_repack=True, release_group="GROUP"),
    ),
    (
        "Old.Movie.1994.DVDRip.XviD-FLOW.avi",
        dict(
            title="Old Movie",
            year=1994,
            source="DVD",
            video_codec="MPEG-4 Visual",
            release_group="FLOW",
        ),
    ),
    (
        "Show.2021.03.15.Episode.720p.WEB.h264-GRP.mkv",
        dict(title="Show", air_date="2021-03-15", source="WEB-DL", release_group="GRP"),
    ),
    (
        "Film.2018.1080p.AMZN.WEBRip.DDP5.1.x264-NTb.mkv",
        dict(title="Film", year=2018, source="WEBRip", audio_codecs=["E-AC-3"]),
    ),
    # --- Messy / hand-named
    ("Spider-Man (2002).avi", dict(title="Spider-Man", year=2002, release_group=None)),
    ("Spider-Man.mkv", dict(title="Spider-Man", release_group=None)),
    ("2001 A Space Odyssey (1968).mkv", dict(title="2001 A Space Odyssey", year=1968)),
    ("1917 (2019).mkv", dict(title="1917", year=2019)),
    (
        "Blade Runner (1982) Final Cut.mkv",
        dict(title="Blade Runner", year=1982, edition="Final Cut"),
    ),
    ("Tron 2.0 (2003).mkv", dict(title="Tron 2.0", year=2003, audio_channels=None)),
    (
        "Friends 1x05 The One With the East German Laundry Detergent.avi",
        dict(title="Friends", season=1, episodes=[5]),
    ),
    ("star wars episode iv.mp4", dict(title="star wars episode iv", year=None)),
    # --- Anime
    (
        "[SubsPlease] Sousou no Frieren - 07 (1080p) [ABCD1234].mkv",
        dict(
            title="Sousou no Frieren",
            absolute_episode=7,
            resolution="1080p",
            release_group="SubsPlease",
        ),
    ),
    (
        "[Erai-raws] Show Name - S02E05 [1080p][Multiple Subtitle].mkv",
        dict(title="Show Name", season=2, episodes=[5], release_group="Erai-raws"),
    ),
    # --- Extras and samples
    (
        "Movie (2001)-trailer.mkv",
        dict(title="Movie", year=2001, extra_suffix="trailer", release_group=None),
    ),
    ("movie.2010.1080p.bluray.x264-sample.mkv", dict(is_sample=True)),
    ("sample.mkv", dict(is_sample=True)),
]


CASES += [
    # Absolute numbers after SxxEyy need 3-4 digits; short numbers stay in the title.
    ("Show (2020) - S01E01 - 24 Hours [WEBDL-1080p].mkv",
     dict(absolute_episode=None, episode_title="24 Hours")),
    ("Show (2020) - S01E01 - 1984 - Pilot [WEBDL-1080p].mkv",
     dict(absolute_episode=None, episode_title="1984 - Pilot")),
    # Tech tags in brackets are never read as languages ("DV" is also Divehi's code).
    ("Movie (2020) [WEBDL-2160p][DV][EAC3 5.1].mkv", dict(languages=[], hdr=["DV"])),
    ("Movie (2020) [WEBDL-1080p][EN+DE].mkv", dict(languages=["en", "de"])),
    ("Movie.2020.1080p.WEB-DL.10bit.x265-GRP.mkv", dict(bit_depth=10, release_group="GRP")),
    # Spaced group only after a bracket; never inside a plain title.
    ("Movie (2020) [Bluray-1080p]-Some Group.mkv", dict(release_group="Some Group")),
    ("Movie - Part Two (2020).mkv", dict(release_group=None, title="Movie - Part Two")),
]  # fmt: skip


@pytest.mark.parametrize(("name", "expected"), CASES, ids=[c[0][:60] for c in CASES])
def test_parse(name: str, expected: dict[str, Any]) -> None:
    parsed = parse_filename(name).to_dict()
    mismatches = {k: (parsed[k], v) for k, v in expected.items() if parsed[k] != v}
    assert not mismatches, f"(got, expected): {mismatches}"


def test_folder_name_keeps_last_token() -> None:
    p = parse_filename("Movie.2001.1080p", has_extension=False)
    assert p.title == "Movie" and p.year == 2001 and p.resolution == "1080p"


def test_brackets_recorded_in_order() -> None:
    p = parse_filename("A (2000) [Bluray-1080p][DTS 5.1][x264].mkv")
    assert p.brackets == ["Bluray-1080p", "DTS 5.1", "x264"]
