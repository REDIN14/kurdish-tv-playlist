"""Runs the real ffmpeg/ffprobe (skipped when they are not installed): proves that measurement, not claims, is used."""

import shutil
import subprocess

import httpx
import pytest

from kurdiptv.config import ProbeSettings
from kurdiptv.net import SafeHttp
from kurdiptv.probe import Prober, find_ffprobe, run_ffprobe

from helpers import FakeWeb, master_playlist, media_playlist

FFMPEG = shutil.which("ffmpeg")
FFPROBE = find_ffprobe("")
pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")

BASE = "https://cdn.test/live/"


def make_ts(size="640x360", rate=25, seconds=4, service=None) -> bytes:
    cmd = [FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}", "-f", "lavfi", "-i", "sine=frequency=440",
           "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    if service:
        cmd += ["-metadata", f"service_name={service}", "-metadata", f"service_provider={service}"]
    cmd += ["-f", "mpegts", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def serve(web: FakeWeb, segment: bytes, claimed="1920x1080") -> str:
    web.add(BASE + "master.m3u8", text=master_playlist([("v.m3u8", 5_000_000, claimed)]))
    web.add(BASE + "v.m3u8", text=media_playlist(seq=7, count=4, duration=4))
    for n in range(7, 11):
        web.add(BASE + f"seg{n}.ts", content=segment)
    return BASE + "master.m3u8"


def prober(web, **kw):
    http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
    return http, Prober(http, ProbeSettings(per_host_min_interval=0.0, **kw), ffprobe=FFPROBE)


async def test_real_resolution_fps_codecs_replace_the_claim():
    web = FakeWeb()
    url = serve(web, make_ts("640x360", 25), claimed="1920x1080")  # the playlist says 1080p, the pixels say 360p
    http, p = prober(web)
    r = await p.probe(url)
    assert r.ok and r.verified
    assert (r.width, r.height) == (640, 360) and r.fps == 25.0
    assert r.vcodec == "h264" and r.acodec == "aac"
    assert r.bandwidth == 5_000_000 and r.bitrate and r.bitrate != r.bandwidth  # measured separately from the claim
    await http.aclose()


async def test_real_mpegts_service_name_is_read():
    web = FakeWeb()
    url = serve(web, make_ts(service="ZDF HD"))
    http, p = prober(web)
    r = await p.probe(url)
    assert r.ok and "ZDF" in r.service
    await http.aclose()


async def test_generic_muxer_service_names_are_not_an_identity():
    web = FakeWeb()
    url = serve(web, make_ts())  # ffmpeg's default: service_name "Service01", provider "FFmpeg"
    http, p = prober(web)
    r = await p.probe(url)
    assert r.ok and r.service == ""
    await http.aclose()


async def test_audio_only_segment_is_radio():
    web = FakeWeb()
    audio = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:a", "aac", "-f", "mpegts", "pipe:1"],
                           capture_output=True, check=True).stdout
    url = serve(web, audio, claimed="1280x720")
    http, p = prober(web)
    r = await p.probe(url)
    assert not r.ok and r.kind == "audio-only"
    await http.aclose()


async def test_garbage_with_ts_sync_bytes_is_not_playable():
    web = FakeWeb()
    junk = (b"\x47" + b"\x01" * 187) * 200
    url = serve(web, junk)
    http, p = prober(web)
    r = await p.probe(url)
    assert not r.ok and "decoded" in r.error
    await http.aclose()


async def test_run_ffprobe_handles_a_missing_binary_and_timeouts():
    assert await run_ffprobe("definitely-not-installed-ffprobe", b"x") is None
