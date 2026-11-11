# media-audit

> Working name. Status: **Phase 0 + 1** (scanner, analyzer, rules, titles; CLI only).
> See [HANDOFF.md](HANDOFF.md) for the full brief and roadmap.

A self-hosted tool that scans a media library on disk and reports what's actually in
it: storage per movie/show/season and technical details for every file (container,
codecs, resolution, HDR/Dolby Vision, audio formats and languages, subtitles, release
group...). It is read-only: it never modifies, moves, or renames media.

## Quick start (local)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). libmediainfo comes bundled
with the `pymediainfo` wheel on Linux x86_64/aarch64.

```sh
uv sync
cp config.example.yml config.yml   # edit library paths
uv run media-audit check-config
uv run media-audit scan
uv run media-audit stats
```

## Quick start (Docker)

```sh
./scripts/make_demo_library.sh               # tiny fake library in docker/media (needs ffmpeg)
docker compose build
docker compose run --rm media-audit check-config
docker compose run --rm media-audit scan
docker compose run --rm media-audit stats
docker compose --profile explore up datasette    # browse the DB: http://localhost:8001
docker compose --profile test run --rm test      # lint + mypy + tests with real MediaInfo
```

For a real library, set `MEDIA_ROOT` to the directory holding the libraries listed in
`docker/config/config.yml` (`movies/`, `movies-anime/`, `tv/`, `anime/`). Media is always mounted
read-only. Keep `/config` (the SQLite DB) on local disk, not on the network share.

## Commands

| command | does |
|---|---|
| `scan [-l LIB] [--no-analyze] [--retry-errors] [-w N]` | walk libraries, analyze new/changed files, rebuild titles |
| `rederive` | re-apply all rules to stored MediaInfo data; reads no media files |
| `stats [-l LIB] [--json]` | library breakdowns |
| `check-config` | validate config, library paths, overlaps, libmediainfo |

### Rescans are cheap

- Files are compared by `(size, mtime)`. Unchanged files are never reopened.
- A file that disappears and reappears elsewhere with the same `(inode, size, mtime)`
  counts as a **move** (e.g. a Radarr rename) and keeps its analysis.
- If a library root looks empty but the DB has files for it (an unmounted share), the
  scan refuses to mark everything deleted unless you pass `--allow-empty`.
- Raw MediaInfo JSON is stored per file, so improving a rule only needs `rederive`.
  Bumping `RULES_VERSION` makes the next scan re-derive automatically.

## Configuration

See [config.example.yml](config.example.yml). Any key can be overridden with
`MEDIA_AUDIT_<KEY>` env vars, using `__` for nesting (`MEDIA_AUDIT_SCAN__WORKERS=4`).
`MEDIA_AUDIT_CONFIG` selects the config file.

## Exploring the data

The main views are `v_media` (one row per live video file with derived fields) and
`v_titles` (one row per title with sizes and 4K/HDR/DV/Atmos badges). Titles that
are the same movie or show in different libraries share a `group_key`.

## Layout

```
src/media_audit/
  config.py            YAML + env config (pydantic)
  db/                  connection, migrations (db/migrations/NNNN_*.sql)
  scanner.py           walk + change/move/delete detection (stat only)
  classify.py          file roles from paths: main/extra/sample/subtitle/other
  analyzer/            Analyzer protocol; mediainfo backend (ffprobe can plug in later)
  parsing/filename.py  own regex filename parser (no LGPL guessit)
  rules/               pure derivations: video/HDR/DV, audio/Atmos/DTS:X, subs, languages
  store.py             writes derived rows
  titles.py            titles/seasons, sidecar subtitles, folder sizes, cross-library links
  pipeline.py          scan orchestration, worker pool, lock
  stats.py, cli.py
scripts/               fixture capture/generation, demo library
tests/                 table-driven rules/parser tests, pipeline tests, MediaInfo integration
```

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Rules are tested against 17 MediaInfo captures from a real library (DV, HDR10+,
Atmos, DTS:X, remuxes, AVI, MP4, anime). At the default `parse_speed: 0`, files whose MKV
headers don't describe a TrueHD stream are re-parsed deeper with a time cap, so Atmos
isn't missed. See
[tests/fixtures/mediainfo/README.md](tests/fixtures/mediainfo/README.md).

## License

MIT. Runtime dependencies: pymediainfo (MIT) with bundled libmediainfo (BSD-2-Clause),
PyYAML (MIT), pydantic (MIT).
