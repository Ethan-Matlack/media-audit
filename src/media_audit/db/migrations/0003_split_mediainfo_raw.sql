-- Move the raw MediaInfo JSON out of `files` into its own table.
--
-- The JSON is ~90% of the database but is only read when deriving fields, while nearly
-- every web page and stats query scans `files`. Inline, it made `files` ~157 MB for
-- ~22k rows, so each of those scans read the whole lot. Afterwards `files` is ~15 MB.
-- (connect() VACUUMs once after this migration to hand the freed space back.)

CREATE TABLE file_mediainfo (
    file_id INTEGER PRIMARY KEY REFERENCES files (id) ON DELETE CASCADE,
    raw     TEXT NOT NULL      -- JSON, MediaInfo --Output=JSON format
);

INSERT INTO file_mediainfo (file_id, raw)
SELECT id, mediainfo_raw FROM files WHERE mediainfo_raw IS NOT NULL;

ALTER TABLE files DROP COLUMN mediainfo_raw;
