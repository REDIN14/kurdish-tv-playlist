"""Test support: an in-memory internet, HLS/PNG builders."""

from __future__ import annotations

import io
import json as jsonlib
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import httpx
from PIL import Image

PUBLIC_IP = "93.184.216.34"


def advance(seconds: float) -> None:
    """Move the application clock forward (stacks with earlier calls)."""
    from kurdiptv import util

    previous = util._clock
    util.set_clock(lambda: previous() + seconds)


def png_bytes(size=(120, 90), color=(200, 30, 30, 255)) -> bytes:
    img = Image.new("RGBA", size, color)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def ts_bytes(packets: int = 40) -> bytes:
    """Looks like MPEG-TS to the sniffer: sync byte every 188 bytes."""
    return (b"\x47" + b"\x00" * 187) * packets


def master_playlist(variants: list[tuple[str, int, str]]) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for uri, bandwidth, resolution in variants:
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={resolution},CODECS="avc1.64001f,mp4a.40.2"')
        lines.append(uri)
    return "\n".join(lines) + "\n"


def media_playlist(seq: int = 100, count: int = 5, duration: int = 6, ended: bool = False, prefix: str = "seg") -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{duration}", f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
    for i in range(count):
        lines += [f"#EXTINF:{duration}.0,", f"{prefix}{seq + i}.ts"]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@dataclass
class FakeStream:
    """A live HLS origin whose state tests can change (dies, advances, changes quality)."""

    url: str  # master or media playlist URL
    variants: list[tuple[str, int, str]] = field(default_factory=lambda: [("v720.m3u8", 2_500_000, "1280x720")])
    master: bool = True
    alive: bool = True
    seq: int = 100
    ended: bool = False
    advancing: bool = True
    status: int = 200
    delay: float = 0.0
    hits: int = 0
    true_height: int | None = None  # what the video really is; a fake ffprobe reads it back from the segment
    service: str = ""  # what the MPEG-TS stream announces itself as (SDT service_name)

    def base(self) -> str:
        return self.url.rsplit("/", 1)[0] + "/"

    @property
    def current_seq(self) -> int:
        """A live playlist moves on with the (application) clock unless the stream is frozen."""
        from kurdiptv import util

        return self.seq + (int(util.now() // 6) if self.advancing else 0)


class FakeWeb:
    """Maps URLs to responses. Unknown URLs answer 404. Records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.streams: list[FakeStream] = []
        self.calls: list[tuple[str, str]] = []
        self.request_headers: list[httpx.Headers] = []
        self.dns_fail: set[str] = set()
        self.private_hosts: set[str] = set()
        self.offline = False

    # --- registration --------------------------------------------------------------------------------------------------
    def add(self, url: str, *, text: str | None = None, json=None, content: bytes | None = None, status: int = 200,
            headers: dict | None = None, content_type: str | None = None) -> None:
        if json is not None:
            content, content_type = jsonlib.dumps(json).encode(), content_type or "application/json"
        elif text is not None:
            content, content_type = text.encode("utf-8"), content_type or "text/html; charset=utf-8"
        hdrs = dict(headers or {})
        if content_type and "content-type" not in {k.lower() for k in hdrs}:
            hdrs["content-type"] = content_type
        body = content or b""
        self.routes[url] = lambda req: httpx.Response(status, content=body, headers=hdrs)

    def add_fn(self, url: str, fn: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[url] = fn

    def add_stream(self, url: str, **kwargs) -> FakeStream:
        stream = FakeStream(url=url, **kwargs)
        self.streams.append(stream)
        base = stream.base()

        def guard(req):
            stream.hits += 1
            if not stream.alive:
                return httpx.Response(404)
            if stream.status != 200:
                return httpx.Response(stream.status)
            return None

        def top(req):
            return guard(req) or httpx.Response(
                200,
                text=master_playlist(stream.variants) if stream.master else media_playlist(stream.current_seq, ended=stream.ended),
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )

        self.routes[url] = top
        names = [v[0] for v in stream.variants] if stream.master else []
        for name in names:
            def media(req, _n=name):
                return guard(req) or httpx.Response(200, text=media_playlist(stream.current_seq, ended=stream.ended, prefix=_n.replace(".m3u8", "_")))
            self.routes[base + name] = media
        # segments: matched by prefix in the handler below
        def segment(req):
            marker = f"H={stream.true_height};".encode() if stream.true_height else b""
            marker += f"S={stream.service};".encode() if stream.service else b""
            return guard(req) or httpx.Response(200, content=ts_bytes() + marker)

        self.routes[("segments", base)] = segment  # type: ignore[index]
        return stream

    # --- transport ---------------------------------------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        self.request_headers.append(request.headers)
        if self.offline:
            raise httpx.ConnectError("network is down", request=request)
        host = request.url.host
        fn = self.routes.get(url)
        if fn is None and request.url.query:
            fn = self.routes.get(url.split("?", 1)[0])  # route registered without the query string
        if fn is None and url.endswith(".ts"):
            fn = self.routes.get(("segments", url.rsplit("/", 1)[0] + "/"))  # type: ignore[arg-type]
        if fn is None:
            return httpx.Response(404, text="not found")
        return fn(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    async def resolver(self, host: str, port: int) -> list[str]:
        if host in self.dns_fail:
            raise OSError(f"[Errno 11001] getaddrinfo failed for {host}")
        if host in self.private_hosts:
            return ["10.0.0.5"]
        return [PUBLIC_IP]

    def count(self, substring: str) -> int:
        return sum(1 for _, u in self.calls if substring in u)

    def host_of(self, url: str) -> str:
        return urlsplit(url).hostname or ""
