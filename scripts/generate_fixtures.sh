#!/usr/bin/env bash
# Regenerate tests/fixtures/mediainfo/generated/ from tiny ffmpeg-encoded files.
#
# These are *real* MediaInfo outputs, but for synthetic encodes: they validate field
# names for formats ffmpeg can produce (HDR10, HLG, AV1, VP9, DTS, TrueHD, subtitles).
# Dolby Vision, HDR10+, Atmos and DTS:X can't be produced this way; those still need
# captures from real files (scripts/capture_fixture.py).
#
# Requires ffmpeg with libx265, libsvtav1, libvpx-vp9. Run from the repo root.
set -euo pipefail

out_dir="tests/fixtures/mediainfo/generated"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$out_dir"

ff() { ffmpeg -hide_banner -loglevel error -y "$@"; }
src_v() { echo "-f lavfi -i testsrc2=size=$1:rate=${2:-24}:duration=1"; }
src_a="-f lavfi -i sine=frequency=440:duration=1"

printf '1\n00:00:00,000 --> 00:00:01,000\nHello\n' > "$work/sub.srt"

# shellcheck disable=SC2046,SC2086
{
ff $(src_v 3840x1600) $src_a -i "$work/sub.srt" -map 0 -map 1 -map 2 \
  -c:v libx265 -preset ultrafast -pix_fmt yuv420p10le \
  -x265-params "hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400:log-level=none" \
  -c:a eac3 -ac 6 -metadata:s:a:0 language=eng -disposition:a:0 default \
  -c:s srt -metadata:s:s:0 language=ger -disposition:s:0 forced \
  "$work/gen_hdr10_hevc_eac3_srt.mkv"

ff $(src_v 1920x1080 25) $src_a -c:v libx265 -preset ultrafast -pix_fmt yuv420p10le \
  -x265-params "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:log-level=none" \
  -c:a ac3 -ac 2 "$work/gen_hlg_hevc_ac3.mkv"

ff $(src_v 1920x800) $src_a -c:v libsvtav1 -preset 12 -pix_fmt yuv420p10le \
  -c:a libopus -ac 2 -metadata:s:a:0 language=jpn "$work/gen_av1_opus.mkv"

ff $(src_v 1280x720) $src_a -c:v libvpx-vp9 -deadline realtime -c:a libopus "$work/gen_vp9_720p.webm"

ff $(src_v 1920x1080) $src_a -i "$work/sub.srt" -map 0 -map 1 -map 2 \
  -c:v libx264 -preset ultrafast -c:a aac -ac 2 -c:s mov_text -metadata:s:a:0 language=eng \
  "$work/gen_h264_aac_movtext.mp4"

ff $(src_v 1920x1080) $src_a -c:v libx264 -preset ultrafast -c:a dca -strict -2 -ac 6 \
  "$work/gen_h264_dts_core.mkv"

ff $(src_v 1920x1080) $src_a -c:v libx264 -preset ultrafast -c:a truehd -strict -2 -ac 6 \
  "$work/gen_h264_truehd.mkv"

ff $(src_v 720x480 30) $src_a -c:v mpeg2video -c:a mp2 "$work/gen_mpeg2_mp2.mpg"
}

for f in "$work"/gen_*; do
  name="$(basename "${f%.*}")"
  uv run --quiet python scripts/capture_fixture.py "$f" --name "$name" --out-dir "$out_dir" --force
done
