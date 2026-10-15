"""A complete fake internet for end-to-end tests: the four source sites, live and dead streams, logos."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from helpers import FakeStream, FakeWeb, png_bytes

FIXTURES = Path(__file__).parent / "fixtures"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>challenges.cloudflare.com</body></html>"

RUDAW_A = "https://live.rudaw.net/hls/rudaw-tv/master.m3u8"
RUDAW_B = "https://svs.itworkscdn.net/rudawlive/rudawlive.smil/playlist.m3u8"
KSAT_A = "https://hlspackager.akamaized.net/live/DB/KURDSAT_HD/HLS/KURDSAT_HD.m3u8"
KSAT_B = "https://kurdsat.akamaized.net/hls/kurdsat.m3u8"
ZAGROS = "https://5a3ed7a72ed4b.streamlock.net/zagrostv/SMIL:myStream.smil/playlist.m3u8"
MIHRAB = "http://baskhd.ddns.net:40/live/MihrabTV/playlist.m3u8"
SPEDA_DEAD = "https://speda.dwasat.com/upload/images/spedahd.m3u8"
KANALD = "https://demiroren-live.daioncdn.net/kanald/kanald.m3u8"
NEWTV = "https://cdn.newchannel.example/live/master.m3u8"
CANARY = "https://canary.test/"

QUALITY = {1080: ("v1080.m3u8", 5_000_000, "1920x1080"), 720: ("v720.m3u8", 2_500_000, "1280x720"),
           576: ("v576.m3u8", 1_500_000, "1024x576"), 480: ("v480.m3u8", 1_000_000, "854x480"), 360: ("v360.m3u8", 700_000, "640x360")}


@dataclass
class World:
    web: FakeWeb
    streams: dict[str, FakeStream] = field(default_factory=dict)
    m3u_entries: list[list[str]] = field(default_factory=list)  # [name, group, url]
    kurdtvs_pages: dict[str, tuple[str, str, list[str]]] = field(default_factory=dict)
    kurditv_posts: list[tuple[int, str, str, int, list[str]]] = field(default_factory=list)
    site_tiles: list[tuple[str, str, str]] = field(default_factory=list)  # href, title, category
    wap_pages: dict[str, str] = field(default_factory=dict)  # href -> stream url
    sources_down: set[str] = field(default_factory=set)

    # ---- streams -----------------------------------------------------------------------------------------------------------
    def stream(self, url: str, height: int = 720, *, alive: bool = True, claim: int | None = None) -> FakeStream:
        """claim: the resolution the playlist advertises (defaults to the truth)."""
        c = claim or height
        s = self.web.add_stream(url, variants=[QUALITY[c]], alive=alive, true_height=height)
        self.streams[url] = s
        return s

    # ---- sources -----------------------------------------------------------------------------------------------------------------
    def publish_sources(self) -> None:
        w = self.web
        for host in ("kurdtvs.net", "kurditv.com", "semir.site123.me", "sem1r.wapsite.me"):
            w.add(f"https://{host}/robots.txt", text="User-agent: *\nDisallow:\n", content_type="text/plain")
        w.add("https://karwan.tv/robots.txt", status=403, text=CHALLENGE, headers={"cf-mitigated": "challenge"})

        # kurdtvs.net
        if "kurdtvs" in self.sources_down:
            w.add("https://kurdtvs.net/iptv.php?format=m3u", status=503, text="down")
            w.add("https://kurdtvs.net/", status=503, text="down")
        else:
            lines = ["#EXTM3U"]
            for name, group, url in self.m3u_entries:
                logo = "https://kurdtvs.net/uploads/rudawhd.png" if "Rudaw" in name else ""
                lines.append(f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo}" group-title="{group}",{name}')
                lines.append(url)
            w.add("https://kurdtvs.net/iptv.php?format=m3u", text="\n".join(lines) + "\n", content_type="audio/x-mpegurl")
            links = "".join(f'<a href="{slug}">{slug}</a>' for slug in self.kurdtvs_pages)
            w.add("https://kurdtvs.net/", text=f"<html><body>{links}</body></html>")
            for slug, (title, logo, urls) in self.kurdtvs_pages.items():
                import json

                streams = json.dumps([{"url": u, "title": "Live", "type": "hls"} for u in urls])
                w.add(
                    f"https://kurdtvs.net{slug}",
                    text=f'<html><head><meta property="og:title" content="{title}"/><meta property="og:image" content="{logo}"/></head>'
                         f"<body><script>const streams = {streams};</script></body></html>",
                )

        # kurditv.com
        w.add("https://kurditv.com/wp-json/wp/v2/categories?per_page=100&_fields=id,name,slug,count",
              json=[{"id": 3, "name": "weşana zindî", "slug": "wesana-zindi", "count": 9}, {"id": 13, "name": "Arabic TV Live", "slug": "arabic", "count": 1}])
        posts = [{"id": pid, "link": f"https://kurditv.com/{pid}/", "slug": slug, "title": {"rendered": title}, "categories": [cat]}
                 for pid, title, slug, cat, _ in self.kurditv_posts]
        if "kurditv" in self.sources_down:
            w.add("https://kurditv.com/wp-json/wp/v2/posts?per_page=100&page=1&_fields=id,link,slug,title,categories", status=500, text="oops")
        else:
            w.add("https://kurditv.com/wp-json/wp/v2/posts?per_page=100&page=1&_fields=id,link,slug,title,categories", json=posts)
        for pid, _, _, _, urls in self.kurditv_posts:
            def vembed(request, _pid=pid, _urls=urls):
                if request.headers.get("referer") != f"https://kurditv.com/embed/{_pid}/":
                    return httpx.Response(403, text="forbidden")
                return httpx.Response(200, json={"data": {"sources": [{"src": u} for u in _urls]}})
            w.add_fn(f"https://kurditv.com/wp-admin/admin-ajax.php?action=vembed_data&post_id={pid}", vembed)

        # semir.site123.me
        if "site123" in self.sources_down:
            w.add("https://semir.site123.me/kurdish-tv-live", status=502, text="bad gateway")
        else:
            tiles = []
            for href, title, cat in self.site_tiles:
                tiles.append((href, title, cat))
            blocks = {}
            for href, title, cat in tiles:
                for block in {"all", cat}:
                    blocks.setdefault(block, []).append(f'<a href="{href}" class="customer-bg" title="{title}" style="display:block; background-image:url(https://images.cdn-files-a.com/uploads/1/x.png)"></a>')
            html = "".join(f'<div class="row carousel-category" data-categories-filter="{k}">{"".join(v)}</div>' for k, v in blocks.items())
            w.add("https://semir.site123.me/kurdish-tv-live", text=f"<html><body>{html}</body></html>")
        for href, stream in self.wap_pages.items():
            w.add(href, text=f'<html><script>var player = new Clappr.Player({{source: "{stream}"}});</script></html>')

        # karwan.tv: behind a bot challenge
        w.add("https://karwan.tv/", status=403, text=CHALLENGE, headers={"cf-mitigated": "challenge"})

        # misc
        w.add(CANARY, status=204)
        w.add("https://kurdtvs.net/uploads/rudawhd.png", content=png_bytes((200, 150)), content_type="image/png")


def build_world(web: FakeWeb) -> World:
    world = World(web)
    world.stream(RUDAW_A, 1080)
    world.stream(RUDAW_B, 720)
    world.stream(KSAT_A, 1080)
    world.stream(KSAT_B, 720)
    world.stream(ZAGROS, 720)
    world.stream(MIHRAB, 576)
    world.stream(KANALD, 720)
    world.stream(SPEDA_DEAD, 720, alive=False)

    world.m3u_entries = [
        ["Rudaw TV Live", "News General Local Kurdish", RUDAW_A],
        ["Kurdsat Tv", "News General Local Kurdish", KSAT_A],
        ["Kurdsat Tv (Alt 2)", "News General Local Kurdish", KSAT_B],
        ["Zagros TV HD", "News Local General Kurdish", ZAGROS],
        ["Mihrab TV", "Kurdish", MIHRAB],
        ["Kanal D TV HD", "Turkish", KANALD],
    ]
    world.kurdtvs_pages = {"/zagros-tv": ("Zagros TV - Live Zindi Rastawxo", "https://kurdtvs.net/uploads/zagros.png", [ZAGROS])}
    world.kurditv_posts = [
        (1, "Rudaw TV  ZINDÎ", "rudaw-tv-zindi", 3, [RUDAW_A]),
        (2, "Speda TV The Voice of Kurdish Society and Culture", "speda-tv", 3, [SPEDA_DEAD]),
        (3, "Das Erste LIVE", "ard", 13, ["https://derste247livede.akamaized.net/hls/live/658317/daserste_de/master.m3u8"]),
    ]
    rudaw_page = "https://sem1r.wapsite.me/HD/RUDAW.tv"
    world.site_tiles = [(rudaw_page, "Rudaw hd", "all")]
    world.wap_pages = {rudaw_page: RUDAW_B}
    world.publish_sources()
    return world


def fake_ffprobe(monkeypatch):
    """Replace ffprobe: the 'true' height travels inside the segment bytes (b'H=720;')."""
    import kurdiptv.probe as probe_mod

    async def run(path, data, timeout=20.0):
        m = re.search(rb"H=(\d+);", data)
        h = int(m.group(1)) if m else 720
        out = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": h * 16 // 9, "height": h, "avg_frame_rate": "25/1"},
                           {"codec_type": "audio", "codec_name": "aac"}]}
        s = re.search(rb"S=([^;]*);", data)
        if s:
            out["programs"] = [{"tags": {"service_name": s.group(1).decode()}}]
        return out

    monkeypatch.setattr(probe_mod, "run_ffprobe", run)
