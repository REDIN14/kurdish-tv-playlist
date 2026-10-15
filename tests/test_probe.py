import dataclasses
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import kurdiptv.probe as probe_mod
from kurdiptv.config import ProbeSettings
from kurdiptv.net import SafeHttp
from kurdiptv.probe import Prober, looks_like_media

from helpers import media_playlist, ts_bytes

CFG = ProbeSettings(per_host_min_interval=0.0, concurrency=4)
BASE = "https://cdn.test/a/"


def make(web, cfg=CFG, ffprobe=None):
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    return http, Prober(http, cfg, ffprobe=ffprobe)


def hi_lo():
    return [("v360.m3u8", 800_000, "640x360"), ("v1080.m3u8", 5_000_000, "1920x1080")]


# ---- container sniffing --------------------------------------------------------------------------------------------------

def test_looks_like_media():
    assert looks_like_media(ts_bytes(3)) == "ts"
    assert looks_like_media(b"\x00\x00\x00\x18styp" + b"\x00" * 20) == "fmp4"
    assert looks_like_media(b"\x00\x00\x00\x20ftypiso5" + b"\x00" * 20) == "fmp4"
    assert looks_like_media(b"ID3\x04" + b"\x00" * 20) == "aac"
    assert looks_like_media(b"<html>not video</html>") == ""
    assert looks_like_media(b"") == ""


# ---- successful probes ------------------------------------------------------------------------------------------------------

async def test_master_playlist_is_probed_at_its_best_variant(web):
    web.add_stream(BASE + "master.m3u8", variants=hi_lo())
    http, prober = make(web)
    r = await prober.probe(BASE + "master.m3u8")
    assert r.ok and r.kind == "hls-master" and r.variants == 2
    assert (r.width, r.height) == (1920, 1080) and r.bandwidth == 5_000_000
    assert r.bitrate == int(len(ts_bytes()) * 8 / 6)  # measured from the segment that was actually downloaded
    assert r.latency_ms is not None and r.final_url == BASE + "master.m3u8"
    assert r.tail_marker.split(":", 1)[0].isdigit() and r.tail_marker.endswith(".ts") and "/v1080_" in r.tail_marker
    assert not r.verified and any("ffprobe" in n for n in r.notes)  # without ffprobe the resolution is only a claim
    assert web.count("v360") == 0  # the lower variant is not touched
    await http.aclose()


async def test_media_playlist_without_master(web):
    web.add_stream(BASE + "live.m3u8", master=False, variants=[])
    http, prober = make(web)
    r = await prober.probe(BASE + "live.m3u8")
    assert r.ok and r.kind == "hls-media" and r.height is None
    await http.aclose()


async def test_ffprobe_measurement_overrides_the_claimed_resolution(web, monkeypatch):
    """The playlist claims 1080p; the actual video is 720p. The measurement wins and is marked verified."""
    web.add_stream(BASE + "master.m3u8", variants=hi_lo())
    seen = {}

    async def fake_ffprobe(path, data, timeout=20.0):
        seen["bytes"] = len(data)
        return {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "avg_frame_rate": "25/1"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "programs": [{"tags": {"service_name": "Service01", "service_provider": "FFmpeg"}}],
        }

    monkeypatch.setattr(probe_mod, "run_ffprobe", fake_ffprobe)
    http, prober = make(web, ffprobe="ffprobe")
    r = await prober.probe(BASE + "master.m3u8")
    assert r.ok and r.verified
    assert (r.width, r.height, r.fps, r.vcodec, r.acodec) == (1280, 720, 25.0, "h264", "aac")
    assert seen["bytes"] == len(ts_bytes())
    assert r.service == ""  # generic muxer defaults are not an identity
    await http.aclose()


async def test_stream_service_name_is_reported(web, monkeypatch):
    web.add_stream(BASE + "master.m3u8", variants=hi_lo())

    async def fake_ffprobe(path, data, timeout=20.0):
        return {"streams": [{"codec_type": "video", "width": 640, "height": 360, "avg_frame_rate": "30000/1001"}],
                "programs": [{"tags": {"service_provider": "ZDF", "service_name": "ZDF HD"}}]}

    monkeypatch.setattr(probe_mod, "run_ffprobe", fake_ffprobe)
    http, prober = make(web, ffprobe="ffprobe")
    r = await prober.probe(BASE + "master.m3u8")
    assert r.service == "ZDF / ZDF HD" and r.fps == 29.97
    await http.aclose()


async def test_audio_only_by_ffprobe_is_rejected(web, monkeypatch):
    web.add_stream(BASE + "master.m3u8", variants=hi_lo())

    async def fake_ffprobe(path, data, timeout=20.0):
        return {"streams": [{"codec_type": "audio", "codec_name": "mp3"}]}

    monkeypatch.setattr(probe_mod, "run_ffprobe", fake_ffprobe)
    http, prober = make(web, ffprobe="ffprobe")
    r = await prober.probe(BASE + "master.m3u8")
    assert not r.ok and r.kind == "audio-only"
    await http.aclose()


async def test_undecodable_segment_is_rejected(web, monkeypatch):
    web.add_stream(BASE + "master.m3u8", variants=hi_lo())

    async def fake_ffprobe(path, data, timeout=20.0):
        return {"streams": []}

    monkeypatch.setattr(probe_mod, "run_ffprobe", fake_ffprobe)
    http, prober = make(web, ffprobe="ffprobe")
    r = await prober.probe(BASE + "master.m3u8")
    assert not r.ok and "decoded" in r.error
    await http.aclose()


async def test_second_variant_is_tried_when_the_best_one_is_broken(web):
    web.add_stream(BASE + "master.m3u8", variants=hi_lo())
    web.routes[BASE + "v1080.m3u8"] = lambda req: httpx.Response(404)
    http, prober = make(web)
    r = await prober.probe(BASE + "master.m3u8")
    assert r.ok and r.height == 360 and r.width == 640  # no claim leaks over from the failed 1080p attempt
    http2, prober1 = make(web, dataclasses.replace(CFG, max_variants_tried=1))
    r1 = await prober1.probe(BASE + "master.m3u8")
    assert not r1.ok and "variant http-404" in r1.error
    await http.aclose()
    await http2.aclose()


async def test_light_probe_checks_structure_cheaply(web):
    stream = web.add_stream(BASE + "master.m3u8", variants=hi_lo())
    http, prober = make(web)
    r = await prober.probe(BASE + "master.m3u8", mode="light")
    assert r.ok and r.mode == "light" and r.bitrate is None and not r.verified
    seg_headers = [h for (m, u), h in zip(web.calls, web.request_headers) if u.endswith(".ts")]
    assert seg_headers and seg_headers[0]["range"] == "bytes=0-4095"
    await http.aclose()


# ---- failures -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,fragment",
    [(404, "http-404"), (410, "http-410"), (403, "refused"), (429, "rate limited"), (503, "http-503")],
)
async def test_http_errors_are_labelled(web, status, fragment):
    web.add(BASE + "x.m3u8", status=status, text="no")
    http, prober = make(web)
    r = await prober.probe(BASE + "x.m3u8")
    assert not r.ok and fragment in r.error and r.http_status == status
    await http.aclose()


async def test_html_page_is_not_a_stream(web):
    web.add(BASE + "x.m3u8", text="<html><body>Please log in</body></html>")
    http, prober = make(web)
    r = await prober.probe(BASE + "x.m3u8")
    assert not r.ok and r.kind == "not-hls" and "web page" in r.error
    await http.aclose()


async def test_endless_transport_stream_behind_an_m3u8_url_is_detected_and_bounded(web):
    web.add(BASE + "x.m3u8", content=ts_bytes(20_000))  # 3.7 MB of raw TS
    http, prober = make(web, dataclasses.replace(CFG, max_playlist_bytes=50_000))
    r = await prober.probe(BASE + "x.m3u8")
    assert not r.ok and "endless MPEG-TS" in r.error
    await http.aclose()


async def test_vod_playlist_is_not_a_live_channel(web):
    web.add_stream(BASE + "live.m3u8", master=False, variants=[], ended=True)
    http, prober = make(web)
    r = await prober.probe(BASE + "live.m3u8")
    assert not r.ok and "not live" in r.error
    await http.aclose()


async def test_empty_and_encrypted_playlists_fail(web):
    web.add(BASE + "empty.m3u8", text="#EXTM3U\n#EXT-X-TARGETDURATION:6\n")
    web.add(BASE + "enc.m3u8", text='#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:6,\na.ts\n')
    http, prober = make(web)
    assert "no segments" in (await prober.probe(BASE + "empty.m3u8")).error
    assert "encrypted" in (await prober.probe(BASE + "enc.m3u8")).error
    await http.aclose()


async def test_segments_must_exist_and_be_media(web):
    web.add(BASE + "live.m3u8", text=media_playlist(seq=1, count=3))
    web.add(BASE + "seg1.ts", status=404, text="gone")
    web.add(BASE + "seg2.ts", content=b"<html>not media</html>")
    web.add(BASE + "seg3.ts", content=b"<html>not media</html>")
    http, prober = make(web)
    assert "segment is not MPEG-TS" in (await prober.probe(BASE + "live.m3u8")).error  # second-to-last segment is used
    web.add(BASE + "seg2.ts", status=404, text="gone")
    assert "segment http-404" in (await prober.probe(BASE + "live.m3u8")).error
    web.add(BASE + "seg2.ts", content=b"")
    assert "empty" in (await prober.probe(BASE + "live.m3u8")).error
    await http.aclose()


async def test_audio_only_master_is_radio(web):
    web.add(BASE + "radio.m3u8", text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"\naudio.m3u8\n')
    http, prober = make(web)
    r = await prober.probe(BASE + "radio.m3u8")
    assert not r.ok and r.kind == "audio-only"
    await http.aclose()


async def test_stale_playlist_by_program_date_time(web):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    web.add(BASE + "live.m3u8", text=f"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:5\n#EXT-X-PROGRAM-DATE-TIME:{old}\n#EXTINF:6,\nseg5.ts\n")
    http, prober = make(web)
    r = await prober.probe(BASE + "live.m3u8")
    assert not r.ok and "stale" in r.error
    await http.aclose()


async def test_private_and_dead_hosts_fail_cleanly(web):
    web.private_hosts.add("lan.test")
    web.dns_fail.add("dead.test")
    http, prober = make(web)
    assert (await prober.probe("http://lan.test/a.m3u8")).error.startswith("refused")
    assert (await prober.probe("http://dead.test/a.m3u8")).error.startswith("dns")
    assert web.calls == []
    await http.aclose()


async def test_redirected_stream_reports_the_final_url(web):
    web.add("https://short.test/a", status=302, headers={"location": BASE + "master.m3u8"})
    web.add_stream(BASE + "master.m3u8", variants=hi_lo())
    http, prober = make(web)
    r = await prober.probe("https://short.test/a")
    assert r.ok and r.final_url == BASE + "master.m3u8"
    await http.aclose()


async def test_fragmented_mp4_stream(web):
    web.add(BASE + "live.m3u8", text='#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:9\n#EXT-X-MAP:URI="init.mp4"\n'
                                    "#EXTINF:4,\ns9.m4s\n#EXTINF:4,\ns10.m4s\n#EXTINF:4,\ns11.m4s\n")
    web.add(BASE + "init.mp4", content=b"\x00\x00\x00\x18ftypiso5" + b"\x00" * 20)
    for n in (9, 10, 11):
        web.add(BASE + f"s{n}.m4s", content=b"\x00\x00\x00\x18moof" + b"\x00" * 200)
    http, prober = make(web)
    r = await prober.probe(BASE + "live.m3u8")
    assert r.ok
    await http.aclose()
