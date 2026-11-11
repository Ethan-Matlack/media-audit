"""Audio track derivations: normalized codec, commercial name, object audio, channels.

Atmos/DTS:X detection uses ``Format_AdditionalFeatures`` (``16-ch``, ``JOC``,
``XLL X``) and ``Format_Commercial_IfAny`` (validated on real captures; older MediaInfo
builds put the same information in ``Format_Profile``).

Header-only parsing (parse_speed 0) sometimes leaves a TrueHD track unread: MediaInfo
then reports the container's bare ``Format: TrueHD`` instead of ``MLP FBA`` and nothing
about Atmos. Such tracks are marked ``details_incomplete``; the analyzer retries those
files with a deeper parse, and if that isn't possible the track title ("Atmos") becomes
an ``object_audio_hint`` instead of a confirmed ``is_object_audio``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from media_audit.rules import mi
from media_audit.rules.languages import normalize_language


@dataclass
class AudioTrack:
    idx: int
    codec: str | None = None
    codec_raw: str | None = None
    commercial_name: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    channels_label: str | None = None
    language: str = "und"
    title: str | None = None
    bitrate: int | None = None
    is_default: bool = False
    is_forced: bool = False
    is_object_audio: bool = False
    details_incomplete: bool = False
    object_audio_hint: str | None = None


_ATMOS_HINT = re.compile(r"(?<![A-Za-z])atmos(?![A-Za-z])", re.I)


def track_details_incomplete(track: mi.Track) -> bool:
    """True when MediaInfo didn't read this audio stream, so object audio is unknown.

    Only TrueHD is known to do this (real capture: Thor Ragnarok at parse_speed 0). A
    parsed TrueHD stream reports ``Format: MLP FBA``; an unread one keeps the
    container's ``TrueHD`` with no commercial name or features.
    """
    fmt = (mi.get(track, "Format") or "").strip().lower()
    return fmt == "truehd" and not (
        mi.get(track, "Format_AdditionalFeatures") or mi.get(track, "Format_Commercial_IfAny")
    )


def needs_deeper_parse(raw: dict[str, Any]) -> bool:
    """True if any audio track in a MediaInfo document is incomplete (see above)."""
    return any(track_details_incomplete(t) for t in mi.tracks(raw, "Audio"))


def _codec_and_object(track: mi.Track) -> tuple[str | None, str | None]:
    """Return (codec, object_audio) where object_audio is 'Atmos', 'DTS:X' or None."""
    fmt = (mi.get(track, "Format") or "").strip()
    low = fmt.lower()
    features = (mi.get(track, "Format_AdditionalFeatures") or "").lower()
    commercial = (mi.get(track, "Format_Commercial_IfAny", "Format_Commercial") or "").lower()
    profile = (mi.get(track, "Format_Profile") or "").lower()
    everything = " ".join((features, commercial, profile))

    if low in {"mlp fba", "truehd", "mlp"} or "truehd" in commercial:
        atmos = "16-ch" in features or "atmos" in everything
        return "TrueHD", "Atmos" if atmos else None
    if low in {"e-ac-3", "eac3"} or low.startswith("e-ac-3"):
        atmos = "joc" in everything or "atmos" in everything or "joc" in low
        return "E-AC-3", "Atmos" if atmos else None
    if low in {"ac-3", "ac3"}:
        # AC-3 can't carry Atmos; anything claiming so is metadata noise.
        return "AC-3", None
    if low.startswith("dts"):
        tokens = set(features.replace("/", " ").split()) | set(profile.replace("/", " ").split())
        is_x = "x" in tokens or "dts:x" in commercial or "dts-x" in commercial
        if "xll" in tokens or "ma" in tokens or "master audio" in commercial:
            return "DTS-HD MA", "DTS:X" if is_x else None
        if "xbr" in tokens or "hra" in tokens or "high resolution" in commercial:
            return "DTS-HD HRA", "DTS:X" if is_x else None
        if "es" in tokens or "xch" in tokens or "xxch" in tokens or "dts-es" in commercial:
            return "DTS-ES", None
        if "express" in commercial or "lbr" in tokens:
            return "DTS Express", None
        return "DTS", "DTS:X" if is_x else None
    if low == "aac":
        return "AAC", None
    if low == "mpeg audio":
        if "layer 3" in profile:
            return "MP3", None
        if "layer 2" in profile:
            return "MP2", None
        return "MPEG Audio", None
    mapping = {
        "flac": "FLAC",
        "opus": "Opus",
        "vorbis": "Vorbis",
        "pcm": "PCM",
        "alac": "ALAC",
        "wma": "WMA",
        "amr": "AMR",
    }
    return mapping.get(low, fmt or None), None


def commercial_name(codec: str | None, object_audio: str | None) -> str | None:
    if codec is None:
        return None
    if object_audio == "DTS:X":
        return "DTS:X"
    if object_audio == "Atmos":
        return f"{codec} Atmos"
    return codec


def channels_label(channels: int | None, layout: str | None) -> str | None:
    if not channels:
        return None
    if layout:
        lfe = sum(1 for part in layout.split() if part.upper().startswith("LFE"))
        if lfe:
            return f"{channels - lfe}.{lfe}"
        return f"{channels}.0"
    defaults = {6: "5.1", 7: "6.1", 8: "7.1"}
    return defaults.get(channels, f"{channels}.0")


def derive_audio(raw: dict[str, Any]) -> list[AudioTrack]:
    result = []
    for idx, track in enumerate(mi.tracks(raw, "Audio")):
        codec, obj = _codec_and_object(track)
        layout = mi.get(track, "ChannelLayout", "ChannelLayout_Original")
        channels = mi.get_int(track, "Channels", "Channels_Original")
        # "Channels" can be the container's claim (an AVI header saying 2) while
        # "Channels_Original" + "ChannelLayout_Original" describe the actual stream
        # (AC-3 acmod 1 = mono). Trust the stream when its layout is self-consistent.
        orig_channels = mi.get_int(track, "Channels_Original")
        orig_layout = mi.get(track, "ChannelLayout_Original")
        if (
            obj is None
            and orig_channels
            and orig_layout
            and len(orig_layout.split()) == orig_channels
            and orig_channels != channels
        ):
            channels, layout = orig_channels, orig_layout
        if layout and channels and len(layout.split()) != channels:
            # Some builds give "Channels: 8" and a layout for the core only; trust the count.
            layout_for_label = None
        else:
            layout_for_label = layout
        incomplete = track_details_incomplete(track)
        title = mi.get(track, "Title")
        hint = "Atmos" if incomplete and title and _ATMOS_HINT.search(title) else None
        name = commercial_name(codec, obj)
        if hint and name:
            name = f"{name} (Atmos?)"
        result.append(
            AudioTrack(
                idx=idx,
                codec=codec,
                codec_raw=" / ".join(
                    x
                    for x in (
                        mi.get(track, "Format"),
                        mi.get(track, "Format_AdditionalFeatures"),
                        mi.get(track, "Format_Commercial_IfAny"),
                    )
                    if x
                )
                or None,
                commercial_name=name,
                channels=channels,
                channel_layout=layout,
                channels_label=channels_label(channels, layout_for_label),
                language=normalize_language(mi.get(track, "Language")),
                title=title,
                bitrate=mi.get_int(track, "BitRate", "BitRate_Nominal"),
                is_default=mi.get_bool(track, "Default"),
                is_forced=mi.get_bool(track, "Forced"),
                is_object_audio=obj is not None,
                details_incomplete=incomplete,
                object_audio_hint=hint,
            )
        )
    return result
