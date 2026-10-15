"""kurdtvs.net: a published dynamic M3U plus one player page per channel (Clappr, `const streams = [...]`).

The home page is paginated (`/?page=2` ...) and lists Turkish, Arabic and international channels next to the Kurdish ones. The M3U
carries group tags ("Local", "Kurdish", "Turkish"...); a stream found only on a channel page has no such tag, so it is marked
`unclassified` and the scope rules ask for other evidence that it is Kurdish before it is published."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..fetcher import FetchProblem
from ..m3u import parse_m3u
from ..models import UNCLASSIFIED, RawCandidate
from ..normalize import fix_mojibake
from .base import SourceAdapter
from .m3u import candidate_from_entry

_STREAMS_RE = re.compile(r"const\s+streams\s*=\s*(\[.*?\])\s*;", re.DOTALL)
_CHANNEL_HREF_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9\-]*$")
_NON_CHANNEL_SLUGS = {"blog", "sitemap", "index", "about", "contact", "privacy", "dmca"}
_TITLE_SUFFIX_RE = re.compile(r"\s*[-–|]?\s*Live\s+Zindi.*$", re.IGNORECASE)


class KurdTvsAdapter(SourceAdapter):
    name = "kurdtvs"

    async def discover(self) -> None:
        base = self.source.url
        listed = 0

        # 1. The site's own playlist file (names, logos, group tags, several URLs per channel).
        playlist_url = urljoin(base, self.opt("playlist_path", "/iptv.php?format=m3u"))
        try:
            page = await self.fetcher.get(playlist_url)
        except FetchProblem as exc:
            if exc.fatal:
                raise
            self.notes.append(f"playlist unavailable: {exc.message}")
        else:
            for entry in parse_m3u(page.text):
                cand = candidate_from_entry(self.source.id, entry, base, playlist_url, confidence="high")
                if cand is None:
                    self.skip("entry-without-name-or-url")
                    continue
                # kurdtvs tags Kurdistan-local channels "Local"/"Kurdistan"; make that explicit for scope decisions.
                if {t.lower() for t in cand.tags} & {"local", "kurdistan", "kurdish"}:
                    cand.tags = tuple(dict.fromkeys(cand.tags + ("Kurdish",)))
                self.add(cand)
                listed += 1

        listed_keys = set(self.found)

        # 2. Each channel's player page: extra URLs (not in the playlist), og:image logo, clean title.
        if self.opt("follow_channel_pages", True):
            home = await self.fetcher.get_optional(base)
            if home is None:
                self.notes.append("home page unavailable; channel pages skipped")
                return
            paths = self._channel_links(home.text)
            # The listing continues on /?page=2, /?page=3 ... until a page brings nothing new.
            for number in range(2, int(self.opt("max_home_pages", 6)) + 1):
                more = await self.fetcher.get_optional(urljoin(base, f"/?page={number}"))
                if more is None:
                    break
                fresh = [p for p in self._channel_links(more.text) if p not in paths]
                if not fresh:
                    break
                paths.extend(fresh)
            for path in paths:
                page = await self.fetcher.get_optional(urljoin(base, path))
                if page is None:
                    continue
                self._parse_channel_page(page.text, page.url)

        for key, cand in self.found.items():
            if key not in listed_keys and UNCLASSIFIED not in cand.tags:
                cand.tags = cand.tags + (UNCLASSIFIED,)

        if listed == 0 and not self.found:
            self.notes.append("neither the playlist nor channel pages yielded streams")

    @staticmethod
    def _channel_links(html_text: str) -> list[str]:
        soup = BeautifulSoup(html_text, "html.parser")
        seen: dict[str, None] = {}
        for a in soup.select("a[href]"):
            href = a["href"].strip()
            if _CHANNEL_HREF_RE.match(href) and href.strip("/").lower() not in _NON_CHANNEL_SLUGS:
                seen.setdefault(href, None)
        return list(seen)

    def _parse_channel_page(self, html_text: str, page_url: str) -> None:
        match = _STREAMS_RE.search(html_text)
        if not match:
            self.skip("page-without-player")
            return
        try:
            streams = json.loads(match.group(1))
        except json.JSONDecodeError:
            self.skip("unparseable-player-config")
            return
        soup = BeautifulSoup(html_text, "html.parser")
        og_title = soup.find("meta", property="og:title")
        title = (og_title.get("content") if og_title else None) or (soup.title.string if soup.title and soup.title.string else "")
        title = _TITLE_SUFFIX_RE.sub("", fix_mojibake(title or "")).strip(" -–|")
        og_image = soup.find("meta", property="og:image")
        logo = og_image.get("content") if og_image and og_image.get("content") else None
        if not title:
            self.skip("page-without-title")
            return
        for stream in streams if isinstance(streams, list) else []:
            url = (stream or {}).get("url", "")
            if not url or ".m3u8" not in url.lower():
                self.skip("not-hls")
                continue
            self.add(
                RawCandidate(
                    source_id=self.source.id,
                    url=url,
                    name=title,
                    logo_url=logo,
                    page_url=page_url,
                    name_confidence="high",
                )
            )
