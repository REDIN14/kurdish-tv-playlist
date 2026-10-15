import dataclasses

import httpx
import pytest

from kurdiptv.adapters import ADAPTERS, run_adapter
from kurdiptv.adapters.base import canonical_stream_url, name_from_url
from kurdiptv.config import SourceConfig
from kurdiptv.db import Database
from kurdiptv.fetcher import RobotsCache, SourceFetcher
from kurdiptv.net import SafeHttp

from conftest import fixture_text

CHALLENGE = "<html><head><title>Just a moment...</title></head><body>challenges.cloudflare.com</body></html>"


async def run(web, settings, source_id, **overrides):
    src = next(s for s in settings.sources if s.id == source_id)
    if overrides:
        src = dataclasses.replace(src, **overrides)
    http = SafeHttp(user_agent="test", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    fetcher = SourceFetcher(http, db, src, RobotsCache(http, db))
    result = await run_adapter(ADAPTERS[src.adapter](src, fetcher))
    await http.aclose()
    return result


def by_url(result):
    return {c.url: c for c in result.candidates}


# ---- helpers ------------------------------------------------------------------------------------------------------------

def test_name_from_url():
    assert name_from_url("http://avrstream.com:1935/live/HormzyarHD/playlist.m3u8") == "Hormzyar HD"
    assert name_from_url("http://x/live/Zoom/index.m3u8") == "Zoom"
    assert name_from_url("http://sem1r.wapsite.me/Local/Khak.HD", last_only=True) == "Khak HD"
    assert name_from_url("http://sem1r.wapsite.me/Local/1mahabad", last_only=True) == "mahabad"
    assert name_from_url("http://sem1r.wapsite.me/Local/1.HD", last_only=True) == ""
    assert name_from_url("https://media.streambrothers.com:19360/8116/8116.m3u8") == ""  # a host is not a channel name
    assert name_from_url("http://sem1r.wapsite.me/Hd/Kurdsat_n", last_only=True) == "Kurdsat n"


def test_wowza_session_chunklists_become_the_master():
    assert canonical_stream_url("http://h:80/sterktv/smil:sterk.smil/chunklist_w208463919_b5160000_sleng.m3u8") == (
        "http://h:80/sterktv/smil:sterk.smil/playlist.m3u8"
    )
    assert canonical_stream_url("https://h/a/index.m3u8") == "https://h/a/index.m3u8"


# ---- kurdtvs.net ------------------------------------------------------------------------------------------------------------

def routes_kurdtvs(web):
    web.add("https://kurdtvs.net/robots.txt", text="User-agent: *\nDisallow: \nDisallow: /cgi-bin/\n", content_type="text/plain")
    web.add("https://kurdtvs.net/iptv.php?format=m3u", text=fixture_text("kurdtvs_iptv.m3u"), content_type="audio/x-mpegurl; charset=utf-8")
    web.add("https://kurdtvs.net/", text=fixture_text("kurdtvs_home.html"))
    web.add("https://kurdtvs.net/zagros-tv", text=fixture_text("kurdtvs_page_zagros.html"))
    web.add("https://kurdtvs.net/kurdistan24-tv", text=fixture_text("kurdtvs_page_k24.html"))


async def test_kurdtvs_reads_playlist_and_channel_pages(web, settings):
    routes_kurdtvs(web)
    res = await run(web, settings, "kurdtvs")
    assert res.status in {"ok", "partial"}  # /rudaw-tv-hd is not routed -> one page fails, the rest survives
    cands = by_url(res)
    rudaw = cands["https://live.rudaw.net/hls/rudaw-tv/master.m3u8"]
    assert rudaw.name == "Rudaw TV Live" and rudaw.logo_url == "https://kurdtvs.net/uploads/rudawhd.png"
    assert "Kurdish" in rudaw.tags and rudaw.name_confidence == "high"
    assert "Kurdsat Tv (Alt 2)" in {c.name for c in res.candidates}  # alternatives are kept as separate URLs
    # the channel page contributes a clean title, a logo and a stream the playlist did not have
    backup = cands["https://zagros-backup.example.tv/live/zagros.m3u8"]
    assert backup.name == "Zagros TV" and backup.logo_url == "https://kurdtvs.net/uploads/zagros.png"
    # the same URL from playlist and page is one candidate carrying both names
    zagros = cands["https://5a3ed7a72ed4b.streamlock.net/zagrostv/SMIL:myStream.smil/playlist.m3u8"]
    assert "Zagros TV" in {zagros.name, *zagros.aliases}
    # non-channel links are never fetched
    assert web.count("about.php") == 0 and web.count("/blog") == 0 and web.count("style.css") == 0
    assert res.skipped.get("page-without-player") == 1  # the YouTube-embed page


async def test_kurdtvs_survives_a_missing_playlist(web, settings):
    routes_kurdtvs(web)
    del web.routes["https://kurdtvs.net/iptv.php?format=m3u"]  # playlist 404s
    res = await run(web, settings, "kurdtvs")
    assert res.candidates  # channel pages still deliver
    assert any("playlist unavailable" in n for n in res.notes)


async def test_kurdtvs_down_entirely_is_a_failed_source_not_a_crash(web, settings):
    web.add("https://kurdtvs.net/robots.txt", text="", content_type="text/plain")
    web.add("https://kurdtvs.net/iptv.php?format=m3u", status=503, text="down")
    web.add("https://kurdtvs.net/", status=503, text="down")
    res = await run(web, settings, "kurdtvs")
    assert res.status == "failed" and not res.candidates and res.error


# ---- kurditv.com -----------------------------------------------------------------------------------------------------------------

def routes_kurditv(web):
    web.add("https://kurditv.com/robots.txt", text="User-agent: *\nDisallow:\n", content_type="text/plain")
    web.add("https://kurditv.com/wp-json/wp/v2/categories?per_page=100&_fields=id,name,slug,count", json=[
        {"id": 3, "name": "weşana zindî", "slug": "wesana-zindi", "count": 3},
        {"id": 13, "name": "Arabic TV Live", "slug": "arabic-tv-live", "count": 1},
    ])
    web.add("https://kurditv.com/wp-json/wp/v2/posts?per_page=100&page=1&_fields=id,link,slug,title,categories", json=[
        {"id": 1, "link": "https://kurditv.com/1/", "slug": "rudaw-tv-zindi", "title": {"rendered": "Rudaw TV  ZIND&Icirc;"}, "categories": [3]},
        {"id": 2, "link": "https://kurditv.com/2/", "slug": "med", "title": {"rendered": "عالم الموسيقى الكردية مع قناة Med Muzik TV"}, "categories": [3]},
        {"id": 3, "link": "https://kurditv.com/3/", "slug": "ard", "title": {"rendered": "Das Erste LIVE"}, "categories": [13]},
        {"id": 4, "link": "https://kurditv.com/4/", "slug": "empty", "title": {"rendered": "Empty Post"}, "categories": [3]},
    ])

    def vembed(post_id, body):
        def fn(request):
            if request.headers.get("referer") != f"https://kurditv.com/embed/{post_id}/":
                return httpx.Response(403, text="forbidden")  # the real endpoint refuses requests without the embed Referer
            return httpx.Response(200, json=body)
        web.add_fn(f"https://kurditv.com/wp-admin/admin-ajax.php?action=vembed_data&post_id={post_id}", fn)

    vembed(1, {"data": {"sources": [{"src": "https://live.rudaw.net/hls/rudaw-tv/master.m3u8"}]}})
    vembed(2, {"data": {"sources": [{"name": "HD", "src": "https://6476e46b58f91.streamlock.net/music/MUSIC2402/chunklist.m3u8"},
                                    {"src": "https://youtube.com/watch?v=abc"}]}})
    vembed(4, {"data": {"sources": []}})


async def test_kurditv_uses_rest_api_and_referer(web, settings):
    routes_kurditv(web)
    res = await run(web, settings, "kurditv")
    assert res.status == "ok", res.error
    cands = by_url(res)
    rudaw = cands["https://live.rudaw.net/hls/rudaw-tv/master.m3u8"]
    assert rudaw.name == "Rudaw TV  ZINDÎ" and rudaw.tags == ("weşana zindî",) and rudaw.page_url == "https://kurditv.com/1/"
    med = cands["https://6476e46b58f91.streamlock.net/music/MUSIC2402/chunklist.m3u8"]
    assert med.name == "Med Muzik TV"  # Latin channel name pulled out of the Arabic sentence
    assert any("عالم" in a for a in med.aliases)
    # a bare Wowza chunklist also gets its master tried
    assert "https://6476e46b58f91.streamlock.net/music/MUSIC2402/playlist.m3u8" in cands
    assert cands["https://6476e46b58f91.streamlock.net/music/MUSIC2402/playlist.m3u8"].note == "derived-master"
    # other categories (Arabic/Turkish/German TV) and non-HLS sources are out
    assert not any("ard" in c.name.lower() for c in res.candidates)
    assert res.skipped["other-category"] == 1 and res.skipped["not-hls"] == 1 and res.skipped["post-without-streams"] == 1


async def test_kurditv_unknown_category_config_fails_loudly(web, settings):
    routes_kurditv(web)
    src_opts = {"include_categories": ["no such category"]}
    res = await run(web, settings, "kurditv", options=src_opts)
    assert res.status == "failed" and "categories" in res.error


# ---- semir.site123.me ------------------------------------------------------------------------------------------------------------

def routes_site123(web):
    web.add("https://semir.site123.me/robots.txt", text="User-agent: *\nAllow: /\n", content_type="text/plain")
    web.add("https://sem1r.wapsite.me/robots.txt", text="Sitemap: http://x/sitemap.xml\n", content_type="text/plain")
    web.add("https://semir.site123.me/kurdish-tv-live", text=fixture_text("site123_tiles.html"))
    page = fixture_text("wapsite_page.html")
    web.add("https://sem1r.wapsite.me/Hd/ZAGROS.TV", text=page.replace("__STREAM__", "https://5a3ed7a72ed4b.streamlock.net/zagrostv/SMIL:myStream.smil/playlist.m3u8"))
    web.add("http://sem1r.wapsite.me/Local/Khak.HD", text=page.replace("__STREAM__", "http://bblserver.ddns.net:1935/stream/khakhd/playlist.m3u8"))
    web.add("http://sem1r.wapsite.me/Local/1mahabad", text=page.replace("__STREAM__", "http://94.182.185.24/live3/mahabad/mahabad.m3u8"))
    web.add("http://sem1r.wapsite.me/Local/Dead.tv", text="<html>no player here</html>")
    web.add("http://sem1r.wapsite.me/Local/Setanta-sport.HD", text=page.replace("__STREAM__", "https://x.example/setanta.m3u8"))


async def test_site123_tiles_and_followed_pages(web, settings):
    routes_site123(web)
    res = await run(web, settings, "site123")
    assert res.status == "ok", res.error
    cands = by_url(res)
    zagros = cands["https://5a3ed7a72ed4b.streamlock.net/zagrostv/SMIL:myStream.smil/playlist.m3u8"]
    assert zagros.name == "ZAGROS TV" and zagros.aliases == ()  # name from the link target; an identical title adds nothing
    assert zagros.logo_url == "https://images.cdn-files-a.com/uploads/1/zagros.png"
    assert cands["http://bblserver.ddns.net:1935/stream/khakhd/playlist.m3u8"].name == "Khak HD"
    mahabad = cands["http://94.182.185.24/live3/mahabad/mahabad.m3u8"]
    assert mahabad.name == "mahabad" and "Wrong title" in mahabad.aliases
    direct = cands["http://avrstream.com:1935/live/HormzyarHD/playlist.m3u8"]  # tile links straight to the stream
    assert direct.name == "Hormzyar HD" and direct.name_confidence == "low" and "Falcon tv2" in direct.aliases
    # skipped on purpose: the sport category, an APK download, a tile with no usable name, a page without a stream
    assert not any("setanta" in c.url for c in res.candidates)
    assert web.count("Setanta") == 0  # the sport page is not even fetched
    assert res.skipped == {"skipped-category": 1, "external-page": 1, "unnamed": 1, "page-without-hls": 1}
    assert web.count("mediafire") == 0


async def test_site123_wapsite_pages_are_not_followed_when_disabled(web, settings):
    routes_site123(web)
    res = await run(web, settings, "site123", options={"follow_links": False, "skip_categories": ["sport"]})
    assert [c.url for c in res.candidates] == ["http://avrstream.com:1935/live/HormzyarHD/playlist.m3u8"]
    assert web.count("wapsite.me/Hd") == 0


async def test_site123_layout_change_is_reported_not_crashing(web, settings):
    web.add("https://semir.site123.me/robots.txt", text="", content_type="text/plain")
    web.add("https://semir.site123.me/kurdish-tv-live", text="<html><body><p>new design</p></body></html>")
    res = await run(web, settings, "site123")
    assert res.status == "failed" and "no streams found" in res.error


# ---- karwan.tv (Cloudflare) and generic scanning --------------------------------------------------------------------------------

async def test_karwan_behind_a_bot_challenge_is_blocked_and_never_circumvented(web, settings):
    web.add("https://karwan.tv/robots.txt", status=403, text=CHALLENGE, headers={"cf-mitigated": "challenge"})
    web.add("https://karwan.tv/", status=403, text=CHALLENGE, headers={"cf-mitigated": "challenge"})
    res = await run(web, settings, "karwan")
    assert res.status == "blocked" and "challenge" in res.error and not res.candidates
    assert web.count("karwan.tv/") == 1  # only the robots.txt request; the page itself is never even attempted


async def test_html_scan_finds_streams_when_the_site_is_open(web, settings):
    web.add("https://karwan.tv/robots.txt", text="User-agent: *\nDisallow:\n", content_type="text/plain")
    web.add("https://karwan.tv/", text='<html><script>var s="https://live.karwan.tv/karwan.tv/cira-tv/playlist.m3u8";</script>'
                                       '<a href="/lists/all.m3u">list</a></html>')
    web.add("https://karwan.tv/lists/all.m3u", text='#EXTM3U\n#EXTINF:-1 tvg-logo="/l/a.png" group-title="Music",Newroz TV\nhttps://cdn.test/newroz/index.m3u8\n')
    res = await run(web, settings, "karwan")
    cands = by_url(res)
    assert res.status == "ok"
    assert cands["https://live.karwan.tv/karwan.tv/cira-tv/playlist.m3u8"].name == "cira tv"
    assert cands["https://cdn.test/newroz/index.m3u8"].logo_url == "https://karwan.tv/l/a.png"


async def test_generic_m3u_source_needs_no_code(web, settings):
    src = SourceConfig(id="extra", adapter="m3u", url="https://lists.test/kurdish.m3u", min_interval=0.0, trust="high")
    web.add("https://lists.test/robots.txt", text="", content_type="text/plain")
    web.add("https://lists.test/kurdish.m3u", text='#EXTM3U\n#EXTINF:-1 tvg-id="x1" tvg-language="Kurdish" group-title="News",New Channel\nhttps://cdn.test/new/master.m3u8\n')
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    res = await run_adapter(ADAPTERS["m3u"](src, SourceFetcher(http, db, src, RobotsCache(http, db))))
    [cand] = res.candidates
    assert (cand.name, cand.station_id, cand.language, cand.tags) == ("New Channel", "x1", "Kurdish", ("News",))
    await http.aclose()


def test_playlist_decorations_are_not_part_of_the_name():
    from kurdiptv.adapters.m3u import clean_playlist_name

    assert clean_playlist_name("Afarin TV (720p) [Not 24/7]") == "Afarin TV"
    assert clean_playlist_name("Rudaw TV (1080p)") == "Rudaw TV"
    assert clean_playlist_name("Zagros TV (Iraq) (1080p)") == "Zagros TV"
    assert clean_playlist_name("Welat TV (1080i)") == "Welat TV"
    assert clean_playlist_name("Channel 8 [Geo-blocked]") == "Channel 8"
    assert clean_playlist_name("Med Muzik") == "Med Muzik"


async def test_m3u_source_can_take_only_listed_channels(web, settings):
    src = SourceConfig(id="regional", adapter="m3u", url="https://lists.test/iq.m3u", min_interval=0.0, trust="high", options={"only": ["AVA Family", "Welat TV"]})
    web.add("https://lists.test/robots.txt", text="", content_type="text/plain")
    web.add("https://lists.test/iq.m3u", text='#EXTM3U\n#EXTINF:-1,Al Sharqiya (1080p)\nhttp://a.test/1.m3u8\n#EXTINF:-1,AVA Family\nhttps://b.test/2.m3u8\n'
                                              '#EXTINF:-1,Welat TV (1080i)\nhttps://c.test/3.m3u8\n#EXTINF:-1,Dijlah TV\nhttps://d.test/4.m3u8\n')
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    res = await run_adapter(ADAPTERS["m3u"](src, SourceFetcher(http, db, src, RobotsCache(http, db))))
    assert [c.name for c in res.candidates] == ["AVA Family", "Welat TV"]
    assert res.skipped == {"not-in-only-list": 2}
    await http.aclose()


async def test_official_adapter_reads_each_broadcaster_page_and_survives_broken_ones(web, settings):
    src = SourceConfig(
        id="official", adapter="official", url="https://a.test/", min_interval=0.0, trust="high", max_requests=50,
        options={"channels": [
            {"name": "Kirkuk TV", "pages": ["https://a.test/live"]},
            {"name": "KNN", "pages": ["https://b.test/live"]},          # behind a bot challenge
            {"name": "Speda TV", "pages": ["https://c.test/live"]},      # 404
            {"name": "Zed TV", "pages": ["https://d.test/live"]},        # page without a stream
            {"name": "Multi TV", "pages": ["https://e.test/live"], "only": r"/main/", "max_streams": 1},
            {"name": "", "pages": ["https://x.test/"]},                   # incomplete entry
        ]},
    )
    for host in "abcde":
        web.add(f"https://{host}.test/robots.txt", text="User-agent: *\nDisallow:\n", content_type="text/plain")
    web.add("https://a.test/live", text='<script>var player = {src: "https:\\/\\/live.kirkuk.example\\/hls\\/stream\\/index.m3u8"};</script>')
    web.add("https://b.test/live", status=403, text=CHALLENGE, headers={"cf-mitigated": "challenge"})
    web.add("https://c.test/live", status=404, text="nope")
    web.add("https://d.test/live", text="<html>coming soon</html>")
    web.add("https://e.test/live", text='<a href="https://cdn.test/ads/promo.m3u8"></a><a href="https://cdn.test/main/a.m3u8"></a><a href="https://cdn.test/main/b.m3u8"></a>')
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    res = await run_adapter(ADAPTERS["official"](src, SourceFetcher(http, db, src, RobotsCache(http, db))))
    got = {c.name: c.url for c in res.candidates}
    assert got == {"Kirkuk TV": "https://live.kirkuk.example/hls/stream/index.m3u8", "Multi TV": "https://cdn.test/main/a.m3u8"}
    kirkuk = next(c for c in res.candidates if c.name == "Kirkuk TV")
    assert kirkuk.name_confidence == "high" and kirkuk.page_url == "https://a.test/live" and "Kurdish" in kirkuk.tags
    assert res.status == "partial"  # some broadcaster pages are down: reported, not fatal
    assert any("KNN" in n and "blocked" in n for n in res.notes) and any("Speda" in n for n in res.notes)
    assert res.skipped["page-blocked"] == 1 and res.skipped["page-http"] == 1 and res.skipped["page-without-stream"] == 1
    assert res.skipped["incomplete-entry"] == 1
    await http.aclose()


async def test_official_source_without_channels_fails_loudly(web, settings):
    src = SourceConfig(id="official", adapter="official", url="https://a.test/", min_interval=0.0)
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    res = await run_adapter(ADAPTERS["official"](src, SourceFetcher(http, db, src, RobotsCache(http, db))))
    assert res.status == "failed" and "options.channels" in res.error
    await http.aclose()


async def test_adapter_crash_is_contained(web, settings):
    class Broken(ADAPTERS["m3u"]):
        async def discover(self):
            raise RuntimeError("markup changed")

    src = next(s for s in settings.sources if s.id == "kurdtvs")
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    res = await run_adapter(Broken(src, SourceFetcher(http, db, src, RobotsCache(http, db))))
    assert res.status == "failed" and "markup changed" in res.error
    await http.aclose()
