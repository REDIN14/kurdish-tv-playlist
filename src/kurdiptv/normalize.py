"""Channel-name normalisation: the basis for matching the same channel across sources."""

from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

# --- text folding ---------------------------------------------------------------------------------

_SUSPICIOUS = re.compile(r"[ÃÂÅÄÐÑ][^\x00-\x7f]")

_LETTER_MAP = str.maketrans(
    {
        # Latin letters that do not decompose
        "ı": "i", "İ": "i", "ø": "o", "ß": "ss", "đ": "d", "ł": "l", "æ": "ae", "œ": "oe",
        # Arabic-script variants (Arabic, Persian, Sorani spellings of the same letter)
        "ك": "ک", "ي": "ی", "ى": "ی", "ې": "ی", "ێ": "ی", "ۍ": "ی", "ئ": "ی",
        "ڕ": "ر", "ڵ": "ل", "ۆ": "و", "ۋ": "و", "ؤ": "و", "ە": "ه", "ہ": "ه", "ة": "ه",
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
        "ـ": "",  # tatweel
    }
)
_ARABIC_MARKS = re.compile(r"[ً-ٰٟۖ-ۭ]")


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 that was decoded as cp1252/latin-1 ("TRT Ã‡ocuk" -> "TRT Çocuk")."""
    if not _SUSPICIOUS.search(text):
        return text
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != text:
            return fixed
    return text


def fold(text: str) -> str:
    """Lower-case, accent-free, script-variant-free form of a string (keeps spaces/punctuation)."""
    text = unicodedata.normalize("NFKC", html.unescape(fix_mojibake(text)))
    text = text.translate(_LETTER_MAP)
    text = _ARABIC_MARKS.sub("", text)
    out = []
    for ch in unicodedata.normalize("NFKD", text.casefold()):
        if unicodedata.combining(ch):
            continue
        if ch.isdigit() and not ch.isascii():
            try:
                ch = str(unicodedata.digit(ch))
            except ValueError:
                pass
        out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out).translate(_LETTER_MAP))


def fold_ascii(text: str) -> str:
    """Folded string with punctuation collapsed to single spaces (good for comparing labels)."""
    return " ".join(re.sub(r"[^\w]+|_", " ", fold(text)).split())


# --- noise ----------------------------------------------------------------------------------------

_NOISE_PHRASES_RAW = [
    # Kurdish
    "پەخشی ڕاستەوخۆ", "پەخشی راستەوخۆ", "ڕاستەوخۆ", "زیندوو", "live zindi rastawxo", "zindi rastawxo", "rastawxo", "rastexo",
    # Arabic
    "البث المباشر", "بث مباشر", "مباشر", "مشاهدة", "شاهد", "قناة", "قناه",
    # Turkish / English / misc
    "canli izle", "canlı izle", "tv channels", "tv channel", "live tv", "watch live", "is a kurdish sports channel",
    "live tv channel in iraq online", "kurdish tv channels", "kurdish satellite television channel",
]
_NOISE_PHRASES = sorted({fold_ascii(p) for p in _NOISE_PHRASES_RAW if fold_ascii(p)}, key=len, reverse=True)

_NOISE_TOKENS = {
    # quality / format
    "hd", "fhd", "uhd", "sd", "hq", "lq", "4k", "8k", "hevc", "h264", "h265",
    # liveness / SEO
    "live", "zindi", "canli", "online", "stream", "streaming", "watch", "free", "direct", "channel", "channels",
    "television", "televizyon", "izle", "satellite", "official", "the",
    # backup markers
    "alt", "backup", "mirror", "main", "source", "server", "link", "alternative",
}
# "1080p", "4K", "8 K" - but not the "4 Kurd" channel: the marker must end at a word boundary.
_QUALITY_RE = re.compile(r"\b(?:(?:2160|1440|1080|720|576|540|480|360|240)\s*p|[48]\s?k)\b")
_PAREN_BACKUP_RE = re.compile(
    r"[\(\[]\s*(?:alt(?:ernative)?|backup|mirror|source|server|link|stream|main|sd|hd|hq)?\s*\d*\s*[\)\]]", re.IGNORECASE
)
_SPLIT_LETTER_DIGIT = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")


def shorten_seo_title(title: str) -> str:
    """"ARYEN TV Kurdish Unveiling Kurdish Culture and Media Tenacity" -> "ARYEN TV".

    Page titles on aggregator sites are often a channel name followed by a marketing sentence. Names of six or
    more words are cut at a separator, or right after the first "TV" within the first four words, else kept to 3 words.
    """
    words = title.split()
    if len(words) <= 5:
        return title
    for sep in (" | ", " – ", " — ", " - ", ": "):
        if sep in title:
            head = title.split(sep)[0].strip()
            if 1 <= len(head.split()) <= 4:
                return head
    for i, word in enumerate(words[:4]):
        if word.lower().strip(",.:;") == "tv":
            return " ".join(words[: i + 1])
    return " ".join(words[:3])


def name_tokens(name: str) -> list[str]:
    """Normalised tokens of a channel name: no noise words, no quality/backup suffixes."""
    if not name:
        return []
    text = shorten_seo_title(fix_mojibake(name))
    text = _PAREN_BACKUP_RE.sub(" ", text)
    text = fold_ascii(text)
    text = _QUALITY_RE.sub(" ", text)
    for phrase in _NOISE_PHRASES:
        if phrase and phrase in text:
            text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", text)
    text = _SPLIT_LETTER_DIGIT.sub(" ", text)
    tokens = [t for t in text.split() if t not in _NOISE_TOKENS]
    # "tv" is dropped, except when only a bare number would remain ("TV 10").
    without_tv = [t for t in tokens if t != "tv"]
    if without_tv and not all(t.isdigit() for t in without_tv):
        tokens = without_tv
    return tokens


def name_key(name: str) -> str:
    return " ".join(name_tokens(name))


def display_name(name: str) -> str:
    """A tidy human name for channels that are not in the catalog (mojibake fixed, SEO suffixes cut)."""
    text = shorten_seo_title(html.unescape(fix_mojibake(name or "")))
    text = re.sub(r"\s*[|–—-]\s*(?:live|zindi|zindî|canl).*$", "", text, flags=re.IGNORECASE)
    text = _PAREN_BACKUP_RE.sub(" ", text)
    text = re.sub(r"\b(?:live|zindi|zindî|canli|canlı|online|hd|fhd|uhd|sd|4k|hq)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -–—|:.,")
    return text or html.unescape(name or "").strip()


_ARABIC_SCRIPT_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN_RUN_RE = re.compile(r"[A-Za-zÀ-ÿĀ-ž0-9][A-Za-zÀ-ÿĀ-ž0-9'’.&+ ]*[A-Za-zÀ-ÿĀ-ž0-9]")


def latin_name_in_mixed_title(title: str) -> str | None:
    """"عالم الموسيقى الكردية مع قناة Med Muzik TV" -> "Med Muzik TV".

    Only for titles that mix Arabic-script text with a Latin-script run; returns the longest Latin run
    (at least 3 letters) or None when the title is purely one script.
    """
    if not _ARABIC_SCRIPT_RE.search(title):
        return None
    runs = [m.group(0).strip() for m in _LATIN_RUN_RE.finditer(title)]
    runs = [r for r in runs if sum(ch.isalpha() for ch in r) >= 3]
    return max(runs, key=len) if runs else None


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two name keys (character level)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_token_subset(a: str, b: str) -> bool:
    """True when one key's tokens are a strict subset of the other's ("zarok" vs "zarok kurmanci")."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb or ta == tb:
        return False
    return ta < tb or tb < ta
