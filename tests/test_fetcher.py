import dataclasses

import pytest

from kurdiptv.config import SourceConfig
from kurdiptv.db import Database
from kurdiptv.fetcher import FetchProblem, RobotsCache, SourceFetcher, find_hls_urls, looks_like_bot_wall
from kurdiptv.net import SafeHttp

import httpx

from helpers import advance

CHALLENGE = "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>challenges.cloudflare.com</body></html>"


def build(web, **source_kw):
    http = SafeHttp(user_agent="KurdishIPTVAggregator/1.0 test", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    src = SourceConfig(id="s", adapter="m3u", url="https://site.test/", min_interval=0.0, **source_kw)
    robots = RobotsCache(http, db)
    return http, db, src, robots


# ---- robots.txt ----------------------------------------------------------------------------------------------------------------

async def test_robots_disallow_rules_apply_to_our_token(web):
    web.add("https://site.test/robots.txt", text="User-agent: *\nDisallow: /private/\n\nUser-agent: KurdishIPTVAggregator\nDisallow: /nope\n",
            content_type="text/plain")
    http, db, src, robots = build(web)
    assert (await robots.allowed("https://site.test/open/page"))[0]
    assert not (await robots.allowed("https://site.test/nope/x"))[0]
    assert (await robots.allowed("https://site.test/private/x"))[0]  # the specific group wins over "*"
    await http.aclose()


async def test_missing_robots_means_everything_is_allowed(web):
    http, db, src, robots = build(web)  # unknown URL -> 404
    assert (await robots.allowed("https://site.test/anything"))[0]
    await http.aclose()


async def test_forbidden_or_challenged_robots_blocks_the_site(web):
    web.add("https://a.test/robots.txt", status=403, text="forbidden")
    web.add("https://b.test/robots.txt", status=403, text=CHALLENGE)
    http, db, src, robots = build(web)
    ok, reason = await robots.allowed("https://a.test/x")
    assert not ok and "forbidden" in reason
    ok, reason = await robots.allowed("https://b.test/x")
    assert not ok and "challenge" in reason
    await http.aclose()


async def test_unreachable_robots_is_retried_after_a_few_minutes(web):
    http, db, src, robots = build(web)
    web.add("https://site.test/robots.txt", status=503, text="down")
    assert not (await robots.allowed("https://site.test/x"))[0]
    web.add("https://site.test/robots.txt", text="User-agent: *\nDisallow:\n", content_type="text/plain")
    assert not (await robots.allowed("https://site.test/x"))[0]  # still cached as unavailable
    advance(400)
    assert (await robots.allowed("https://site.test/x"))[0]
    await http.aclose()


# ---- page fetching -----------------------------------------------------------------------------------------------------------------

async def test_get_checks_robots_and_reports_disallowed_pages(web):
    web.add("https://site.test/robots.txt", text="User-agent: *\nDisallow: /secret\n", content_type="text/plain")
    web.add("https://site.test/secret/a", text="x")
    http, db, src, robots = build(web)
    fetcher = SourceFetcher(http, db, src, robots)
    with pytest.raises(FetchProblem) as exc:
        await fetcher.get("https://site.test/secret/a")
    assert exc.value.kind == "robots" and exc.value.fatal
    assert fetcher.stats.robots_disallowed == ["https://site.test/secret/a"]
    assert web.count("/secret/a") == 0  # never requested
    await http.aclose()


async def test_cloudflare_challenge_is_reported_as_blocked_not_bypassed(web):
    web.add("https://site.test/", status=403, text=CHALLENGE, headers={"cf-mitigated": "challenge"})
    http, db, src, robots = build(web)
    fetcher = SourceFetcher(http, db, src, robots)
    with pytest.raises(FetchProblem) as exc:
        await fetcher.get("https://site.test/")
    assert exc.value.kind == "blocked" and exc.value.fatal
    assert fetcher.stats.blocked
    assert web.count("site.test/") == 2  # robots.txt + one honest attempt; no retries, no tricks
    await http.aclose()


def test_bot_wall_detection():
    assert looks_like_bot_wall(403, httpx.Headers({}), CHALLENGE)
    assert looks_like_bot_wall(200, httpx.Headers({"cf-mitigated": "challenge"}), "")
    assert not looks_like_bot_wall(403, httpx.Headers({}), "Forbidden")
    assert looks_like_bot_wall(200, httpx.Headers({}), CHALLENGE)  # the Cloudflare page is recognised even with a 200
    assert not looks_like_bot_wall(200, httpx.Headers({}), "<html>Just a moment...</html>")  # the weak phrase alone is not enough
    sucuri = "<html><title>You are being redirected...</title><script>var sucuri_cloudproxy_js='';</script></html>"
    assert looks_like_bot_wall(307, httpx.Headers({}), sucuri)  # Sucuri's JS challenge answers 307
    assert not looks_like_bot_wall(200, httpx.Headers({}), "<html>" + "x" * 30_000 + "sucuri_cloudproxy_js</html>")  # a big real page


async def test_pages_are_cached_and_revalidated(web):
    calls = {"n": 0}

    def page(req):
        calls["n"] += 1
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="<html>hello</html>", headers={"etag": '"v1"', "content-type": "text/html"})

    web.add_fn("https://site.test/p", page)
    http, db, src, robots = build(web, page_ttl_hours=1)
    f = SourceFetcher(http, db, src, robots)
    p1 = await f.get("https://site.test/p")
    p2 = await f.get("https://site.test/p")
    assert p1.text == p2.text and p2.from_cache and calls["n"] == 1 and f.stats.cache_hits == 1

    advance(7200)  # TTL over: conditional request, 304 -> cached body reused
    f2 = SourceFetcher(http, db, src, robots)
    p3 = await f2.get("https://site.test/p")
    assert p3.text == "<html>hello</html>" and calls["n"] == 2 and p3.from_cache
    await http.aclose()


async def test_force_refresh_ignores_the_cache(web):
    web.add("https://site.test/p", text="v1")
    http, db, src, robots = build(web, page_ttl_hours=1)
    await SourceFetcher(http, db, src, robots).get("https://site.test/p")
    web.add("https://site.test/p", text="v2")
    page = await SourceFetcher(http, db, src, robots, force_refresh=True).get("https://site.test/p")
    assert page.text == "v2"
    await http.aclose()


async def test_request_budget_is_enforced(web):
    for i in range(5):
        web.add(f"https://site.test/{i}", text="x")
    http, db, src, robots = build(web, max_requests=3)
    f = SourceFetcher(http, db, src, robots)
    for i in range(3):
        await f.get(f"https://site.test/{i}")
    with pytest.raises(FetchProblem) as exc:
        await f.get("https://site.test/3")
    assert exc.value.kind == "budget"
    await http.aclose()


async def test_rate_limit_and_http_errors(web):
    web.add("https://site.test/limited", status=429, headers={"retry-after": "60"}, text="slow down")
    web.add("https://site.test/gone", status=404, text="nope")
    http, db, src, robots = build(web)
    f = SourceFetcher(http, db, src, robots)
    with pytest.raises(FetchProblem) as exc:
        await f.get("https://site.test/limited")
    assert exc.value.kind == "rate_limited" and exc.value.fatal
    with pytest.raises(FetchProblem) as exc:
        await f.get("https://site.test/gone")
    assert exc.value.kind == "http" and not exc.value.fatal
    assert await f.get_optional("https://site.test/gone") is None  # a single bad page is not fatal
    with pytest.raises(FetchProblem):
        await f.get_optional("https://site.test/limited")  # but a fatal one still propagates
    await http.aclose()


async def test_private_targets_are_refused(web):
    web.private_hosts.add("site.test")
    http, db, src, robots = build(web)
    f = SourceFetcher(http, db, src, robots, respect_robots=False)
    with pytest.raises(FetchProblem) as exc:
        await f.get("https://site.test/x")
    assert exc.value.kind == "unsafe"
    await http.aclose()


# ---- URL extraction ----------------------------------------------------------------------------------------------------------------------

def test_find_hls_urls_handles_json_escapes_and_duplicates():
    text = r'''
      var a = "https://cdn.test/live/a.m3u8";
      streams = [{"url":"https:\/\/cdn.test\/live\/b.m3u8?x=1&amp;y=2"}];
      <source src='http://cdn.test:1935/live/a.m3u8'>
      again "https://cdn.test/live/a.m3u8" and an image https://cdn.test/logo.png
    '''
    assert find_hls_urls(text) == [
        "https://cdn.test/live/a.m3u8",
        "https://cdn.test/live/b.m3u8?x=1&y=2",
        "http://cdn.test:1935/live/a.m3u8",
    ]


def test_find_hls_urls_returns_the_stream_inside_a_player_wrapper():
    text = (
        '<iframe src="https://hlsplayer.net/embed?type=m3u8&amp;src=https://stream.badinan.xyz/welat/tv/playlist.m3u8"></iframe>'
        ' <a href="https://player.test/p?file=https%3A%2F%2Fcdn.test%2Flive%2Fx.m3u8&amp;autoplay=1">x</a>'
        ' <a href="https://cdn.test/live/y.m3u8?quality=hd&amp;redirect=1">y</a>'
    )
    assert find_hls_urls(text) == [
        "https://stream.badinan.xyz/welat/tv/playlist.m3u8",
        "https://cdn.test/live/x.m3u8",
        "https://cdn.test/live/y.m3u8?quality=hd&redirect=1",
    ]
