# MediaInfo fixtures

Rules-engine tests load fixtures by stem, searching in this order:

1. `real/`: captures from the real library (`scripts/capture_fixture.py`, MediaInfo
   24.12, `parse_speed` 0 unless the stem says otherwise). Checked by
   `tests/test_real_fixtures.py` against what each filename claims.
2. `generated/`: real MediaInfo output for tiny ffmpeg encodes
   (`scripts/generate_fixtures.sh`).
3. `synthetic/`: written by hand. The real captures confirmed their field shapes
   (`Dolby Vision / SMPTE ST 2086`, `dvhe.08 / `, `HDR10 / HDR10+ Profile A`,
   `MLP FBA` + `16-ch`, `JOC`, `XLL`). They're still the only coverage for
   **DV profile 5, profile 7 and P8.4 HLG**, which haven't been found in the library yet.

A `real/` capture with the same stem as a synthetic one replaces it. Real captures
use descriptive `real_*` names because their contents don't exactly match the
synthetic scenarios.

## What the real captures showed (2026-11-10)

- `parse_speed` 0 (headers only) reads 0.1–7 MB in under 3 s per file, including 75 GB
  remuxes, and detects DV profile/compatibility, HDR10+, DTS:X and most Atmos. Any
  non-zero speed has unpredictable cost on the SMB mount: 0.05 took 8 s on one file and
  over 3 minutes on another.
- Some MKVs don't expose TrueHD details in their headers, so at speed 0 MediaInfo
  reports bare `Format: TrueHD` and Atmos is unknown (`real_bluray_hdr10_truehd_atmos`).
  The analyzer retries those files at `retry_parse_speed` (0.05) with a time cap
  (`real_..._speed005` is the result: 2.4 s, Atmos confirmed). If the retry fails, the
  track is marked `details_incomplete` and its title gives an "Atmos?" hint.
- The "DV HDR10" remuxes seen so far are profile 8.1, not profile 7.
- AVI headers can disagree with the stream: `Channels` 2 vs `Channels_Original` 1.
  The rules trust the stream's own layout.

## Adding a capture

```sh
uv run python scripts/capture_fixture.py "<file>" --name real_<description>
```

Add an entry to `tests/test_real_fixtures.py`. If a rule turns out wrong, fix the rule
(the capture is the truth) and bump `RULES_VERSION` in `src/media_audit/rules/derive.py`.
