"""Focused filename parser for media releases.

Handles three broad naming styles:

* **Radarr/Sonarr ("Plex-style")**:
  ``Movie (2001) {tmdb-671} {edition-Theatrical} [Bluray-2160p][HDR10][DTS-X 7.1][x265]-GRP``
* **Scene**: ``Movie.2001.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos-GRP``
* **Plain / hand-named**: ``Movie (2001).mkv``, ``Show - 1x02 - Title.avi``,
  ``[Group] Anime Show - 07 (1080p) [ABCD1234].mkv``

This module only describes what the *name claims*. It never decides whether the claim
is true; the rules engine compares claims against MediaInfo data.

All patterns are case-insensitive and anchored on "word" boundaries where a word
boundary is any non-alphanumeric character, since releases separate tokens with
spaces, dots, underscores, hyphens and brackets interchangeably.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from media_audit.rules.languages import is_known_code, normalize_language

_B = r"(?<![A-Za-z0-9])"  # left word boundary
_E = r"(?![A-Za-z0-9])"  # right word boundary

# --------------------------------------------------------------------------- IDs/edition

_ID_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Plex/Radarr: {tmdb-123}  Jellyfin: [tmdbid-123]  Some users: {tmdbid-123}
    ("tmdb", re.compile(r"[\{\[]tmdb(?:id)?[-=](\d+)[\}\]]", re.I)),
    ("imdb", re.compile(r"[\{\[]imdb(?:id)?[-=](tt\d+)[\}\]]", re.I)),
    ("tvdb", re.compile(r"[\{\[]tvdb(?:id)?[-=](\d+)[\}\]]", re.I)),
]
_EDITION_TAG = re.compile(r"\{edition-([^}]+)\}", re.I)

_EDITION_WORDS = re.compile(
    _B + r"(?P<ed>"
    r"(?:Extended|Ultimate|Special|Collector'?s|Anniversary|Limited|Definitive|"
    r"Criterion|Signature|Deluxe)[ ._-](?:Edition|Cut|Collection)"
    r"|Director'?s[ ._-]Cut|Directors[ ._-]Cut|Final[ ._-]Cut|Theatrical(?:[ ._-]Cut)?"
    r"|Extended|Unrated|Uncut|Uncensored|Remastered|IMAX(?:[ ._-]Enhanced)?|Open[ ._-]Matte"
    r"|Criterion"
    r")" + _E,
    re.I,
)

# Plex local extras suffixes ("Movie (2001)-trailer.mkv").
EXTRA_SUFFIXES = frozenset(
    {
        "behindthescenes",
        "deleted",
        "featurette",
        "interview",
        "scene",
        "short",
        "trailer",
        "other",
    }
)

# ------------------------------------------------------------------------- episodes

_SXXEYY = re.compile(
    _B + r"S(?P<s>\d{1,4})[ ._-]?E(?P<e>\d{1,4})(?P<more>(?:[ ._-]?-?[ ._-]?E\d{1,4})*)" + _E,
    re.I,
)
_MORE_EP = re.compile(r"E(\d{1,4})", re.I)
_NXNN = re.compile(_B + r"(?P<s>\d{1,2})x(?P<e>\d{2,3})(?:-(?:\d{1,2}x)?(?P<e2>\d{2,3}))?" + _E)
_DAILY = re.compile(
    _B + r"(?P<y>(?:19|20)\d{2})[ .-](?P<m>0[1-9]|1[0-2])[ .-](?P<d>[0-2]\d|3[01])" + _E
)
# Anime absolute numbering: "Show - 07", "Show - 107v2". Only used for bracket-group names.
_ABSOLUTE = re.compile(r"\s-\s(?P<e>\d{2,4})(?:v\d)?" + _E)

# ------------------------------------------------------------------------- tech tags


@dataclass(frozen=True)
class _TagDef:
    category: str  # resolution | source | remux | codec | audio | object_audio | channels |
    #                hdr | revision | strong
    value: str
    pattern: re.Pattern[str]


def _t(category: str, value: str, regex: str) -> _TagDef:
    # Audio codecs are often glued to their channel count ("DDP5.1", "AAC2.0"), so
    # only a following *letter* ends them.
    right = r"(?![A-Za-z])" if category == "audio" else _E
    return _TagDef(category, value, re.compile(_B + "(?:" + regex + ")" + right, re.I))


_TAGS: list[_TagDef] = [
    # Resolution. 4K/UHD are claims of 2160p.
    _t("resolution", "4320p", r"4320[pi]|8K"),
    _t("resolution", "2160p", r"2160[pi]|4K|UHD"),
    _t("resolution", "1440p", r"1440[pi]"),
    _t("resolution", "1080p", r"1080[pi]"),
    _t("resolution", "720p", r"720[pi]"),
    _t("resolution", "576p", r"576[pi]"),
    _t("resolution", "540p", r"540[pi]"),
    _t("resolution", "480p", r"480[pi]"),
    _t("resolution", "360p", r"360[pi]"),
    # Sources
    _t("source", "BluRay", r"Blu-?Ray|BDRip|BRRip|BDRemux|BD(?=[ ._-]?Remux)|BD25|BD50|BD66|BD100"),
    _t("source", "WEB-DL", r"WEB-?DL|WEB(?![ ._-]?(?:Rip|DL))"),
    _t("source", "WEBRip", r"WEB-?Rip"),
    _t("source", "HDTV", r"HDTV|PDTV"),
    _t("source", "SDTV", r"SDTV|DSR|TVRip"),
    _t("source", "DVD", r"DVD(?:-?Rip|-?R|5|9)?|DVD-?Remux"),
    _t("source", "Raw-HD", r"Raw-?HD"),
    _t("source", "BR-DISK", r"BR-?DISK"),
    _t("source", "CAM", r"CAM(?:Rip)?|HDCAM"),
    _t("source", "Telesync", r"TELESYNC|HDTS"),
    _t("source", "Telecine", r"TELECINE"),
    _t("source", "Workprint", r"WORKPRINT"),
    _t("source", "DVDSCR", r"DVDSCR|SCREENER"),
    _t("remux", "Remux", r"REMUX|BDRemux|DVD-?Remux"),
    # Video codecs (normalized to the names the analyzer produces)
    _t("codec", "HEVC", r"[xh]\.?265|HEVC"),
    _t("codec", "AVC", r"[xh]\.?264|AVC"),
    _t("codec", "AV1", r"AV1"),
    _t("codec", "VP9", r"VP9"),
    _t("codec", "VC-1", r"VC-?1"),
    _t("codec", "MPEG-2", r"MPEG-?2"),
    _t("codec", "MPEG-4 Visual", r"XviD|DivX"),
    # HDR. Order matters: HDR10+ before HDR10 before HDR.
    _t("hdr", "DV", r"DV|DoVi|Dolby[ ._-]?Vision"),
    _t("hdr", "HDR10+", r"HDR10(?:\+|Plus|P)"),
    _t("hdr", "HDR10", r"HDR10(?!\+|Plus|P)"),
    _t("hdr", "HDR", r"HDR(?!10)"),
    _t("hdr", "HLG", r"HLG"),
    # Object audio
    _t("object_audio", "Atmos", r"Atmos|DDPA|TrueHDA"),
    _t("object_audio", "DTS:X", r"DTS[-:. ]?X"),
    # Audio codecs. DTS variants most-specific first.
    _t("audio", "TrueHD", r"TrueHD"),
    _t("audio", "DTS-HD MA", r"DTS-?HD[ ._-]?MA|DTS-?MA"),
    _t("audio", "DTS-HD HRA", r"DTS-?HD[ ._-]?(?:HRA|HR)"),
    _t("audio", "DTS-HD", r"DTS-?HD(?![ ._-]?(?:MA|HRA|HR))"),
    _t("audio", "DTS-ES", r"DTS-?ES"),
    _t("audio", "DTS", r"DTS(?![-:. ]?(?:X|HD|MA|ES)(?![A-Za-z0-9]))"),
    _t("audio", "E-AC-3", r"E-?AC-?3|DDP(?=\d|A|[ ._-]|$)|DD\+|DDPA"),
    _t("audio", "AC-3", r"AC-?3|DD(?![P+A-Za-z])"),
    _t("audio", "AAC", r"AAC(?:-?LC)?"),
    _t("audio", "FLAC", r"FLAC"),
    _t("audio", "Opus", r"Opus"),
    _t("audio", "PCM", r"L?PCM"),
    _t("audio", "MP3", r"MP3"),
    # Revisions
    _t("revision", "Proper", r"PROPER"),
    _t("revision", "Repack", r"REPACK|RERIP"),
]

# Channels need a looser left boundary: "DDP5.1", "AAC2.0", "DD+7.1", "TrueHD.7.1".
_CHANNELS = re.compile(r"(?<!\d)(?<!\d\.)(?P<a>[1-9])[ .](?P<b>[0-2])(?:ch)?(?!\d)(?!\.\d)", re.I)

_STRONG_CATEGORIES = frozenset({"resolution", "source", "remux", "codec", "hdr", "audio"})

# Radarr/Sonarr {Quality Full} bracket content: "Bluray-2160p", "WEBDL-1080p Proper".
_QUALITY_BRACKET = re.compile(
    r"^(?P<src>Bluray|Blu-ray|WEBDL|WEB-DL|WEBRip|WEB|HDTV|SDTV|DVD|DVD-R|Remux|Raw-HD|"
    r"BR-DISK|CAM|TELESYNC|TELECINE|WORKPRINT|DVDSCR|REGIONAL|Unknown)"
    r"(?:-(?P<res>\d{3,4}p))?(?P<rev>(?:\s+(?:Proper|REPACK|v\d+))*)$",
    re.I,
)
_QUALITY_SOURCE = {
    "bluray": "BluRay",
    "blu-ray": "BluRay",
    "webdl": "WEB-DL",
    "web-dl": "WEB-DL",
    "web": "WEB-DL",
    "webrip": "WEBRip",
    "hdtv": "HDTV",
    "sdtv": "SDTV",
    "dvd": "DVD",
    "dvd-r": "DVD",
    "remux": "BluRay",
    "raw-hd": "Raw-HD",
    "br-disk": "BR-DISK",
    "cam": "CAM",
    "telesync": "Telesync",
    "telecine": "Telecine",
    "workprint": "Workprint",
    "dvdscr": "DVDSCR",
}

_YEAR = re.compile(
    r"(?<![A-Za-z0-9])(?P<paren>[\(\[])?(?P<y>(?:18|19|20)\d{2})(?(paren)[\)\]])(?![A-Za-z0-9])"
)
_BRACKETS = re.compile(r"\[([^\[\]]*)\]")
_LEADING_GROUP = re.compile(r"^\[(?P<g>[^\[\]]+)\]\s*")
_TRAILING_JUNK = re.compile(
    r"(?:[\[\(](?:rarbg|rartv|eztv(?:\.re)?|ettv|publichd|TGx|ion10|PSA|YTS(?:\.\w+)?)[\]\)]"
    r"|\[[0-9A-F]{8}\])\s*$",
    re.I,
)
_GROUP_AT_END = re.compile(r"-(?P<g>[A-Za-z0-9][A-Za-z0-9_.@&!]*?)$")
_SEPARATORS_ONLY = re.compile(r"^[\s._\-–—:,()\[\]{}]*$")
# "-Anime Time": a group with spaces is only trusted directly after a closing bracket.
_SPACED_GROUP_AFTER_BRACKET = re.compile(
    r"\]-(?P<g>[A-Za-z0-9][A-Za-z0-9 _.@&!'-]{0,38}[A-Za-z0-9])$"
)
_BIT_DEPTH = re.compile(_B + r"(?P<b>8|10|12)[ ._-]?bit" + _E, re.I)
# Sonarr anime format: "Show - S01E05 - 017 - Title" (absolute number after SxxEyy).
# 3-4 digits only, so episode titles like "S01E01 - 24 Hours" aren't misread.
_ABSOLUTE_AFTER_SXXEYY = re.compile(r"^\s*-\s*(?P<e>\d{3,4})(?=\s+-\s|\s*\[|\s*$)")
# "[JA]", "[ZH+EN]": Sonarr {MediaInfo AudioLanguages}.
_LANGUAGE_BRACKET = re.compile(r"^[A-Za-z]{2,3}(?:\+[A-Za-z]{2,3})*$")


@dataclass
class ParsedName:
    title: str | None = None
    year: int | None = None
    tmdb_id: str | None = None
    imdb_id: str | None = None
    tvdb_id: str | None = None
    edition: str | None = None
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    air_date: str | None = None
    absolute_episode: int | None = None
    episode_title: str | None = None
    resolution: str | None = None
    source: str | None = None
    is_remux: bool = False
    is_proper: bool = False
    is_repack: bool = False
    quality_tag: str | None = None
    video_codec: str | None = None
    video_codec_tag: str | None = None
    audio_codecs: list[str] = field(default_factory=list)
    audio_object: str | None = None
    audio_channels: str | None = None
    audio_tag: str | None = None
    hdr: list[str] = field(default_factory=list)
    release_group: str | None = None
    bit_depth: int | None = None
    languages: list[str] = field(default_factory=list)
    extra_suffix: str | None = None
    is_sample: bool = False
    brackets: list[str] = field(default_factory=list)
    style: str = "plain"  # radarr | scene | plain

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Match:
    category: str
    value: str
    raw: str
    start: int
    end: int


def _blank(text: str, start: int, end: int) -> str:
    """Replace a span with spaces, preserving offsets."""
    return text[:start] + " " * (end - start) + text[end:]


def _clean_title(text: str) -> str | None:
    # Scene names use dots/underscores as spaces; Radarr names use real spaces.
    t = text.replace("_", " ")
    if " " not in text.strip():
        t = t.replace(".", " ")
    t = re.sub(r"\s+", " ", t)
    t = t.strip(" -–—._:,([{")
    t = re.sub(r"\(\s*\)$", "", t).strip()
    return t or None


def _find_tags(text: str) -> list[_Match]:
    matches: list[_Match] = []
    for tag in _TAGS:
        for m in tag.pattern.finditer(text):
            matches.append(_Match(tag.category, tag.value, m.group(0), m.start(), m.end()))
    for m in _CHANNELS.finditer(text):
        matches.append(
            _Match("channels", f"{m.group('a')}.{m.group('b')}", m.group(0), m.start(), m.end())
        )
    # Drop matches fully contained in a longer match of the same category
    # (e.g. "DTS" inside "DTS-HD MA").
    matches.sort(key=lambda t: (t.start, -(t.end - t.start)))
    kept: list[_Match] = []
    for cand in matches:
        contained = any(
            k.start <= cand.start
            and cand.end <= k.end
            and (k.end - k.start) > (cand.end - cand.start)
            and k.category == cand.category
            for k in kept
        )
        if not contained:
            kept.append(cand)
    return kept


def parse_filename(filename: str, *, has_extension: bool = True) -> ParsedName:
    """Parse a file or folder name (no directories).

    Pass ``has_extension=False`` for folder names so ``Movie.2001.1080p`` keeps its
    last token.
    """
    p = ParsedName()
    stem = filename
    dot = stem.rfind(".")
    if has_extension and dot > 0 and len(stem) - dot <= 6 and " " not in stem[dot:]:
        stem = stem[:dot]

    work = stem

    # IDs and explicit edition tags anywhere in the name.
    for kind, pattern in _ID_PATTERNS:
        for m in pattern.finditer(work):
            setattr(p, f"{kind}_id", m.group(1))
            work = _blank(work, m.start(), m.end())
            p.style = "radarr"
    m_ed = _EDITION_TAG.search(work)
    if m_ed:
        p.edition = m_ed.group(1).strip()
        work = _blank(work, m_ed.start(), m_ed.end())
        p.style = "radarr"

    # Leading "[Group]" (fansub style).
    m_lead = _LEADING_GROUP.match(work)
    leading_group = None
    if m_lead and not _QUALITY_BRACKET.match(m_lead.group("g")):
        leading_group = m_lead.group("g").strip()
        work = _blank(work, m_lead.start(), m_lead.end())

    # Trailing tracker / CRC junk.
    while True:
        m_junk = _TRAILING_JUNK.search(work.rstrip())
        if not m_junk:
            break
        work = work[: m_junk.start()]

    # Plex extras suffix: "Movie (2001)-trailer".
    m_suffix = re.search(r"-(?P<s>[a-z]+)\s*$", work)
    if m_suffix and m_suffix.group("s").lower() in EXTRA_SUFFIXES:
        p.extra_suffix = m_suffix.group("s").lower()
        work = work[: m_suffix.start()]

    if re.search(_B + r"sample" + _E, work, re.I):
        p.is_sample = True

    # Bracket contents (Radarr/Sonarr put quality/tech info in brackets).
    for m_br in _BRACKETS.finditer(work):
        content = m_br.group(1).strip()
        if not content:
            continue
        p.brackets.append(content)
        if _LANGUAGE_BRACKET.match(content) and not any(
            t.pattern.fullmatch(content) for t in _TAGS
        ):
            codes = content.split("+")
            if all(is_known_code(c) for c in codes):
                p.languages.extend(normalize_language(c) for c in codes)
        q = _QUALITY_BRACKET.match(content)
        if q and p.quality_tag is None:
            p.quality_tag = content
            p.style = "radarr"
            p.source = _QUALITY_SOURCE.get(q.group("src").lower(), p.source)
            if q.group("src").lower() == "remux":
                p.is_remux = True
            if q.group("res"):
                p.resolution = q.group("res").lower()
            rev = (q.group("rev") or "").lower()
            p.is_proper = p.is_proper or "proper" in rev
            p.is_repack = p.is_repack or "repack" in rev

    # Episode markers.
    ep_start = None
    ep_end = None
    m_s = _SXXEYY.search(work)
    if m_s:
        p.season = int(m_s.group("s"))
        p.episodes = [int(m_s.group("e"))]
        p.episodes += [int(x) for x in _MORE_EP.findall(m_s.group("more") or "")]
        ep_start, ep_end = m_s.start(), m_s.end()
        m_abs = _ABSOLUTE_AFTER_SXXEYY.match(work[ep_end:])
        if m_abs and not _YEAR.fullmatch(m_abs.group("e")):
            p.absolute_episode = int(m_abs.group("e"))
            ep_end += m_abs.end()
    else:
        m_x = _NXNN.search(work)
        if m_x:
            p.season = int(m_x.group("s"))
            p.episodes = [int(m_x.group("e"))]
            if m_x.group("e2"):
                p.episodes.append(int(m_x.group("e2")))
            ep_start, ep_end = m_x.start(), m_x.end()
        else:
            m_d = _DAILY.search(work)
            if m_d:
                p.air_date = f"{m_d.group('y')}-{m_d.group('m')}-{m_d.group('d')}"
                ep_start, ep_end = m_d.start(), m_d.end()
            elif leading_group:
                m_a = _ABSOLUTE.search(work)
                if m_a:
                    p.absolute_episode = int(m_a.group("e"))
                    ep_start, ep_end = m_a.start(), m_a.end()
    if len(p.episodes) > 1:
        # "S01E01-E03" means 1..3; "S01E01E02" lists explicitly. Expand ranges.
        lo, hi = p.episodes[0], p.episodes[-1]
        if hi > lo and len(p.episodes) == 2 and "-" in work[ep_start:ep_end]:
            p.episodes = list(range(lo, hi + 1))

    # Technical tags; the earliest "strong" one bounds the title.
    tags = _find_tags(work)
    first_bracket = work.find("[")
    strong_starts = [t.start for t in tags if t.category in _STRONG_CATEGORIES]
    tech_start = min(strong_starts) if strong_starts else len(work)
    if first_bracket >= 0:
        tech_start = min(tech_start, first_bracket)
    # A strong tag at position 0 is part of the title ("UHD Planet", "WEB of Lies"), skip it.
    if tech_start == 0:
        later = [s for s in strong_starts if s > 0]
        tech_start = min(later) if later else len(work)

    title_limit = min(x for x in (ep_start, tech_start) if x is not None)

    # Year: the last candidate before the title limit that isn't at the very start.
    year_match = None
    for m_y in _YEAR.finditer(work):
        if m_y.start() == 0 and not m_y.group("paren"):
            continue
        if p.air_date and ep_start is not None and m_y.start() == ep_start:
            continue
        if m_y.start() < title_limit or (year_match is None and m_y.group("paren")):
            year_match = m_y
    # Ignore a year that only appears after the episode marker (e.g. episode titles).
    if year_match and ep_start is not None and year_match.start() > ep_start:
        year_match = None

    title_end = title_limit
    if year_match:
        p.year = int(year_match.group("y"))
        title_end = min(title_end, year_match.start())

    p.title = _clean_title(work[:title_end])

    # Episode title: between the episode marker and the tech region.
    if ep_end is not None:
        rest_end = tech_start if tech_start > ep_end else len(work)
        between = work[ep_end:rest_end]
        cleaned = _clean_title(between)
        if cleaned and not _SEPARATORS_ONLY.match(cleaned):
            p.episode_title = cleaned

    # Only trust tags after the title (avoid "Tron 2.0", "4K Restoration Doc" in titles).
    tech_from = max(title_end, ep_end or 0) if p.title else 0
    tech_tags = [t for t in tags if t.start >= tech_from]

    def first(category: str) -> _Match | None:
        return next((t for t in tech_tags if t.category == category), None)

    if p.resolution is None and (r := first("resolution")):
        p.resolution = r.value
    if p.source is None and (r_src := first("source")):
        p.source = r_src.value
    if first("remux"):
        p.is_remux = True
        p.source = p.source or "BluRay"
    if c := first("codec"):
        p.video_codec = c.value
        p.video_codec_tag = c.raw
    audio = [t for t in tech_tags if t.category == "audio"]
    for a in audio:
        if a.value not in p.audio_codecs:
            p.audio_codecs.append(a.value)
    if o := first("object_audio"):
        p.audio_object = o.value
        if o.value == "DTS:X" and not p.audio_codecs:
            p.audio_codecs.append("DTS-HD MA")
        if o.raw.upper() == "DDPA" and "E-AC-3" not in p.audio_codecs:
            p.audio_codecs.append("E-AC-3")
    if ch := first("channels"):
        p.audio_channels = ch.value
    audio_like = [t for t in tech_tags if t.category in {"audio", "object_audio", "channels"}]
    if audio_like:
        span_start = min(t.start for t in audio_like)
        span_end = max(t.end for t in audio_like)
        # Dots become spaces except between digits, so "TrueHD.7.1.Atmos" -> "TrueHD 7.1 Atmos".
        tag = re.sub(r"(?<!\d)\.|\.(?!\d)|_", " ", work[span_start:span_end])
        p.audio_tag = re.sub(r"\s+", " ", tag).strip()
    for h in tech_tags:
        if h.category == "hdr" and h.value not in p.hdr:
            p.hdr.append(h.value)
    if m_bits := _BIT_DEPTH.search(work, tech_from):
        p.bit_depth = int(m_bits.group("b"))
    for rev in (t for t in tech_tags if t.category == "revision"):
        if rev.value == "Proper":
            p.is_proper = True
        else:
            p.is_repack = True

    if p.edition is None:
        m_e = _EDITION_WORDS.search(work, tech_from if p.title else 0)
        if m_e:
            p.edition = re.sub(r"[._]", " ", m_e.group("ed")).strip()

    # Release group: trailing "-GROUP" that follows a tech token or a bracket.
    m_g = _GROUP_AT_END.search(work.rstrip())
    if m_g and m_g.start() >= tech_from:
        before = work[: m_g.start()].rstrip()
        follows_tech = before.endswith(("]", ")")) or any(t.end == m_g.start() for t in tags)
        group = m_g.group("g")
        if follows_tech and group.lower() not in EXTRA_SUFFIXES and len(group) <= 40:
            p.release_group = group
    m_sg = _SPACED_GROUP_AFTER_BRACKET.search(work.rstrip())
    if p.release_group is None and m_sg and m_sg.start() >= tech_from:
        p.release_group = m_sg.group("g")
    if p.release_group is None and leading_group:
        p.release_group = leading_group

    if p.style != "radarr" and " " not in stem and stem.count(".") >= 2:
        p.style = "scene"
    return p
