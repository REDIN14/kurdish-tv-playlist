"""Generic page scanner for sites with no dedicated adapter (used for karwan.tv).

It makes one honest, robots-respecting request. If the site answers with a bot challenge the source is
reported as "blocked" - this adapter never tries to defeat such protection."""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..fetcher import find_hls_urls
from ..m3u import parse_m3u
from ..models import RawCandidate
from .base import SourceAdapter, name_from_url
from .m3u import candidate_from_entry


class HtmlScanAdapter(SourceAdapter):
    name = "html_scan"

    async def discover(self) -> None:
        page = await self.fetcher.get(self.source.url)
        soup = BeautifulSoup(page.text, "html.parser")

        for url in find_hls_urls(page.text):
            name = name_from_url(url)
            if not name:
                self.skip("unnamed")
                continue
            self.add(RawCandidate(self.source.id, url, name, page_url=page.url, name_confidence="low"))

        playlist_links = []
        for a in soup.select("a[href]"):
            href = urljoin(page.url, a["href"].strip())
            if href.lower().split("?")[0].endswith(".m3u") and href not in playlist_links:
                playlist_links.append(href)
        for href in playlist_links[:5]:
            pl = await self.fetcher.get_optional(href)
            if pl is None:
                continue
            for entry in parse_m3u(pl.text):
                cand = candidate_from_entry(self.source.id, entry, pl.url, pl.url, confidence="medium")
                if cand is not None:
                    self.add(cand)
