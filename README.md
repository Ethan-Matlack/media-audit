# media-audit

> Working name. Status: **Phase 0 + 1** (scanner, analyzer, rules, titles; CLI only).

A self-hosted tool that scans a media library on disk and reports what's actually in
it: storage per movie/show/season and technical details for every file (container,
codecs, resolution, HDR/Dolby Vision, audio formats and languages, subtitles, release
group...). It is read-only: it never modifies, moves, or renames media.

## Quick start (Unraid)

The image is published to GitHub Container Registry as
`ghcr.io/ethan-matlack/media-audit`.

1. **Add the template.** In the Unraid terminal:
   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/my-media-audit.xml \
     https://raw.githubusercontent.com/Ethan-Matlack/media-audit/main/unraid/media-audit.xml
   ```
2. **Create the container.** Docker → Add Container → Template: **media-audit**. Set:
   - **Media**: the folder that contains your libraries, e.g. `/mnt/user/media/content`
     if that's where `movies/` and `tv/` live. It's mounted read-only.
   - **Config**: leave it at `/mnt/user/appdata/media-audit`.
   - **PUID / PGID** (under advanced): leave them at `99` / `100` (nobody:users).

   Click Apply.
3. **First start writes a config and stops.** The container looks at the folders under
   Media and writes `/mnt/user/appdata/media-audit/config.yml`. Folder names containing
   "movie"/"film" become movie libraries, and "tv"/"show"/"series"/"anime" become TV
   libraries. Anything else (books, music, photos...) is listed but commented out.
   Nothing is scanned this time. Open the container log to see what it found, and edit
   `config.yml` if a type is wrong.
4. **Each further start runs one scan and stops.** Press **Start** to scan, and follow
   progress with the container's log icon. The first scan reads every file, so it takes a
   while (about an hour for ~19,000 files; see [Scan times](#scan-times)). Later scans
   only read new or changed files, so they finish in seconds to minutes. Stopping a scan
   midway is safe: the next start continues where it left off.
5. **See the results** from the Unraid terminal:
   ```sh
   docker run --rm -e PUID=99 -e PGID=100 -v /mnt/user/appdata/media-audit:/config \
     ghcr.io/ethan-matlack/media-audit stats
   ```
   (A web UI is planned; see Phase 3 in [HANDOFF.md](HANDOFF.md).)

**Scheduled scans:** with the User Scripts plugin, add a script containing
`docker start media-audit` and schedule it (e.g. daily). The container stays "stopped"
in the Docker tab between scans; that's expected.

**Updates:** a new `:latest` image shows as "update ready" on the Docker tab like any
other app.

## Quick start (Docker Compose)

```sh
./scripts/make_demo_library.sh               # tiny fake library in docker/media (needs ffmpeg)
docker compose build
docker compose run --rm media-audit check-config
docker compose run --rm media-audit scan
docker compose run --rm media-audit stats
docker compose --profile explore up datasette    # browse the DB: http://localhost:8001
docker compose --profile test run --rm test      # lint + mypy + tests with real MediaInfo
```

For a real library, set `MEDIA_ROOT` in `.env` to the folder holding the libraries
listed in [docker/config/config.yml](docker/config/config.yml). That file is set up for
the author's layout (`movies/`, `movies-anime/`, `tv/`, `anime/`); edit it, or delete
it and let the first start generate one. To use the published image instead of building,
replace `build:` with `image: ghcr.io/ethan-matlack/media-audit:latest`.

### How the container behaves

- **Mounts:** `/config` (config file and SQLite DB; keep it on local disk, not on the
  network share) and `/media` (your libraries, mounted read-only).
- **User:** started as root (Unraid's default), it makes `/config` writable for
  `PUID:PGID` (default 1000:1000), then drops to that user. Started with `--user` or
  compose `user:`, it simply runs as that user.
- **First start:** with no `/config/config.yml`, it writes one from the folders under
  `/media` and exits without scanning. `media-audit init-config` does the same thing
  outside Docker.
- **Default command** is `scan`. Pass another command to run something else, e.g.
  `docker run ... ghcr.io/ethan-matlack/media-audit stats`.
- **Image tags:** `latest` follows `main`, `X.Y.Z` and `X.Y` come from release tags, and
  `sha-<commit>` marks a specific build.

## Quick start (local Python)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). libmediainfo comes bundled
with the `pymediainfo` wheel on Linux x86_64/aarch64.

```sh
uv sync
uv run media-audit init-config --media-root /path/to/media   # or copy config.example.yml
uv run media-audit check-config
uv run media-audit scan
uv run media-audit stats
```

## Commands

| command | does |
|---|---|
| `scan [-l LIB] [--no-analyze] [--retry-errors] [-w N]` | walk libraries, analyze new/changed files, rebuild titles |
| `rederive` | re-apply all rules to stored MediaInfo data; reads no media files |
| `stats [-l LIB] [--json]` | library breakdowns |
| `check-config` | validate config, library paths, overlaps, libmediainfo |
| `init-config [--media-root DIR] [--force]` | write a starter config from the folders in DIR |

### Rescans are cheap

- Files are compared by `(size, mtime)`. Unchanged files are never reopened.
- A file that disappears and reappears elsewhere with the same `(inode, size, mtime)`
  counts as a **move** (e.g. a Radarr rename) and keeps its analysis.
- If a library root looks empty but the DB has files for it (an unmounted share), the
  scan refuses to mark everything deleted unless you pass `--allow-empty`.
- Raw MediaInfo JSON is stored per file, so improving a rule only needs `rederive`.
  Bumping `RULES_VERSION` makes the next scan re-derive automatically.

### Scan times

For reference, a first full scan of a mixed movie and TV library of **19,244 files**
took **55 minutes** in the Docker container on an Unraid server, with the default
settings (2 workers). That works out to about
350 files a minute, or roughly 0.17 seconds per file.

- **What it depends on:** analysis only reads file headers (`parse_speed: 0`), so time
  depends mostly on how many files you have and how fast your disks respond, not on how
  big the files are. Files with incomplete TrueHD headers get a deeper re-read, capped
  at `retry_timeout` (60 s) each, so those cost more.
- **Workers:** `scan.workers` (default 2) sets how many files are analyzed at once. More
  workers can help on SSDs or several array disks, but can slow down a single spinning
  disk.
- **Rescans:** unchanged files aren't reopened, so a rescan costs one directory walk
  plus the new or changed files.

## Configuration

See [config.example.yml](config.example.yml). Any key can be overridden with
`MEDIA_AUDIT_<KEY>` env vars, using `__` for nesting (`MEDIA_AUDIT_SCAN__WORKERS=4`).
`MEDIA_AUDIT_CONFIG` selects the config file. `MEDIA_AUDIT_AUTO_INIT` and
`MEDIA_AUDIT_MEDIA_ROOT` control the Docker image's first-start config and are not
config keys.

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
  init_config.py       starter config from a media folder (first start / init-config)
scripts/               fixture capture/generation, demo library
docker/                entrypoint (PUID/PGID), compose config
unraid/                Unraid container template
.github/workflows/     build, test and publish the image
tests/                 table-driven rules/parser tests, pipeline tests, MediaInfo integration
```

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

### CI and releases

[.github/workflows/docker.yml](.github/workflows/docker.yml) builds the Docker test
image and runs lint, mypy and the full test suite (with real MediaInfo and ffmpeg) on
every pull request and push. On pushes to `main` and `v*` tags it then publishes the
runtime image to GHCR using the built-in `GITHUB_TOKEN`.

- **First publish:** the package starts out private. Make it public (GitHub →
  Packages → media-audit → Package settings → Change visibility) so Unraid can pull it
  without a login.
- **Release:** `git tag v0.1.0 && git push origin v0.1.0` publishes `:0.1.0` and
  `:0.1`. Bump `version` in `pyproject.toml` and `src/media_audit/__init__.py` first.

Rules are tested against 17 MediaInfo captures from a real library (DV, HDR10+,
Atmos, DTS:X, remuxes, AVI, MP4, anime). At the default `parse_speed: 0`, files whose MKV
headers don't describe a TrueHD stream are re-parsed deeper with a time cap, so Atmos
isn't missed. See
[tests/fixtures/mediainfo/README.md](tests/fixtures/mediainfo/README.md).

## License

MIT. Runtime dependencies: pymediainfo (MIT) with bundled libmediainfo (BSD-2-Clause),
PyYAML (MIT), pydantic (MIT).
