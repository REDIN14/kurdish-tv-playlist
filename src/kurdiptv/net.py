"""Bounded, SSRF-safe HTTP fetching plus per-host politeness limits."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeURL(Exception):
    """The URL must not be fetched (bad scheme, credentials, private/loopback target...)."""


class FetchError(Exception):
    """Network-level failure. `kind` is a short stable label used in reports and health data."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.message = message


@dataclass
class Fetched:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes
    truncated: bool = False
    elapsed_ms: int = 0
    ttfb_ms: int = 0
    redirects: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self, encoding: str | None = None) -> str:
        enc = encoding or self._charset() or "utf-8"
        if enc.lower() in {"utf-8", "utf8"}:
            enc = "utf-8-sig"  # tolerate a BOM
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8-sig", errors="replace")

    def _charset(self) -> str | None:
        ct = self.headers.get("content-type", "")
        for part in ct.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                return value.strip("\"'")
        return None


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


class SafeHttp:
    """Thin wrapper over httpx.AsyncClient.

    * only http/https, no credentials in URLs
    * every hop (including redirects) must resolve to public addresses unless allow_private
    * bodies are read incrementally and cut at ``max_bytes``; total time is bounded
    """

    def __init__(
        self,
        *,
        user_agent: str,
        connect_timeout: float = 8.0,
        read_timeout: float = 20.0,
        max_redirects: int = 5,
        allow_private: bool = False,
        verify_tls: bool = True,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve_ttl: float = 300.0,
    ):
        self.user_agent = user_agent
        self.max_redirects = max_redirects
        self.allow_private = allow_private
        self._resolver = resolver or system_resolver
        self._resolve_ttl = resolve_ttl
        self._resolved: dict[tuple[str, int], tuple[float, list[str]]] = {}
        self._timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            timeout=self._timeout,
            follow_redirects=False,
            verify=verify_tls,
            transport=transport,
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- guard -------------------------------------------------------------
    async def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise UnsafeURL(f"unsupported scheme: {parts.scheme or '(none)'}")
        host = parts.hostname
        if not host:
            raise UnsafeURL("URL has no host")
        if parts.username or parts.password:
            raise UnsafeURL("credentials in URL")
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise UnsafeURL(f"bad port: {exc}") from exc
        if port == 0:
            raise UnsafeURL("port 0")
        if self.allow_private:
            return
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not is_public_ip(host):
                raise UnsafeURL(f"non-public address: {host}")
            return
        key = (host.lower(), port)
        cached = self._resolved.get(key)
        t = time.monotonic()
        if cached and t - cached[0] < self._resolve_ttl:
            addrs = cached[1]
        else:
            try:
                addrs = await self._resolver(host, port)
            except (OSError, UnicodeError) as exc:
                raise FetchError("dns", str(exc)[:120]) from exc
            self._resolved[key] = (t, addrs)
        if not addrs:
            raise FetchError("dns", "no addresses")
        bad = [a for a in addrs if not is_public_ip(a)]
        if bad:
            raise UnsafeURL(f"{host} resolves to non-public address {bad[0]}")

    # --- fetch -------------------------------------------------------------
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = 2_000_000,
        timeout: float | None = None,
        total_timeout: float | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> Fetched:
        hdrs = dict(headers or {})
        if byte_range is not None:
            hdrs["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        req_timeout = httpx.Timeout(timeout) if timeout else self._timeout
        deadline = total_timeout or ((timeout or self._timeout.read or 20.0) * 2.5)
        started = time.monotonic()
        try:
            async with asyncio.timeout(deadline):
                return await self._get(url, hdrs, max_bytes, req_timeout, started)
        except TimeoutError as exc:
            raise FetchError("timeout", f"exceeded {deadline:.0f}s") from exc

    async def _get(self, url, hdrs, max_bytes, req_timeout, started) -> Fetched:
        current = url
        redirects: list[str] = []
        for _ in range(self.max_redirects + 1):
            await self.check_url(current)
            try:
                async with self._client.stream("GET", current, headers=hdrs, timeout=req_timeout) as resp:
                    ttfb = int((time.monotonic() - started) * 1000)
                    if resp.status_code in {301, 302, 303, 307, 308} and resp.headers.get("location"):
                        current = urljoin(current, resp.headers["location"])
                        redirects.append(current)
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    async for chunk in resp.aiter_bytes(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            keep = len(chunk) - (total - max_bytes)
                            if keep > 0:
                                chunks.append(chunk[:keep])
                            truncated = True
                            break
                        chunks.append(chunk)
                    return Fetched(
                        url=current,
                        status=resp.status_code,
                        headers=resp.headers,
                        body=b"".join(chunks),
                        truncated=truncated,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        ttfb_ms=ttfb,
                        redirects=redirects,
                    )
            except httpx.ConnectTimeout as exc:
                raise FetchError("connect-timeout", str(exc)[:120]) from exc
            except httpx.ReadTimeout as exc:
                raise FetchError("read-timeout", str(exc)[:120]) from exc
            except httpx.PoolTimeout as exc:
                raise FetchError("pool-timeout", str(exc)[:120]) from exc
            except httpx.ConnectError as exc:
                text = str(exc)
                low = text.lower()
                if "certificate" in low or "ssl" in low or "tls" in low:
                    raise FetchError("tls", text[:120]) from exc
                if "getaddrinfo" in low or "name or service" in low or "nodename" in low or "name resolution" in low:
                    raise FetchError("dns", text[:120]) from exc
                raise FetchError("connect", text[:120]) from exc
            except httpx.TooManyRedirects as exc:
                raise FetchError("redirects", str(exc)[:120]) from exc
            except httpx.UnsupportedProtocol as exc:
                raise UnsafeURL(str(exc)) from exc
            except httpx.InvalidURL as exc:
                raise UnsafeURL(f"invalid URL: {exc}") from exc
            except httpx.HTTPError as exc:
                raise FetchError("network", f"{type(exc).__name__}: {str(exc)[:100]}") from exc
        raise FetchError("redirects", f"more than {self.max_redirects} redirects")


class HostLimiter:
    """Caps concurrent requests per host and spaces them out (politeness / rate limit)."""

    def __init__(self, max_concurrent: int = 2, min_interval: float = 0.0):
        self.max_concurrent = max(1, max_concurrent)
        self.min_interval = max(0.0, min_interval)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_ok: dict[str, float] = {}

    @asynccontextmanager
    async def slot(self, host: str):
        sem = self._sems.setdefault(host, asyncio.Semaphore(self.max_concurrent))
        async with sem:
            if self.min_interval:
                lock = self._locks.setdefault(host, asyncio.Lock())
                async with lock:
                    wait = self._next_ok.get(host, 0.0) - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._next_ok[host] = time.monotonic() + self.min_interval
            yield
