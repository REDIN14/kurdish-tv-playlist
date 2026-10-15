"""Polite page fetching for source adapters: robots.txt, budgets, spacing, caching, bot-wall detection."""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import parse_qs, urlsplit

from .config import SourceConfig
from .db import Database
from .net import FetchError, Fetched, HostLimiter, SafeHttp, UnsafeURL
from .util import clip, now

ROBOTS_TOKEN = "KurdishIPTVAggregator"

_BOT_WALL_MARKERS = (
    "just a moment...",
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "ddos protection by",
    "__cf_chl_",
    "sucuri_cloudproxy_js",  # Sucuri: "You are being redirected... Javascript is required"
    "_incapsula_resource",
    "incapsula incident id",
)
# These interstitials are served with unusual status codes (Sucuri answers 307), so the marker check is not limited
# to 401/403/429/503 - but only the strong, specific markers count outside those statuses.
_STRONG_MARKERS = ("sucuri_cloudproxy_js", "_incapsula_resource", "challenges.cloudflare.com", "cf-browser-verification", "__cf_chl_")


class FetchProblem(Exception):
    """A page could not be used. kind: blocked | robots | budget | rate_limited | http | network | unsafe"""

    FATAL = {"blocked", "robots", "budget", "rate_limited"}

    def __init__(self, kind: str, message: str, status: int | None = None, url: str = ""):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.status = status
        self.url = url

    @property
    def fatal(self) -> bool:
        return self.kind in self.FATAL


def looks_like_bot_wall(status: int, headers, body_text: str) -> bool:
    """True for Cloudflare-style interstitials that ask a browser to prove it is human."""
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if status in {401, 403, 429, 503}:
        low = body_text[:6000].lower()
        if any(marker in low for marker in _BOT_WALL_MARKERS):
            return True
    elif status in {200, 202, 301, 302, 307, 308} and len(body_text) < 20_000:
        low = body_text[:6000].lower()
        if any(marker in low for marker in _STRONG_MARKERS):
            return True
    return False


@dataclass
class Page:
    url: str
    status: int
    text: str
    content_type: str
    from_cache: bool = False

    def json(self):
        return json.loads(self.text)


@dataclass
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    statuses: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    robots_disallowed: list[str] = field(default_factory=list)
    blocked: str = ""
    first_status: int | None = None

    def note_status(self, status: int) -> None:
        self.statuses[status] = self.statuses.get(status, 0) + 1
        if self.first_status is None:
            self.first_status = status

    def error(self, message: str) -> None:
        if len(self.errors) < 25:
            self.errors.append(clip(message, 240))


class RobotsCache:
    """robots.txt per origin, cached in memory and in the HTTP cache (24 h)."""

    def __init__(self, http: SafeHttp, db: Database, ttl_hours: float = 24.0):
        self.http = http
        self.db = db
        self.ttl = ttl_hours * 3600
        self._mem: dict[str, tuple[float, robotparser.RobotFileParser | None, str]] = {}

    async def allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self._mem.get(origin)
        if entry is None or now() - entry[0] > (self.ttl if entry[1] is not None else 300):
            entry = await self._load(origin)
            self._mem[origin] = entry
        _, parser, reason = entry
        if parser is None:
            return False, reason
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if parser.can_fetch(ROBOTS_TOKEN, f"{origin}{path}"):
            return True, ""
        return False, "disallowed by robots.txt"

    async def _load(self, origin: str) -> tuple[float, robotparser.RobotFileParser | None, str]:
        url = origin + "/robots.txt"
        key = "robots:" + origin
        row = self.db.one("SELECT fetched_at, body FROM http_cache WHERE url = ?", (key,))
        if row is not None and now() - row["fetched_at"] < self.ttl and row["body"] is not None:
            return now(), self._parse(zlib.decompress(row["body"]).decode("utf-8", "replace")), ""
        try:
            res = await self.http.get(url, max_bytes=200_000, total_timeout=20)
        except (FetchError, UnsafeURL) as exc:
            return now(), None, f"robots.txt unreachable ({exc})"
        text = res.text()
        if res.status == 200:
            if looks_like_bot_wall(res.status, res.headers, text):
                return now(), None, "robots.txt answered with a bot challenge"
            self.db.execute(
                "INSERT INTO http_cache (url, fetched_at, status, content_type, body) VALUES (?, ?, 200, 'text/plain', ?) "
                "ON CONFLICT (url) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body",
                (key, now(), zlib.compress(text.encode("utf-8"))),
            )
            return now(), self._parse(text), ""
        if res.status in {401, 403}:
            if looks_like_bot_wall(res.status, res.headers, text):
                return now(), None, f"robots.txt answered with a bot challenge (HTTP {res.status})"
            return now(), None, f"robots.txt forbidden (HTTP {res.status})"
        if 400 <= res.status < 500:
            return now(), self._parse(""), ""  # no robots.txt: everything allowed
        return now(), None, f"robots.txt unavailable (HTTP {res.status})"

    @staticmethod
    def _parse(text: str) -> robotparser.RobotFileParser:
        parser = robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        return parser


class SourceFetcher:
    """What adapters use to read pages: honours robots.txt, request budget and spacing."""

    def __init__(
        self,
        http: SafeHttp,
        db: Database,
        source: SourceConfig,
        robots: RobotsCache,
        *,
        respect_robots: bool = True,
        force_refresh: bool = False,
    ):
        self.http = http
        self.db = db
        self.source = source
        self.robots = robots
        self.respect_robots = respect_robots
        self.force_refresh = force_refresh
        self.stats = FetchStats()
        self._limiter = HostLimiter(max_concurrent=1, min_interval=source.min_interval)

    # --- public -------------------------------------------------------------
    async def get(
        self,
        url: str,
        *,
        ttl_hours: float | None = None,
        max_bytes: int = 3_000_000,
        headers: dict[str, str] | None = None,
    ) -> Page:
        ttl = (self.source.page_ttl_hours if ttl_hours is None else ttl_hours) * 3600
        cached = self.db.one(
            "SELECT fetched_at, status, etag, last_modified, content_type, body FROM http_cache WHERE url = ?", (url,)
        )
        if cached is not None and not self.force_refresh and ttl > 0 and now() - cached["fetched_at"] < ttl:
            self.stats.cache_hits += 1
            return self._from_cache(url, cached)

        if self.stats.requests >= self.source.max_requests:
            raise FetchProblem("budget", f"request budget of {self.source.max_requests} reached", url=url)

        if self.respect_robots:
            ok, reason = await self.robots.allowed(url)
            if not ok:
                self.stats.robots_disallowed.append(url)
                kind = "blocked" if "challenge" in reason else "robots"
                if kind == "blocked":
                    self.stats.blocked = reason
                raise FetchProblem(kind, reason, url=url)

        req_headers = dict(headers or {})
        if cached is not None and cached["body"] is not None:
            if cached["etag"]:
                req_headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                req_headers["If-Modified-Since"] = cached["last_modified"]

        host = urlsplit(url).hostname or ""
        self.stats.requests += 1
        try:
            async with self._limiter.slot(host):
                res = await self.http.get(url, headers=req_headers, max_bytes=max_bytes)
        except UnsafeURL as exc:
            self.stats.error(f"{url}: refused ({exc})")
            raise FetchProblem("unsafe", str(exc), url=url) from exc
        except FetchError as exc:
            self.stats.error(f"{url}: {exc}")
            raise FetchProblem("network", str(exc), url=url) from exc

        self.stats.note_status(res.status)
        if res.status == 304 and cached is not None:
            self.db.execute("UPDATE http_cache SET fetched_at = ? WHERE url = ?", (now(), url))
            self.stats.cache_hits += 1
            return self._from_cache(url, cached)

        text = res.text()
        if looks_like_bot_wall(res.status, res.headers, text):
            self.stats.blocked = f"bot challenge (HTTP {res.status})"
            self.stats.error(f"{url}: bot challenge (HTTP {res.status})")
            raise FetchProblem("blocked", f"site answered with a bot challenge (HTTP {res.status})", res.status, url)
        if res.status == 429:
            retry = res.headers.get("retry-after", "?")
            self.stats.error(f"{url}: rate limited (retry-after {retry})")
            raise FetchProblem("rate_limited", f"HTTP 429 (retry-after {retry})", 429, url)
        if res.status >= 400:
            self.stats.error(f"{url}: HTTP {res.status}")
            raise FetchProblem("http", f"HTTP {res.status}", res.status, url)

        self._store(url, res, text)
        return Page(url=res.url, status=res.status, text=text, content_type=res.content_type)

    async def get_optional(self, url: str, **kwargs) -> Page | None:
        """Like get(), but a failure of this one page is recorded and swallowed unless it is fatal for the source."""
        try:
            return await self.get(url, **kwargs)
        except FetchProblem as exc:
            if exc.fatal:
                raise
            self.stats.error(f"{url}: {exc.message}")
            return None

    # --- cache --------------------------------------------------------------
    def _from_cache(self, url: str, row) -> Page:
        body = zlib.decompress(row["body"]) if row["body"] else b""
        return Page(url=url, status=row["status"], text=body.decode("utf-8", "replace"), content_type=row["content_type"] or "", from_cache=True)

    def _store(self, url: str, res: Fetched, text: str) -> None:
        if res.truncated:
            return
        self.db.execute(
            "INSERT INTO http_cache (url, fetched_at, status, etag, last_modified, content_type, body) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (url) DO UPDATE SET fetched_at = excluded.fetched_at, status = excluded.status, etag = excluded.etag, "
            "last_modified = excluded.last_modified, content_type = excluded.content_type, body = excluded.body",
            (
                url,
                now(),
                res.status,
                res.headers.get("etag"),
                res.headers.get("last-modified"),
                res.content_type,
                zlib.compress(text.encode("utf-8"), 6),
            ),
        )


_M3U8_RE = re.compile(
    r"""https?:(?:\\?/){2}(?:[^\s"'<>\\)\]]|\\/)+?\.m3u8(?:\?(?:[^\s"'<>\\)\]]|\\/)*)?""", re.IGNORECASE
)


_WRAPPER_PARAMS = {"src", "url", "file", "source", "stream", "link", "video", "m3u8"}


def _unwrap_player_url(url: str) -> str:
    """A third-party player page that takes the stream as a parameter (hlsplayer.net/embed?type=m3u8&src=https://x/y.m3u8)
    is not the stream: return the stream address inside it."""
    query = urlsplit(url).query
    if not query:
        return url
    for key, values in parse_qs(query).items():
        if key.lower() in _WRAPPER_PARAMS:
            for value in values:
                if value.lower().startswith(("http://", "https://")) and ".m3u8" in value.lower():
                    return value
    return url


def find_hls_urls(text: str) -> list[str]:
    """All .m3u8 URLs in a block of HTML/JS (handles JSON-escaped slashes), in order, de-duplicated. A player page that wraps
    the stream address in its own query string is replaced by the stream address it carries."""
    seen: dict[str, None] = {}
    for match in _M3U8_RE.finditer(text):
        url = match.group(0).replace("\\/", "/").replace("&amp;", "&")
        seen.setdefault(_unwrap_player_url(url), None)
    return list(seen)
