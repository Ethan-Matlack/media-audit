#!/usr/bin/env bash
# Build a tiny fake media library (1-second ffmpeg clips with realistic names) for
# trying the scanner and Docker setup without a real share.
#
#   ./scripts/make_demo_library.sh [target-dir]     # default: docker/media
#
# Uses the same layout as the server (movies/, movies-anime/, tv/, anime/), so
# docker/config/config.yml works unchanged.
#
# Includes: Radarr-style and messy names, HD + 4K copies of the same movie in two
# folders (linked by tmdb id), an HDR10 file, an HLG file, multi-version folder, extras, a sample,
# sidecar subtitles, a hardlink, a corrupt file, a name/content mismatch (the
# "2160p" file is really 1080p), TV seasons + specials, and leftover junk.
set -euo pipefail

root="${1:-docker/media}"
if [[ -e "$root" ]] && [[ -n "$(ls -A "$root" 2>/dev/null)" ]]; then
  echo "error: $root exists and is not empty" >&2
  exit 1
fi
mkdir -p "$root"
cd "$root"

ff() { ffmpeg -hide_banner -loglevel error -y "$@"; }

clip() {  # clip <out> <WxH> <vcodec args...> -- <acodec args...>
  local out="$1" size="$2"; shift 2
  local vargs=() aargs=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do vargs+=("$1"); shift; done
  [[ $# -gt 0 ]] && shift
  aargs=("$@")
  mkdir -p "$(dirname "$out")"
  ff -f lavfi -i "testsrc2=size=${size}:rate=24:duration=1" \
     -f lavfi -i "sine=frequency=440:duration=1" \
     -map 0 -map 1 "${vargs[@]}" "${aargs[@]}" "$out"
}

H264=(-c:v libx264 -preset ultrafast)
HEVC10=(-c:v libx265 -preset ultrafast -pix_fmt yuv420p10le -x265-params log-level=none)
HDR10=(-c:v libx265 -preset ultrafast -pix_fmt yuv420p10le
       -x265-params "log-level=none:hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400")
HLG=(-c:v libx265 -preset ultrafast -pix_fmt yuv420p10le
     -x265-params "log-level=none:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc")
ENG=(-metadata:s:a:0 language=eng)

hp_hd="movies/Harry Potter and the Philosopher's Stone (2001)"
hp_4k="movies/Harry Potter and the Philosophers Stone (2001) {tmdb-671}"
clip "$hp_hd/Harry Potter and the Philosophers Stone (2001) {tmdb-671} {edition-Theatrical} [WEBDL-1080p][EAC3 5.1][h265].mkv" \
  1920x800 "${HEVC10[@]}" -- -c:a eac3 -ac 6 "${ENG[@]}"
printf '1\n00:00:00,000 --> 00:00:01,000\nHello\n' \
  > "$hp_hd/Harry Potter and the Philosophers Stone (2001) {tmdb-671} {edition-Theatrical} [WEBDL-1080p][EAC3 5.1][h265].en.srt"
printf '<movie/>\n' > "$hp_hd/movie.nfo"
clip "$hp_4k/Harry Potter and the Philosophers Stone (2001) {tmdb-671} [Bluray-2160p][HDR10][EAC3 5.1][x265]-TERMiNAL.mkv" \
  3840x1600 "${HDR10[@]}" -- -c:a eac3 -ac 6 "${ENG[@]}"

clip "movies/Heat (1995)/Heat (1995) {tmdb-949} [Bluray-1080p][AC3 5.1][x264]-GRP.mkv" \
  1920x1080 "${H264[@]}" -- -c:a ac3 -ac 6 "${ENG[@]}"
# Name claims 2160p HDR but the file is 1080p SDR (Phase 2 mismatch flag target).
clip "movies/Heat (1995)/Heat (1995) {tmdb-949} [Bluray-2160p][HDR10][x265]-FAKE.mkv" \
  1920x1080 "${HEVC10[@]}" -- -c:a aac -ac 2 "${ENG[@]}"
clip "movies/Heat (1995)/Featurettes/Making of Heat.mkv" 1280x720 "${H264[@]}" -- -c:a aac
clip "movies/Heat (1995)/Sample/heat-sample.mkv" 1280x720 "${H264[@]}" -- -c:a aac

# Messy, pre-Radarr names
clip "movies/old movie/Old.Movie.1994.DVDRip.XviD-FLOW.avi" 720x304 \
  -c:v mpeg4 -vtag XVID -- -c:a libmp3lame
printf 'visit our site\n' > "movies/old movie/RARBG.txt"
clip "movies/Nature Doc (2016)/Nature Doc 2016.mp4" 1920x1080 "${HLG[@]}" -- -c:a aac
clip "movies/Loose Movie (1999).mkv" 1280x720 "${H264[@]}" -- -c:a aac -metadata:s:a:0 language=fre
# Hardlinked copy (counted once in unique size)
mkdir -p "movies/Heat (1995)/Copies"
ln "movies/Heat (1995)/Heat (1995) {tmdb-949} [Bluray-1080p][AC3 5.1][x264]-GRP.mkv" \
   "movies/Heat (1995)/Copies/Heat hardlink.mkv"
# Truncated file
mkdir -p "movies/Broken (2010)"
head -c 4096 /dev/urandom > "movies/Broken (2010)/Broken (2010) [WEBDL-1080p].mkv"

bb="tv/Breaking Bad (2008) {tvdb-81189}"
for ep in 1 2; do
  clip "$bb/Season 01/Breaking Bad (2008) - S01E0${ep} - Episode ${ep} [Bluray-1080p][DTS 5.1][x264]-SPARKS.mkv" \
    1920x1080 "${H264[@]}" -- -c:a dca -strict -2 -ac 6 "${ENG[@]}"
done
clip "$bb/Season 02/Breaking Bad (2008) - S02E01 - Seven Thirty-Seven [Bluray-1080p][x264].mkv" \
  1920x1080 "${H264[@]}" -- -c:a aac "${ENG[@]}"
clip "$bb/Specials/Breaking Bad (2008) - S00E01 - Good Cop Bad Cop.mkv" 1280x720 "${H264[@]}" -- -c:a aac
printf '<tvshow/>\n' > "$bb/tvshow.nfo"
clip "tv/Some Show/Some.Show.S03E07.720p.HDTV.x264-KILLERS.mkv" 1280x720 "${H264[@]}" -- -c:a aac
clip "anime/Anime Show (2024) {tvdb-123456}/Season 01/Anime Show (2024) - S01E07 - 007 - Episode Seven [WEBDL-1080p][8bit][h264][Opus 2.0][JA]-Anime Time.mkv" \
  1920x1080 "${H264[@]}" -- -c:a libopus -metadata:s:a:0 language=jpn
clip "anime/[SubsPlease] Other Show/[SubsPlease] Other Show - 12 (1080p) [ABCD1234].mkv" \
  1920x1080 "${H264[@]}" -- -c:a libopus -metadata:s:a:0 language=jpn
clip "movies-anime/Akira (1988) {tmdb-149}/Akira (1988) {tmdb-149} [Bluray-1080p][FLAC 2.0][x264].mkv" \
  1920x1040 "${H264[@]}" -- -c:a flac -ac 2 -metadata:s:a:0 language=jpn

echo "demo library created in $(pwd)"
find . -type f | sort
