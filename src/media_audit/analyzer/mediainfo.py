"""MediaInfo backend via pymediainfo (MIT) + libmediainfo (BSD-2-Clause).

Disk friendliness: ``parse_speed`` controls how much of each file MediaInfo reads.
We default to 0 (headers only). Measured on real files over an SMB mount (2026-11-10):
at 0, 0.1-7 MB read and under 3 s per file, including 75 GB remuxes, with DV
profile/compatibility, HDR10+, Atmos (TrueHD and E-AC-3 JOC), DTS:X and all tracks
still detected; on the files compared, the output matched 0.5 exactly. MediaInfo's
own default of 0.5 took over 3 minutes per remux.

One known gap at speed 0: some MKVs don't describe their TrueHD stream in the headers,
so Atmos goes undetected. When the rules flag that (``needs_deeper_parse``), the file is
parsed again at ``retry_parse_speed`` in a child process killed after ``retry_timeout``
seconds. Non-zero speeds have unpredictable cost on network mounts (0.05 took 2.6 s on
one file and over 3 minutes on another), hence the hard limit. Each stored document
records what happened under its ``"media_audit"`` key.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from typing import Any

from media_audit.analyzer import AnalyzerError, AnalyzerUnavailable
from media_audit.rules.audio import needs_deeper_parse

log = logging.getLogger(__name__)


class MediaInfoAnalyzer:
    name = "mediainfo"

    def __init__(
        self,
        parse_speed: float = 0.0,
        retry_parse_speed: float = 0.05,
        retry_timeout: float = 60.0,
    ) -> None:
        try:
            from pymediainfo import MediaInfo
        except ImportError as exc:  # pragma: no cover - pymediainfo is a hard dependency
            raise AnalyzerUnavailable("pymediainfo is not installed") from exc
        if not MediaInfo.can_parse():
            raise AnalyzerUnavailable(
                "libmediainfo was not found. Install it (Debian/Ubuntu: libmediainfo0v5, "
                "Arch: libmediainfo, macOS: brew install media-info) or use the Docker image."
            )
        self._mi = MediaInfo
        self.parse_speed = parse_speed
        self.retry_parse_speed = retry_parse_speed
        self.retry_timeout = retry_timeout
        self._version: str | None = None

    def version(self) -> str:
        # Taken from the first parsed document's "creatingLibrary" block.
        return self._version or "mediainfo"

    def analyze(self, path: str) -> dict[str, Any]:
        try:
            output = self._mi.parse(path, output="JSON", parse_speed=self.parse_speed, full=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AnalyzerError(f"MediaInfo failed: {exc}") from exc
        doc = _load(output)
        if doc is None:
            raise AnalyzerError("MediaInfo found no tracks (unreadable or not a media file)")
        if self._version is None:
            lib = doc.get("creatingLibrary") or {}
            if lib.get("version"):
                self._version = f"mediainfo {lib['version']}"

        meta: dict[str, Any] = {"parse_speed": self.parse_speed}
        if self.retry_timeout > 0 and needs_deeper_parse(doc):
            deeper, retry = self._deeper_parse(path)
            meta["retry"] = retry
            if deeper is not None:
                doc = deeper
                meta["parse_speed"] = self.retry_parse_speed
        doc["media_audit"] = meta
        return doc

    def _deeper_parse(self, path: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Re-parse in a child process with a hard time limit. Never raises."""
        info: dict[str, Any] = {"parse_speed": self.retry_parse_speed}
        cmd = [sys.executable, "-m", "media_audit.analyzer._child", path,
               str(self.retry_parse_speed)]  # fmt: skip
        started = time.monotonic()
        try:
            proc = subprocess.run(  # fixed argv, no shell
                cmd, capture_output=True, timeout=self.retry_timeout, check=False
            )
        except subprocess.TimeoutExpired:
            info["status"] = "timeout"
            log.info("deeper parse timed out after %ss: %s", self.retry_timeout, path)
            return None, info
        finally:
            info["seconds"] = round(time.monotonic() - started, 1)
        doc = _load(proc.stdout.decode("utf-8", "replace")) if proc.returncode == 0 else None
        if doc is None:
            info["status"] = "error"
            log.warning("deeper parse failed (exit %s): %s", proc.returncode, path)
            return None, info
        info["status"] = "ok"
        return doc, info


def _load(output: str | None) -> dict[str, Any] | None:
    """Parse MediaInfo JSON output; ``None`` if it's empty, invalid, or has no tracks."""
    try:
        doc = json.loads(output) if output else None
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict) or not (doc.get("media") or {}).get("track"):
        return None
    return doc
