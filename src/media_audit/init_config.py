"""Generate a starter config.yml from the folders under a media root.

Used by ``media-audit init-config`` and automatically by the Docker image on first
start (when ``/config/config.yml`` doesn't exist yet). Folder names decide the type:
names containing "movie"/"film" become movie libraries, names containing
"tv"/"show"/"series"/"anime" become TV libraries ("movies-anime" is movies: the movie
rule wins). Anything else is written as a commented-out entry for the user to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_MOVIE_WORDS = frozenset({"movie", "movies", "film", "films", "cinema"})
_TV_WORDS = frozenset({"tv", "show", "shows", "series", "anime", "episodes", "television"})


@dataclass(frozen=True)
class Candidate:
    name: str
    path: Path
    type: str | None  # "movies" | "tv" | None (unrecognized)


def guess_type(folder_name: str) -> str | None:
    words = set(re.split(r"[^a-z0-9]+", folder_name.lower()))
    # Also match glued words like "4kmovies" / "tvshows".
    joined = re.sub(r"[^a-z0-9]+", "", folder_name.lower())
    if words & _MOVIE_WORDS or any(w in joined for w in ("movie", "film")):
        return "movies"
    if words & _TV_WORDS or any(w in joined for w in ("tvshow", "series", "anime")):
        return "tv"
    return None


def discover(media_root: Path) -> list[Candidate]:
    if not media_root.is_dir():
        return []
    found = []
    for entry in sorted(media_root.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir() and not entry.name.startswith((".", "@", "#", "$")):
            found.append(Candidate(entry.name, entry, guess_type(entry.name)))
    return found


def _yaml_str(value: str) -> str:
    """Quote a scalar only when YAML would misread it."""
    if re.fullmatch(r"[A-Za-z0-9_./-][A-Za-z0-9 _./()-]*", value) and value.strip() == value:
        return value
    return "'" + value.replace("'", "''") + "'"


def render(media_root: Path, database: Path, candidates: list[Candidate]) -> str:
    libs = [c for c in candidates if c.type]
    other = [c for c in candidates if not c.type]
    lines = [
        "# media-audit configuration. Generated on first start; edit freely.",
        "# Full reference: https://github.com/Ethan-Matlack/media-audit/blob/main/config.example.yml",
        "#",
        f"# Libraries were detected from the folders in {media_root}. Check each `type`:",
        "#   movies -> <root>/<Movie (Year)>/<files>",
        "#   tv     -> <root>/<Show (Year)>/<Season NN>/<episodes>",
        "",
        f"database: {_yaml_str(str(database))}",
        "",
        "libraries:",
    ]
    if not libs:
        lines += [
            "  # No movie/TV folders were recognized; add yours here, e.g.:",
            "  # - name: movies",
            f"  #   path: {_yaml_str(str(media_root / 'movies'))}",
            "  #   type: movies",
            "  []",
        ]
    for c in libs:
        lines += [
            f"  - name: {_yaml_str(c.name)}",
            f"    path: {_yaml_str(str(c.path))}",
            f"    type: {c.type}",
        ]
    if other:
        lines.append("  # Not recognized as video libraries (uncomment and set a type to scan):")
        for c in other:
            lines += [
                f"  # - name: {_yaml_str(c.name)}",
                f"  #   path: {_yaml_str(str(c.path))}",
                "  #   type: movies",
            ]
    lines += [
        "",
        "scan:",
        "  # Parallel readers; keep low (1-4) for spinning disks and network mounts.",
        "  workers: 2",
        "  # Headers only. Files whose TrueHD/Atmos details aren't in the headers are",
        "  # re-parsed at retry_parse_speed with a time limit (retry_timeout: 0 disables).",
        "  parse_speed: 0",
        "  retry_parse_speed: 0.05",
        "  retry_timeout: 60",
        "",
        "linking:",
        "  # Link the same movie/show across libraries (e.g. movies + movies-4k).",
        "  by_id: true",
        "  by_name_year: true",
        "",
        "languages:",
        "  preferred_audio: [en]",
        "",
    ]
    return "\n".join(lines)


def write_default_config(
    target: Path, media_root: Path, database: Path, *, force: bool = False
) -> tuple[str, list[Candidate]]:
    if target.exists() and not force:
        raise FileExistsError(target)
    candidates = discover(media_root)
    text = render(media_root, database, candidates)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return text, candidates
