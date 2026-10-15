from kurdiptv.hls import MasterPlaylist, MediaPlaylist, parse_attrs, parse_playlist
from kurdiptv.m3u import attr_value, parse_m3u, render_extinf

from helpers import master_playlist, media_playlist


# ---- M3U ----------------------------------------------------------------------------------------------------------------

def test_parse_extinf_attributes_and_title():
    text = (
        '#EXTM3U\n#EXTINF:-1 tvg-id="a" tvg-name="Rudaw, HD" tvg-logo="http://x/y.png" group-title="News General",Rudaw TV\n'
        "http://host/a.m3u8\n"
    )
    [entry] = parse_m3u(text)
    assert entry.name == "Rudaw TV"
    assert entry.url == "http://host/a.m3u8"
    assert entry.attrs["tvg-name"] == "Rudaw, HD"  # a comma inside a quoted value must not end the attributes
    assert entry.attrs["group-title"] == "News General"


def test_parse_handles_bom_blank_lines_options_and_unknown_directives():
    text = "﻿#EXTM3U\n\n#EXTINF:-1,Plain\n#EXTVLCOPT:http-user-agent=x\n#SOMETHING\nhttp://a/b.m3u8\n#EXTINF:-1,NoUrl\n"
    entries = parse_m3u(text)
    assert [e.name for e in entries] == ["Plain"]
    assert entries[0].options == ["#EXTVLCOPT:http-user-agent=x"]


def test_render_roundtrip_and_escaping():
    line = render_extinf('Name, "Quoted"', {"tvg-id": "x", "tvg-name": 'a "b"', "group-title": "", "tvg-logo": "http://l/p.png"})
    assert "group-title" not in line  # empty attributes are dropped
    [entry] = parse_m3u("#EXTM3U\n" + line + "\nhttp://u/v.m3u8\n")
    assert entry.attrs["tvg-id"] == "x"
    assert '"' not in entry.attrs["tvg-name"].replace("'", "")
    assert "," not in entry.name
    assert attr_value("a\nb\r\"c\"") == "a b 'c'"


# ---- HLS ------------------------------------------------------------------------------------------------------------------

def test_attr_list_with_quoted_commas():
    attrs = parse_attrs('BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",FRAME-RATE=29.970')
    assert attrs["CODECS"] == "avc1.64001f,mp4a.40.2"
    assert attrs["RESOLUTION"] == "1280x720"


def test_master_playlist_variants():
    text = master_playlist([("low.m3u8", 800_000, "640x360"), ("hi.m3u8", 5_000_000, "1920x1080")])
    parsed = parse_playlist(text, "https://cdn.test/live/master.m3u8")
    assert isinstance(parsed, MasterPlaylist)
    assert [(v.height, v.bandwidth) for v in parsed.variants] == [(360, 800_000), (1080, 5_000_000)]
    assert parsed.variants[1].uri == "https://cdn.test/live/hi.m3u8"  # resolved against the playlist URL
    assert parsed.variants[0].has_video and not parsed.variants[0].audio_only


def test_audio_only_variant_is_detected():
    text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"\nradio.m3u8\n'
    [variant] = parse_playlist(text, "https://x/y.m3u8").variants
    assert variant.audio_only


def test_media_playlist_fields():
    parsed = parse_playlist(media_playlist(seq=42, count=4), "https://cdn.test/live/v.m3u8")
    assert isinstance(parsed, MediaPlaylist)
    assert parsed.media_sequence == 42
    assert len(parsed.segments) == 4
    assert parsed.segments[0].uri == "https://cdn.test/live/seg42.ts"
    assert not parsed.ended and not parsed.encrypted
    assert parsed.tail_marker == "42:https://cdn.test/live/seg45.ts"


def test_media_playlist_flags():
    ended = parse_playlist(media_playlist(ended=True), "https://x/a.m3u8")
    assert ended.ended
    enc = parse_playlist('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:6,\na.ts\n', "https://x/a.m3u8")
    assert enc.encrypted
    clear = parse_playlist('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:6,\na.ts\n', "https://x/a.m3u8")
    assert not clear.encrypted


def test_fmp4_map_and_byterange():
    text = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXT-X-BYTERANGE:1000@2000\n#EXTINF:4,\nmedia.mp4\n'
    parsed = parse_playlist(text, "https://x/live/p.m3u8")
    assert parsed.map_uri == "https://x/live/init.mp4"
    assert parsed.segments[0].byterange == (1000, 2000)


def test_not_hls_returns_none():
    assert parse_playlist("<html>login</html>", "https://x/") is None
    assert parse_playlist("", "https://x/") is None
    assert parse_playlist("\x47\x40\x00", "https://x/") is None
