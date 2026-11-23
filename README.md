# media-audit

> Status: In-development. Working, but not yet fully-featured or guaranteed stable.

A self-hosted tool that scans your media library and reports what's actually in it:
how much space every movie, show and season takes, and the technical details of every
file. It's read-only: it never modifies, moves or renames your media.

## What it does

A web UI that keeps itself up to date (it rescans on a schedule, and a rescan only reads
new or changed files):

- **Overview:** storage per library and its growth over time, plus breakdowns by
  resolution, HDR/Dolby Vision, codec, audio format, language, container, source and
  release group. Every bar links to the matching files.
- **Titles:** every movie and show with 4K/HDR/DV/Atmos badges. A title's page shows its
  seasons, average episode size, and every file's video, audio and subtitle tracks.
- **Files:** filter by anything (each filter takes several values and *is* / *is not*),
  group the results by title or season, sort by size or GB per hour, export to CSV.
- **Issues:** unreadable files, duplicates, truncated episodes, mislabelled
  resolutions, stray and leftover files, empty folders, gaps in episode numbering and
  more, with the space you'd get back. Leftovers can be cleared with a **cleanup script**
  that you review and run yourself.
- **Scans:** live progress, history, **Scan now**, and help when a library's folder has
  been renamed or moved.
- **Settings:** everything, from libraries to scan schedule to which checks run, without
  editing a config file.

It runs three ways:

| | Best for |
|---|---|
| **Unraid** (template) | an Unraid server |
| **Docker** (any host) | other NAS and servers |
| **Python** (no container) | a desktop, or working on media-audit |

All three include the same **command line** (`media-audit scan`, `stats`, `library`…),
for scripts and quick answers without the web UI; see [Command line](#command-line).

## Installation

### Unraid

1. Add the template, in the Unraid terminal:
   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/my-media-audit.xml \
     https://raw.githubusercontent.com/Ethan-Matlack/media-audit/main/unraid/media-audit.xml
   ```
2. Docker → **Add Container** → Template: **media-audit**. Set **Media** to the folder
   that holds your libraries (e.g. `/mnt/user/media/content`), and **Host media path**
   to the same folder. Leave the rest. Click **Apply**.
3. Open the **WebUI** (container icon → WebUI, or `http://<server>:8484`), check the
   libraries it found under **Settings → Libraries**, and press **Scan now**.

Updates show as "update ready" on the Docker tab like any other container.

### Docker

```sh
docker run -d --name media-audit --restart unless-stopped \
  -p 8484:8484 \
  -e TZ=Europe/London -e PUID=1000 -e PGID=1000 \
  -v /path/to/appdata/media-audit:/config \
  -v /path/to/media:/media:ro \
  ghcr.io/ethan-matlack/media-audit:latest
```

Then open `http://<host>:8484`, check the libraries under **Settings → Libraries**, and
press **Scan now**. `/config` holds the config and database (keep it on local disk, not
a network share); `/media` is your libraries, read-only. The same with Compose:

```yaml
services:
  media-audit:
    image: ghcr.io/ethan-matlack/media-audit:latest
    restart: unless-stopped
    ports: ["8484:8484"]
    environment: { TZ: Europe/London, PUID: "1000", PGID: "1000" }
    volumes:
      - /path/to/appdata/media-audit:/config
      - /path/to/media:/media:ro
```

### Python

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). libmediainfo comes with
the `pymediainfo` wheel on Linux x86_64/aarch64.

```sh
git clone https://github.com/Ethan-Matlack/media-audit && cd media-audit
uv sync
uv run media-audit init-config --media-root /path/to/media
uv run media-audit serve          # http://localhost:8484
```

`init-config` writes `config.yml` in the current folder; the database is created next
to it.

## Using media-audit

### First start and scanning

On first start, media-audit looks at the folders in your media folder and sets them up
as libraries: names with "movie" or "film" become movie libraries, names with "tv",
"show", "series" or "anime" become TV libraries, and anything else is left out. It
doesn't scan until you've had a chance to check them (**Settings → Libraries**) and
press **Scan now**.

The first scan reads every file once: a mixed movie and TV library of **19,244 videos**
(21,698 files including subtitles and artwork) took **55 minutes** on an Unraid server
with the default settings, about 350 files a minute. Time depends on how many files you
have and how fast the disks respond, not on file size: only headers are read. The
**Scans** page shows progress. Stopping mid-scan is safe; the next scan picks up where
it left off.

After that, rescans are cheap: unchanged files (same size and modified time) are never
reopened, so a rescan is one walk of the folders plus whatever is new or changed. The
same library rescans in about a minute. A file that moved or was renamed (same disk,
inode, size and time) keeps its analysis. If a library suddenly looks empty (an
unmounted share), the scan refuses to mark everything deleted.

### Settings

**Settings** covers everything:

- **Libraries:** add (with a folder browser, or one click from folders found in your
  media folders), rename (history is kept), move, change type, switch off or remove.
  Individual Issues checks can be switched off per library.
- **Media folders:** where libraries live, and where each folder is on the server (for
  cleanup scripts; see [Libraries in several folders](#libraries-in-several-folders)).
- **Scanning:** the schedule (below), files read at once (`2`; raise it for SSDs or
  several array disks, keep it low for one spinning disk), parse speeds, ignore patterns.
- **Issues:** each check on or off, and their thresholds.
- **General:** linking titles across libraries, preferred languages, the web UI port.

Changes are saved to `config.yml` (comments and layout kept, the previous 5 versions
kept as `config.yml.bak-…`) and apply straight away; settings that need a restart say
so. The file is still the source of truth, so editing it by hand works too; see
[config.example.yml](config.example.yml).

**Scan schedule** is a 5-field cron expression (minute, hour, day of month, month,
weekday) in the container's time zone (`TZ`):

| schedule | runs |
|---|---|
| `0 3 * * *` (default) | 03:00 every day |
| `0 */6 * * *` | every 6 hours |
| `30 2 * * sun` | Sundays at 02:30 |
| `@hourly`, `@daily`, `@weekly` | shortcuts |
| `off` or empty | never (use **Scan now**) |

Only one scan runs at a time; one that comes due while another is running is skipped.

### Issues

After every scan, media-audit checks the library using what it already knows (no media
files are re-read). The **Issues** page groups the results by check, errors first.

| Code | Severity | What it means |
|---|---|---|
| `unreadable` | error | MediaInfo couldn't read a video stream: damaged, incomplete, or not video |
| `disc_image` | error | `.iso`/`.img` instead of a video file |
| `no_audio` | warning | video with no audio track |
| `stray_file` | warning | not video, subtitles, artwork or metadata (partial downloads, `.txt`…), or an `.nfo` media servers don't read; extras folders are exempt |
| `no_video_folder` | warning | movie, show or season folder with only artwork, metadata or other leftovers |
| `empty_folder` | warning | folder with nothing in it (ignored files don't count) |
| `split_season` | warning | one season's episodes in two folders, e.g. `Season 1` and `Season 01` |
| `duplicate_episode` | warning | one episode stored as several files; shows the space freed by keeping the largest |
| `orphan_subtitle` | warning | subtitle file with no video of the same name |
| `multi_version_movie` | info | several versions of one movie (often deliberate) |
| `cross_library` | info | the same title in two libraries (libraries marked as sharing titles, e.g. HD and 4K, aren't reported) |
| `short_runtime` | warning | episode much shorter than the rest of its season: often truncated or the wrong file |
| `resolution_mismatch` | warning | filename says e.g. 1080p but the video isn't (upscales, mislabels) |
| `bitrate_outlier` | info | GB/hour far from the rest of the season, or from movies at the same resolution |
| `unnumbered_episode` | warning | no episode number in the filename |
| `missing_episodes` | info | gaps in episode numbering within a season |
| `episode_outside_season` | info | episode in the show folder while the rest are in season folders |
| `no_ids` | info | no `{tmdb-…}`/`{tvdb-…}`/`{imdb-…}` in folder or file names |
| `mixed_resolution_season` | info | one season at several resolutions |
| `legacy_codec` | info | Xvid/DivX, MPEG-2, MPEG-1 or VC-1: upgrade candidates |
| `no_preferred_audio` | info | no audio track in your preferred languages |

Seasons are only compared with themselves (runtime, bitrate) when they have enough
episodes, and specials never are. Missing episodes are counted from E01 (or from the
first episode when a season starts past E03, as with absolute anime numbering); missing
episodes after the last one present can't be detected.

**Ignore** hides an issue you've accepted, until the files involved change.

**Cleanup scripts** (Issues → **Cleanup script…**) handle the leftovers that need no
judgement. media-audit can't change your media, so it writes a script that you review
and run on the server:

| Issue | With `--apply` |
|---|---|
| Folders with no videos left | files move to quarantine, then the folder is removed |
| Stray files, subtitles with no matching video | move to quarantine |
| Empty folders | removed, if still empty |

- **Nothing is deleted:** files move to `.media-audit-quarantine/<time>/` inside their
  library, and a restore script is written as they go. Delete the quarantine yourself
  once you've checked your media server.
- **Dry run by default:** `bash cleanup-….sh` only lists what it would do; `--apply`
  does it, after asking you to type `apply`.
- **Checked again when run:** a file that changed since the scan, a folder that gained a
  video, or a path outside its library is skipped and listed.
- Scripts are saved to `cleanup/` next to the config (`/config/cleanup/` in a container),
  or downloaded. They need bash and GNU coreutils/findutils, which any Linux server has.

### Exploring the data

The **Files** page is for drilling down. For example, *anime with no English audio*:
Library = anime, Audio language → *Has none of* → en. Each filter lists its values with
how many files have them given your other filters.

- *Is not* includes files where the value is unknown; pick "(unknown)" to filter on
  those directly.
- Audio and subtitle filters look at every track: *Has none of en* keeps a Japanese-only
  file and one with no audio, but not a dual-audio one.
- **Group by Titles or Seasons** shows one row per show or season, with how many of its
  files match: "22 of 22" is the whole season pack, "1 of 14" one odd episode.
- The URL can be shared or bookmarked, and **Export CSV** uses the same filters.
- **Group by → Chart** turns the filtered files into a bar chart: group them by a
  property (release group, resolution, dynamic range, DV profile, codec, audio or
  subtitle language, source, year…), pick a measure (files, size, share, median or
  average GB/h or bitrate, running time), and optionally **split by** a second property.
  *Which release groups have the highest bitrates?* Group by release group, measure
  median GB/h. *What share of my 4K is DV P7 vs P8?* Filter to 2160p, group by DV
  profile, measure share. Groups smaller than a minimum (5 files for medians and
  averages) are left out so one big file can't top a chart, each bar shows how many
  files it's based on, and every bar links to its files. The Overview's breakdowns
  each have a **Chart →** link.

For anything else, the database is plain SQLite: open it with `sqlite3` or
[Datasette](https://datasette.io). The `v_media` view has one row per video file, and
`v_titles` one per title; the same title in different libraries shares a `group_key`.

### Libraries in several folders

Libraries can live on different shares or disks. In a container, mount each one
**under** `/media` (two host folders can't share one container path):

```
/mnt/user/media/content1  →  /media/content1
/mnt/disks/archive        →  /media/archive
```

Cleanup scripts then need to know where each mount is on the server: add it under
**Settings → Media folders**. On Unraid, **Host media path** already covers `/media`;
only extra mounts need adding. The longest matching folder wins.

**Renamed or moved a library's folder?** The Scans page notices when a library's
folder is gone and another folder holds the same files, and offers to **remap** it: the
library (and its history, analysis and ignored issues) points at the new folder, and
nothing is read again. Any library can also be remapped from the list on the Scans
page, or moved in **Settings → Libraries**.

### Command line

Every install includes it. In a container, run it with `docker exec`, as the same user
as the service so any files it writes have the right owner:

```sh
docker exec -u 99:100 media-audit media-audit stats      # Unraid: 99:100
```

| Command | Does |
|---|---|
| `serve [--host H] [--port P]` | web UI and scheduled scans (the container default) |
| `scan [-l LIB] [--no-analyze] [--retry-errors] [-w N]` | scan now |
| `stats [-l LIB] [--json]` | library breakdowns |
| `rederive` | re-apply the rules to stored data without reading media |
| `library list\|add\|set\|remove` | manage libraries (renames keep history) |
| `remap-library LIB --path DIR [--name NEW]` | point a library at its renamed or moved folder |
| `path-mapping list\|add\|remove` | where container folders are on the server |
| `check-config` | check the config, library folders and libmediainfo |
| `init-config [--media-root DIR]` | write a starter config from the folders in DIR |

Commands that change the config take `--dry-run` and save exactly as the Settings page
does.

### Config file and environment variables

media-audit looks for its config in this order: `-c path/to/config.yml`, the
`MEDIA_AUDIT_CONFIG` variable, `config.yml` in the current folder, then
`/config/config.yml` (the container sets `MEDIA_AUDIT_CONFIG=/config/config.yml`).
Relative paths in it are relative to the config file.

Any setting can be forced with an environment variable, `MEDIA_AUDIT_<KEY>` with `__`
for nesting (e.g. `MEDIA_AUDIT_SCAN__WORKERS=4`). A setting forced this way is shown
locked in Settings, naming the variable; an empty variable counts as unset.

### The container

- **Default command** is `serve`: the web UI and the scheduler. Any other command runs
  once instead, e.g. `docker run --rm … ghcr.io/ethan-matlack/media-audit stats`.
- **User:** started as root, it makes `/config` writable for `PUID:PGID` (default
  1000:1000; Unraid's template uses 99:100) and drops to that user.
- **Logs:** each start prints a banner with the version and container name; progress of
  every scan is logged too. A healthcheck runs on `/healthz`.
- **Image tags:** `latest` (releases from `main`), `X.Y.Z` / `X.Y` (a specific release),
  `dev` (development builds; see [Development](#development)).

### Access

There's no login. Keep the port on your LAN, or put it behind your reverse proxy's
authentication if you expose it. Anyone who can open the web UI can change settings and
start scans; your media stays read-only regardless.

## Development

Contributions and forks are welcome. Everything you need to run, test and publish your
own build is below.

### Setup and running locally

```sh
git clone https://github.com/Ethan-Matlack/media-audit && cd media-audit
uv sync
```

Keep local run data in `.dev/` (git-ignored), the way the container keeps it in
`/config`:

```sh
mkdir -p .dev
printf 'database: media-audit.db\nlibraries: []\nscan:\n  schedule: off\n' > .dev/config.yml
export MEDIA_AUDIT_CONFIG=.dev/config.yml
uv run media-audit serve          # http://localhost:8484
```

To try it on real data, copy a database from a server to `.dev/media-audit.db`.
Scanning needs media: `scripts/make_demo_library.sh [dir]` builds a small fake library
(needs ffmpeg; `docker/media` by default, which the compose setup below uses) with the awkward cases: HDR and DV files, duplicates, extras, a corrupt
file, mislabelled resolutions, stray files. Template, CSS and JS changes show on a
browser refresh; Python changes need a restart.

[docker-compose.yml](docker-compose.yml) runs the same thing in a container built from
your checkout, against `docker/media` (or `MEDIA_ROOT`), with
[docker/config/config.yml](docker/config/config.yml) as its config:

```sh
docker compose up -d --build                       # http://localhost:8484
docker compose --profile explore up datasette      # SQL browser on :8001
```

### Tests and checks

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
docker compose --profile test run --rm test        # all of it, with real MediaInfo and ffmpeg
```

CI runs the last one on every push and pull request. Rules are tested against MediaInfo
captures from a real library (DV, HDR10+, Atmos, DTS:X, remuxes, anime); see
[tests/fixtures/mediainfo/README.md](tests/fixtures/mediainfo/README.md) for capturing
more.

### Project layout

```
src/media_audit/
  cli.py               command line; serve, scan, stats, library, …
  config.py            config model (pydantic), YAML + environment variables
  settings.py          every config change (Settings page, CLI): ruamel.yaml, checked, backed up
  db/                  connection and migrations (db/migrations/NNNN_*.sql or .py)
  scanner.py           folder walk, change/move/delete detection (stat only)
  analyzer/            MediaInfo backend behind an Analyzer protocol
  parsing/             filename parser (no guessit)
  rules/               pure derivations: video/HDR/DV, audio, subtitles, languages, Issues checks
  classify.py          file roles: main/extra/sample/subtitle/other
  titles.py            titles and seasons, sidecars, sizes, cross-library links
  pipeline.py          scan orchestration, worker pool, scan lock
  flags.py             runs the Issues checks after each scan
  cleanup.py           cleanup scripts (generated text; never run by media-audit)
  remap.py             renamed or moved library folders
  cron.py, mounts.py, backups.py, build.py, splash.py, stats.py, init_config.py
  web/                 Starlette app, Jinja templates, CSS/JS (no build step, no CDN)
scripts/               demo library, MediaInfo fixture capture
docker/                entrypoint (PUID/PGID) and the compose config
unraid/                Unraid template
tests/
```

### Making changes

- **Database:** never edit a migration that has shipped; add the next
  `db/migrations/NNNN_name.sql`. Use a `.py` migration (`migrate(conn, version)`) when
  SQL alone can't do it safely. Stored paths are relative to their library.
- **Rules:** derived fields come from stored MediaInfo, so bump `RULES_VERSION`
  ([rules/derive.py](src/media_audit/rules/derive.py)) and the next scan re-derives
  without reading media. Issues checks live in
  [rules/flags.py](src/media_audit/rules/flags.py) (bump `FLAGS_VERSION`).
- **Settings:** a new setting is a field in [config.py](src/media_audit/config.py) plus
  one `Field(...)` in [web/settings_form.py](src/media_audit/web/settings_form.py); the
  Settings page renders and saves it from there.
- **Dependencies:** media-audit is GPL-3.0-or-later, so runtime dependencies must be
  GPL-compatible (the current ones are listed in [pyproject.toml](pyproject.toml)).
  Avoid adding them where the standard library will do.

### CI and releases

[.github/workflows/docker.yml](.github/workflows/docker.yml) tests every push and pull
request, then publishes the image to the repository's own GitHub Container Registry
(`ghcr.io/<owner>/<repo>`, using the built-in token; a fork publishes to its own):

| Push | Image tags |
|---|---|
| to `dev` | `:dev`, `:sha-<commit>` |
| a merge into `main` | `:latest`, `:sha-<commit>` |
| a tag `vX.Y.Z` | `:X.Y.Z`, `:X.Y` |

Each image is stamped with its build (`dev`, `main` or `release`) and commit, shown in
the web UI's footer, the start-up banner and `/healthz`. A new package starts private;
make it public (Package settings → Change visibility) so servers can pull it without
logging in. Protecting `main` (require a pull request and the `test` check) keeps a
failing build off `:latest`.

**Releasing:**

1. Work on `dev`; every push updates `:dev`.
2. Bump the version in [src/media_audit/\_\_init\_\_.py](src/media_audit/__init__.py), the
   only place it lives. Versions are plain `X.Y.Z`: `X.Y.Z+1` for fixes, `X.Y+1.0` for
   features.
3. Open a pull request `dev` → `main` and merge it (*Create a merge commit*):
   `:latest` is published.
4. Tag the merge on `main`: `git checkout main && git pull && git tag vX.Y.Z && git push
   origin vX.Y.Z`. CI refuses a tag that doesn't match the version.
5. Bring `dev` level: `git checkout dev && git pull && git merge --ff-only main && git
   push`.

Tag on `main` after merging, never on `dev` before: the merge is a new commit, so a tag
on `dev` points at a different commit than `:latest`. Never move a pushed tag; release
the next version instead. On the package page, `:latest` and a release are separate
images even when built from the same code (their build stamps differ).

**Trying dev builds on Unraid:** add a second container from the template with
repository `ghcr.io/<owner>/media-audit:dev`, another port, and **its own** config
folder. A dev build may upgrade the database, and a release then refuses to open it;
copy `media-audit.db` across to start with real data.

## License

Copyright (C) 2026 Ethan Matlack

media-audit is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. See [LICENSE](LICENSE)
for the full text.

Releases up to and including 0.4.0 were published under the MIT license.
