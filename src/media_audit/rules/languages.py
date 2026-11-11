"""Language code normalization.

Everything is normalized to a lowercase ISO 639-1 two-letter code where one exists
(``en``, ``de``, ``ja``), otherwise to the ISO 639-2/3 three-letter code. Region and
script subtags are dropped (``en-US`` -> ``en``, ``zh-Hant`` -> ``zh``). Missing,
blank, or explicitly undetermined values all become ``und``.

A built-in table is used rather than ``pycountry`` (LGPL) to keep dependencies
permissive. Unknown codes pass through lowercased so nothing is silently lost.
"""

from __future__ import annotations

import re

UNDETERMINED = "und"

# fmt: off
# ISO 639-2 (bibliographic and terminology forms) -> ISO 639-1.
_ISO639_2_TO_1: dict[str, str] = {
    "afr": "af", "alb": "sq", "sqi": "sq", "amh": "am", "ara": "ar", "arm": "hy",
    "hye": "hy", "aze": "az", "baq": "eu", "eus": "eu", "bel": "be", "ben": "bn",
    "bos": "bs", "bul": "bg", "bur": "my", "mya": "my", "cat": "ca", "chi": "zh",
    "zho": "zh", "hrv": "hr", "cze": "cs", "ces": "cs", "dan": "da", "dut": "nl",
    "nld": "nl", "eng": "en", "est": "et", "fao": "fo", "fil": "tl", "fin": "fi",
    "fre": "fr", "fra": "fr", "geo": "ka", "kat": "ka", "ger": "de", "deu": "de",
    "gle": "ga", "glg": "gl", "gre": "el", "ell": "el", "guj": "gu", "heb": "he",
    "hin": "hi", "hun": "hu", "ice": "is", "isl": "is", "ind": "id", "ita": "it",
    "jpn": "ja", "kan": "kn", "kaz": "kk", "khm": "km", "kor": "ko", "kur": "ku",
    "lao": "lo", "lat": "la", "lav": "lv", "lit": "lt", "ltz": "lb", "mac": "mk",
    "mkd": "mk", "may": "ms", "msa": "ms", "mal": "ml", "mlt": "mt", "mao": "mi",
    "mri": "mi", "mar": "mr", "mon": "mn", "nep": "ne", "nor": "no", "nob": "nb",
    "nno": "nn", "per": "fa", "fas": "fa", "pol": "pl", "por": "pt", "pan": "pa",
    "pus": "ps", "rum": "ro", "ron": "ro", "rus": "ru", "srp": "sr", "sin": "si",
    "slo": "sk", "slk": "sk", "slv": "sl", "som": "so", "spa": "es", "swa": "sw",
    "swe": "sv", "tgl": "tl", "tam": "ta", "tel": "te", "tha": "th", "tib": "bo",
    "bod": "bo", "cmn": "zh", "tur": "tr", "ukr": "uk", "urd": "ur", "uzb": "uz", "vie": "vi",
    "wel": "cy", "cym": "cy", "yid": "yi", "yor": "yo", "zul": "zu", "xho": "xh",
    "epo": "eo", "gla": "gd", "bre": "br", "cos": "co", "hat": "ht", "haw": "haw",
}

_ISO639_1 = frozenset(_ISO639_2_TO_1.values()) | {
    "aa", "ab", "ak", "an", "as", "av", "ay", "ba", "bh", "bi", "bm", "ce", "ch",
    "cr", "cu", "cv", "dv", "dz", "ee", "ff", "fj", "fy", "gn", "gv", "ha", "ho",
    "hz", "ia", "ie", "ig", "ii", "ik", "io", "iu", "jv", "kg", "ki", "kj", "kl",
    "kr", "ks", "kv", "kw", "ky", "lg", "li", "ln", "lu", "mg", "mh", "na", "nd",
    "ng", "nr", "nv", "ny", "oc", "oj", "om", "or", "os", "pi", "qu", "rm", "rn",
    "rw", "sa", "sc", "sd", "se", "sg", "sm", "sn", "ss", "st", "su", "tg", "ti",
    "tk", "tn", "to", "ts", "tt", "tw", "ty", "ug", "ve", "vo", "wa", "wo", "za",
}

# Values meaning "no language information".
_UNDETERMINED_VALUES = frozenset({"", "und", "undetermined", "unknown", "none", "n/a", "xx"})

# Special ISO 639-2 codes kept as-is: multiple, no linguistic content, uncoded.
SPECIAL_CODES = frozenset({"mul", "zxx", "mis"})

# English names that show up in filenames and some MediaInfo builds.
_NAMES: dict[str, str] = {
    "english": "en", "german": "de", "french": "fr", "spanish": "es", "italian": "it",
    "japanese": "ja", "korean": "ko", "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
    "russian": "ru", "portuguese": "pt", "dutch": "nl", "swedish": "sv", "norwegian": "no",
    "danish": "da", "finnish": "fi", "polish": "pl", "czech": "cs", "hungarian": "hu",
    "greek": "el", "turkish": "tr", "hebrew": "he", "arabic": "ar", "hindi": "hi",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "ukrainian": "uk",
    "romanian": "ro", "bulgarian": "bg", "croatian": "hr", "serbian": "sr",
    "slovak": "sk", "slovenian": "sl", "icelandic": "is", "persian": "fa",
    "malay": "ms", "tamil": "ta", "telugu": "te", "catalan": "ca", "basque": "eu",
    "latvian": "lv", "lithuanian": "lt", "estonian": "et", "tagalog": "tl",
    "filipino": "tl",
}
# fmt: on

_SPLIT = re.compile(r"[-_]")


def normalize_language(value: str | None) -> str:
    """Normalize a language code or name. Returns ``und`` when unknown/missing."""
    if value is None:
        return UNDETERMINED
    text = value.strip().lower()
    if text in _UNDETERMINED_VALUES:
        return UNDETERMINED
    if text in _NAMES:
        return _NAMES[text]
    primary = _SPLIT.split(text, maxsplit=1)[0]
    if primary in _UNDETERMINED_VALUES:
        return UNDETERMINED
    if primary in SPECIAL_CODES:
        return primary
    if len(primary) == 2:
        return primary
    if len(primary) == 3:
        return _ISO639_2_TO_1.get(primary, primary)
    return _NAMES.get(primary, primary)


def is_known_code(token: str) -> bool:
    """True if ``token`` is a recognizable language code (used when parsing sidecar names)."""
    t = token.lower()
    return t in _ISO639_1 or t in _ISO639_2_TO_1 or t in _NAMES or t in SPECIAL_CODES
