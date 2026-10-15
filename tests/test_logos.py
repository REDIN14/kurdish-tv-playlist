import dataclasses
import io

import pytest
from PIL import Image

from kurdiptv.config import LogoSettings
from kurdiptv.db import Database
from kurdiptv.fetcher import RobotsCache
from kurdiptv.logos import LogoCatalog, LogoManager, _build_index, make_placeholder, normalise_logo
from kurdiptv.net import SafeHttp

from helpers import advance, png_bytes

CFG = LogoSettings()


def encode(img, fmt):
    out = io.BytesIO()
    img.save(out, format=fmt)
    return out.getvalue()


# ---- validation ---------------------------------------------------------------------------------------------------------------

def test_valid_images_become_png():
    for fmt in ("PNG", "JPEG", "GIF", "WEBP"):
        data = encode(Image.new("RGB", (300, 200), (10, 120, 200)), fmt)
        png, w, h = normalise_logo(data, CFG)
        assert png.startswith(b"\x89PNG") and (w, h) == (300, 200), fmt


def test_large_images_are_scaled_down_keeping_aspect():
    png, w, h = normalise_logo(png_bytes((2000, 1000)), CFG)
    assert (w, h) == (512, 256)
    assert Image.open(io.BytesIO(png)).size == (512, 256)


def test_transparency_is_preserved():
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (20, 20, 80, 80))
    png, _, _ = normalise_logo(encode(img, "PNG"), CFG)
    assert Image.open(io.BytesIO(png)).getpixel((1, 1))[3] == 0


@pytest.mark.parametrize(
    "data,fragment",
    [
        (b"", "empty"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><circle r='5'/></svg>", "not a PNG"),
        (b"<html><body>404</body></html>", "not a PNG"),
        (png_bytes((20, 20)), "too small"),
        (png_bytes((5000, 60)), "too large"),
        (png_bytes((100, 100), (0, 0, 0, 0)), "transparent"),
        (png_bytes((100, 100))[:60], "undecodable"),
    ],
)
def test_bad_images_are_rejected_with_a_reason(data, fragment):
    with pytest.raises(ValueError, match=fragment):
        normalise_logo(data, CFG)


# ---- placeholder -------------------------------------------------------------------------------------------------------------------

def test_placeholder_is_a_valid_deterministic_png():
    a = make_placeholder("Rudaw TV", "rudaw-tv", 512)
    assert Image.open(io.BytesIO(a)).size == (512, 512)
    assert a == make_placeholder("Rudaw TV", "rudaw-tv", 512)
    assert a != make_placeholder("Zagros TV", "zagros-tv", 512)
    assert Image.open(io.BytesIO(make_placeholder("رووداو", "rudaw-ar", 256))).size == (256, 256)  # non-Latin name falls back to the id
    assert Image.open(io.BytesIO(make_placeholder("A Very Long Channel Name That Must Wrap Nicely", "long", 512))).size == (512, 512)


# ---- manager --------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def manager(settings, web):
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    mgr = LogoManager(settings, http, db, RobotsCache(http, db))
    mgr.limiter.min_interval = 0.0
    return mgr


def img_route(web, url, **kw):
    web.add(url, content=png_bytes(), content_type="image/png", **kw)


async def test_source_logo_is_downloaded_validated_and_cached(manager, web):
    img_route(web, "https://src.test/logo.png")
    info = await manager.resolve("rudaw-tv", "Rudaw TV", ["Rudaw TV"], ["https://src.test/logo.png"])
    assert info.kind == "source" and info.origin == "https://src.test/logo.png"
    assert manager.path_for("rudaw-tv").read_bytes().startswith(b"\x89PNG")
    before = web.count("logo.png")
    again = await manager.resolve("rudaw-tv", "Rudaw TV", ["Rudaw TV"], ["https://src.test/logo.png"])
    assert again.sha1 == info.sha1 and web.count("logo.png") == before  # no new download within the TTL


async def test_falls_through_bad_sources_to_the_catalogue_then_the_placeholder(manager, web):
    web.add("https://src.test/missing.png", status=404, text="gone")
    web.add("https://src.test/page.png", text="<html>not an image</html>", content_type="image/png")
    web.add("https://src.test/vector.svg", text="<svg/>", content_type="image/svg+xml")
    web.add("https://src.test/huge.png", content=b"\x89PNG\r\n\x1a\n" + b"0" * 2_000_000, content_type="image/png")
    img_route(web, "https://catalog.test/rudaw.png")
    manager.catalog = LogoCatalog({"rudaw": ["https://catalog.test/rudaw.png"]})
    bad = [f"https://src.test/{n}" for n in ("missing.png", "page.png", "vector.svg")]
    info = await manager.resolve("rudaw-tv", "Rudaw TV", ["Rudaw TV"], bad)
    assert info.kind == "catalog"

    info2 = await manager.resolve("nobody", "Nobody TV", ["Nobody TV"], ["https://src.test/huge.png"])
    assert info2.kind == "placeholder"
    assert Image.open(manager.path_for("nobody")).size == (512, 512)


async def test_wrong_content_type_is_rejected(manager, web):
    web.add("https://src.test/a.png", content=png_bytes(), content_type="text/html")
    info = await manager.resolve("x", "X", ["X"], ["https://src.test/a.png"])
    assert info.kind == "placeholder"


async def test_private_image_hosts_are_refused(manager, web):
    web.private_hosts.add("intranet.test")
    img_route(web, "http://intranet.test/logo.png")
    info = await manager.resolve("x", "X", ["X"], ["http://intranet.test/logo.png"])
    assert info.kind == "placeholder" and web.calls.count(("GET", "http://intranet.test/logo.png")) == 0


async def test_robots_disallowed_logo_is_not_fetched(manager, web):
    web.add("https://src.test/robots.txt", text="User-agent: *\nDisallow: /uploads/\n", content_type="text/plain")
    img_route(web, "https://src.test/uploads/logo.png")
    info = await manager.resolve("x", "X", ["X"], ["https://src.test/uploads/logo.png"])
    assert info.kind == "placeholder" and web.count("/uploads/logo.png") == 0


async def test_placeholder_is_replaced_once_a_real_logo_becomes_available(manager, web):
    first = await manager.resolve("x", "X Channel", ["X Channel"], ["https://src.test/late.png"])
    assert first.kind == "placeholder"
    img_route(web, "https://src.test/late.png")
    same_day = await manager.resolve("x", "X Channel", ["X Channel"], ["https://src.test/late.png"])
    assert same_day.kind == "placeholder"  # not retried every refresh
    advance(2 * 86400)
    later = await manager.resolve("x", "X Channel", ["X Channel"], ["https://src.test/late.png"])
    assert later.kind == "source"


async def test_real_logo_survives_a_failing_refresh(manager, web):
    img_route(web, "https://src.test/l.png")
    first = await manager.resolve("x", "X", ["X"], ["https://src.test/l.png"])
    web.add("https://src.test/l.png", status=500, text="down")
    advance(30 * 86400)  # TTL expired, source now broken
    kept = await manager.resolve("x", "X", ["X"], ["https://src.test/l.png"])
    assert kept.kind == "source" and kept.sha1 == first.sha1


async def test_policy_overrides(manager, web):
    img_route(web, "https://src.test/wrong.png")
    img_route(web, "https://fixed.test/right.png")
    forced = await manager.resolve("a", "A TV", ["A TV"], ["https://src.test/wrong.png"], override="placeholder")
    assert forced.kind == "placeholder" and web.count("wrong.png") == 0
    replaced = await manager.resolve("b", "B TV", ["B TV"], ["https://src.test/wrong.png"], override="https://fixed.test/right.png")
    assert replaced.kind == "source" and replaced.origin == "https://fixed.test/right.png" and web.count("wrong.png") == 0


# ---- iptv-org catalogue -------------------------------------------------------------------------------------------------------------------

def test_catalogue_index_prefers_png_and_skips_vectors():
    channels = [
        {"id": "Rudaw.iq", "name": "Rudaw", "alt_names": ["Rûdaw", "ڕووداو"], "is_nsfw": False},
        {"id": "Porn.xx", "name": "Hidden", "alt_names": [], "is_nsfw": True},
        {"id": "NoLogo.iq", "name": "No Logo", "alt_names": []},
    ]
    logos = [
        {"channel": "Rudaw.iq", "format": "SVG", "url": "https://l/vector.svg", "width": 512},
        {"channel": "Rudaw.iq", "format": "JPEG", "url": "https://l/a.jpg", "width": 512},
        {"channel": "Rudaw.iq", "format": "PNG", "url": "https://l/b.png", "width": 800},
        {"channel": "Porn.xx", "format": "PNG", "url": "https://l/p.png", "width": 500},
    ]
    index = _build_index(channels, logos)
    cat = LogoCatalog(index)
    assert cat.find(["Rudaw TV HD"]) == ["https://l/b.png", "https://l/a.jpg"]
    assert cat.find(["Rûdaw"]) == cat.find(["Rudaw"])
    assert cat.find(["Hidden"]) == [] and cat.find(["No Logo"]) == []


async def test_catalogue_loading_uses_cache_and_survives_outages(settings, web):
    channels = [{"id": "R.iq", "name": "Rudaw", "alt_names": []}]
    logos = [{"channel": "R.iq", "format": "PNG", "url": "https://l/r.png", "width": 512}]
    web.add("https://iptv-org.github.io/api/channels.json", json=channels)
    web.add("https://iptv-org.github.io/api/logos.json", json=logos)
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    db = Database(":memory:")
    cfg = dataclasses.replace(settings.logos, use_catalog=True)
    cache = settings.data_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    cat = await LogoCatalog.load(http, db, cfg, cache)
    assert cat.find(["Rudaw"]) == ["https://l/r.png"]
    calls = len(web.calls)
    cat2 = await LogoCatalog.load(http, db, cfg, cache)
    assert cat2.find(["Rudaw"]) == ["https://l/r.png"] and len(web.calls) == calls  # still fresh: no download
    advance(10 * 86400)
    web.offline = True
    cat3 = await LogoCatalog.load(http, db, cfg, cache)
    assert cat3.find(["Rudaw"]) == ["https://l/r.png"]  # catalogue unreachable: the previous copy is used
    await http.aclose()
