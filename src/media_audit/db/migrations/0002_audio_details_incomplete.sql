-- Audio tracks MediaInfo couldn't read at the configured parse speed (bare "TrueHD"),
-- and the object-audio hint taken from the track title in that case.
ALTER TABLE audio_tracks ADD COLUMN details_incomplete INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audio_tracks ADD COLUMN object_audio_hint TEXT;
