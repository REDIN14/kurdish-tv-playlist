"""Generic adapter: any M3U/M3U8 channel-list URL. Add a source entry with ``adapter: m3u`` - no code needed."""

from __future__ import annotations

from urllib.parse import urljoin

from ..m3u import M3UEntry, parse_m3u
from ..models import RawCandidate
from .base import SourceAdapter


def candidate_from_entry(source_id: str, entry: M3UEntry, base_url: str, page_url: str | None, *, confidence: str = "medium") -> RawCandidate | None:
    name = entry.name or entry.attrs.get("tvg-name", "")
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
        for entry in entries:
            cand = candidate_from_entry(self.source.id, entry, self.source.url, self.source.url)
            if cand is None:
                self.skip("entry-without-name-or-url")
                continue
            self.add(cand)
