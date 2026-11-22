# media-audit

> Status: In-development. Working, but not yet fully-featured or guaranteed stable.

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

The config is found in this order: `-c path/to/config.yml`, then the
`MEDIA_AUDIT_CONFIG` variable, then `config.yml` in the current folder, then
`/config/config.yml` (the Docker image sets `MEDIA_AUDIT_CONFIG=/config/config.yml`).
Relative paths in the config, such as `database: media-audit.db`, are relative to the
config file. The database, its backups and saved cleanup scripts all end up next to it.

## Commands

| command | does |
|---|---|
| `serve [--host H] [--port P]` | web UI plus scheduled scans (the Docker default) |
| `scan [-l LIB] [--no-analyze] [--retry-errors] [-w N]` | walk libraries, analyze new/changed files, rebuild titles |
| `rederive` | re-apply all rules to stored MediaInfo data; reads no media files |
| `stats [-l LIB] [--json]` | library breakdowns |
| `remap-library LIB --path DIR [--name NEW] [--dry-run]` | point a library at its renamed/moved folder, keeping its history |
| `library list\|add\|set\|remove …` | change libraries in config.yml (see below) |
| `path-mapping list\|add\|remove …` | container-to-server folder mappings for cleanup scripts |
| `check-config` | validate config, library paths, overlaps, libmediainfo |
| `init-config [--media-root DIR] [--force]` | write a starter config from the folders in DIR |

### Several media folders

Libraries don't have to share one folder. In Docker, mount each media folder **under**
`/media` (two host folders can't share the same container path; Docker refuses):

```
/mnt/user/media/content1  ->  /media/content1
/mnt/user/media/content2  ->  /media/content2   (on Unraid: Add another Path)
```

Libraries are then e.g. `/media/content1/movies` and `/media/content2/movies` (folder
names can repeat; library *names* must be unique). For cleanup scripts, tell media-audit
where each mount is on the server:

```yaml
path_mappings:
  - container: /media/content1
    host: /mnt/user/media/content1
  - container: /media/content2
    host: /mnt/user/media/content2
```

or `media-audit path-mapping add /media/content2 --host /mnt/user/media/content2`. The
longest matching folder wins. `media_folders` (default `[/media]`) lists where
libraries live, for finding moved libraries; add another entry if you mount media
somewhere other than under `/media`.

Everything media-audit stores about a library's files is relative to the library's
folder, so a library can be moved, renamed or remapped without anything being read
again, and libraries can be on different disks or mounts.

### Changing the config from the command line

`media-audit library list|add|set|remove` and `media-audit path-mapping list|add|remove`
change config.yml for you: comments and layout are kept, the result is checked before
saving, the old file is kept as `config.yml.bak-<time>` (the newest 5 are kept), and `--dry-run` shows the change
without saving. Renaming a library (`library set tv --rename shows`) keeps its history.
Settings set by `MEDIA_AUDIT_*` environment variables can't be changed this way (the
command says which variable to change instead). The web UI's settings will work the same
way.

### Renamed or moved library folders

Rename `movies` to `movies2` on disk and media-audit would otherwise see a missing
library and a new one, and read every file again. Instead:

* **Scans page:** when a library's folder is gone and another folder holds the same files
  (title folder names, then the sizes of a sample of files), a banner suggests the
  remap. **Review remap…** shows how much matches, what changes in config.yml (a diff;
  comments and layout are kept) and then **Remap**: the library points at the new folder
  (and name, if you change it), its stored paths move with it, history, analysis and
  ignored issues are kept, and a quick scan confirms it. The database and config.yml are
  backed up first (`*.bak-…` next to them; the newest 2 database backups of each kind and 5
  config backups are kept). Any library can be remapped by hand from
  the **Libraries** list on the same page; **It's a different library** hides a wrong
  suggestion.
* **Same name, new path:** change `path:` in the config and the next scan follows the
  library to its new folder without re-reading anything.
* If config.yml can't be edited safely (e.g. libraries come from `MEDIA_AUDIT_LIBRARIES`),
  nothing is changed and the page shows the lines to change by hand.

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

Everything can be set up from the web UI's **Settings** page, without touching the
config file:

* **Libraries:** add (with a folder browser, or one click from the folders found in your
  media folders, with the type guessed from the name), rename (the library keeps its
  history), move, change type, switch off, or remove. Each library can also switch off
  individual Issues checks.
* **Media folders and server paths** (path mappings for cleanup scripts); mounts that
  have no server path yet are suggested.
* **Scanning** (schedule with presets and the next run shown, files read at once, parse
  speeds, ignore patterns), **linking**, **preferred languages**, **Issues** (each check
  on or off, thresholds, expected file types) and the **web UI** port.

Changes are written to config.yml, keeping its comments and layout, and checked before
they're saved. The previous version is kept as `config.yml.bak-<time>`; only the newest 5
are kept, so they don't pile up. They apply
straight away (issues are recalculated and the schedule refreshed). Only the web UI's
port and address need a restart. Settings set by `MEDIA_AUDIT_*` environment variables
(e.g. in the Unraid template) are shown but locked, with the variable named.

The file is still the source of truth, so hand edits work too. See
[config.example.yml](config.example.yml). Any key can be overridden with
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
| `split_season` | warning | one season's episodes in two folders, e.g. `Season 1` and `Season 01`, or `Specials` and `Season 00`; an empty or video-less leftover is reported as an empty folder / folder with no videos instead |
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
| Empty folders, including a leftover `Season 1` next to `Season 01` | removed if still empty |

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

The script runs on the server, which sees different paths than the container, so it
needs a **path mapping** for each mounted folder (see [Several media folders](#several-media-folders)).
On Unraid the **Host media path** variable does this for `/media`: set it to the same
folder as **Media**. Until a mapping exists, the page won't create a script. It needs
bash and GNU coreutils/findutils (Unraid and any Linux server have them).

## Exploring the data

The **Files** page is for drilling down. Every filter (library, resolution, dynamic
range, codec, audio, audio and subtitle language, container, source, release group)
takes several values and an **Is / Is not** switch, and lists each value with how many
files have it given the *other* filters. For example, *anime files with no English
audio track*: Library = anime, Audio language → Has none of → en.

* "Is not" includes files where the value is unknown (e.g. unreadable files for
  "not 1080p"); pick "(unknown)" to filter on those directly.
* Audio and subtitle filters look at every track: "Has none of en" keeps a
  Japanese-only file and one with no audio, but not a dual-audio one. An untagged track
  is "(unknown)", never assumed to be English.
* **Group by Titles or Seasons** collapses the list into one row per show/movie or per
  season (movies stay one row each), with the files that match out of all the files in
  that group: "22 of 22" means the whole season pack, "1 of 14" one odd episode. Each
  row shows the group's resolutions, codecs, audio languages and release groups, and
  **Files →** lists just that group's matching files. Export CSV exports one row per
  group when grouped.
* Path can be "contains" or "doesn't contain". The page shows the total size of the
  matching files, active filters as removable chips, and the URL is shareable; Export
  CSV uses the same filters.

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
  remap.py             renamed/moved library folders: detection and remapping
  settings.py          every config change (CLI, remap, web UI): ruamel.yaml, checked, backed up
  web/settings_*.py    the Settings page: field definitions and handlers
  mounts.py            the container's mount table (which media folders are mounted)
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

**Running the web UI locally:** keep everything it writes in `.dev/` (git-ignored), the
same way the container keeps it in `/config`:

```sh
mkdir -p .dev
cat > .dev/config.yml <<'YAML'
database: media-audit.db   # .dev/media-audit.db
libraries: []              # or real libraries; set them up in Settings
scan:
  schedule: off
YAML
export MEDIA_AUDIT_CONFIG=.dev/config.yml
uv run media-audit serve   # http://localhost:8484
```

To work on real data, copy a database from the server to `.dev/media-audit.db`. Pages
that read the media folders (scans, the folder browser) need the media too;
`scripts/make_demo_library.sh` builds a small fake library for that. Templates, CSS
and JS changes show on a browser refresh; Python changes need a restart.

### CI and releases

[.github/workflows/docker.yml](.github/workflows/docker.yml) builds the Docker test
image and runs lint, mypy and the full test suite (with real MediaInfo and ffmpeg) on
every pull request and push. Pull requests only run the tests. Pushes then publish the
runtime image to GHCR (using the built-in `GITHUB_TOKEN`):

| You push | Image tags | Built from | Shows as (footer, banner, `/healthz`) |
|---|---|---|---|
| a commit to `dev` | `:dev`, `:sha-<commit>` | the newest commit on `dev` | `0.8.0-dev (3e63404)` |
| a merge into `main` | `:latest`, `:sha-<commit>` | the newest commit on `main` (the merge commit) | `0.8.0`, build `main · e6cfb0f` |
| a tag `v0.8.0` | `:0.8.0`, `:0.8` | the tagged commit | `0.8.0`, build `release · 3e63404` |

Each push is its own build, and each build is its own image, even from the same code:
the build type (`dev`, `main`, `release`) and commit are stamped into the image, so the
images differ.

#### Day to day and releasing

1. **Work on `dev`.** Every push updates `:dev`. Leave the version alone between
   releases.
2. **When it's ready, bump the version on `dev`** in
   [src/media_audit/__init__.py](src/media_audit/__init__.py), the one place it lives
   (`pyproject.toml` reads it from there). Versions are plain `X.Y.Z`: `X.Y.Z+1` for
   fixes, `X.Y+1.0` for features. Commit and push.
3. **Open a pull request `dev` → `main`**, wait for the green check, and merge it with
   *Create a merge commit*. That publishes `:latest`.
4. **Tag `main`:**
   ```sh
   git checkout main && git pull
   git tag v0.8.0 && git push origin v0.8.0
   ```
   CI refuses a tag that doesn't match `__version__`, so a forgotten bump can't be
   published under the wrong number.
5. **Bring `dev` level with `main`** (it's one merge commit behind after step 3):
   ```sh
   git checkout dev && git pull && git merge --ff-only main && git push
   ```

Tags only ever point at one commit, so make them on `main` after merging, never on
`dev` before. A merge adds a new commit, so a tag made on `dev` points at a different
commit than `:latest`. Squash or rebase merges create new commits too, and a tag on
`dev` would then point at a commit `main` doesn't contain. Never move a tag once it's
been pushed: bump to the next version instead.

#### Reading the GHCR package page

The page lists **images**, newest first, with their tags. What you'll see after a
release, and why it's expected:

- **`:latest` is a separate entry from `:0.8.0`,** with a different commit: `:latest` is
  built from the merge commit on `main`, the tag from the commit you tagged. The code
  is the same (a merge from `dev` changes no files when `main` has nothing of its own);
  the images differ only in their build stamp. Tagging `main` after the merge (step 4)
  makes them share a commit.
- **`:dev` can sit just below `:0.8.0` and look older,** when the `dev` push and the tag
  push build the same commit at the same time and the dev build finishes first. It's
  the same code; check the commit in the footer or `/healthz`.
- **Only one entry shows `sha-<commit>`** when two builds come from the same commit: the
  tag goes to whichever image was pushed last.
- **Older dev builds** keep their `sha-` tag after `:dev` moves on. *View all versions*
  shows them. A quick push after another can cancel the first build (only the newest
  commit on a branch is built), so not every commit gets an image.

#### One-time setup

- **First publish:** the package starts out private. Make it public (GitHub →
  Packages → media-audit → Package settings → Change visibility) so Unraid can pull it
  without a login.
- **Protect `main`** (GitHub → Settings → Branches → add a rule for `main`: require a
  pull request and the `test` status check), so a failing build can't reach `:latest`
  and nothing lands on `main` except through a PR from `dev`.

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

Copyright (C) 2026 Ethan Matlack

media-audit is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed in
the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE)
for the full text.

Releases up to and including 0.4.0 were published under the MIT license.

Runtime dependencies, all GPL-compatible: pymediainfo (MIT) with bundled libmediainfo
(BSD-2-Clause), PyYAML (MIT), pydantic (MIT), Starlette (BSD-3-Clause), uvicorn
(BSD-3-Clause), Jinja2 (BSD-3-Clause), ruamel.yaml (MIT), and their dependencies (anyio, h11, click, idna,
MarkupSafe, typing-extensions: MIT, BSD or PSF licensed).
