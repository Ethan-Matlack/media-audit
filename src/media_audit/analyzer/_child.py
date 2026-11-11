"""Child-process entry point for a deeper MediaInfo parse.

Runs as ``python -m media_audit.analyzer._child <path> <parse_speed>`` and prints
MediaInfo JSON to stdout. A separate process lets the parent enforce a hard time limit
(a libmediainfo call can't be interrupted inside a thread).
"""

from __future__ import annotations

import sys


def main() -> int:
    from pymediainfo import MediaInfo

    path, speed = sys.argv[1], float(sys.argv[2])
    output = MediaInfo.parse(path, output="JSON", parse_speed=speed, full=False)
    sys.stdout.write(str(output or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
