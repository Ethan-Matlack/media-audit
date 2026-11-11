#!/usr/bin/env python3
"""Capture MediaInfo JSON from a media file as a test fixture.

Reads only what a normal scan reads (same analyzer settings) and never modifies the
file. The stored path is scrubbed so fixtures don't leak your directory layout, and
Menu (chapter) tracks are dropped to keep fixtures small.

    uv run python scripts/capture_fixture.py "/mnt/media/movies/X (2001)/X.mkv" \
        --name dv_p8_hdr10_eac3_atmos

With ``--name`` matching a synthetic fixture stem, the real capture takes precedence
in the tests (tests/fixtures/mediainfo/real/ is searched first). After capturing, run
``uv run pytest tests/test_rules.py`` to see whether the rules agree with reality.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tests/fixtures/mediainfo/real"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--name", help="fixture stem (default: derived from the filename)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--keep-menu", action="store_true", help="keep chapter tracks")
    parser.add_argument("--parse-speed", type=float, default=0.0)
    parser.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    args = parser.parse_args()

    from media_audit.analyzer import get_analyzer

    analyzer = get_analyzer("mediainfo", parse_speed=args.parse_speed)
    doc = analyzer.analyze(str(args.file))

    name = args.name or re.sub(r"[^a-z0-9]+", "_", args.file.stem.lower()).strip("_")[:80]
    media = doc.get("media", {})
    media["@ref"] = f"/media/CAPTURED{args.file.suffix.lower()}"
    if not args.keep_menu:
        media["track"] = [t for t in media.get("track", []) if t.get("@type") != "Menu"]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{name}.json"
    if out.exists() and not args.force:
        print(f"error: {out} exists (use --force)", file=sys.stderr)
        return 1
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({analyzer.version()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
