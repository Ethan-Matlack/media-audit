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

    schedule: str | None = None
    """Cron expression for scheduled scans (used by the web service, Phase 3)."""


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

    # Reserved for later phases; accepted now so configs written today keep working.
    flags: dict[str, Any] = Field(default_factory=dict)
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
        if not key.startswith(ENV_PREFIX) or key == CONFIG_ENV:
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        try:
            value = yaml.safe_load(raw) if raw != "" else ""
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
