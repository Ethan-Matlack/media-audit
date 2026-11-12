# media-audit

> Working name. Status: **Phases 0–2 + web UI** (scanner, analyzer, rules, titles,
> issues, web UI with scheduled scans).

A self-hosted tool that scans a media library on disk and reports what's actually in
it: storage per movie/show/season and technical details for every file (container,
codecs, resolution, HDR/Dolby Vision, audio formats and languages, subtitles, release
group...). It is read-only: it never modifies, moves, or renames media.

The Docker container stays running. It serves a web UI and rescans on a schedule:

- **Overview:** storage per library and library growth over time, plus charts of
  resolution, HDR/Dolby Vision, codecs, audio formats, languages, containers, sources
  and release groups. Every bar links to the matching files.
- **Titles:** a sortable, filterable list with 4K/HDR/DV/Atmos badges. Each title has
  a page with its seasons and every file's video, audio and subtitle tracks.
- **Files:** filter by anything (resolution, HDR, codec, audio format, language, group,
  source, size, unreadable files...), sort by size or GB per hour, and export to CSV.
- **Issues:** unreadable files, duplicates, truncated episodes, mislabelled
  resolutions, stray files, gaps in episode numbering and more, with the space you'd
  recover and an **Ignore** button (see [Issues](#issues)).
- **Scans:** history, live progress, **Scan now** and **Stop**.

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
   - **Web UI port**: `8484`, unless something else uses it.
   - **Scan schedule**: leave empty for 03:00 daily, or set a cron expression (see
     [Scheduled scans](#scheduled-scans)).
   - **PUID / PGID** (under advanced): leave them at `99` / `100` (nobody:users).

   Click Apply.
3. **Open the web UI** from the container's icon → **WebUI**, or at
   `http://<server>:8484`. On first start the container writes
   `/mnt/user/appdata/media-audit/config.yml` from the folders under Media:
   - Folder names containing "movie"/"film" become movie libraries.
   - Names containing "tv"/"show"/"series"/"anime" become TV libraries.
   - Anything else (books, music, photos...) is listed but commented out.

   The UI shows a banner while nothing has been scanned. Check the library list (the
   container log shows it too), edit `config.yml` if a type is wrong (no restart
   needed), then press **Scan now**.
4. **Wait for the first scan.** It reads every file once: about an hour for ~19,000
   files (see [Scan times](#scan-times)). The **Scans** page shows live progress, and
   pages fill in when it finishes. Stopping the container mid-scan is safe: the next
   scan continues where it left off.
5. **After that, it keeps itself up to date.** A scan runs at 03:00 daily by default,
   and each one only reads new or changed files (about a minute for ~21,700 files).

**Command line:** the CLI still works inside the running container. Use `-u 99:100` so
any files it writes belong to the same user as the service:
```sh
docker exec -u 99:100 media-audit media-audit stats
docker exec -u 99:100 media-audit media-audit scan --retry-errors
```

**Updates:** a new `:latest` image shows as "update ready" on the Docker tab like any
other app.

**Access:** there's no login. Keep the port on your LAN, or put it behind your reverse
proxy's authentication if you expose it. The UI can only read the database and start
or stop scans.

## Quick start (Docker Compose)

```sh
./scripts/make_demo_library.sh               # tiny fake library in docker/media (needs ffmpeg)
docker compose up -d --build                 # web UI: http://localhost:8484
docker compose exec media-audit media-audit stats
docker compose --profile explore up datasette    # raw SQL browser: http://localhost:8001
docker compose --profile test run --rm test      # lint + mypy + tests with real MediaInfo
```

For a real library, set `MEDIA_ROOT` in `.env` to the folder holding the libraries
listed in [docker/config/config.yml](docker/config/config.yml). That file is set up for
the author's layout (`movies/`, `movies-anime/`, `tv/`, `anime/`); edit it, or delete
it and let the first start generate one. Set `WEB_PORT` to publish the UI on another
port. To use the published image instead of building, replace `build:` with
`image: ghcr.io/ethan-matlack/media-audit:latest`.

### How the container behaves

- **Default command** is `serve`: the web UI on port 8484 plus the scan scheduler, in
  one long-running process. Pass another command to run it once instead, e.g.
  `docker run --rm ... ghcr.io/ethan-matlack/media-audit stats`.
- **Mounts:** `/config` (config file and SQLite DB; keep it on local disk, not on the
  network share) and `/media` (your libraries, mounted read-only).
- **User:** started as root (Unraid's default), it makes `/config` writable for
  `PUID:PGID` (default 1000:1000), then drops to that user. Started with `--user` or
  compose `user:`, it simply runs as that user.
- **First start:** with no `/config/config.yml`, it writes one from the folders under
  `/media` and waits for **Scan now** (or the next scheduled scan) rather than scanning
  straight away. `media-audit init-config` does the same thing outside Docker.
- **Config changes** are picked up without a restart. `config.yml` is re-read before
  every scan, and the schedule is re-checked every 20 seconds. A broken config is shown
  in the UI, and the last valid one stays in use.
- **One scan at a time:** a scheduled scan, **Scan now** and a `docker exec ... scan`
  share a lock. If one is already running, the others are skipped, not queued.
- **Stopping:** `docker stop` (or **Stop** on the Scans page) finishes the files being
  read and stops. The next scan resumes from there. A scan killed outright (power loss)
  is marked `interrupted` next time.
- **Start-up banner:** each start prints a banner to the log with the version, author,
  container name and the user it runs as, so restarts are easy to spot.
- **Health:** the image has a healthcheck on `/healthz`.
- **Image tags:** `latest` follows `main`, `dev` follows the `dev` branch, `X.Y.Z` and
  `X.Y` come from release tags, and `sha-<commit>` marks a specific build (see
  [CI and releases](#ci-and-releases)).

## Quick start (local Python)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). libmediainfo comes bundled
with the `pymediainfo` wheel on Linux x86_64/aarch64.

```sh
uv sync
uv run media-audit init-config --media-root /path/to/media   # or copy config.example.yml
uv run media-audit check-config
uv run media-audit scan
uv run media-audit stats
uv run media-audit serve          # web UI on http://localhost:8484 + scheduled scans
```

## Commands

| command | does |
|---|---|
| `serve [--host H] [--port P]` | web UI plus scheduled scans (the Docker default) |
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

For reference, a first full scan of a mixed movie and TV library of **19,244 video
files** (21,698 files in total, including subtitles and artwork) took **55 minutes** in the Docker container on an Unraid server, with the default
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
  plus the new or changed files. On the same library, a rescan with a few renames and
  deletions took **61 seconds** for 21,691 files.

## Configuration

See [config.example.yml](config.example.yml). Any key can be overridden with
`MEDIA_AUDIT_<KEY>` env vars, using `__` for nesting (`MEDIA_AUDIT_SCAN__WORKERS=4`).
An empty variable counts as unset, so the config file decides. To turn something off,
use a value like `off` (e.g. `MEDIA_AUDIT_SCAN__SCHEDULE=off`).
`MEDIA_AUDIT_CONFIG` selects the config file. `MEDIA_AUDIT_AUTO_INIT` and
`MEDIA_AUDIT_MEDIA_ROOT` control the Docker image's first-start config and are not
config keys.

### Scheduled scans

`scan.schedule` is a standard 5-field cron expression (minute, hour, day of month,
month, weekday), in the container's time zone (`TZ`; Unraid sets it from the server's
own setting):

| schedule | runs |
|---|---|
| `0 3 * * *` (default) | 03:00 every day |
| `0 */6 * * *` | every 6 hours |
| `30 2 * * sun` | Sundays at 02:30 |
| `@hourly`, `@daily`, `@weekly` | shortcuts |
| `off` | never (use **Scan now**) |

Ranges (`mon-fri`), lists (`1,15`) and steps (`*/15`) work as usual. Rescans are cheap,
so even hourly is fine. A scheduled scan that comes due while another is running is
skipped.

## Issues

After every scan (and `rederive`), media-audit checks the library for problems using
the data it already has; no media files are re-read. The **Issues** page groups them by
check, errors first; the nav shows how many errors and warnings are open.

| Code | Severity | What it means |
|---|---|---|
| `unreadable` | error | MediaInfo couldn't read a video stream: damaged, incomplete, or not video |
| `disc_image` | error | `.iso`/`.img` instead of a video file |
| `no_audio` | warning | video with no audio track |
| `stray_file` | warning | not video, subtitles, artwork or metadata (partial downloads, `.txt`, `.dat`...), or an `.nfo` media servers don't read (scene release info, or left over from a moved video); extras folders are exempt |
| `no_video_folder` | warning | movie, show or season folder with only artwork, metadata or other leftovers |
| `empty_folder` | warning | folder with nothing in it (files matched by `scan.ignore` don't count) |
| `split_season` | warning | one season in two folders, e.g. `Season 1` and `Season 01`, or `Specials` and `Season 00` |
| `duplicate_episode` | warning | one episode stored as several files; shows the space freed by keeping the largest |
| `orphan_subtitle` | warning | subtitle file with no video of the same name |
| `multi_version_movie` | info | several versions of one movie (often deliberate) |
| `cross_library` | info | the same title in two libraries |
| `short_runtime` | warning | episode much shorter than its season's median: often truncated or the wrong file |
| `resolution_mismatch` | warning | filename says e.g. 1080p but the video isn't (upscales, mislabels) |
| `bitrate_outlier` | info | GB/hour far from the rest of the season, or from movies at the same resolution |
| `unnumbered_episode` | warning | no episode number in the filename |
| `missing_episodes` | info | gaps in episode numbering within a season |
| `episode_outside_season` | info | episode directly in the show folder while the rest are in season folders (flat layouts aren't flagged) |
| `no_ids` | info | no `{tmdb-…}`/`{tvdb-…}`/`{imdb-…}` in folder or file names |
| `mixed_resolution_season` | info | one season at several resolutions |
| `legacy_codec` | info | Xvid/DivX, MPEG-2, MPEG-1 or VC-1: upgrade candidates |
| `no_preferred_audio` | info | no audio in `languages.preferred_audio` |

Peer comparisons skip specials and seasons with fewer than `flags.min_peers` episodes.
Gaps in episode numbers are counted from E01, or from the first episode when a season
starts past E03 (absolute anime numbering); missing episodes after the last one present
can't be detected without an online database. Empty folders are recorded during the scan,
so they show up after the first scan with this version.

**Ignore** hides an issue you've accepted (a deliberate second version, a known short
episode). It lasts until the files involved change, then the issue comes back. Ignores
are kept in the database and survive rescans and `rederive`. **Show ignored** lists them.

Thresholds, allowed file types and which checks run (globally or per library) are set
under `flags:` in [config.example.yml](config.example.yml).

### Cleanup scripts

media-audit never changes your media; in Docker it can't, because `/media` is mounted
read-only. For the leftovers that need no judgement, it can instead **write a script
that you review and run yourself** on the server: **Issues → Cleanup script…**.

The script is built from the issues currently shown (the Issues page filters apply;
ignored issues are always left out):

| Issue | With `--apply` |
|---|---|
| Folders with no videos left | files move to quarantine, then the empty folder is removed |
| Stray files, subtitles with no matching video | move to quarantine |
| Empty folders, including the empty half of a split season (`Season 1` next to `Season 01`) | removed if still empty |

Duplicates, unreadable files and the other checks need a decision and aren't scripted.

* **Nothing is deleted.** Files move to `.media-audit-quarantine/<date-time>/` inside
  their library folder, keeping their folder structure (same filesystem, so it's
  instant). Scans skip that folder. A `*-restore.sh` script is written as files move.
  Delete the quarantine yourself once you've checked your media server.
* **Dry run by default.** `bash cleanup-….sh` only lists what it would do; `--apply`
  does it, after showing the warning again and asking you to type `apply`.
* **Checked again at run time.** Each file must still have the size and modification
  time it had at the scan; folders must have no videos in them and be truly empty
  before they're removed; paths must resolve inside their library folder (symlinks
  aren't followed). Anything else is skipped and listed.
* **Save to appdata** (the default) writes the script to `/config/cleanup/`, i.e.
  `/mnt/user/appdata/media-audit/cleanup/` on Unraid, and shows the commands to run.
  **Download** gives you the same file. A log is written next to the script.

The script runs on the host, which sees different paths than the container, so set
`cleanup.host_media_path` to the host folder mounted at `/media`. On Unraid that's the
**Host media path** variable: the same folder as **Media**. Until it's set, the page
won't create a script. It needs bash and GNU coreutils/findutils (Unraid and any
Linux server have them).

## Exploring the data

The web UI covers the common questions. For anything else, the SQLite database is
plain SQL: open it with `sqlite3`, or run the Datasette profile in compose. The main
views are `v_media` (one row per live video file with derived fields) and
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
  cron.py              cron expressions for scheduled scans
  flags.py             runs the Issues checks after each scan (rules/flags.py: the checks)
  cleanup.py           cleanup scripts for Issues (generated text; never run by media-audit)
  web/                 `serve`: scan service + scheduler, Starlette app, Jinja templates,
                       static CSS/JS (no build step, no CDN)
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
every pull request and push, then publishes the runtime image to GHCR using the
built-in `GITHUB_TOKEN`:

| Push to | Publishes | Shows as |
|---|---|---|
| `dev` branch | `:dev` and `:sha-<commit>` | `0.4.0-dev (abc1234)` |
| `main` | `:latest` and `:sha-<commit>` | `0.4.0` |
| tag `v0.4.0` | `:0.4.0` and `:0.4` | `0.4.0` |

Pull requests run the tests but never publish. The version and build (channel and
commit) appear in the start-up banner, the web UI footer and `/healthz`.

**Workflow:** commit to `dev` day to day (every push updates `:dev`). Merge `dev` into
`main` when it's ready, which updates `:latest`. Then release:

1. Bump `__version__` in [src/media_audit/__init__.py](src/media_audit/__init__.py), the
   one place the version lives (`pyproject.toml` reads it from there). Versions are
   plain `X.Y.Z`: `X.Y.Z+1` for fixes, `X.Y+1.0` for features.
2. Commit and push to `main`.
3. `git tag v0.4.0 && git push origin v0.4.0`. CI refuses a tag that doesn't match
   `__version__`.

- **First publish:** the package starts out private. Make it public (GitHub →
  Packages → media-audit → Package settings → Change visibility) so Unraid can pull it
  without a login.
- **Protect `main`** (GitHub → Settings → Branches → add a rule for `main`, require the
  `test` status check) so a failing build can't reach `:latest`.

#### Trying dev builds on Unraid

Add a second container from the same template (Docker → Add Container → media-audit):

- **Name** `media-audit-dev`, **Repository** `ghcr.io/ethan-matlack/media-audit:dev`.
- **Web UI port:** another port, e.g. `8485`.
- **Config:** its **own** folder, e.g. `/mnt/user/appdata/media-audit-dev`. Dev builds
  may upgrade the database schema, and the `:latest` container then refuses to open
  it. Copy `media-audit.db` from the main appdata folder to start with real data, or
  let it do its own first scan.
- **Media:** the same path as the main container (it's read-only).

Rules are tested against 17 MediaInfo captures from a real library (DV, HDR10+,
Atmos, DTS:X, remuxes, AVI, MP4, anime). At the default `parse_speed: 0`, files whose MKV
headers don't describe a TrueHD stream are re-parsed deeper with a time cap, so Atmos
isn't missed. See
[tests/fixtures/mediainfo/README.md](tests/fixtures/mediainfo/README.md).

## License

MIT. Runtime dependencies: pymediainfo (MIT) with bundled libmediainfo (BSD-2-Clause),
PyYAML (MIT), pydantic (MIT), Starlette (BSD-3-Clause), uvicorn (BSD-3-Clause), Jinja2
(BSD-3-Clause), and their dependencies (anyio, h11, click, idna, MarkupSafe,
typing-extensions: MIT, BSD or PSF licensed).
