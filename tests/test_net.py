import asyncio
import time

import httpx
import pytest

from kurdiptv.net import FetchError, HostLimiter, SafeHttp, UnsafeURL, is_public_ip

from helpers import FakeWeb


def make(web: FakeWeb, **kw) -> SafeHttp:
    return SafeHttp(user_agent="test-agent", resolver=web.resolver, transport=web.transport(), **kw)


@pytest.mark.parametrize(
    "ip,public",
    [("93.184.216.34", True), ("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.0.9", False),
     ("172.16.5.5", False), ("169.254.169.254", False), ("100.64.0.1", False), ("::1", False), ("fe80::1", False),
     ("::ffff:127.0.0.1", False), ("0.0.0.0", False), ("not-an-ip", False)],
)
def test_is_public_ip(ip, public):
    assert is_public_ip(ip) is public


async def test_get_returns_body_and_timings(web):
    web.add("https://a.test/x.m3u8", text="#EXTM3U\n", headers={"content-type": "application/vnd.apple.mpegurl; charset=utf-8"})
    http = make(web)
    res = await http.get("https://a.test/x.m3u8")
    assert res.status == 200 and res.text() == "#EXTM3U\n"
    assert res.content_type == "application/vnd.apple.mpegurl"
    assert res.elapsed_ms >= 0 and res.ttfb_ms >= 0 and not res.truncated
    assert web.request_headers[0]["user-agent"] == "test-agent"
    await http.aclose()


@pytest.mark.parametrize(
    "url",
    ["ftp://a.test/x", "file:///etc/passwd", "gopher://a.test/", "http://user:pw@a.test/x", "http:///nohost",
     "http://127.0.0.1/admin", "http://192.168.1.1/", "http://[::1]:8080/", "http://169.254.169.254/latest/meta-data/"],
)
async def test_unsafe_urls_are_refused_before_any_request(web, url):
    http = make(web)
    with pytest.raises(UnsafeURL):
        await http.get(url)
    assert web.calls == []
    await http.aclose()


async def test_host_resolving_to_a_private_address_is_refused(web):
    web.private_hosts.add("internal.test")
    http = make(web)
    with pytest.raises(UnsafeURL):
        await http.get("http://internal.test/stream.m3u8")
    assert web.calls == []
    await http.aclose()


async def test_allow_private_switches_the_guard_off(web):
    web.private_hosts.add("internal.test")
    web.add("http://internal.test/a", text="ok")
    http = make(web, allow_private=True)
    assert (await http.get("http://internal.test/a")).status == 200
    await http.aclose()


async def test_redirect_to_a_private_address_is_blocked(web):
    web.add("https://a.test/go", status=302, headers={"location": "http://127.0.0.1:8080/secret"})
    http = make(web)
    with pytest.raises(UnsafeURL):
        await http.get("https://a.test/go")
    assert not any("127.0.0.1" in u for _, u in web.calls)
    await http.aclose()


async def test_redirects_are_followed_and_recorded(web):
    web.add("https://a.test/1", status=301, headers={"location": "/2"})
    web.add("https://a.test/2", status=302, headers={"location": "https://b.test/final"})
    web.add("https://b.test/final", text="done")
    http = make(web)
    res = await http.get("https://a.test/1")
    assert res.text() == "done" and res.url == "https://b.test/final"
    assert res.redirects == ["https://a.test/2", "https://b.test/final"]
    await http.aclose()


async def test_redirect_loop_is_cut(web):
    web.add("https://a.test/loop", status=302, headers={"location": "https://a.test/loop"})
    http = make(web, max_redirects=3)
    with pytest.raises(FetchError) as exc:
        await http.get("https://a.test/loop")
    assert exc.value.kind == "redirects"
    await http.aclose()


async def test_body_is_cut_at_max_bytes(web):
    web.add("https://a.test/big", content=b"x" * 100_000)
    http = make(web)
    res = await http.get("https://a.test/big", max_bytes=1000)
    assert len(res.body) == 1000 and res.truncated
    await http.aclose()


async def test_dns_failure_is_classified(web):
    web.dns_fail.add("dead.test")
    http = make(web)
    with pytest.raises(FetchError) as exc:
        await http.get("https://dead.test/a")
    assert exc.value.kind == "dns"
    await http.aclose()


@pytest.mark.parametrize(
    "raised,kind",
    [(httpx.ConnectTimeout("t"), "connect-timeout"), (httpx.ReadTimeout("t"), "read-timeout"),
     (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "tls"),
     (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "dns"), (httpx.ConnectError("refused"), "connect"),
     (httpx.RemoteProtocolError("bad"), "network")],
)
async def test_transport_errors_get_stable_labels(web, raised, kind):
    def boom(request):
        raise raised

    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=httpx.MockTransport(boom))
    with pytest.raises(FetchError) as exc:
        await http.get("https://a.test/x")
    assert exc.value.kind == kind
    await http.aclose()


async def test_total_deadline_bounds_a_slow_server(web):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text="late")

    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=httpx.MockTransport(slow))
    t0 = time.monotonic()
    with pytest.raises(FetchError) as exc:
        await http.get("https://a.test/x", total_timeout=0.3)
    assert exc.value.kind == "timeout" and time.monotonic() - t0 < 2
    await http.aclose()


async def test_range_header_is_sent(web):
    web.add("https://a.test/seg.ts", content=b"abc")
    http = make(web)
    await http.get("https://a.test/seg.ts", byte_range=(0, 1023))
    assert web.request_headers[0]["range"] == "bytes=0-1023"
    await http.aclose()


async def test_host_limiter_caps_concurrency_and_spaces_requests():
    limiter = HostLimiter(max_concurrent=2, min_interval=0.05)
    active = peak = 0

    async def work():
        nonlocal active, peak
        async with limiter.slot("h"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    t0 = time.monotonic()
    await asyncio.gather(*[work() for _ in range(6)])
    assert peak <= 2
    assert time.monotonic() - t0 >= 0.05 * 5 * 0.9  # six requests need five gaps
    # a different host is independent
    t1 = time.monotonic()
    await asyncio.gather(*[limiter_slot(limiter, f"host{i}") for i in range(4)])
    assert time.monotonic() - t1 < 0.2


async def limiter_slot(limiter, host):
    async with limiter.slot(host):
        await asyncio.sleep(0)
