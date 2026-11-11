-- Initial schema. See HANDOFF.md §6.
--
-- Conventions:
--   * Timestamps are ISO-8601 UTC text ("2026-11-08T20:21:00Z").
--   * Booleans are INTEGER 0/1.
--   * Large raw JSON columns are declared last in their table so SQLite keeps the
--     small columns in the row's first page and full-table queries stay fast.

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE libraries (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    path       TEXT NOT NULL,
    type       TEXT NOT NULL CHECK (type IN ('movies', 'tv')),
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE scans (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'ok', 'failed', 'interrupted')),
    libraries      TEXT,             -- comma-separated library names covered by this scan
    files_seen     INTEGER NOT NULL DEFAULT 0,
    files_new      INTEGER NOT NULL DEFAULT 0,
    files_changed  INTEGER NOT NULL DEFAULT 0,
    files_moved    INTEGER NOT NULL DEFAULT 0,  -- renamed/moved, matched by inode+size+mtime
    files_deleted  INTEGER NOT NULL DEFAULT 0,
    files_analyzed INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    message        TEXT
);

-- One small aggregate document per finished scan, kept indefinitely, for growth charts.
CREATE TABLE scan_snapshots (
    scan_id    INTEGER PRIMARY KEY REFERENCES scans (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL        -- JSON
);

CREATE TABLE titles (
    id                  INTEGER PRIMARY KEY,
    library_id          INTEGER NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN ('movie', 'show')),
    name                TEXT NOT NULL,
    year                INTEGER,
    tmdb_id             TEXT,
    imdb_id             TEXT,
    tvdb_id             TEXT,
    folder_path         TEXT NOT NULL,  -- for loose files in a library root, the file's path
    group_key           TEXT,           -- titles sharing a group_key are the same movie/show
    file_count          INTEGER NOT NULL DEFAULT 0,
    video_count         INTEGER NOT NULL DEFAULT 0,  -- main-role video files
    version_count       INTEGER NOT NULL DEFAULT 0,  -- movies: distinct main files
    folder_size_bytes   INTEGER NOT NULL DEFAULT 0,  -- apparent size, all files
    folder_unique_bytes INTEGER NOT NULL DEFAULT 0,  -- deduplicated by (dev, inode)
    media_size_bytes    INTEGER NOT NULL DEFAULT 0,  -- video files (any role)
    other_size_bytes    INTEGER NOT NULL DEFAULT 0,  -- everything else
    updated_at          TEXT NOT NULL,
    UNIQUE (library_id, folder_path)
);
CREATE INDEX idx_titles_group ON titles (group_key);

CREATE TABLE seasons (
    id                  INTEGER PRIMARY KEY,
    title_id            INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    season_number       INTEGER,        -- NULL when it can't be determined
    folder_path         TEXT,           -- NULL when episodes sit directly in the show folder
    episode_count       INTEGER NOT NULL DEFAULT 0,
    folder_size_bytes   INTEGER NOT NULL DEFAULT 0,
    folder_unique_bytes INTEGER NOT NULL DEFAULT 0,
    UNIQUE (title_id, season_number)
);

CREATE TABLE files (
    id                  INTEGER PRIMARY KEY,
    library_id          INTEGER NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    title_id            INTEGER REFERENCES titles (id) ON DELETE SET NULL,
    season_id           INTEGER REFERENCES seasons (id) ON DELETE SET NULL,
    parent_file_id      INTEGER REFERENCES files (id) ON DELETE SET NULL,  -- sidecar -> video
    path                TEXT NOT NULL UNIQUE,
    rel_path            TEXT NOT NULL,  -- relative to the library root
    filename            TEXT NOT NULL,
    ext                 TEXT NOT NULL,  -- lowercase, no dot; '' if none
    role                TEXT NOT NULL
                        CHECK (role IN ('main', 'extra', 'sample', 'subtitle', 'other')),
    is_video            INTEGER NOT NULL DEFAULT 0,
    season_number       INTEGER,
    episode_number      INTEGER,
    episode_number_end  INTEGER,        -- multi-episode files (S01E01-E03)
    size_bytes          INTEGER NOT NULL,
    mtime_ns            INTEGER NOT NULL,
    dev                 INTEGER,
    inode               INTEGER,        -- NULL/0 when the filesystem doesn't provide stable inodes
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    changed_at          TEXT,           -- last time size/mtime changed (needs re-analysis)
    deleted_at          TEXT,
    analyzed_at         TEXT,
    analyzer            TEXT,           -- e.g. 'mediainfo 24.12'
    analyze_error       TEXT,
    derived_at          TEXT,
    filename_parsed     TEXT,           -- JSON
    mediainfo_raw       TEXT            -- JSON, MediaInfo --Output=JSON format
);
CREATE INDEX idx_files_library_active ON files (library_id, deleted_at);
CREATE INDEX idx_files_title ON files (title_id);
CREATE INDEX idx_files_season ON files (season_id);
CREATE INDEX idx_files_inode ON files (inode, size_bytes, mtime_ns);
CREATE INDEX idx_files_needs_analysis ON files (is_video, analyzed_at) WHERE deleted_at IS NULL;

CREATE TABLE video_info (
    file_id          INTEGER PRIMARY KEY REFERENCES files (id) ON DELETE CASCADE,
    container        TEXT,
    duration_s       REAL,
    overall_bitrate  INTEGER,        -- bits/s
    gb_per_hour      REAL,
    codec            TEXT,           -- normalized: HEVC, AVC, AV1, VP9, MPEG-2, VC-1, MPEG-4 ASP...
    codec_raw        TEXT,
    profile          TEXT,
    bit_depth        INTEGER,
    width            INTEGER,
    height           INTEGER,
    resolution_class TEXT,           -- 2160p | 1080p | 720p | SD | 4320p
    frame_rate       REAL,
    scan_type        TEXT,
    dynamic_range    TEXT,           -- SDR | HDR10 | HDR10+ | HLG | DV
    hdr_formats      TEXT,           -- JSON list of every HDR format detected, e.g. ["DV","HDR10"]
    dv_profile       INTEGER,
    dv_level         INTEGER,
    dv_compatibility TEXT,           -- base layer: HDR10 | SDR | HLG | NULL (none, e.g. profile 5)
    dv_has_fallback  INTEGER,
    color_primaries  TEXT,
    transfer         TEXT,
    hdr_formats_raw  TEXT            -- MediaInfo's HDR_Format string, for debugging
);

CREATE TABLE audio_tracks (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,   -- order among audio tracks, 0-based
    codec           TEXT,               -- normalized: TrueHD, E-AC-3, AC-3, DTS-HD MA, DTS, AAC...
    codec_raw       TEXT,
    commercial_name TEXT,               -- e.g. 'TrueHD Atmos', 'DTS:X', 'E-AC-3 Atmos'
    channels        INTEGER,
    channel_layout  TEXT,
    channels_label  TEXT,               -- '7.1', '5.1', '2.0'
    language        TEXT NOT NULL DEFAULT 'und',
    title           TEXT,
    bitrate         INTEGER,
    is_default      INTEGER NOT NULL DEFAULT 0,
    is_forced       INTEGER NOT NULL DEFAULT 0,
    is_object_audio INTEGER NOT NULL DEFAULT 0,
    UNIQUE (file_id, idx)
);
CREATE INDEX idx_audio_file ON audio_tracks (file_id);

CREATE TABLE subtitle_tracks (
    id                  INTEGER PRIMARY KEY,
    file_id             INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    idx                 INTEGER NOT NULL,  -- embedded: order among text tracks; external: 1000+
    format              TEXT,              -- SRT, PGS, ASS, SSA, VobSub, WebVTT, ...
    language            TEXT NOT NULL DEFAULT 'und',
    title               TEXT,
    is_forced           INTEGER NOT NULL DEFAULT 0,
    is_default          INTEGER NOT NULL DEFAULT 0,
    is_hearing_impaired INTEGER NOT NULL DEFAULT 0,
    is_external         INTEGER NOT NULL DEFAULT 0,
    external_file_id    INTEGER REFERENCES files (id) ON DELETE CASCADE,
    UNIQUE (file_id, idx)
);
CREATE INDEX idx_subs_file ON subtitle_tracks (file_id);

CREATE TABLE release_info (
    file_id         INTEGER PRIMARY KEY REFERENCES files (id) ON DELETE CASCADE,
    release_group   TEXT,
    source          TEXT,       -- BluRay | WEB-DL | WEBRip | HDTV | DVD | ...
    edition         TEXT,
    quality_tag     TEXT,       -- the bracketed Radarr/Sonarr quality, e.g. 'Bluray-2160p'
    resolution_tag  TEXT,       -- resolution claimed by the name, e.g. '2160p'
    video_codec_tag TEXT,       -- as written: x265 | h265 | HEVC | ...
    audio_tag       TEXT,       -- as written: 'DTS-X 7.1' | 'EAC3 7.1' | ...
    hdr_tags        TEXT,       -- JSON list as written: ["HDR10"], ["DV","HDR10"]
    is_remux        INTEGER NOT NULL DEFAULT 0,
    is_proper       INTEGER NOT NULL DEFAULT 0,
    is_repack       INTEGER NOT NULL DEFAULT 0
);

-- Phase 2 fills this in.
CREATE TABLE flags (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER REFERENCES files (id) ON DELETE CASCADE,
    title_id   INTEGER REFERENCES titles (id) ON DELETE CASCADE,
    code       TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'error')),
    detail     TEXT,
    created_at TEXT NOT NULL,
    CHECK (file_id IS NOT NULL OR title_id IS NOT NULL)
);
CREATE INDEX idx_flags_code ON flags (code);
CREATE INDEX idx_flags_file ON flags (file_id);

-- Every non-deleted video file with its derived fields. The main view for browsing.
CREATE VIEW v_media AS
SELECT
    f.id AS file_id,
    l.name AS library,
    l.type AS library_type,
    t.name AS title,
    t.year AS year,
    f.season_number,
    f.episode_number,
    f.role,
    f.rel_path,
    f.filename,
    f.ext,
    f.size_bytes,
    round(f.size_bytes / 1e9, 2) AS size_gb,
    v.container,
    v.duration_s,
    v.gb_per_hour,
    v.overall_bitrate,
    v.codec AS video_codec,
    v.profile AS video_profile,
    v.bit_depth,
    v.width,
    v.height,
    v.resolution_class,
    v.frame_rate,
    v.dynamic_range,
    v.hdr_formats,
    v.dv_profile,
    v.dv_compatibility,
    v.dv_has_fallback,
    (SELECT group_concat(a.commercial_name || ' ' || coalesce(a.channels_label, '') || ' [' || a.language || ']', ', ')
       FROM audio_tracks a WHERE a.file_id = f.id) AS audio,
    (SELECT group_concat(DISTINCT s.language) FROM subtitle_tracks s WHERE s.file_id = f.id) AS subtitle_languages,
    r.release_group,
    r.source,
    r.edition,
    r.quality_tag,
    r.is_remux,
    f.analyze_error,
    f.path
FROM files f
JOIN libraries l ON l.id = f.library_id
LEFT JOIN titles t ON t.id = f.title_id
LEFT JOIN video_info v ON v.file_id = f.id
LEFT JOIN release_info r ON r.file_id = f.id
WHERE f.deleted_at IS NULL AND f.is_video = 1;

-- One row per title with size and quick badges.
CREATE VIEW v_titles AS
SELECT
    t.id AS title_id,
    l.name AS library,
    t.kind,
    t.name,
    t.year,
    t.tmdb_id,
    t.imdb_id,
    t.tvdb_id,
    coalesce(t.group_key, 'title:' || t.id) AS group_key,
    t.file_count,
    t.video_count,
    t.version_count,
    t.folder_size_bytes,
    round(t.folder_size_bytes / 1e9, 2) AS folder_size_gb,
    t.folder_unique_bytes,
    t.media_size_bytes,
    t.other_size_bytes,
    (SELECT count(*) FROM seasons s WHERE s.title_id = t.id) AS season_count,
    EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND v.resolution_class IN ('2160p', '4320p')) AS has_4k,
    EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND v.dynamic_range <> 'SDR') AS has_hdr,
    EXISTS (SELECT 1 FROM files f JOIN video_info v ON v.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND v.dv_profile IS NOT NULL) AS has_dv,
    EXISTS (SELECT 1 FROM files f JOIN audio_tracks a ON a.file_id = f.id
            WHERE f.title_id = t.id AND f.deleted_at IS NULL AND f.role = 'main'
              AND a.is_object_audio = 1) AS has_object_audio,
    t.folder_path
FROM titles t
JOIN libraries l ON l.id = t.library_id;
