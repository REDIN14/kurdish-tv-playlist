from pathlib import Path

import pytest

from kurdiptv.config import ConfigError
from kurdiptv.policy import Policy, credential_reason, strip_ephemeral_query

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def policy():
    return Policy.load(REPO / "config" / "policy.yaml")


# ---- URLs that must never be published ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@host.example/live.m3u8",
        "http://185.166.25.198:25461/live/kurdcinema/kurdcinema/1576.m3u8",  # Xtream-Codes: user/pass in the path
        "http://panel.example/get.php?username=a&password=b&type=m3u",
        "http://wowzaprod219-i.akamaihd.net/hls/live/1003949/x/playlist.m3u8?wmsAuthSign=abc",
        "https://cdn.example/live/a.m3u8?token=abcdef",
        "https://cdn.example/live/a.m3u8?hdnts=exp=1~acl=*~hmac=zz",
    ],
)
def test_credentialed_urls_are_rejected(url):
    assert credential_reason(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://live.rudaw.net/hls/rudaw-tv/master.m3u8",
        "http://bblserver.ddns.net:1935/stream/khakhd/playlist.m3u8",
        "https://cdn.example/live/channel/index.m3u8?stream=1",
    ],
)
def test_plain_urls_are_accepted(url):
    assert credential_reason(url) is None


def test_session_ids_are_stripped_not_rejected():
    url = "http://wowzaprod219-i.akamaihd.net/hls/live/1003949/c9aa559e/playlist.m3u8?nimblesessionid=1234"
    assert strip_ephemeral_query(url) == "http://wowzaprod219-i.akamaihd.net/hls/live/1003949/c9aa559e/playlist.m3u8"
    assert strip_ephemeral_query("http://a/b.m3u8?x=1&nimblesessionid=2") == "http://a/b.m3u8?x=1"
    assert strip_ephemeral_query("http://a/b.m3u8") == "http://a/b.m3u8"


# ---- brands / scope -------------------------------------------------------------------------------------------------------------

def test_foreign_brands_are_denied_by_name(policy):
    for name in ["MBC Action", "Eurosport 1 HD", "Al Jazeera English", "Sky News Arabia", "RT Arabic Live", "Nat Geo Wild",
                 "A Spor HD", "Kanal D TV HD", "DW Eng", "France 24", "Dubai One TV Movie", "ABC News", "Star TV HD"]:
        assert policy.denied_by_name(name), name


def test_kurdish_channels_are_not_denied(policy):
    for name in ["Rudaw TV", "TRT Kurdî", "Kurdistan 24", "Zagros TV", "Mihrab TV", "Çira TV", "Hormzyar", "Kurdsat News",
                 "Waar TV", "Duhok TV", "Mmn life"]:
        assert policy.denied_by_name(name) is None, name


def test_foreign_streams_are_denied_by_url(policy):
    for url in [
        "https://derste247livede.akamaized.net/hls/live/658317/daserste_de/master.m3u8",
        "https://zdf-hls-15.akamaized.net/hls/live/2016498/de/high/master.m3u8",
        "https://drfamaga5qlivcf.vcdn.com.vn/vtv06/vtv06@1080p.m3u8",
        "https://rt-glb.rttv.com/dvr/rtnews/playlist.m3u8",
    ]:
        assert policy.denied_by_url(url), url
    assert policy.denied_by_url("https://live.rudaw.net/hls/rudaw-tv/master.m3u8") is None
    assert policy.denied_by_url("https://tv-trtkurdi.medya.trt.com.tr/master.m3u8") is None


def test_short_tokens_never_hit_urls(policy):
    # "rt" and "dw" are only brand names when they ARE the channel name, not when they sit inside a URL path
    assert policy.denied_by_url("https://example.com/rt/dw/stream.m3u8") is None


def test_rights_sensitive_content(policy):
    assert policy.rights_reason("Kurdistan Sport", "http://x/a.m3u8")
    assert policy.rights_reason("Like HD", "http://217.74.16.163:9590/live/likemovies/video.m3u8")
    assert policy.rights_reason("Mmn movies", "http://x/y.m3u8")
    assert policy.rights_reason("Rudaw TV", "https://live.rudaw.net/hls/rudaw-tv/master.m3u8") is None


def test_foreign_tags_need_a_kurdish_marker_to_survive(policy):
    assert policy.foreign_only(("Turkish",)) == "turkish"
    assert policy.foreign_only(("News", "Turkish", "Kurdish")) is None
    assert policy.foreign_only(("News", "General")) is None
    assert policy.has_kurdish_marker("Kurdsat News")
    assert policy.has_kurdish_marker("کوردستان")
    assert not policy.has_kurdish_marker("Eurosport")


# ---- blocklist --------------------------------------------------------------------------------------------------------------------

def test_blocklist_matches_ids_names_hosts_and_url_parts(tmp_path):
    cfg = tmp_path / "policy.yaml"
    cfg.write_text(
        "blocklist:\n  channel_ids: [Bad-Channel]\n  names: ['Taken Down TV']\n  hosts: [evil.example]\n  url_contains: ['/private/']\n",
        encoding="utf-8",
    )
    p = Policy.load(cfg)
    assert p.blocked(channel_id="bad-channel", name="x", url="http://ok/a.m3u8")
    assert p.blocked(channel_id="x", name="Taken Down TV HD", url="http://ok/a.m3u8")
    assert p.blocked(channel_id="x", name="x", url="http://cdn.evil.example/a.m3u8")
    assert p.blocked(channel_id="x", name="x", url="http://ok/PRIVATE/a.m3u8".replace("PRIVATE", "private"))
    assert p.blocked(channel_id="fine", name="Fine TV", url="http://ok/a.m3u8") is None


def test_unknown_policy_sections_are_rejected(tmp_path):
    cfg = tmp_path / "policy.yaml"
    cfg.write_text("blocklst: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Policy.load(cfg)
