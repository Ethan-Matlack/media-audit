-- Issues (Phase 2): the `flags` table reserved in 0001 was never written; replace it with
-- the shape the checks need, and add a table of issues the user chose to ignore.
--
-- `flags` is rebuilt from scratch after every scan and rederive (media_audit/flags.py),
-- so it never needs migrating data. `flag_ignores` is user data and is never rebuilt.

DROP TABLE flags;

CREATE TABLE flags (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL,      -- which check (see media_audit/rules/flags.py)
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    key           TEXT NOT NULL UNIQUE,  -- stable identity across scans (ignores match on it)
    fingerprint   TEXT NOT NULL,      -- what it depends on; an ignore lapses when this changes
    library_id    INTEGER NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    title_id      INTEGER REFERENCES titles (id) ON DELETE CASCADE,
    file_id       INTEGER REFERENCES files (id) ON DELETE CASCADE,
    season_number INTEGER,
    message       TEXT NOT NULL,      -- one plain-language line: what's wrong, with numbers
    data          TEXT,               -- JSON details for display (e.g. the duplicate files)
    bytes_recoverable INTEGER,        -- duplicates: space freed by keeping only the largest
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_flags_code ON flags (code);
CREATE INDEX idx_flags_title ON flags (title_id);
CREATE INDEX idx_flags_file ON flags (file_id);
CREATE INDEX idx_flags_library ON flags (library_id);

CREATE TABLE flag_ignores (
    key         TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    code        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
