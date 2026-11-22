"""What's mounted where, from the kernel's mount table (Linux; empty elsewhere).

In Docker every volume is a mount point, so this tells which media folders are actually
mounted. A library whose folder sits under no mount at all often means a volume was
removed from the container rather than a folder renamed.
"""

from __future__ import annotations

from pathlib import Path

MOUNTINFO = Path("/proc/self/mountinfo")


def _unescape(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \\ooo octal.
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and field[i + 1 : i + 4].isdigit() and len(field[i + 1 : i + 4]) == 3:
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mount_points(mountinfo: Path = MOUNTINFO) -> list[str]:
    """Every mount point, e.g. ['/', '/config', '/media', '/media/content2']."""
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    points = {_unescape(line.split()[4]) for line in lines if len(line.split()) > 4}
    return sorted(points)


def mounts_under(folders: list[str], mountinfo: Path = MOUNTINFO) -> list[str]:
    """Mount points at or below any of ``folders``: candidate media folders."""
    out = []
    for point in mount_points(mountinfo):
        if any(point == f or point.startswith(f.rstrip("/") + "/") for f in folders):
            out.append(point)
    return out


def mount_of(path: str, mountinfo: Path = MOUNTINFO) -> str | None:
    """The mount point ``path`` lives on (the longest one containing it)."""
    path = path.rstrip("/") or "/"
    best = None
    for point in mount_points(mountinfo):
        if (path == point or point == "/" or path.startswith(point + "/")) and (
            best is None or len(point) > len(best)
        ):
            best = point
    return best
