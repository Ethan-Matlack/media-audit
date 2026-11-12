"""Cleanup scripts: turn selected Issues into a bash script the user reviews and runs.

media-audit never changes media itself (``/media`` stays read-only). This module only
*writes text*: a script that, when the user runs it on the host, moves junk into a
quarantine folder inside each library and removes folders that are empty. Nothing is
deleted outright except empty folders, and the script writes a restore script as it goes.

Safety lives in the script as well as here, because it may run long after it was
generated:

* dry run unless ``--apply`` is given;
* every file is re-checked (still a regular file, same size and mtime as at the scan),
  every folder is re-checked (still there, no videos in it now, really empty before
  removal), and anything that changed is skipped and reported;
* every path is relative to a library folder that must exist, with no ``..``;
* every path is quoted with :func:`shlex.quote`, never pasted into the script raw.

Phase 1 covers checks whose fix needs no judgement: empty folders (including the empty
half of a split season), folders with no videos left, stray files and orphaned
subtitles.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from media_audit import __version__
from media_audit.classify import QUARANTINE_DIR, VIDEO_EXTENSIONS
from media_audit.config import Config

SCRIPT_CODES = ("no_video_folder", "stray_file", "orphan_subtitle", "empty_folder",
                "split_season")  # fmt: skip
SCRIPT_NAME = "cleanup-{stamp}.sh"
SCRIPT_RE = r"^cleanup-\d{8}-\d{6}\.sh$"

DISCLAIMER = (
    "This script was generated from media-audit's last scan, not from what is on disk "
    "right now. media-audit does not run it and cannot: it has read-only access to your "
    "media. You are responsible for reading it and for running it. Run it once without "
    "--apply first (a dry run that changes nothing) and check the list. With --apply it "
    "moves files into a quarantine folder inside each library and removes folders that "
    "are empty; it never deletes a file. Keep the quarantine until you have checked your "
    "media server, and keep backups. Use it at your own risk."
)


@dataclass
class Section:
    key: str
    heading: str
    action: str  # one line for the preview page: what --apply does
    lines: list[str] = field(default_factory=list)  # script lines
    folders: int = 0
    files: int = 0

    @property
    def count(self) -> int:
        return self.folders or self.files


@dataclass
class Plan:
    sections: list[Section]
    roots: list[tuple[str, str]]  # (library name, folder on the host), index + 1 = ROOT_n
    unmapped: list[str]  # libraries whose folder isn't under cleanup.media_mount
    skipped_paths: int  # paths refused while generating (shouldn't happen)
    scan_finished: str | None

    @property
    def empty(self) -> bool:
        return not any(s.lines for s in self.sections)

    @property
    def digest(self) -> str:
        """Identifies the content, so a save matches what was previewed."""
        body = json.dumps([self.roots, [(s.key, s.lines) for s in self.sections]])
        return hashlib.sha256(body.encode()).hexdigest()[:16]


def _safe_rel(rel: str) -> bool:
    if not rel or rel.startswith("/") or "\0" in rel:
        return False
    parts = rel.split("/")  # not PurePosixPath: it quietly drops "." and empty parts
    return all(p not in {"", ".", ".."} for p in parts) and parts[0] != QUARANTINE_DIR


def _q(value: object) -> str:
    return shlex.quote(str(value))


_SELECT = """
    SELECT fl.code, fl.data, fl.library_id, l.name AS library, l.path AS library_path,
           f.rel_path, f.size_bytes, f.mtime_ns
    FROM flags fl
    JOIN libraries l ON l.id = fl.library_id AND l.enabled = 1
    LEFT JOIN titles t ON t.id = fl.title_id
    LEFT JOIN files f ON f.id = fl.file_id AND f.deleted_at IS NULL
    LEFT JOIN flag_ignores ig ON ig.key = fl.key AND ig.fingerprint = fl.fingerprint
"""


def build_plan(conn: sqlite3.Connection, config: Config, where: str = "1 = 1",
               params: list[Any] | None = None) -> Plan:  # fmt: skip
    """What a cleanup script for the selected issues would do.

    ``where``/``params`` select issues like the Issues page filters do (aliases ``fl``,
    ``l``, ``t``, ``f``, ``ig``). Ignored issues are always left out.
    """
    marks = ",".join("?" * len(SCRIPT_CODES))
    rows = conn.execute(
        f"{_SELECT} WHERE ({where}) AND ig.key IS NULL AND fl.code IN ({marks}) "
        "ORDER BY l.name, fl.code, f.rel_path, fl.key",
        [*(params or []), *SCRIPT_CODES],
    ).fetchall()

    roots: dict[int, int] = {}  # library id -> ROOT_n
    root_list: list[tuple[str, str]] = []
    unmapped: set[str] = set()
    refused = 0

    def root_of(r: sqlite3.Row) -> int | None:
        if r["library_id"] in roots:
            return roots[r["library_id"]]
        host = config.cleanup.host_root(r["library_path"])
        if host is None:
            unmapped.add(r["library"])
            return None
        root_list.append((r["library"], host))
        roots[r["library_id"]] = len(root_list)
        return len(root_list)

    junk = Section("junk", "Folders with no videos left",
                   "files move to quarantine, then the empty folder is removed")  # fmt: skip
    stray = Section("stray", "Stray files", "move to quarantine")
    orphan = Section("orphan", "Subtitle files with no matching video", "move to quarantine")
    empty = Section("empty", "Empty folders", "removed (only if still empty)")

    covered: dict[int, list[str]] = {}  # library id -> junk folders handled as a whole

    def is_covered(lib_id: int, rel: str) -> bool:
        return any(rel == d or rel.startswith(d + "/") for d in covered.get(lib_id, []))

    def move_line(n: int, rel: str, size: int, mtime_ns: int) -> str:
        return f"quarantine {n} {_q(rel)} {size} {mtime_ns // 1_000_000_000}"

    # Folders first: their files are then left out of the per-file sections.
    for r in rows:
        if r["code"] != "no_video_folder":
            continue
        folder = (json.loads(r["data"]) if r["data"] else {}).get("folder", "")
        n = root_of(r)
        if n is None:
            continue
        if not _safe_rel(folder):
            refused += 1
            continue
        files = conn.execute(
            "SELECT rel_path, size_bytes, mtime_ns FROM files WHERE library_id = ? "
            "AND deleted_at IS NULL AND substr(rel_path, 1, ?) = ? ORDER BY rel_path",
            (r["library_id"], len(folder) + 1, folder + "/"),
        ).fetchall()
        if not all(_safe_rel(f["rel_path"]) for f in files):
            refused += 1
            continue
        covered.setdefault(r["library_id"], []).append(folder)
        junk.folders += 1
        junk.files += len(files)
        junk.lines.append(f"if folder_without_videos {n} {_q(folder)}; then")
        junk.lines.extend(
            "  " + move_line(n, f["rel_path"], f["size_bytes"], f["mtime_ns"]) for f in files
        )
        junk.lines.append(f"  remove_empty {n} {_q(folder)} after-moves")
        junk.lines.append("fi")

    for r in rows:
        code = r["code"]
        if code in {"stray_file", "orphan_subtitle"}:
            if r["rel_path"] is None or is_covered(r["library_id"], r["rel_path"]):
                continue
            n = root_of(r)
            if n is None:
                continue
            if not _safe_rel(r["rel_path"]):
                refused += 1
                continue
            sec = stray if code == "stray_file" else orphan
            sec.files += 1
            sec.lines.append(move_line(n, r["rel_path"], r["size_bytes"], r["mtime_ns"]))
        elif code in {"empty_folder", "split_season"}:
            data = json.loads(r["data"]) if r["data"] else {}
            if code == "empty_folder":
                folders = [data.get("folder", "")]
            else:  # only the empty half of a split season, e.g. a leftover "Season 1"
                folders = [d["path"] for d in data.get("folders", []) if d.get("empty")]
            for folder in folders:
                if is_covered(r["library_id"], folder):
                    continue
                n = root_of(r)
                if n is None:
                    continue
                if not _safe_rel(folder):
                    refused += 1
                    continue
                empty.folders += 1
                empty.lines.append(f"remove_empty {n} {_q(folder)}")

    scan = conn.execute(
        "SELECT finished_at FROM scans WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return Plan([junk, stray, orphan, empty], root_list, sorted(unmapped), refused,
                scan["finished_at"] if scan else None)  # fmt: skip


def script_name(now: datetime) -> str:
    return SCRIPT_NAME.format(stamp=now.strftime("%Y%m%d-%H%M%S"))


def render_script(plan: Plan, now: datetime) -> str:
    """The script text. ``now`` names the quarantine folder and the log."""
    stamp = now.strftime("%Y%m%d-%H%M%S")
    wrapped = _wrap(DISCLAIMER, 74)
    contents = [f"#   {s.heading}: {_count_text(s)}" for s in plan.sections if s.lines]
    video_find = " -o ".join(f"-iname {_q('*.' + e)}" for e in sorted(VIDEO_EXTENSIONS))
    roots = "\n".join(f"ROOT_{i}={_q(path)}\nNAME_{i}={_q(name)}"
                      for i, (name, path) in enumerate(plan.roots, 1))  # fmt: skip
    root_numbers = " ".join(str(i) for i in range(1, len(plan.roots) + 1))
    body: list[str] = []
    for s in plan.sections:
        if not s.lines:
            continue
        body.append("")
        body.append(f"section {_q(f'{s.heading} ({_count_text(s)})')}")
        body.extend(s.lines)

    return _TEMPLATE.format(
        generated=now.strftime("%Y-%m-%d %H:%M"),
        version=__version__,
        scan=_local(plan.scan_finished) if plan.scan_finished else "(no finished scan)",
        disclaimer="\n".join(f"#  {line}" for line in wrapped),
        disclaimer_echo="\n".join(f"  {line}" for line in wrapped),
        contents="\n".join(contents) or "#   (nothing)",
        stamp=stamp,
        quarantine=QUARANTINE_DIR,
        roots=roots or "# (no libraries)",
        root_numbers=root_numbers,
        video_find=video_find,
        body="\n".join(body),
    )


def save_script(text: str, directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o755)
    tmp.replace(path)
    return path


def in_docker() -> bool:
    """Inside a container the host's paths differ, so cleanup.host_media_path is needed."""
    return Path("/.dockerenv").exists()


def scripts_dir(config: Config) -> Path:
    """Saved scripts live next to the database, i.e. in /config/cleanup in Docker."""
    return config.database.parent / "cleanup"


def saved_scripts(directory: Path, limit: int = 10) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    out = []
    for p in sorted(directory.glob("cleanup-*.sh"), reverse=True)[:limit]:
        log = p.with_suffix(".log")
        out.append({"name": p.name, "size": p.stat().st_size, "applied": log.exists(),
                    "restore": p.with_name(p.stem + "-restore.sh").exists()})  # fmt: skip
    return out


def _count_text(s: Section) -> str:
    if s.key == "junk":
        return f"{s.folders} folder{'s' if s.folders != 1 else ''}, {s.files} file" \
            f"{'s' if s.files != 1 else ''}"  # fmt: skip
    n = s.count
    word = "folder" if s.key == "empty" else "file"
    return f"{n} {word}{'s' if n != 1 else ''}"


def _local(iso: str) -> str:
    """A stored UTC timestamp in the server's time zone, like the generated time."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _wrap(text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


_TEMPLATE = r"""#!/usr/bin/env bash
# Paths are single-quoted on purpose (never expanded); ROOT_n/NAME_n are read via ${{!v}}.
# shellcheck disable=SC2016,SC2034
# ============================================================================
#  media-audit cleanup script
#  Generated {generated} by media-audit {version}
#  from the scan that finished {scan}.
#
#  READ THIS BEFORE RUNNING IT
{disclaimer}
# ============================================================================
#
#  Run it on the server, not inside the container (the container can't write to
#  your media):
#
#    bash this-script.sh            dry run: lists what it would do, changes nothing
#    bash this-script.sh --apply    does it (asks you to confirm first)
#
#  What it covers:
{contents}
#
#  Files are moved, not deleted, to <library folder>/{quarantine}/{stamp}/
#  keeping their folder structure. A restore script is written next to this one
#  as files are moved. Every item is checked again before anything happens to it:
#  files must have the same size and modification time as at the scan, folders
#  must have no videos in them and be empty before they're removed. Anything else
#  is skipped and reported. A log is written next to this script with --apply.
# ============================================================================
set -u
export LC_ALL=C

STAMP={stamp}
QUARANTINE={quarantine}
# Library folders on this machine (cleanup.host_media_path in media-audit's config).
{roots}
VIDEO_FIND=( {video_find} )

APPLY=0
case "${{1-}}" in
  --apply) APPLY=1 ;;
  ''|--dry-run) ;;
  *) echo "usage: bash $0 [--apply]" >&2; exit 2 ;;
esac
HERE=$(cd -- "$(dirname -- "$0")" && pwd) || exit 1
NAME=$(basename -- "$0" .sh)
LOG="$HERE/$NAME.log"
RESTORE="$HERE/$NAME-restore.sh"
changed=0
skipped=0

say() {{ printf '%s\n' "$*"; if [ "$APPLY" = 1 ]; then printf '%s\n' "$*" >>"$LOG"; fi; }}
section() {{ say ""; say "== $1"; }}
skip() {{ skipped=$((skipped + 1)); say "  skip    $1  ($2)"; }}
root() {{ local v="ROOT_$1"; printf '%s' "${{!v}}"; }}
label() {{ local v="NAME_$1"; printf '%s: ' "${{!v}}"; }}  # "tv: " before each path
safe_rel() {{
  case "$1" in
    ''|/*|.|./*|..|../*|*/.|*/./*|*/..|*/../*|"$QUARANTINE"|"$QUARANTINE"/*) return 1 ;;
  esac
}}

inside_root() {{  # root number, path: true if it really resolves to inside that library
  local real top
  real=$(realpath -m -- "$2") || return 1
  top=$(realpath -- "$(root "$1")") || return 1
  case "$real" in "$top"/?*) return 0 ;; esac
  return 1
}}

restore_line() {{  # quarantined path, original path
  if [ ! -e "$RESTORE" ]; then
    printf '#!/usr/bin/env bash\n# Moves back what %s.sh put in quarantine.\n' "$NAME" >"$RESTORE"
    printf '# Run: bash %q\nset -u\n' "$RESTORE" >>"$RESTORE"
  fi
  printf 'mkdir -p -- %q && mv -n -- %q %q\n' "$(dirname -- "$2")" "$1" "$2" >>"$RESTORE"
}}

quarantine() {{  # root number, relative path, size, mtime (seconds) at the scan
  local d; d="$(label "$1")$2"
  local r p dest now
  r=$(root "$1"); p="$r/$2"
  if ! safe_rel "$2"; then skip "$d" "unsafe path"; return; fi
  if [ -L "$p" ] || [ ! -f "$p" ]; then skip "$d" "no longer there"; return; fi
  if ! inside_root "$1" "$p"; then skip "$d" "outside the library folder"; return; fi
  if ! now=$(stat -c '%s %Y' -- "$p"); then skip "$d" "can't read it"; return; fi
  if [ "$now" != "$3 $4" ]; then skip "$d" "changed since the scan"; return; fi
  dest="$r/$QUARANTINE/$STAMP/$2"
  if [ -e "$dest" ]; then skip "$d" "already in quarantine"; return; fi
  if [ "$APPLY" = 0 ]; then
    changed=$((changed + 1)); say "  would move    $d"; return
  fi
  if ! mkdir -p -- "$(dirname -- "$dest")" || ! mv -n -- "$p" "$dest" || [ -e "$p" ]; then
    skip "$d" "move failed"; return
  fi
  restore_line "$dest" "$p"
  changed=$((changed + 1)); say "  moved   $d"
}}

folder_without_videos() {{  # root number, relative folder; false (and skip) otherwise
  local d; d="$(label "$1")$2"
  local p found
  p="$(root "$1")/$2"
  if ! safe_rel "$2"; then skip "$d/" "unsafe path"; return 1; fi
  if [ -L "$p" ] || [ ! -d "$p" ]; then skip "$d/" "no longer there"; return 1; fi
  if ! inside_root "$1" "$p"; then skip "$d/" "outside the library folder"; return 1; fi
  found=$(find "$p" -type f \( "${{VIDEO_FIND[@]}}" \) -print -quit)
  if [ -n "$found" ]; then skip "$d/" "has a video now: ${{found#"$p"/}}"; return 1; fi
  say "  folder  $d/"
}}

remove_empty() {{  # root number, relative folder, ["after-moves"]
  local d; d="$(label "$1")$2"
  local p left
  p="$(root "$1")/$2"
  if ! safe_rel "$2"; then skip "$d/" "unsafe path"; return; fi
  if [ -L "$p" ] || [ ! -d "$p" ]; then skip "$d/" "no longer there"; return; fi
  if ! inside_root "$1" "$p"; then skip "$d/" "outside the library folder"; return; fi
  if [ "$APPLY" = 0 ] && [ "${{3-}}" = after-moves ]; then
    changed=$((changed + 1)); say "  would remove  $d/  (once the files above are moved)"; return
  fi
  left=$(find "$p" -mindepth 1 ! -type d -print -quit)
  if [ -n "$left" ]; then skip "$d/" "not empty: ${{left#"$p"/}}"; return; fi
  if [ "$APPLY" = 0 ]; then
    changed=$((changed + 1)); say "  would remove  $d/"; return
  fi
  # Only empty folders: -type d -empty never matches a file.
  find "$p" -depth -type d -empty -delete
  if [ -e "$p" ]; then skip "$d/" "couldn't remove it"; return; fi
  changed=$((changed + 1)); say "  removed $d/"
}}

for n in {root_numbers}; do
  r=$(root "$n")
  if [ -z "$r" ] || [ "$r" = / ] || [ ! -d "$r" ]; then
    echo "Stopped before doing anything: library folder '$r' isn't a folder on this machine." >&2
    echo "Set cleanup.host_media_path in media-audit's config (where its /media folder is" >&2
    echo "on the host), generate a new script, and run that one." >&2
    exit 1
  fi
done

if [ "$APPLY" = 1 ]; then
  echo
  echo "READ THIS FIRST"
  cat <<'DISCLAIMER'
{disclaimer_echo}
DISCLAIMER
  echo
  if [ -t 0 ]; then
    printf "Type 'apply' to make these changes: "
    read -r answer
    if [ "$answer" != apply ]; then echo "Nothing changed."; exit 1; fi
  fi
  say "media-audit cleanup $NAME, applied $(date '+%Y-%m-%d %H:%M:%S')"
else
  echo "DRY RUN: nothing will be changed. Run with --apply to make these changes."
fi
{body}

say ""
if [ "$APPLY" = 1 ]; then
  if [ "$(id -u)" = 0 ]; then  # quarantine folders: same owner as the library folder
    for n in {root_numbers}; do
      r=$(root "$n")
      if [ -d "$r/$QUARANTINE" ]; then
        find "$r/$QUARANTINE" -type d -exec chown --reference="$r" {{}} + 2>/dev/null
      fi
    done
  fi
  say "Done: $changed changed, $skipped skipped."
  if [ -e "$RESTORE" ]; then
    say "Moved files are in <library folder>/$QUARANTINE/$STAMP/"
    say "To put them back: bash $RESTORE"
  fi
  say "Log: $LOG"
  say "Run a scan in media-audit afterwards to clear the issues."
else
  echo "Dry run: $changed would change, $skipped would be skipped. Nothing was changed."
fi
"""
