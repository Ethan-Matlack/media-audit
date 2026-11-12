"""Start-up banner for ``serve``, so a container (re)start is easy to spot in the log."""

from __future__ import annotations

import os
import platform
import socket
from pathlib import Path

from media_audit import __author__, __url__
from media_audit.build import build_label, display_version

_WIDTH = 60


def container_name() -> str | None:
    """The container's name when Docker/Unraid exposes it (Unraid sets HOST_CONTAINERNAME)."""
    name = os.environ.get("HOST_CONTAINERNAME") or os.environ.get("CONTAINER_NAME")
    if name:
        return name
    if Path("/.dockerenv").exists():
        return socket.gethostname()  # Docker's default hostname: the short container ID
    return None


def render() -> str:
    rows = [
        ("Version", display_version()),
        ("Author", __author__),
        ("Project", __url__),
    ]
    build = build_label()
    if build:
        rows.append(("Build", build))
    name = container_name()
    if name:
        rows.append(("Container", name))
    if hasattr(os, "getuid"):
        rows.append(("Running as", f"uid {os.getuid()}, gid {os.getgid()}"))
    rows.append(("Python", platform.python_version()))

    title = "m e d i a - a u d i t"
    rule = "=" * _WIDTH
    lines = [
        "",
        rule,
        title.center(_WIDTH).rstrip(),
        "media library audit · read-only".center(_WIDTH).rstrip(),
        "-" * _WIDTH,
        *(f"  {label:<11} {value}" for label, value in rows),
        rule,
        "",
    ]
    return "\n".join(lines)
