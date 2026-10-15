"""A broadcaster's own web pages: every .m3u8 address found on a page belongs to the channel named in the config.

    - id: official
      adapter: official
      options:
        channels:
          - {name: "Kirkuk TV", pages: ["https://kirkuktv.net/KU/Live"]}

The pages are read like any source (robots.txt, 1 request/second, cached, bot walls reported as "blocked" and never
bypassed). A page that is down or protected only costs that one channel; the others are still read. When a broadcaster
fixes or moves its stream, the new address appears here at the next refresh - nothing to edit.
"""

from __future__ import annotations

import re

from ..fetcher import FetchProblem, find_hls_urls
from ..models import RawCandidate
from .base import SourceAdapter


class OfficialAdapter(SourceAdapter):
    name = "official"

    async def discover(self) -> None:
        channels = self.opt("channels", [])
        if not channels:
            raise ValueError("source needs options.channels: [{name: ..., pages: [...]}]")
        for entry in channels:
            channel = str(entry.get("name", "")).strip()
            pages = entry.get("pages") or []
            if not channel or not pages:
                self.skip("incomplete-entry")
                continue
            only = re.compile(entry["only"]) if entry.get("only") else None  # optional: keep matching addresses only
            limit = int(entry.get("max_streams", 3))
            for page_url in pages:
                try:
                    page = await self.fetcher.get(page_url)
                except FetchProblem as exc:
                    if exc.kind == "budget":
                        raise
                    self.notes.append(f"{channel}: {page_url} -> {exc.kind}: {exc.message[:90]}")
                    self.skip(f"page-{exc.kind}")
                    continue
                streams = [u for u in find_hls_urls(page.text) if only is None or only.search(u)]
                if not streams:
                    self.skip("page-without-stream")
                    continue
                for url in streams[:limit]:
                    self.add(
                        RawCandidate(
                            source_id=self.source.id,
                            url=url,
                            name=channel,
                            tags=("Kurdish",),
                            language=entry.get("language"),
                            country=entry.get("country"),
                            page_url=page.url,
                            name_confidence="high",
                        )
                    )
