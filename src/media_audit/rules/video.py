"""Video derivations: container, codec, resolution class, dynamic range / Dolby Vision.

PROVISIONAL: the exact MediaInfo strings for HDR10+/Dolby Vision vary between
MediaInfo versions. The parsing here is deliberately tolerant and is covered by
synthetic fixtures; it must be re-validated against real captures (see
``tests/fixtures/mediainfo/README.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from media_audit.rules import mi

# --------------------------------------------------------------------- resolution

# (class, min_width, min_height). Width is primary: widescreen films are cropped
# vertically (1920x800 is still 1080p, 3840x1600 is still 2160p). Height catches
# narrow-but-tall content (1440x1080 anamorphic HDTV, 960x720 4:3).
_RESOLUTION_CLASSES: list[tuple[str, int, int]] = [
    ("4320p", 7000, 4000),
    ("2160p", 3400, 2000),
    ("1080p", 1800, 1000),
    ("720p", 1200, 700),
]


def resolution_class(width: int | None, height: int | None) -> str | None:
    if not width and not height:
        return None
    w = width or 0
    h = height or 0
    for name, min_w, min_h in _RESOLUTION_CLASSES:
        if w >= min_w or h >= min_h:
            return name
    return "SD"


# --------------------------------------------------------------------- container

_CONTAINERS = {
    "matroska": "MKV",
    "webm": "WebM",
    "mpeg-4": "MP4",
    "avi": "AVI",
    "windows media": "WMV",
    "mpeg-ts": "TS",
    "bdav": "M2TS",
    "mpeg-ps": "MPEG-PS",
    "flash video": "FLV",
    "ogg": "OGG",
    "realmedia": "RM",
    "dvd video": "VOB",
    "iso 9660": "ISO",
}


def container_name(general: mi.Track | None, ext: str) -> str | None:
    fmt = mi.get(general, "Format")
    if fmt is None:
        return ext.upper() or None
    low = fmt.lower()
    if low == "mpeg-4":
        profile = (mi.get(general, "Format_Profile") or "").lower()
        if "quicktime" in profile or ext == "mov":
            return "MOV"
    return _CONTAINERS.get(low, fmt)


# --------------------------------------------------------------------- codec


def video_codec(track: mi.Track | None) -> str | None:
    fmt = mi.get(track, "Format")
    if fmt is None:
        return None
    low = fmt.lower()
    if low == "mpeg video":
        version = mi.get(track, "Format_Version") or ""
        if "1" in version:
            return "MPEG-1"
        return "MPEG-2"
    mapping = {
        "hevc": "HEVC",
        "avc": "AVC",
        "av1": "AV1",
        "vp9": "VP9",
        "vp8": "VP8",
        "vc-1": "VC-1",
        "mpeg-4 visual": "MPEG-4 Visual",
        "h.263": "H.263",
        "prores": "ProRes",
        "realvideo 4": "RealVideo",
        "wmv2": "WMV2",
        "wmv1": "WMV1",
        "msmpeg4v3": "MS-MPEG4",
    }
    return mapping.get(low, fmt)


# --------------------------------------------------------------------- HDR / DV

_DV_PROFILE = re.compile(r"(?:dvhe|dvh1|dvav|dva1|dav1|davc)\.(\d{1,2})(?:\.(\d{1,2}))?", re.I)
_DV_PROFILE_WORD = re.compile(r"Profile\s+(\d{1,2})(?:\.(\d))?", re.I)


@dataclass
class HdrInfo:
    dynamic_range: str = "SDR"
    formats: list[str] = field(default_factory=list)
    dv_profile: int | None = None
    dv_level: int | None = None
    dv_compatibility: str | None = None  # HDR10 | SDR | HLG | None
    dv_has_fallback: bool | None = None
    raw: str | None = None


def hdr_info(track: mi.Track | None) -> HdrInfo:
    info = HdrInfo()
    if not track:
        return info
    hdr_format = mi.get(track, "HDR_Format")
    info.raw = hdr_format
    formats = mi.split_multi(hdr_format)
    profiles = mi.split_multi(mi.get(track, "HDR_Format_Profile"))
    levels = mi.split_multi(mi.get(track, "HDR_Format_Level"))
    compats = mi.split_multi(mi.get(track, "HDR_Format_Compatibility"))
    transfer = (
        mi.get(track, "transfer_characteristics", "transfer_characteristics_Original") or ""
    ).upper()
    hdr_text = " / ".join(formats + compats).lower()

    found: list[str] = []
    dv_index = next((i for i, f in enumerate(formats) if "dolby vision" in f.lower()), None)
    if dv_index is not None:
        found.append("DV")
        prof_src = " ".join(
            x
            for x in (
                profiles[dv_index] if dv_index < len(profiles) else "",
                formats[dv_index],
            )
            if x
        )
        m = _DV_PROFILE.search(prof_src) or _DV_PROFILE_WORD.search(prof_src)
        if m:
            info.dv_profile = int(m.group(1))
            if m.group(2) and m.re is _DV_PROFILE:
                info.dv_level = int(m.group(2))
        if info.dv_level is None and dv_index < len(levels):
            lvl = re.search(r"\d+", levels[dv_index])
            if lvl:
                info.dv_level = int(lvl.group(0))
        compat = compats[dv_index] if dv_index < len(compats) else ""
        info.dv_compatibility = _dv_compatibility(info.dv_profile, compat, transfer)
        info.dv_has_fallback = info.dv_compatibility is not None

    if "2094 app 4" in hdr_text or "hdr10+" in hdr_text:
        found.append("HDR10+")
    if (
        "2086" in hdr_text
        or re.search(r"hdr10(?!\+)", hdr_text)
        or (transfer == "PQ" and info.dv_profile != 5)
    ):
        found.append("HDR10")
    if "HLG" in transfer or "B67" in transfer or "hlg" in hdr_text:
        found.append("HLG")

    # Dolby Vision profile 5 has no HDR10 base layer even though some builds report PQ.
    if info.dv_profile == 5:
        found = [f for f in found if f not in {"HDR10", "HDR10+"}]

    seen: list[str] = []
    for f in found:
        if f not in seen:
            seen.append(f)
    info.formats = seen
    for primary in ("DV", "HDR10+", "HDR10", "HLG"):
        if primary in seen:
            info.dynamic_range = primary
            break
    return info


def _dv_compatibility(profile: int | None, compat: str, transfer: str) -> str | None:
    c = compat.lower()
    if profile == 5:
        return None
    if "hdr10" in c or "blu-ray" in c:
        return "HDR10"
    if "hlg" in c:
        return "HLG"
    if "sdr" in c:
        return "SDR"
    # No compatibility string: infer from the profile, then the base layer's transfer.
    if profile == 7:
        return "HDR10"
    if profile == 4:
        return "SDR"
    if profile in {8, 9, 10}:
        if transfer == "PQ":
            return "HDR10"
        if "HLG" in transfer or "B67" in transfer:
            return "HLG"
        if transfer:
            return "SDR"
    return None


# --------------------------------------------------------------------- assembly


@dataclass
class VideoInfo:
    container: str | None = None
    duration_s: float | None = None
    overall_bitrate: int | None = None
    gb_per_hour: float | None = None
    codec: str | None = None
    codec_raw: str | None = None
    profile: str | None = None
    bit_depth: int | None = None
    width: int | None = None
    height: int | None = None
    resolution_class: str | None = None
    frame_rate: float | None = None
    scan_type: str | None = None
    dynamic_range: str | None = None
    hdr_formats: list[str] = field(default_factory=list)
    dv_profile: int | None = None
    dv_level: int | None = None
    dv_compatibility: str | None = None
    dv_has_fallback: bool | None = None
    color_primaries: str | None = None
    transfer: str | None = None
    hdr_formats_raw: str | None = None


def derive_video(raw: dict[str, Any], size_bytes: int, ext: str) -> VideoInfo:
    general = mi.first_track(raw, "General")
    video = mi.first_track(raw, "Video")
    v = VideoInfo()
    v.container = container_name(general, ext)
    v.duration_s = mi.get_float(general, "Duration") or mi.get_float(video, "Duration")
    v.overall_bitrate = mi.get_int(general, "OverallBitRate")
    if v.duration_s and v.duration_s > 0:
        v.gb_per_hour = round((size_bytes / 1e9) / (v.duration_s / 3600), 3)
        if v.overall_bitrate is None:
            v.overall_bitrate = int(size_bytes * 8 / v.duration_s)
    if video is None:
        return v
    v.codec = video_codec(video)
    v.codec_raw = " / ".join(x for x in (mi.get(video, "Format"), mi.get(video, "CodecID")) if x)
    v.profile = mi.get(video, "Format_Profile")
    v.bit_depth = mi.get_int(video, "BitDepth")
    v.width = mi.get_int(video, "Width")
    v.height = mi.get_int(video, "Height")
    v.resolution_class = resolution_class(v.width, v.height)
    fr = mi.get_float(video, "FrameRate", "FrameRate_Nominal", "FrameRate_Original")
    v.frame_rate = round(fr, 3) if fr else None
    v.scan_type = mi.get(video, "ScanType")
    v.color_primaries = mi.get(video, "colour_primaries")
    v.transfer = mi.get(video, "transfer_characteristics")
    hdr = hdr_info(video)
    v.dynamic_range = hdr.dynamic_range
    v.hdr_formats = hdr.formats
    v.dv_profile = hdr.dv_profile
    v.dv_level = hdr.dv_level
    v.dv_compatibility = hdr.dv_compatibility
    v.dv_has_fallback = hdr.dv_has_fallback
    v.hdr_formats_raw = hdr.raw
    return v
