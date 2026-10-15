"""Adapter contract: one small class per source so a markup change breaks only that source."""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from urllib.parse import unquote, urlsplit

from ..config import SourceConfig
from ..fetcher import FetchProblem, SourceFetcher
from ..models import RawCandidate, SourceResult
from ..policy import strip_ephemeral_query
from ..util import clip, rendition_master, url_key

log = logging.getLogger(__name__)

_WOWZA_SESSION_CHUNKLIST = re.compile(r"/chunklist_w\d+[^/]*\.m3u8", re.IGNORECASE)
_GENERIC_WORDS = {
    "hls", "stream", "streams", "live", "index", "playlist", "master", "m3u8", "chunklist", "chunks", "app", "media",
    "video", "videos", "smil", "livestream", "livetrans", "channel", "channels", "upload", "uploads", "tv", "hd",
    "www", "http", "https", "com", "net", "org", "tr", "store", "xyz", "info", "live20", "ts", "local",
}


def canonical_stream_url(url: str) -> str:
    """Wowza session chunklists (…/chunklist_w123456_b5160000.m3u8) die with the session; use the master instead."""
    return _WOWZA_SESSION_CHUNKLIST.sub("/playlist.m3u8", url)


def name_from_url(url: str, *, last_only: bool = False) -> str:
    """Best-effort channel name from a page/stream URL.

    Stream URLs: the last path segment that is not boilerplate ("playlist", "index"...), else the host label.
    ``last_only`` is for page URLs whose folders are categories (…/Local/Khak.HD): only the final segment counts.
    """
    parts = urlsplit(url)
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if last_only:
        segments = segments[-1:]
    for seg in reversed(segments):
        seg = re.sub(r"\.(m3u8?|html?|php|aspx?)$", "", seg, flags=re.IGNORECASE)
        seg = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", seg)  # HormzyarHD -> Hormzyar HD
        if last_only:
            seg = re.sub(r"^1(?=[A-Za-z]{3})", "", seg)  # the page author prefixes "1" to force sort order: 1afarin.tv
        words = [w for w in re.split(r"[^0-9A-Za-zÀ-ž]+", seg) if w]
        meaningful = [w for w in words if w.lower() not in _GENERIC_WORDS]
        if meaningful and not all(w.isdigit() for w in meaningful):
            return " ".join(words)
    # No host-name fallback: a host label is usually the hosting provider (streambrothers, akamaized), not the channel.
    return ""


class SourceAdapter:
    """Subclasses implement discover(); they call add() for each stream they find."""

    name = "base"

    def __init__(self, source: SourceConfig, fetcher: SourceFetcher):
        self.source = source
        self.fetcher = fetcher
        self.found: dict[str, RawCandidate] = {}
        self.notes: list[str] = []
        self.skipped: dict[str, int] = {}

    # --- helpers for subclasses -------------------------------------------
    def opt(self, key: str, default=None):
        return self.source.options.get(key, default)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def add(self, cand: RawCandidate) -> None:
        cand.url = strip_ephemeral_query(canonical_stream_url(cand.url.strip()))
        if not cand.url.lower().startswith(("http://", "https://")):
            self.skip("not-http-url")
            return
        key = url_key(cand.url)
        known = self.found.get(key)
        if known is None:
            self.found[key] = cand
            # A single rendition (a bare Wowza ".../chunklist.m3u8", a packager ".../NAME-avc1_2500000=10002.m3u8") has a master
            # playlist next to it that offers all of them. Try the master as well - probing decides which works and which is better.
            master = rendition_master(cand.url)
            if master:
                self.add(dataclasses.replace(cand, url=master, note="derived-master"))
            return
        # Same URL seen twice in one source: keep the richer record.
        known.logo_url = known.logo_url or cand.logo_url
        known.page_url = known.page_url or cand.page_url
        known.language = known.language or cand.language
        known.country = known.country or cand.country
        known.tags = tuple(dict.fromkeys(known.tags + cand.tags))
        known.aliases = tuple(dict.fromkeys(known.aliases + (cand.name,) + cand.aliases))
        known.station_id = known.station_id or cand.station_id

    async def discover(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


async def run_adapter(adapter: SourceAdapter) -> SourceResult:
    """Run one adapter; never raises. Failure of one source must not affect the others."""
    started = time.monotonic()
    fetcher = adapter.fetcher
    status = "ok"
    error = ""
    try:
        await adapter.discover()
    except FetchProblem as exc:
        status = "blocked" if exc.kind in {"blocked", "robots"} else "failed"
        error = f"{exc.kind}: {exc.message}"
    except Exception as exc:  # parsing bug, unexpected markup... record it and carry on
        log.exception("source %s crashed", adapter.source.id)
        status = "failed"
        error = f"{type(exc).__name__}: {clip(str(exc), 200)}"
    candidates = list(adapter.found.values())
    stats = fetcher.stats
    if status == "ok" and stats.errors and not candidates:
        status, error = "failed", stats.errors[0]
    elif status == "ok" and stats.errors:
        status = "partial"
        error = f"{len(stats.errors)} page(s) failed; first: {stats.errors[0]}"
    elif status in {"blocked", "failed"} and candidates:
        status = "partial"
    if status == "ok" and not candidates:
        status, error = "failed", "no streams found (page layout changed?)"
    return SourceResult(
        source_id=adapter.source.id,
        status=status,
        candidates=candidates,
        http_status=stats.first_status,
        requests=stats.requests,
        cache_hits=stats.cache_hits,
        duration_ms=int((time.monotonic() - started) * 1000),
        error=error,
        notes=adapter.notes,
        robots_disallowed=stats.robots_disallowed,
        statuses=dict(stats.statuses),
        skipped=dict(adapter.skipped),
    )
