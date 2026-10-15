"""semir.site123.me: a tile grid. Tiles link either straight to an .m3u8 or to a small player page on
wapsite.me; tile titles are frequently stale, so names come from the link target, titles are only hints."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..fetcher import find_hls_urls
from ..models import RawCandidate
from ..normalize import fold_ascii
from .base import SourceAdapter, name_from_url

_BG_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")


class Site123Adapter(SourceAdapter):
    name = "site123"

    async def discover(self) -> None:
        page = await self.fetcher.get(self.source.url)
        soup = BeautifulSoup(page.text, "html.parser")
        skip_cats = {fold_ascii(c) for c in self.opt("skip_categories", [])}
        follow = bool(self.opt("follow_links", True))
        follow_hosts = tuple(h.lower() for h in self.opt("follow_hosts", ["wapsite.me"]))

        tiles: dict[str, dict] = {}
        skipped_hrefs: set[str] = set()
        blocks = soup.select("div.carousel-category[data-categories-filter]")
        for block in blocks:
            category = fold_ascii(block.get("data-categories-filter", ""))
            for a in block.select("a.customer-bg[href]"):
                href = a["href"].strip()
                if category in skip_cats:
                    skipped_hrefs.add(href)
                    continue
                tiles.setdefault(href, self._tile(a))
        if not blocks:  # layout changed: fall back to every tile on the page
            for a in soup.select("a.customer-bg[href]"):
                tiles.setdefault(a["href"].strip(), self._tile(a))
            self.notes.append("category blocks not found; scanned all tiles")
        for href in skipped_hrefs:
            if tiles.pop(href, None) is not None:
                self.skip("skipped-category")
        if not tiles:
            self.notes.append("no tiles found on the page")

        for href, tile in tiles.items():
            title = tile["title"]
            logo = tile["logo"]
            lower = href.lower()
            host = (urlsplit(href).hostname or "").lower()
            if ".m3u8" in lower:
                name = name_from_url(href) or title
                if not name:
                    self.skip("unnamed")
                    continue
                aliases = (title,) if title and title != name else ()
                self.add(RawCandidate(self.source.id, href, name, aliases=aliases, logo_url=logo, page_url=self.source.url, name_confidence="low"))
            elif follow and host and any(host == h or host.endswith("." + h) for h in follow_hosts):
                await self._follow(href, title, logo)
            else:
                self.skip("external-page")

    @staticmethod
    def _tile(a) -> dict:
        match = _BG_RE.search(a.get("style", ""))
        return {"title": (a.get("title") or "").strip(), "logo": match.group(1) if match else None}

    async def _follow(self, href: str, title: str, logo: str | None) -> None:
        name = name_from_url(href, last_only=True)
        if not name or all(w.isdigit() for w in name.split()):
            self.skip("unnamed")
            return
        page = await self.fetcher.get_optional(href, max_bytes=600_000)
        if page is None:
            return
        urls = find_hls_urls(page.text)
        if not urls:
            self.skip("page-without-hls")
            return
        aliases = (title,) if title and fold_ascii(title) != fold_ascii(name) else ()
        for url in urls[:3]:
            self.add(RawCandidate(self.source.id, url, name, aliases=aliases, logo_url=logo, page_url=href, name_confidence="medium"))
