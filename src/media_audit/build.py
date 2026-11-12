"""Which build this is: release, main, or a dev build, and from which commit.

CI stamps the Docker image with two environment variables (see the Dockerfile and
.github/workflows/docker.yml). Outside Docker neither is set and the plain version is
shown.
"""

from __future__ import annotations

import os

from media_audit import __version__

CHANNEL_ENV = "MEDIA_AUDIT_BUILD_CHANNEL"  # release | main | dev
SHA_ENV = "MEDIA_AUDIT_BUILD_SHA"  # git commit the image was built from


def channel() -> str | None:
    return os.environ.get(CHANNEL_ENV) or None


def commit() -> str | None:
    sha = os.environ.get(SHA_ENV) or ""
    return sha[:7] or None


def display_version() -> str:
    """'0.4.0' for releases and main; '0.4.0-dev (abc1234)' for dev builds."""
    if channel() == "dev":
        sha = commit()
        return f"{__version__}-dev ({sha})" if sha else f"{__version__}-dev"
    return __version__


def build_label() -> str | None:
    """'dev · abc1234' / 'main · abc1234' / 'release · abc1234', or None outside CI."""
    parts = [p for p in (channel(), commit()) if p]
    return " · ".join(parts) or None


def asset_tag() -> str:
    """Cache-buster for CSS/JS: changes with every build, not just every version."""
    return commit() or __version__
