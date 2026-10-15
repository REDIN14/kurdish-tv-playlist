"""Generic adapter: any M3U/M3U8 channel-list URL. Add a source entry with ``adapter: m3u`` - no code needed."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from ..m3u import M3UEntry, parse_m3u
from ..models import RawCandidate
from ..normalize import name_key
from .base import SourceAdapter

# Playlist decorations that are not part of a channel's name: "[Not 24/7]", "[Geo-blocked]", "(1080p)", "(Iraq)".
_BRACKETED = re.compile(r"\s*\[[^\]]*\]")
_RESOLUTION_PAREN = re.compile(r"\s*\((?:\d{3,4}[pi]|SD|HD|FHD|UHD|4K)\)", re.IGNORECASE)
_COUNTRY_PAREN = re.compile(r"\s*\((?:Iraq|Syria|Turkey|T[uü]rkiye|Iran|Kurdistan|Germany)\)", re.IGNORECASE)


def clean_playlist_name(name: str) -> str:
    name = _BRACKETED.sub("", name)
    name = _RESOLUTION_PAREN.sub("", name)
    name = _COUNTRY_PAREN.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


def candidate_from_entry(source_id: str, entry: M3UEntry, base_url: str, page_url: str | None, *, confidence: str = "medium") -> RawCandidate | None:
    name = clean_playlist_name(entry.name or entry.attrs.get("tvg-name", ""))
    if not name or not entry.url:
        return None
    group = entry.attrs.get("group-title", "")
    tags = tuple(w for w in group.replace(";", " ").replace("|", " ").replace(",", " ").split() if w)
    logo = entry.attrs.get("tvg-logo") or None
    if logo:
        logo = urljoin(base_url, logo)
    aliases = ()
    tvg_name = entry.attrs.get("tvg-name", "")
    if tvg_name and tvg_name != name:
        aliases = (tvg_name,)
    return RawCandidate(
        source_id=source_id,
        url=urljoin(base_url, entry.url),
        name=name,
        aliases=aliases,
        logo_url=logo,
        tags=tags,
        language=entry.attrs.get("tvg-language") or None,
        country=entry.attrs.get("tvg-country") or None,
        page_url=page_url,
        name_confidence=confidence,
        station_id=entry.attrs.get("tvg-id") or None,
    )


class M3UAdapter(SourceAdapter):
    name = "m3u"

    async def discover(self) -> None:
        page = await self.fetcher.get(self.source.url)
        entries = parse_m3u(page.text)
        if not entries:
            self.notes.append("document contained no #EXTINF entries")
        # optional: take only the listed channels from a big regional list (names compared after normalisation)
        wanted = {name_key(n) for n in self.opt("only", []) if name_key(n)}
        for entry in entries:
            cand = candidate_from_entry(self.source.id, entry, self.source.url, self.source.url)
            if cand is None:
                self.skip("entry-without-name-or-url")
                continue
            if wanted and name_key(cand.name) not in wanted:
                self.skip("not-in-only-list")
                continue
            self.add(cand)
