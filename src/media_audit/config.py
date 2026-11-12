"""Configuration: a YAML file plus ``MEDIA_AUDIT_*`` environment overrides.

Environment overrides use ``__`` to reach nested keys, and values are parsed as
YAML so types come through naturally::

    MEDIA_AUDIT_DATABASE=/tmp/test.db
    MEDIA_AUDIT_SCAN__WORKERS=4
    MEDIA_AUDIT_LANGUAGES__PREFERRED_AUDIO="[en, de]"

``MEDIA_AUDIT_CONFIG`` selects the config file itself and is not an override.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ENV_PREFIX = "MEDIA_AUDIT_"
CONFIG_ENV = "MEDIA_AUDIT_CONFIG"
AUTO_INIT_ENV = "MEDIA_AUDIT_AUTO_INIT"  # Docker image: create a config on first start
MEDIA_ROOT_ENV = "MEDIA_AUDIT_MEDIA_ROOT"  # where that first-start config looks for libraries
# MEDIA_AUDIT_* variables that control the program rather than override config keys.
NON_CONFIG_ENV = frozenset({
    CONFIG_ENV, AUTO_INIT_ENV, MEDIA_ROOT_ENV,
    "MEDIA_AUDIT_BUILD_CHANNEL", "MEDIA_AUDIT_BUILD_SHA",  # stamped by CI, see build.py
})  # fmt: skip
DEFAULT_CONFIG_PATHS = (Path("config.yml"), Path("/config/config.yml"))

DEFAULT_IGNORE = [
    # NAS / OS metadata directories and files
    "@eaDir",
    "#recycle",
    "#snapshot",
    ".@__thumb",
    ".Trash-*",
    ".Trashes",
    ".AppleDouble",
    "lost+found",
    "$RECYCLE.BIN",
    "System Volume Information",
    ".DS_Store",
    "._*",
    "Thumbs.db",
    "desktop.ini",
    # Partial downloads
    "*.part",
    "*.!qB",
]


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LibraryConfig(_Model):
    name: str = Field(min_length=1)
    path: Path
    type: Literal["movies", "tv"]
    enabled: bool = True


class ScanConfig(_Model):
    workers: int = Field(default=2, ge=1, le=32)
    """Parallel MediaInfo workers. Keep this low for spinning disks / network mounts."""

    parse_speed: float = Field(default=0.0, ge=0.0, le=1.0)
    """MediaInfo ParseSpeed. 0 reads headers only and still detects DV/HDR10+/Atmos/DTS:X
    (see analyzer/mediainfo.py); MediaInfo's own default of 0.5 can take minutes per
    remux over a network mount."""

    retry_parse_speed: float = Field(default=0.05, gt=0.0, le=1.0)
    """Deeper ParseSpeed for files whose audio MediaInfo couldn't read at ``parse_speed``
    (TrueHD whose Atmos isn't described in the MKV headers)."""

    retry_timeout: float = Field(default=60.0, ge=0.0)
    """Seconds before that deeper parse is abandoned (the speed-0 result is kept and the
    track title is used as an Atmos hint). 0 disables the retry."""

    follow_symlinks: bool = False
    ignore: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE))
    """Glob patterns matched against each file or directory *name* (not the full path)."""

    schedule: str | None = "0 3 * * *"
    """Cron expression (container local time, i.e. ``TZ``) for scans run by ``serve``.
    ``off``/``none``/empty disables scheduled scans; "Scan now" still works."""

    @field_validator("schedule", mode="before")
    @classmethod
    def _schedule(cls, v: object) -> str | None:
        # YAML reads `off`/`no`/`false` as booleans; all of them mean "disabled".
        off = {"", "off", "none", "never", "disabled", "false"}
        if v is None or v is False or (isinstance(v, str) and v.strip().lower() in off):
            return None
        if not isinstance(v, str):
            raise ValueError("schedule must be a cron expression like '0 3 * * *' or 'off'")
        from media_audit.cron import CronError, parse_cron

        try:
            parse_cron(v)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return v.strip()


class WebConfig(_Model):
    """The ``serve`` command's web UI."""

    host: str = "0.0.0.0"  # all interfaces: a container service; publish the port to limit access
    port: int = Field(default=8484, ge=1, le=65535)


class CleanupConfig(_Model):
    """Cleanup scripts (Issues page). media-audit only writes the script; you run it."""

    # Where the media folder is on the machine that runs the script, e.g. /mnt/user/media.
    # Unset: the script uses the library paths exactly as media-audit sees them (right
    # outside Docker; inside Docker the script will refuse to run until this is set).
    host_media_path: str | None = None
    # The same folder as media-audit sees it: the container side of the media mount.
    media_mount: str = "/media"
    # Where /config is on the host (e.g. /mnt/user/appdata/media-audit). Only used to show
    # the command to run a saved script.
    host_config_path: str | None = None

    @field_validator("host_media_path", "host_config_path")
    @classmethod
    def _absolute(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not v.startswith("/"):
            raise ValueError("must be an absolute path, like /mnt/user/media")
        if not v.rstrip("/"):
            raise ValueError("can't be the filesystem root")
        return v.rstrip("/")

    def host_root(self, library_path: str) -> str | None:
        """A library folder as the host sees it; None if it isn't under media_mount."""
        path = library_path.rstrip("/")
        if self.host_media_path is None:
            return path
        mount = self.media_mount.rstrip("/")
        if path == mount:
            return self.host_media_path
        if path.startswith(mount + "/"):
            return self.host_media_path + path[len(mount) :]
        return None


class FlagsConfig(_Model):
    """The Issues checks (media_audit/rules/flags.py). Changes apply at the next scan or
    container start."""

    disabled: list[str] = Field(default_factory=list)
    """Check codes to switch off everywhere, e.g. [no_ids, legacy_codec]."""

    disabled_by_library: dict[str, list[str]] = Field(default_factory=dict)
    """Library name -> check codes to switch off there, e.g. {anime: [no_preferred_audio]}."""

    min_peers: int = Field(default=4, ge=2)
    """Episodes a season needs before its files are compared with each other."""

    short_runtime: float = Field(default=0.6, gt=0, lt=1)
    """Flag an episode shorter than this fraction of its season's median running time."""

    bitrate_low: float = Field(default=0.4, gt=0, lt=1)
    """Flag an episode below this fraction of its season's median GB/hour."""

    bitrate_high: float = Field(default=2.5, gt=1)
    """Flag an episode above this multiple of its season's median GB/hour."""

    movie_bitrate_low: float = Field(default=0.25, gt=0, lt=1)
    """Flag a movie below this fraction of the median GB/hour of movies at its resolution."""

    allowed_extensions: list[str] = Field(
        default_factory=lambda: ["nfo", "jpg", "jpeg", "png", "webp", "gif", "tbn", "bif",
                                 "xml", "json", "sfv", "md5"]
    )  # fmt: skip
    """Non-video, non-subtitle files that are expected in a library (artwork, metadata).
    Anything else is reported as a stray file."""

    strict_nfo: bool = True
    """Report .nfo files media servers don't read (anything but tvshow.nfo, season.nfo,
    movie.nfo or <video name>.nfo) as stray files: scene release info and leftovers."""

    @field_validator("allowed_extensions")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [e.lower().lstrip(".") for e in v]

    def enabled(self, code: str, library: str | None = None) -> bool:
        if code in self.disabled:
            return False
        return not (library and code in self.disabled_by_library.get(library, []))


class LinkingConfig(_Model):
    """How titles in different libraries (e.g. ``movies`` and ``movies-4k``) are linked."""

    by_id: bool = True
    by_name_year: bool = True


class LanguagesConfig(_Model):
    preferred_audio: list[str] = Field(default_factory=lambda: ["en"])
    preferred_subtitles: list[str] = Field(default_factory=list)

    @field_validator("preferred_audio", "preferred_subtitles")
    @classmethod
    def _normalize(cls, v: list[str]) -> list[str]:
        from media_audit.rules.languages import normalize_language

        return [normalize_language(code) for code in v]


class Config(_Model):
    database: Path = Path("/config/media-audit.db")
    libraries: list[LibraryConfig] = Field(default_factory=list)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    linking: LinkingConfig = Field(default_factory=LinkingConfig)
    languages: LanguagesConfig = Field(default_factory=LanguagesConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    flags: FlagsConfig = Field(default_factory=FlagsConfig)  # the Issues checks
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)

    # Reserved for later phases; accepted now so configs written today keep working.
    naming: dict[str, Any] = Field(default_factory=dict)
    integrations: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_names(self) -> Config:
        names = [lib.name for lib in self.libraries]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate library names: {', '.join(sorted(dupes))}")
        return self

    def library(self, name: str) -> LibraryConfig:
        for lib in self.libraries:
            if lib.name == name:
                return lib
        raise ConfigError(f"no library named {name!r} in config")


def find_config_path(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env)
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> None:
    for key, raw in environ.items():
        # Empty values count as unset (e.g. a cleared field in the Unraid template), so the
        # config file decides. Use "off"/"null" etc. to actually clear a setting.
        if not key.startswith(ENV_PREFIX) or key in NON_CONFIG_ENV or raw == "":
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        target = data
        for part in parts[:-1]:
            nxt = target.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                target[part] = nxt
            target = nxt
        target[parts[-1]] = value


def load_config(path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
    """Load config from ``path`` (or the default locations) and apply env overrides."""
    environ = dict(os.environ) if environ is None else environ
    resolved = find_config_path(path)
    data: dict[str, Any] = {}
    base_dir = Path.cwd()

    if resolved is not None:
        if not resolved.is_file():
            raise ConfigError(f"config file not found: {resolved}")
        try:
            loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{resolved}: invalid YAML: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{resolved}: top level must be a mapping")
        data = loaded
        base_dir = resolved.resolve().parent

    _apply_env_overrides(data, environ)

    try:
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        where = str(resolved) if resolved else "config"
        raise ConfigError(f"{where}: {exc}") from exc

    # Relative paths are relative to the config file, not the working directory.
    if not cfg.database.is_absolute():
        cfg.database = base_dir / cfg.database
    for lib in cfg.libraries:
        if not lib.path.is_absolute():
            lib.path = base_dir / lib.path
    return cfg
