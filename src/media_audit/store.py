"""Persist derived data. The only module that maps rules-engine dataclasses to tables."""

from __future__ import annotations

import json
import logging
import sqlite3

from media_audit.db import get_meta, set_meta, transaction
from media_audit.rules.derive import RULES_VERSION, Derived, derive

log = logging.getLogger(__name__)


def _b(value: bool | None) -> int | None:
    return None if value is None else int(value)


def write_derived(
    conn: sqlite3.Connection, file_id: int, is_video: bool, d: Derived, now: str
) -> None:
    p = d.parsed
    episode = p.episodes[0] if p.episodes else p.absolute_episode
    episode_end = p.episodes[-1] if len(p.episodes) > 1 else None
    conn.execute(
        "UPDATE files SET filename_parsed = ?, season_number = ?, episode_number = ?, "
        "episode_number_end = ?, derived_at = ? WHERE id = ?",
        (json.dumps(p.to_dict(), separators=(",", ":")), p.season, episode, episode_end, now,
         file_id),
    )  # fmt: skip

    conn.execute("DELETE FROM release_info WHERE file_id = ?", (file_id,))
    if is_video:
        r = d.release
        conn.execute(
            "INSERT INTO release_info (file_id, release_group, source, edition, quality_tag, "
            "resolution_tag, video_codec_tag, audio_tag, hdr_tags, is_remux, is_proper, "
            "is_repack) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, r.release_group, r.source, r.edition, r.quality_tag, r.resolution_tag,
             r.video_codec_tag, r.audio_tag, json.dumps(r.hdr_tags) if r.hdr_tags else None,
             int(r.is_remux), int(r.is_proper), int(r.is_repack)),
        )  # fmt: skip

    conn.execute("DELETE FROM video_info WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM audio_tracks WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM subtitle_tracks WHERE file_id = ? AND is_external = 0", (file_id,))
    v = d.video
    if v is None:
        return
    conn.execute(
        "INSERT INTO video_info (file_id, container, duration_s, overall_bitrate, gb_per_hour, "
        "codec, codec_raw, profile, bit_depth, width, height, resolution_class, frame_rate, "
        "scan_type, dynamic_range, hdr_formats, dv_profile, dv_level, dv_compatibility, "
        "dv_has_fallback, color_primaries, transfer, hdr_formats_raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (file_id, v.container, v.duration_s, v.overall_bitrate, v.gb_per_hour, v.codec,
         v.codec_raw, v.profile, v.bit_depth, v.width, v.height, v.resolution_class,
         v.frame_rate, v.scan_type, v.dynamic_range,
         json.dumps(v.hdr_formats) if v.hdr_formats else None, v.dv_profile, v.dv_level,
         v.dv_compatibility, _b(v.dv_has_fallback), v.color_primaries, v.transfer,
         v.hdr_formats_raw),
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO audio_tracks (file_id, idx, codec, codec_raw, commercial_name, channels, "
        "channel_layout, channels_label, language, title, bitrate, is_default, is_forced, "
        "is_object_audio, details_incomplete, object_audio_hint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (file_id, a.idx, a.codec, a.codec_raw, a.commercial_name, a.channels,
             a.channel_layout, a.channels_label, a.language, a.title, a.bitrate,
             int(a.is_default), int(a.is_forced), int(a.is_object_audio),
             int(a.details_incomplete), a.object_audio_hint)
            for a in d.audio
        ],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO subtitle_tracks (file_id, idx, format, language, title, is_forced, "
        "is_default, is_hearing_impaired, is_external) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        [
            (file_id, s.idx, s.format, s.language, s.title, int(s.is_forced),
             int(s.is_default), int(s.is_hearing_impaired))
            for s in d.subtitles
        ],
    )  # fmt: skip


def derive_pending(conn: sqlite3.Connection, now: str, library_ids: list[int] | None = None) -> int:
    """Derive every live file whose ``derived_at`` is NULL. Returns the count."""
    sql = (
        "SELECT id, filename, ext, size_bytes, is_video, mediainfo_raw FROM files "
        "WHERE deleted_at IS NULL AND derived_at IS NULL"
    )
    params: list[object] = []
    if library_ids is not None:
        sql += f" AND library_id IN ({','.join('?' * len(library_ids))})"
        params.extend(library_ids)
    rows = conn.execute(sql, params).fetchall()
    count = 0
    for start in range(0, len(rows), 500):
        with transaction(conn):
            for row in rows[start : start + 500]:
                raw = None
                if row["mediainfo_raw"]:
                    try:
                        raw = json.loads(row["mediainfo_raw"])
                    except json.JSONDecodeError:
                        log.warning("file %s: stored MediaInfo JSON is corrupt", row["id"])
                d = derive(row["filename"], row["ext"], row["size_bytes"], raw)
                write_derived(conn, row["id"], bool(row["is_video"]), d, now)
                count += 1
    return count


def ensure_rules_version(conn: sqlite3.Connection) -> bool:
    """If derivation rules changed since the data was written, queue a full re-derive."""
    stored = get_meta(conn, "rules_version")
    if stored == str(RULES_VERSION):
        return False
    with transaction(conn):
        conn.execute("UPDATE files SET derived_at = NULL")
        set_meta(conn, "rules_version", str(RULES_VERSION))
    return stored is not None


def invalidate_all(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("UPDATE files SET derived_at = NULL")
