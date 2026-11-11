"""Group files into titles (movies/shows) and seasons, link sidecars, compute sizes.

Everything here is derived from paths and already-stored data; no file reads. It's
cheap enough to recompute for a whole library on every scan, which keeps it simple
and always consistent.

Layout assumptions (the Plex/Jellyfin convention Radarr/Sonarr produce):

* movies: ``<root>/<Movie (Year)>/<files...>``; loose files directly in the root
  become one title per video file.
* tv: ``<root>/<Show (Year)>/<Season NN | Specials>/<episodes...>``; episodes may also
  sit directly in the show folder, in which case the season comes from ``SxxEyy``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from media_audit import classify
from media_audit.config import LinkingConfig
from media_audit.db import transaction
from media_audit.parsing import parse_filename
from media_audit.rules.languages import UNDETERMINED, is_known_code, normalize_language
from media_audit.rules.subtitles import EXTERNAL_IDX_BASE, parse_sidecar

log = logging.getLogger(__name__)

_SUBS_FOLDERS = frozenset({"subs", "subtitles", "sub"})


@dataclass
class _TitleAcc:
    folder_path: str
    folder_name: str | None  # None for loose files
    kind: str
    file_ids: list[int] = field(default_factory=list)
    main_parsed: list[dict[str, object]] = field(default_factory=list)
    # season number -> (folder_path or None, file ids)
    seasons: dict[int | None, tuple[str | None, list[int]]] = field(default_factory=dict)


def normalize_name(name: str) -> str:
    """Case/accent/punctuation-insensitive key: "Philosopher's Stone" == "Philosophers Stone"."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _stem(name: str) -> str:
    ext = classify.extension(name)
    return name[: -(len(ext) + 1)] if ext else name


def resolve_library(
    conn: sqlite3.Connection, library_id: int, root: str, library_type: str, now: str
) -> None:
    """Rebuild titles/seasons for one library and assign title_id/season_id to files."""
    kind = "movie" if library_type == "movies" else "show"
    rows = conn.execute(
        "SELECT id, rel_path, filename, role, is_video, season_number, filename_parsed "
        "FROM files WHERE library_id = ? AND deleted_at IS NULL",
        (library_id,),
    ).fetchall()
    root = root.rstrip("/")

    titles: dict[str, _TitleAcc] = {}
    # Loose files in a movies root: group by stem of the video they belong to.
    loose_videos: dict[str, str] = {}  # dir-less stem -> title folder_path (the video path)
    loose_others: list[sqlite3.Row] = []

    for row in rows:
        parts = PurePosixPath(row["rel_path"]).parts
        parsed = json.loads(row["filename_parsed"]) if row["filename_parsed"] else None
        if len(parts) == 1:
            if row["is_video"] and row["role"] == "main":
                path = f"{root}/{row['rel_path']}"
                acc = titles.setdefault(path, _TitleAcc(path, None, kind))
                acc.file_ids.append(row["id"])
                if parsed:
                    acc.main_parsed.append(parsed)
                loose_videos[_stem(row["filename"])] = path
                if kind == "show":
                    _add_to_season(acc, row, parsed, None, None)
            else:
                loose_others.append(row)
            continue

        folder = parts[0]
        path = f"{root}/{folder}"
        acc = titles.setdefault(path, _TitleAcc(path, folder, kind))
        acc.file_ids.append(row["id"])
        if row["is_video"] and row["role"] == "main" and parsed:
            acc.main_parsed.append(parsed)
        if kind == "show":
            season_dir = None
            season_num = None
            if len(parts) >= 3:
                n = classify.season_folder_number(parts[1])
                if n is not None:
                    season_dir, season_num = f"{path}/{parts[1]}", n
            _add_to_season(acc, row, parsed, season_dir, season_num)

    for row in loose_others:
        stem = _stem(row["filename"])
        # "Movie (2001).en.srt" / "Movie (2001).nfo" -> the loose video with that stem.
        owner = next(
            (p for s, p in loose_videos.items() if stem == s or stem.startswith(s + ".")), None
        )
        if owner:
            titles[owner].file_ids.append(row["id"])

    with transaction(conn):
        existing = {
            r["folder_path"]: r["id"]
            for r in conn.execute(
                "SELECT id, folder_path FROM titles WHERE library_id = ?", (library_id,)
            )
        }
        keep: set[int] = set()
        file_title: list[tuple[int, int | None, int]] = []

        for path, acc in titles.items():
            name, year, ids = _title_identity(acc)
            title_id = existing.get(path)
            if title_id is None:
                cur = conn.execute(
                    "INSERT INTO titles (library_id, kind, name, year, tmdb_id, imdb_id, "
                    "tvdb_id, folder_path, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (library_id, kind, name, year, ids.get("tmdb"), ids.get("imdb"),
                     ids.get("tvdb"), path, now),
                )  # fmt: skip
                title_id = int(cur.lastrowid or 0)
            else:
                conn.execute(
                    "UPDATE titles SET kind = ?, name = ?, year = ?, tmdb_id = ?, imdb_id = ?, "
                    "tvdb_id = ?, updated_at = ? WHERE id = ?",
                    (kind, name, year, ids.get("tmdb"), ids.get("imdb"), ids.get("tvdb"), now,
                     title_id),
                )  # fmt: skip
            keep.add(title_id)

            season_ids: dict[int, int] = {}
            if kind == "show":
                existing_seasons = {
                    r["season_number"]: r["id"]
                    for r in conn.execute(
                        "SELECT id, season_number FROM seasons WHERE title_id = ?", (title_id,)
                    )
                }
                keep_seasons = set()
                for number, (season_dir, ids_in_season) in acc.seasons.items():
                    sid = existing_seasons.get(number)
                    if sid is None:
                        cur = conn.execute(
                            "INSERT INTO seasons (title_id, season_number, folder_path) "
                            "VALUES (?, ?, ?)",
                            (title_id, number, season_dir),
                        )
                        sid = int(cur.lastrowid or 0)
                    else:
                        conn.execute(
                            "UPDATE seasons SET folder_path = ? WHERE id = ?", (season_dir, sid)
                        )
                    keep_seasons.add(sid)
                    for fid in ids_in_season:
                        season_ids[fid] = sid
                stale = set(existing_seasons.values()) - keep_seasons
                conn.executemany("DELETE FROM seasons WHERE id = ?", [(s,) for s in stale])

            for fid in acc.file_ids:
                file_title.append((title_id, season_ids.get(fid), fid))

        stale_titles = set(existing.values()) - keep
        conn.executemany("DELETE FROM titles WHERE id = ?", [(t,) for t in stale_titles])
        conn.execute(
            "UPDATE files SET title_id = NULL, season_id = NULL WHERE library_id = ?",
            (library_id,),
        )
        conn.executemany("UPDATE files SET title_id = ?, season_id = ? WHERE id = ?", file_title)

        _link_sidecars(conn, library_id)
        _update_sizes(conn, library_id)


def _add_to_season(
    acc: _TitleAcc,
    row: sqlite3.Row,
    parsed: dict[str, object] | None,
    season_dir: str | None,
    season_num: int | None,
) -> None:
    if season_num is None and row["is_video"] and row["role"] == "main":
        season_num = row["season_number"]
    is_episode = bool(row["is_video"]) and row["role"] == "main"
    if season_num is None and season_dir is None and not is_episode:
        return  # non-episode file in the show folder (poster.jpg, tvshow.nfo)
    folder, ids = acc.seasons.get(season_num, (season_dir, []))
    ids.append(row["id"])
    acc.seasons[season_num] = (folder or season_dir, ids)


def _title_identity(acc: _TitleAcc) -> tuple[str, int | None, dict[str, str]]:
    folder = parse_filename(acc.folder_name, has_extension=False) if acc.folder_name else None
    ids: dict[str, str] = {}
    for key in ("tmdb", "imdb", "tvdb"):
        value = getattr(folder, f"{key}_id") if folder else None
        if not value:
            votes = Counter(str(p[f"{key}_id"]) for p in acc.main_parsed if p.get(f"{key}_id"))
            value = votes.most_common(1)[0][0] if votes else None
        if value:
            ids[key] = value
    name = folder.title if folder and folder.title else None
    year = folder.year if folder else None
    if (name is None or year is None) and acc.main_parsed:
        names = Counter(str(p["title"]) for p in acc.main_parsed if p.get("title"))
        years = Counter(int(str(p["year"])) for p in acc.main_parsed if p.get("year"))
        if name is None and names:
            name = names.most_common(1)[0][0]
        if year is None and years:
            year = years.most_common(1)[0][0]
    if name is None:
        name = acc.folder_name or PurePosixPath(acc.folder_path).name
    return name, year, ids


def _link_sidecars(conn: sqlite3.Connection, library_id: int) -> None:
    """Attach subtitle files to their video and record them as external tracks."""
    conn.execute(
        "DELETE FROM subtitle_tracks WHERE is_external = 1 AND file_id IN "
        "(SELECT id FROM files WHERE library_id = ?)",
        (library_id,),
    )
    conn.execute(
        "UPDATE files SET parent_file_id = NULL WHERE library_id = ? AND role = 'subtitle'",
        (library_id,),
    )
    rows = conn.execute(
        "SELECT id, rel_path, filename, role, is_video FROM files "
        "WHERE library_id = ? AND deleted_at IS NULL AND (is_video = 1 OR role = 'subtitle')",
        (library_id,),
    ).fetchall()
    videos_by_dir: dict[str, list[sqlite3.Row]] = defaultdict(list)
    subs: list[sqlite3.Row] = []
    for row in rows:
        parent = str(PurePosixPath(row["rel_path"]).parent)
        if row["is_video"]:
            videos_by_dir[parent].append(row)
        else:
            subs.append(row)

    next_idx: dict[int, int] = defaultdict(lambda: EXTERNAL_IDX_BASE)
    inserts = []
    parents = []
    for sub in subs:
        rel = PurePosixPath(sub["rel_path"])
        parent = str(rel.parent)
        best = None
        best_len = -1
        track = None
        for video in videos_by_dir.get(parent, []):
            stem = _stem(video["filename"])
            if len(stem) <= best_len:
                continue
            t = parse_sidecar(stem, sub["filename"])
            if t is not None:
                best, best_len, track = video, len(stem), t
        if best is None and rel.parent.name.lower() in _SUBS_FOLDERS:
            # "Movie/Subs/2_English.srt": belongs to the single main video one level up.
            candidates = [
                v for v in videos_by_dir.get(str(rel.parent.parent), []) if v["role"] == "main"
            ]
            if len(candidates) == 1:
                best = candidates[0]
                track = parse_sidecar(_stem(sub["filename"]), sub["filename"])
                if track is not None:
                    track.language = _language_from_tokens(_stem(sub["filename"]))
        if best is None or track is None:
            continue
        idx = next_idx[best["id"]]
        next_idx[best["id"]] += 1
        inserts.append(
            (best["id"], idx, track.format, track.language, track.title, int(track.is_forced),
             int(track.is_default), int(track.is_hearing_impaired), sub["id"])
        )  # fmt: skip
        parents.append((best["id"], sub["id"]))

    conn.executemany(
        "INSERT INTO subtitle_tracks (file_id, idx, format, language, title, is_forced, "
        "is_default, is_hearing_impaired, is_external, external_file_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        inserts,
    )
    conn.executemany("UPDATE files SET parent_file_id = ? WHERE id = ?", parents)


def _language_from_tokens(stem: str) -> str:
    for token in re.split(r"[._ \-]+", stem):
        if token and not token.isdigit() and is_known_code(token):
            return normalize_language(token)
    return UNDETERMINED


def _update_sizes(conn: sqlite3.Connection, library_id: int) -> None:
    unique_key = "CASE WHEN inode IS NULL THEN 'f' || id ELSE dev || ':' || inode END"
    conn.execute(
        f"""
        UPDATE titles SET
            file_count = coalesce(agg.files, 0),
            video_count = coalesce(agg.videos, 0),
            version_count = CASE WHEN titles.kind = 'movie' THEN coalesce(agg.videos, 0) ELSE 0 END,
            folder_size_bytes = coalesce(agg.total, 0),
            media_size_bytes = coalesce(agg.media, 0),
            other_size_bytes = coalesce(agg.total, 0) - coalesce(agg.media, 0),
            folder_unique_bytes = coalesce(uniq.total, 0)
        FROM (
            SELECT title_id, count(*) AS files,
                   sum(role = 'main' AND is_video = 1) AS videos,
                   sum(size_bytes) AS total,
                   sum(CASE WHEN is_video = 1 THEN size_bytes ELSE 0 END) AS media
            FROM files WHERE library_id = :lib AND deleted_at IS NULL AND title_id IS NOT NULL
            GROUP BY title_id
        ) AS agg
        LEFT JOIN (
            SELECT title_id, sum(size) AS total FROM (
                SELECT title_id, max(size_bytes) AS size FROM files
                WHERE library_id = :lib AND deleted_at IS NULL AND title_id IS NOT NULL
                GROUP BY title_id, {unique_key}
            ) GROUP BY title_id
        ) AS uniq ON uniq.title_id = agg.title_id
        WHERE titles.id = agg.title_id
        """,
        {"lib": library_id},
    )
    conn.execute(
        f"""
        UPDATE seasons SET
            episode_count = coalesce(agg.episodes, 0),
            folder_size_bytes = coalesce(agg.total, 0),
            folder_unique_bytes = coalesce(uniq.total, 0)
        FROM (
            SELECT season_id,
                   count(DISTINCT CASE WHEN role = 'main' AND is_video = 1
                         THEN coalesce(episode_number, -id) END) AS episodes,
                   sum(size_bytes) AS total
            FROM files WHERE library_id = :lib AND deleted_at IS NULL AND season_id IS NOT NULL
            GROUP BY season_id
        ) AS agg
        LEFT JOIN (
            SELECT season_id, sum(size) AS total FROM (
                SELECT season_id, max(size_bytes) AS size FROM files
                WHERE library_id = :lib AND deleted_at IS NULL AND season_id IS NOT NULL
                GROUP BY season_id, {unique_key}
            ) GROUP BY season_id
        ) AS uniq ON uniq.season_id = agg.season_id
        WHERE seasons.id = agg.season_id
        """,
        {"lib": library_id},
    )


# ------------------------------------------------------------------- cross-library linking


def link_titles(conn: sqlite3.Connection, linking: LinkingConfig) -> None:
    """Assign ``group_key`` so the same movie/show in different libraries links up.

    Titles are joined if they share any embedded ID (when ``by_id``) or the same
    normalized name + year (when ``by_name_year``). Linking is transitive, so a title
    with only an ID and one with only a matching name/year still join through a third.
    A name/year match never joins groups whose IDs disagree (e.g. two different films
    both called "Lincoln (2012)"); ID links are applied first so that check sees them.
    Titles linked to nothing keep ``group_key = NULL``.
    """
    rows = conn.execute(
        "SELECT id, kind, name, year, tmdb_id, imdb_id, tvdb_id FROM titles"
    ).fetchall()
    id_cols = ("tmdb_id", "imdb_id", "tvdb_id")
    parent: dict[int, int] = {}
    # For each group root: the IDs its members carry, per ID type.
    group_ids: dict[int, dict[str, set[str]]] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def conflicts(a: dict[str, set[str]], b: dict[str, set[str]]) -> bool:
        return any(a[col] and b[col] and not (a[col] & b[col]) for col in id_cols)

    def union(a: int, b: int, *, check_ids: bool) -> None:
        ra, rb = find(a), find(b)
        if ra == rb or (check_ids and conflicts(group_ids[ra], group_ids[rb])):
            return
        keep, drop = min(ra, rb), max(ra, rb)
        parent[drop] = keep
        dropped = group_ids.pop(drop)
        for col in id_cols:
            group_ids[keep][col] |= dropped[col]

    keys_by_title: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        parent[r["id"]] = r["id"]
        group_ids[r["id"]] = {
            col: ({r[col]} if linking.by_id and r[col] else set()) for col in id_cols
        }

    def link(key_of: Callable[[sqlite3.Row], list[str]], *, check_ids: bool) -> None:
        owner: dict[str, int] = {}
        for r in rows:
            for k in key_of(r):
                keys_by_title[r["id"]].append(k)
                if k in owner:
                    union(owner[k], r["id"], check_ids=check_ids)
                else:
                    owner[k] = r["id"]

    if linking.by_id:
        link(lambda r: [f"{r['kind']}:{c[:4]}:{r[c]}" for c in id_cols if r[c]], check_ids=False)
    if linking.by_name_year:
        link(
            lambda r: (
                [f"{r['kind']}:name:{normalize_name(r['name'])}|{r['year']}"]
                if r["year"] and r["name"]
                else []
            ),
            check_ids=True,
        )

    members: dict[int, list[int]] = defaultdict(list)
    for tid in parent:
        members[find(tid)].append(tid)

    updates: list[tuple[str | None, int]] = []
    for group in members.values():
        if len(group) == 1:
            updates.append((None, group[0]))
            continue
        all_keys = sorted({k for t in group for k in keys_by_title[t]})
        # Prefer an ID-based key as the label: stable when names get corrected.
        label = next((k for k in all_keys if ":name:" not in k), all_keys[0])
        updates.extend((label, t) for t in group)
    with transaction(conn):
        conn.executemany("UPDATE titles SET group_key = ? WHERE id = ?", updates)
