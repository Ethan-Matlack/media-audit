"""Subtitle derivations for embedded (MediaInfo Text tracks) and sidecar files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from media_audit.rules import mi
from media_audit.rules.languages import UNDETERMINED, is_known_code, normalize_language

EXTERNAL_IDX_BASE = 1000


@dataclass
class SubtitleTrack:
    idx: int
    format: str | None = None
    language: str = UNDETERMINED
    title: str | None = None
    is_forced: bool = False
    is_default: bool = False
    is_hearing_impaired: bool = False
    is_external: bool = False
    external_file_id: int | None = None


_FORMATS = {
    "utf-8": "SRT",
    "subrip": "SRT",
    "ass": "ASS",
    "ssa": "SSA",
    "pgs": "PGS",
    "vobsub": "VobSub",
    "rle": "VobSub",
    "dvb subtitle": "DVB",
    "timed text": "mov_text",
    "tx3g": "mov_text",
    "webvtt": "WebVTT",
    "eia-608": "EIA-608",
    "eia-708": "EIA-708",
    "hdmv text": "HDMV Text",
}

_HI = re.compile(r"(?<![A-Za-z])(SDH|HI|CC|hearing[ -]impaired)(?![A-Za-z])", re.I)
_FORCED_TITLE = re.compile(r"(?<![A-Za-z])forced(?![A-Za-z])", re.I)


def subtitle_format(track: mi.Track) -> str | None:
    fmt = mi.get(track, "Format")
    if fmt is None:
        codec_id = (mi.get(track, "CodecID") or "").upper()
        if "PGS" in codec_id:
            return "PGS"
        if "UTF8" in codec_id:
            return "SRT"
        return None
    return _FORMATS.get(fmt.lower(), fmt)


def derive_subtitles(raw: dict[str, Any]) -> list[SubtitleTrack]:
    result = []
    for idx, track in enumerate(mi.tracks(raw, "Text")):
        title = mi.get(track, "Title")
        hi = mi.get_bool(track, "HearingImpaired", "Hearing_Impaired") or bool(
            title and _HI.search(title)
        )
        forced = mi.get_bool(track, "Forced") or bool(title and _FORCED_TITLE.search(title))
        result.append(
            SubtitleTrack(
                idx=idx,
                format=subtitle_format(track),
                language=normalize_language(mi.get(track, "Language")),
                title=title,
                is_forced=forced,
                is_default=mi.get_bool(track, "Default"),
                is_hearing_impaired=hi,
            )
        )
    return result


_SIDECAR_FORMATS = {
    "srt": "SRT",
    "ass": "ASS",
    "ssa": "SSA",
    "sub": "VobSub",
    "idx": "VobSub",
    "sup": "PGS",
    "vtt": "WebVTT",
    "smi": "SAMI",
}


def parse_sidecar(video_stem: str, sidecar_name: str) -> SubtitleTrack | None:
    """Describe ``Movie.en.forced.srt`` relative to video ``Movie``.

    Returns ``None`` when ``sidecar_name`` doesn't belong to ``video_stem``.
    Recognizes language codes/names and ``forced``, ``sdh``/``hi``/``cc``, ``default``
    tokens after the video's stem, in any order.
    """
    dot = sidecar_name.rfind(".")
    if dot <= 0:
        return None
    ext = sidecar_name[dot + 1 :].lower()
    if ext not in _SIDECAR_FORMATS:
        return None
    stem = sidecar_name[:dot]
    if stem != video_stem and not stem.startswith(video_stem + "."):
        return None
    tokens = [t for t in stem[len(video_stem) :].split(".") if t]
    track = SubtitleTrack(idx=EXTERNAL_IDX_BASE, format=_SIDECAR_FORMATS[ext], is_external=True)
    titles: list[str] = []
    for token in tokens:
        low = token.lower()
        if low == "forced":
            track.is_forced = True
        elif low in {"sdh", "hi", "cc"}:
            track.is_hearing_impaired = True
        elif low == "default":
            track.is_default = True
        elif track.language == UNDETERMINED and is_known_code(low.split("-")[0]):
            track.language = normalize_language(low)
        else:
            titles.append(token)
    track.title = " ".join(titles) or None
    return track
