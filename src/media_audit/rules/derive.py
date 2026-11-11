"""Top-level pure derivation: (filename, raw MediaInfo, size) -> derived records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from media_audit.parsing import ParsedName, parse_filename
from media_audit.rules.audio import AudioTrack, derive_audio
from media_audit.rules.subtitles import SubtitleTrack, derive_subtitles
from media_audit.rules.video import VideoInfo, derive_video

# Bump when derivation logic changes; stored data is re-derived automatically.
RULES_VERSION = 3


@dataclass
class ReleaseInfo:
    release_group: str | None
    source: str | None
    edition: str | None
    quality_tag: str | None
    resolution_tag: str | None
    video_codec_tag: str | None
    audio_tag: str | None
    hdr_tags: list[str]
    is_remux: bool
    is_proper: bool
    is_repack: bool


@dataclass
class Derived:
    parsed: ParsedName
    release: ReleaseInfo
    video: VideoInfo | None = None
    audio: list[AudioTrack] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)


def release_from_parsed(p: ParsedName) -> ReleaseInfo:
    return ReleaseInfo(
        release_group=p.release_group,
        source=p.source,
        edition=p.edition,
        quality_tag=p.quality_tag,
        resolution_tag=p.resolution,
        video_codec_tag=p.video_codec_tag,
        audio_tag=p.audio_tag,
        hdr_tags=list(p.hdr),
        is_remux=p.is_remux,
        is_proper=p.is_proper,
        is_repack=p.is_repack,
    )


def derive(filename: str, ext: str, size_bytes: int, raw: dict[str, Any] | None) -> Derived:
    parsed = parse_filename(filename)
    result = Derived(parsed=parsed, release=release_from_parsed(parsed))
    if raw:
        result.video = derive_video(raw, size_bytes, ext)
        result.audio = derive_audio(raw)
        result.subtitles = derive_subtitles(raw)
    return result
