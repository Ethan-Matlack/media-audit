"""Rules engine: pure functions from raw analyzer output + parsed filename to derived data.

Nothing in this package touches the filesystem or the database. That keeps the rules
table-testable and lets ``media-audit rederive`` re-apply them to stored data.
"""
