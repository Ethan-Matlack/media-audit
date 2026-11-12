-- Folders with nothing in them (after scan.ignore), recorded by each scan so the Issues
-- page can point out leftovers, e.g. an old "Season 1" next to its renamed "Season 01".
-- Only the topmost empty folder of an empty subtree is stored. Rewritten for a library
-- on every scan of it, so this never needs migrating data.
CREATE TABLE empty_folders (
    library_id INTEGER NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    rel_path   TEXT NOT NULL,
    PRIMARY KEY (library_id, rel_path)
) WITHOUT ROWID;
