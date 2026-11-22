"""The Settings page's fields: what each setting is, how it's shown, how a form sets it.

Adding a setting to the page is one :class:`Field` here; the template renders sections
from this list and :func:`parse_section` turns the submitted form back into values for
:meth:`media_audit.settings.ConfigStore.set_values`. Only values that differ from the
current config are written, so saving a section doesn't copy defaults into config.yml.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from media_audit.config import Config
from media_audit.rules.flags import CATEGORIES, CHECKS


@dataclass(frozen=True)
class Field:
    key: str  # dotted config key
    label: str
    kind: str  # bool | int | float | text | path | words | lines | cron | checks
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    placeholder: str = ""
    restart: bool = False  # takes effect after the container restarts


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    intro: str
    fields: tuple[Field, ...]


# fmt: off
SECTIONS: tuple[Section, ...] = (
    Section(
        "scanning",
        "Scanning",
        "How and when your libraries are scanned.",
        (
            Field(
                "scan.schedule",
                "Scan schedule",
                "cron",
                "A cron expression: five fields, minute hour day-of-month month day-of-week, "
                "in the container's time zone. For example 0 3 * * * is daily at 03:00, "
                "0 */6 * * * every 6 hours, 0 3 * * 0 Sundays at 03:00. Leave it empty or type "
                "off for no scheduled scans (Scan now on the Scans page still works).",
                placeholder="0 3 * * *",
            ),
            Field(
                "scan.workers",
                "Files read at once",
                "int",
                "How many files MediaInfo reads in parallel. Keep it low (1-4) for array disks "
                "and network shares.",
                min=1,
                max=32,
                step=1,
            ),
            Field(
                "scan.parse_speed",
                "MediaInfo parse speed",
                "float",
                "0 reads headers only, which is fast and still finds DV, HDR10+, Atmos and "
                "DTS:X. Higher reads more of each file (much slower).",
                0,
                1,
                0.05,
            ),
            Field(
                "scan.retry_parse_speed",
                "Deeper parse speed for TrueHD",
                "float",
                "Used again on files whose TrueHD/Atmos details aren't in the headers.",
                0.01,
                1,
                0.01,
            ),
            Field(
                "scan.retry_timeout",
                "Deeper parse time limit (seconds)",
                "float",
                "Give up on that deeper read after this long. 0 turns the retry off.",
                0,
                3600,
                1,
            ),
            Field(
                "scan.follow_symlinks",
                "Follow symbolic links",
                "bool",
                "Scan folders and files that are symlinks.",
            ),
            Field(
                "scan.ignore",
                "Ignore these names",
                "lines",
                "Files and folders whose name matches one of these patterns (one per line; * "
                "and ? work) are skipped, e.g. @eaDir or *.part.",
                placeholder="@eaDir\n.DS_Store\n*.part",
            ),
        ),
    ),
    Section(
        "linking",
        "Linking titles across libraries",
        "The same movie or show in two libraries (e.g. movies and movies-4k) is shown as "
        "one, and listed on the Issues page.",
        (
            Field(
                "linking.by_id",
                "Link by ID",
                "bool",
                "Same {tmdb-…}, {tvdb-…} or {imdb-…} in the folder or file names.",
            ),
            Field(
                "linking.by_name_year",
                "Link by name and year",
                "bool",
                "Same title and year (ignoring case and punctuation) when there's no ID.",
            ),
        ),
    ),
    Section(
        "languages",
        "Languages",
        "Used by the Issues page and the Files filters.",
        (
            Field(
                "languages.preferred_audio",
                "Preferred audio languages",
                "words",
                "Two- or three-letter codes or names, e.g. en, ja. Files with none of these "
                "are listed under No audio in your preferred language.",
                placeholder="en",
            ),
            Field(
                "languages.preferred_subtitles",
                "Preferred subtitle languages",
                "words",
                "Same format. Leave empty if you don't mind.",
                placeholder="en",
            ),
        ),
    ),
    Section(
        "issues",
        "Issues",
        "Which checks run, and how sensitive they are. Changes apply "
        "straight away; checks can also be switched off per library (Libraries, Edit).",
        (
            Field(
                "flags.disabled", "Checks", "checks", "Untick a check to switch it off everywhere."
            ),
            Field(
                "flags.min_peers",
                "Episodes needed to compare a season",
                "int",
                "Runtime and bitrate are only compared within seasons with at least this many "
                "episodes.",
                2,
                100,
                1,
            ),
            Field(
                "flags.short_runtime",
                "Short episode threshold",
                "float",
                "Flag an episode shorter than this fraction of its season's median (0.6 = 60%).",
                0.05,
                0.95,
                0.05,
            ),
            Field(
                "flags.bitrate_low",
                "Low bitrate threshold (episodes)",
                "float",
                "Below this fraction of the season's median GB/hour.",
                0.05,
                0.95,
                0.05,
            ),
            Field(
                "flags.bitrate_high",
                "High bitrate threshold (episodes)",
                "float",
                "Above this multiple of the season's median GB/hour.",
                1.1,
                20,
                0.1,
            ),
            Field(
                "flags.movie_bitrate_low",
                "Low bitrate threshold (movies)",
                "float",
                "Below this fraction of the median for movies at the same resolution.",
                0.05,
                0.95,
                0.05,
            ),
            Field(
                "flags.allowed_extensions",
                "Expected file types",
                "words",
                "Non-video, non-subtitle files that belong in a library (artwork, metadata). "
                "Anything else is a stray file.",
                placeholder="nfo, jpg, png",
            ),
            Field(
                "flags.strict_nfo",
                "Only media-server .nfo files",
                "bool",
                "Treat .nfo files media servers don't read (scene release info, leftovers) "
                "as stray files.",
            ),
        ),
    ),
    Section(
        "cleanup",
        "Cleanup scripts",
        "Cleanup scripts run on the server, not in the "
        "container. Where each media folder is on the server is set under Media folders.",
        (
            Field(
                "cleanup.host_config_path",
                "This app's data folder on the server",
                "path",
                "Where /config is on the server, e.g. /mnt/user/appdata/media-audit. Only used "
                "to show the command for running a saved script.",
                placeholder="/mnt/user/appdata/media-audit",
            ),
        ),
    ),
    Section(
        "web",
        "Web UI",
        "Takes effect after the container restarts. In Docker, change the "
        "published port in the container settings instead.",
        (
            Field("web.port", "Port", "int", "", 1, 65535, 1, restart=True),
            Field(
                "web.host",
                "Listen on",
                "text",
                "0.0.0.0 listens on every network interface (normal in a container).",
                restart=True,
            ),
        ),
    ),
)
# fmt: on
SECTION_KEYS = {s.key: s for s in SECTIONS}


# The page's tabs, and which tab each section (by id) is on.
TABS: tuple[tuple[str, str], ...] = (
    ("libraries", "Libraries"),
    ("folders", "Media folders"),
    ("scanning", "Scanning"),
    ("issues", "Issues"),
    ("general", "General"),
)
TAB_OF: dict[str, str] = {
    "libraries": "libraries", "folders": "folders", "scanning": "scanning",
    "issues": "issues", "linking": "general", "languages": "general", "cleanup": "general",
    "web": "general", "about": "general",
}  # fmt: skip


def tab_for(*candidates: str | None) -> str:
    """The first candidate that is a tab or a section (as its tab); else the first tab."""
    for c in candidates:
        if c in TAB_OF:
            return TAB_OF[c]
        if c in dict(TABS):
            return c
    return TABS[0][0]


def current(cfg: Config, key: str) -> Any:
    node: Any = cfg
    for part in key.split("."):
        node = getattr(node, part)
    return node


def display(cfg: Config, f: Field) -> Any:
    """The current value as the form shows it."""
    value = current(cfg, f.key)
    if f.kind == "cron":
        return value or "off"
    if f.kind == "words":
        return ", ".join(value)
    if f.kind == "lines":
        return "\n".join(value)
    if f.kind in {"path", "text"}:
        return value or ""
    return value


def check_groups(cfg: Config) -> list[tuple[str, list[dict[str, Any]]]]:
    """The checks by category, each with whether it's on."""
    off = set(cfg.flags.disabled)
    groups = []
    for cat, label in CATEGORIES.items():
        items = [
            {"code": c.code, "heading": c.heading, "severity": c.severity, "on": c.code not in off}
            for c in CHECKS.values()
            if c.category == cat
        ]
        groups.append((label, items))
    return groups


class FormError(ValueError):
    def __init__(self, label: str, message: str) -> None:
        super().__init__(f"{label}: {message}")


def _split_words(raw: str) -> list[str]:
    return [w for w in (x.strip() for x in raw.replace("\n", ",").split(",")) if w]


def parse_value(f: Field, form: Mapping[str, list[str]]) -> Any:
    values = form.get(f.key, [])
    raw = values[-1] if values else ""
    if f.kind == "bool":
        return raw in {"1", "on", "true"}
    if f.kind == "checks":
        on = set(values) - {""}  # "" marks the list as submitted when nothing is ticked
        return [code for code in CHECKS if code not in on]
    raw = raw.strip()
    if f.kind in {"int", "float"}:
        if raw == "":
            raise FormError(f.label, "enter a number")
        try:
            num: float = int(raw) if f.kind == "int" else float(raw)
        except ValueError as exc:
            raise FormError(
                f.label, f"{raw!r} isn't a {'whole ' if f.kind == 'int' else ''}number"
            ) from exc
        if (f.min is not None and num < f.min) or (f.max is not None and num > f.max):
            raise FormError(f.label, f"must be between {f.min:g} and {f.max:g}")
        return num
    if f.kind == "cron":
        return None if raw.lower() in {"", "off"} else raw
    if f.kind == "words":
        return _split_words(raw)
    if f.kind == "lines":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if f.kind == "path":
        if raw and not raw.startswith("/"):
            raise FormError(f.label, "must be an absolute path, like /mnt/user/appdata/media-audit")
        return raw.rstrip("/") or None
    return raw


def parse_section(section: Section, form: Mapping[str, list[str]], cfg: Config,
                  locked: set[str]) -> dict[str, Any]:  # fmt: skip
    """Values that changed, by config key. Raises FormError for a bad value."""
    changes: dict[str, Any] = {}
    for f in section.fields:
        if f.key in locked or f.key not in form:
            continue  # locked, or not on the submitted form: unchanged
        value = parse_value(f, form)
        now = current(cfg, f.key)
        if f.kind == "cron":
            same = (value or None) == (now or None)
        elif f.kind == "checks":
            same = sorted(value) == sorted(now)
        else:
            same = value == now
        if not same:
            changes[f.key] = "off" if f.kind == "cron" and value is None else value
    return changes
