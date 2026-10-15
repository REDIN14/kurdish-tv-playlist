from pathlib import Path

import pytest

from kurdiptv.catalog import Catalog
from kurdiptv.config import ConfigError, SourceConfig
from kurdiptv.match import match_candidates
from kurdiptv.models import RawCandidate
from kurdiptv.policy import Policy

REPO = Path(__file__).resolve().parents[1]

HIGH = SourceConfig(id="high", adapter="m3u", url="https://h.test/", trust="high")
HIGH2 = SourceConfig(id="high2", adapter="m3u", url="https://h2.test/", trust="high")
LOW = SourceConfig(id="low", adapter="m3u", url="https://l.test/", trust="low")
LIST = SourceConfig(id="list", adapter="m3u", url="https://list.test/", trust="low", catalog_only=True)  # a community list


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(REPO / "config" / "catalog.yaml")


@pytest.fixture(scope="module")
def policy():
    return Policy.load(REPO / "config" / "policy.yaml")


def cand(source, name, url, *, tags=(), aliases=(), conf="medium"):
    return RawCandidate(source_id=source.id, url=url, name=name, tags=tuple(tags), aliases=tuple(aliases), name_confidence=conf)


def run(items, catalog, policy):
    return match_candidates([(i + 1, c, src) for i, (src, c) in enumerate(items)], catalog, policy)


# ---- de-duplication -------------------------------------------------------------------------------------------------------------

def test_same_channel_from_several_sources_becomes_one_channel_with_all_urls(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Rudaw TV Live", "https://live.rudaw.net/hls/rudaw-tv/master.m3u8", tags=("Kurdish",))),
        (HIGH2, cand(HIGH2, "Rudaw TV ZINDÎ", "https://cdn2.example.net/rudaw/index.m3u8")),
        (LOW, cand(LOW, "RUDAW tv", "https://svs.itworkscdn.net/rudawlive/rudawlive.smil/playlist.m3u8")),
        (HIGH, cand(HIGH, "Rudaw HD", "https://third.example.org/rudaw/playlist.m3u8")),
    ], catalog, policy)
    assert list(res.groups) == ["rudaw-tv"]
    group = res.groups["rudaw-tv"]
    assert len(group.members) == 4 and group.sources == {"high", "high2", "low"}
    assert group.name == "Rudaw TV" and group.catalog is not None


def test_variants_of_a_name_are_one_channel(catalog, policy):
    names = ["Kurdsat Tv (Alt 2)", "KURDSAT HD", "Kurdsat", "Kurdsat TV Live"]
    res = run([(HIGH, cand(HIGH, n, f"https://h{i}.example/k.m3u8")) for i, n in enumerate(names)], catalog, policy)
    assert list(res.groups) == ["kurdsat-tv"] and len(res.groups["kurdsat-tv"].members) == 4


def test_unknown_channels_with_equal_normalised_names_are_merged(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Zhwan HD", "https://a.example/zhwan.m3u8", tags=("Kurdish",))),
        (HIGH2, cand(HIGH2, "ZHWÂN live", "https://b.example/zhwan.m3u8")),
    ], catalog, policy)
    assert list(res.groups) == ["zhwan"]
    group = res.groups["zhwan"]
    assert group.unknown and len(group.members) == 2 and group.name == "Zhwan"


def test_identical_urls_are_not_the_dedup_criterion(catalog, policy):
    # two different channels must stay different even if a (mislabelled) source reuses nothing else about them
    res = run([
        (HIGH, cand(HIGH, "Kurdsat News", "https://a.example/news.m3u8")),
        (HIGH, cand(HIGH, "Kurdsat TV", "https://b.example/tv.m3u8")),
    ], catalog, policy)
    assert set(res.groups) == {"kurdsat-news", "kurdsat-tv"}


def test_stream_url_beats_a_wrong_label(catalog, policy):
    res = run([(LOW, cand(LOW, "Kurdmax badini", "https://6476e46b58f91.streamlock.net/liveTrans/KurdmaxS0rani!/playlist.m3u8"))], catalog, policy)
    assert list(res.groups) == ["kurdmax-sorani"]
    assert any("stream URL belongs to Kurdmax Sorani" in w for w in res.groups["kurdmax-sorani"].warnings)


def test_one_url_claimed_by_two_channels_is_resolved_and_reported(catalog, policy):
    url = "https://app-live.org/live/3268334b/index.m3u8"
    res = run([
        (HIGH, cand(HIGH, "Amozhgary TV", url, conf="high")),
        (HIGH2, cand(HIGH2, "Qallam Ainy TV Live", url, conf="medium")),
    ], catalog, policy)
    assert list(res.groups) == ["amozhgary-tv"]  # the catalogued name with better confidence wins
    assert len(res.groups["amozhgary-tv"].members) == 2
    [w] = [w for w in res.warnings if w["kind"] == "label-conflict"]
    assert "Qallam" in w["message"] and "amozhgary-tv" in w["message"]


# ---- uncertain matches are never merged ----------------------------------------------------------------------------------------------

def test_similar_names_stay_separate_and_raise_a_review_warning(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "ZED TV", "https://a.example/zed.m3u8", tags=("Kurdish",))),
        (HIGH, cand(HIGH, "ZED TV Plus", "https://b.example/zedplus.m3u8", tags=("Kurdish",))),
    ], catalog, policy)
    assert set(res.groups) == {"zed", "zed-plus"}
    assert any(w["kind"] == "possible-duplicate" for w in res.warnings)


def test_catalog_channels_of_one_family_are_not_confused(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Kurdmax Sorani", "https://a.example/s.m3u8")),
        (HIGH, cand(HIGH, "Kurdmax Kurmanci", "https://a.example/k.m3u8")),
        (HIGH, cand(HIGH, "Kurdmax Show", "https://a.example/show.m3u8")),
        (HIGH, cand(HIGH, "Kurdmax Music HD", "https://a.example/m.m3u8")),
        (HIGH, cand(HIGH, "Zarok TV Kurmanci", "https://zindikurmanci.zaroktv.com.tr/hls/stream.m3u8")),
        (HIGH, cand(HIGH, "Zarok TV ZINDÎ", "https://zindisorani.zaroktv.com.tr/hls/stream.m3u8")),  # URL says Sorani
    ], catalog, policy)
    assert set(res.groups) == {"kurdmax-sorani", "kurdmax-kurmanci", "kurdmax-show", "kurdmax-music", "zarok-tv-kurmanci", "zarok-tv-sorani"}


def test_never_merge_override_silences_the_warning(catalog, tmp_path):
    cfg = tmp_path / "policy.yaml"
    cfg.write_text((REPO / "config" / "policy.yaml").read_text(encoding="utf-8").replace("never_merge: []", "never_merge: [[zed, zed-plus]]"), encoding="utf-8")
    p = Policy.load(cfg)
    res = run([
        (HIGH, cand(HIGH, "ZED TV", "https://a.example/zed.m3u8", tags=("Kurdish",))),
        (HIGH, cand(HIGH, "ZED TV Plus", "https://b.example/zedplus.m3u8", tags=("Kurdish",))),
    ], catalog, p)
    assert set(res.groups) == {"zed", "zed-plus"} and not res.warnings


# ---- scope ----------------------------------------------------------------------------------------------------------------------------------

def reasons(res):
    return {m.cand.name: m.reason.split(":")[0] for m in res.excluded}


def test_new_channel_from_a_trusted_source_appears_without_any_catalog_entry(catalog, policy):
    res = run([(HIGH, cand(HIGH, "Brand New Kurdish TV", "https://x.example/new/master.m3u8"))], catalog, policy)
    assert "brand-new-kurdish" in res.groups and res.groups["brand-new-kurdish"].unknown


def test_trusted_unknown_can_be_held_for_review(catalog, tmp_path):
    cfg = tmp_path / "policy.yaml"
    cfg.write_text((REPO / "config" / "policy.yaml").read_text(encoding="utf-8").replace("unknown_from_trusted: include", "unknown_from_trusted: review"), encoding="utf-8")
    res = run([(HIGH, cand(HIGH, "Brand New TV", "https://x.example/n.m3u8"))], catalog, Policy.load(cfg))
    assert not res.groups and reasons(res) == {"Brand New TV": "needs-review"}


def test_unclassified_channel_page_needs_a_kurdish_marker_or_a_second_trusted_source(catalog, policy):
    # kurdtvs.net lists Turkish, Arabic and international TV beside the Kurdish channels; a bare channel page says nothing
    # about language, so "NASA TV Live" must not become part of a Kurdish playlist just because the site is trusted.
    bare = ("unclassified",)
    res = run([
        (HIGH, cand(HIGH, "Euro Star", "https://x.example/eurostar/master.m3u8", tags=bare)),
        (HIGH, cand(HIGH, "Kurdish Kids TV", "https://x.example/kids/master.m3u8", tags=bare)),  # the name says Kurdish
        (HIGH, cand(HIGH, "Shar TV", "https://x.example/shar/master.m3u8", tags=bare)),  # also in another trusted source
        (HIGH2, cand(HIGH2, "Shar TV", "https://y.example/shar/master.m3u8", tags=("Kurdish",))),
        (HIGH, cand(HIGH, "Rudaw HD", "https://x.example/rudaw/master.m3u8", tags=bare)),  # a catalogue channel is unaffected
    ], catalog, policy)
    assert set(res.groups) == {"kurdish-kids", "shar", "rudaw-tv"}
    assert reasons(res) == {"Euro Star": "needs-review"}
    assert "nothing marks it as Kurdish" in next(m.reason for m in res.excluded if m.cand.name == "Euro Star")


def test_community_list_adds_streams_to_known_channels_but_cannot_introduce_one(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Brand New Kurdish TV", "https://x.example/new/master.m3u8")),  # known from a normal source
        (LIST, cand(LIST, "brand new kurdish tv hd", "https://backup.example/new/playlist.m3u8")),  # same channel, second stream
        (LIST, cand(LIST, "Rudaw", "https://live.rudaw.net/hls/rudaw-tv/stream_1080p.m3u8")),  # a catalogue channel
        (LIST, cand(LIST, "Tom And Jerry", "https://cartoons.example/tj/playlist.m3u8")),  # nobody else lists it
        (LIST, cand(LIST, "Nat Geo World", "https://x.example/ng/playlist.m3u8")),  # a denied brand says why
    ], catalog, policy)
    assert set(res.groups) == {"brand-new-kurdish", "rudaw-tv"}
    assert len(res.groups["brand-new-kurdish"].members) == 2 and res.groups["rudaw-tv"].members[0].source_id == "list"
    assert reasons(res) == {"Tom And Jerry": "needs-review", "Nat Geo World": "out-of-scope"}
    assert "community list" in next(m.reason for m in res.excluded if m.cand.name == "Tom And Jerry")


def test_a_community_lists_label_never_renames_a_published_channel(catalog, policy):
    # the same stream under two spellings: the normal source's id must win or the channel's tvg-id would change overnight
    res = run([
        (LIST, cand(LIST, "ku | Hormzyar TV", "http://avrstream.com:1935/live/HormzyarHD/playlist.m3u8")),
        (LOW, cand(LOW, "Hormzyar HD", "http://avrstream.com:1935/live/HormzyarHD/playlist.m3u8")),
        (HIGH, cand(HIGH, "Hormzyar HD", "https://other.example/hormzyar/master.m3u8", tags=("Kurdish",))),
    ], catalog, policy)
    assert list(res.groups) == ["hormzyar"] and len(res.groups["hormzyar"].members) == 3


def test_community_lists_do_not_corroborate_a_low_trust_source(catalog, policy):
    res = run([
        (LOW, cand(LOW, "Amed tv", "http://176.236.106.172:8080/hls/ferat.m3u8")),
        (LIST, cand(LIST, "Amed TV", "https://other.example/amed/master.m3u8")),
    ], catalog, policy)
    assert not res.groups and "uncorroborated" in {m.reason.split(":")[0] for m in res.excluded}


def test_low_trust_unknown_needs_corroboration(catalog, policy):
    res = run([
        (LOW, cand(LOW, "Khak Kids", "http://bblserver.ddns.net:1935/stream/khakkids/playlist.m3u8")),  # name echoed in URL
        (LOW, cand(LOW, "Amed tv", "http://176.236.106.172:8080/hls/ferat.m3u8")),  # name contradicts URL
        (LOW, cand(LOW, "Delaltv", "http://seetvlivee.example/delal/index.m3u8")),  # spelling variant is fine
    ], catalog, policy)
    assert set(res.groups) == {"khak-kids", "delaltv"}
    assert reasons(res) == {"Amed tv": "uncorroborated"}


def test_low_trust_unknown_is_accepted_when_a_second_source_agrees(catalog, policy):
    res = run([
        (LOW, cand(LOW, "Amed tv", "http://176.236.106.172:8080/hls/ferat.m3u8")),
        (HIGH, cand(HIGH, "Amed TV", "https://other.example/amed/master.m3u8")),
    ], catalog, policy)
    assert "amed" in res.groups and len(res.groups["amed"].members) == 2


def test_low_trust_label_on_an_unknown_server_is_not_believed(catalog, policy):
    res = run([(LOW, cand(LOW, "Rudaw HD", "http://some-random-server.example:8000/live/other/playlist.m3u8"))], catalog, policy)
    assert not res.groups and reasons(res) == {"Rudaw HD": "unconfirmed-identity"}


def test_trusted_label_on_a_new_server_is_accepted(catalog, policy):
    res = run([(HIGH, cand(HIGH, "Rudaw HD", "https://new-cdn.example/rudaw/master.m3u8"))], catalog, policy)
    assert list(res.groups) == ["rudaw-tv"]


def test_foreign_and_pay_tv_and_sports_never_get_through(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Kanal D TV HD", "https://x.example/kd/playlist.m3u8", tags=("Turkish",))),
        (HIGH, cand(HIGH, "MBC Action", "https://x.example/mbc/index.m3u8")),
        (HIGH, cand(HIGH, "Eurosport 1 HD", "https://x.example/e/index.m3u8")),
        (HIGH, cand(HIGH, "Some Foreign News", "https://x.example/f/index.m3u8", tags=("English", "News"))),
        (HIGH, cand(HIGH, "Duhok Sport", "https://x.example/ds/index.m3u8", tags=("Kurdish",))),
        (HIGH, cand(HIGH, "Cartoon Box", "https://x.example/likemovies/index.m3u8", tags=("Kurdish",))),
        (HIGH, cand(HIGH, "Mislabelled TV", "https://zdf-hls-15.akamaized.net/hls/live/2016498/de/high/master.m3u8", tags=("Kurdish",))),
    ], catalog, policy)
    assert not res.groups
    kinds = reasons(res)
    assert kinds["Kanal D TV HD"] == "out-of-scope"
    assert kinds["MBC Action"] == "out-of-scope" and kinds["Eurosport 1 HD"] == "out-of-scope"
    assert kinds["Some Foreign News"] == "out-of-scope"
    assert kinds["Duhok Sport"] == "rights-sensitive" and kinds["Cartoon Box"] == "rights-sensitive"
    assert kinds["Mislabelled TV"] == "identity-mismatch"


def test_catalog_channel_with_a_foreign_stream_url_is_rejected(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Duhok TV Sports is a Kurdish sports channel", "https://derste247livede.akamaized.net/hls/live/658317/daserste_de/master.m3u8")),
        (HIGH, cand(HIGH, "Duhok TV", "https://zdf-hls-15.akamaized.net/hls/live/2016498/de/high/master.m3u8")),
        (HIGH, cand(HIGH, "Duhok TV", "https://drfamaga5qlivcf.vcdn.com.vn/vtv06/vtv06@480p.m3u8")),
        (HIGH, cand(HIGH, "Duhok TV", "http://duhok.badinan.xyz:1935/live/DuhokTV/playlist.m3u8")),
    ], catalog, policy)
    assert [m.cand.url for m in res.groups["duhok-tv"].members] == ["http://duhok.badinan.xyz:1935/live/DuhokTV/playlist.m3u8"]
    assert len(res.excluded) == 3


def test_credentialed_urls_are_excluded_whoever_lists_them(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Kurdcinema action", "http://185.166.25.198:25461/live/kurdcinema/kurdcinema/1581.m3u8")),
        (HIGH, cand(HIGH, "Tokened TV", "https://x.example/a.m3u8?token=abc123")),
    ], catalog, policy)
    assert not res.groups and set(reasons(res).values()) == {"unsafe-url"}


def test_blocklist_removes_a_channel_everywhere(catalog, tmp_path):
    cfg = tmp_path / "policy.yaml"
    cfg.write_text((REPO / "config" / "policy.yaml").read_text(encoding="utf-8").replace("channel_ids: []", "channel_ids: [rudaw-tv]"), encoding="utf-8")
    res = run([(HIGH, cand(HIGH, "Rudaw TV", "https://live.rudaw.net/hls/rudaw-tv/master.m3u8"))], catalog, Policy.load(cfg))
    assert not res.groups and reasons(res) == {"Rudaw TV": "blocklist"}


def test_unnamed_streams_are_dropped(catalog, policy):
    res = run([(HIGH, cand(HIGH, "HD LIVE", "https://x.example/a.m3u8"))], catalog, policy)
    assert not res.groups and reasons(res) == {"HD LIVE": "unnamed"}


def test_force_alias_override(catalog, tmp_path):
    cfg = tmp_path / "policy.yaml"
    cfg.write_text((REPO / "config" / "policy.yaml").read_text(encoding="utf-8").replace("force_alias: {}", "force_alias: {'Weird Label': kurdsat-news}"), encoding="utf-8")
    res = run([(HIGH, cand(HIGH, "Weird Label", "https://x.example/w.m3u8"))], catalog, Policy.load(cfg))
    assert list(res.groups) == ["kurdsat-news"]


# ---- catalog file ---------------------------------------------------------------------------------------------------------------------------

def test_catalog_rejects_alias_collisions_and_typos(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("channels:\n  - {id: a, name: Alpha, aliases: [Shared]}\n  - {id: b, name: Beta, aliases: [Shared]}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="claimed by both"):
        Catalog.load(bad)
    bad.write_text("channels:\n  - {id: a, name: Alpha, aliasses: [x]}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown field"):
        Catalog.load(bad)
    bad.write_text("channels:\n  - {id: a, name: Alpha}\n  - {id: a, name: Other}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate"):
        Catalog.load(bad)


def test_iptv_org_style_names_merge_with_the_other_sources_spellings(catalog, policy):
    res = run([
        (HIGH, cand(HIGH, "Channel8", "https://live.channel8.com/Channel8-Kurdish/index.fmp4.m3u8")),
        (HIGH2, cand(HIGH2, "CHANNEL8 Kurdish TV", "https://other.example/c8/master.m3u8")),
        (HIGH, cand(HIGH, "Kurd Channel", "https://kurdchhls.wns.live/hls/stream.m3u8")),
        (HIGH2, cand(HIGH2, "kurdch TV", "https://kurd-channel.ikoflix.com/hls/kurd-channel.m3u8")),
        (HIGH, cand(HIGH, "Mixkurdy", "https://avr.host247.net/live/Mix-kurdy/playlist.m3u8")),
        (LOW, cand(LOW, "Mixkurdish HD", "http://217.74.16.163:9590/live/mixkurdish/video.m3u8")),
        (HIGH, cand(HIGH, "AVA Media", "https://ava002.xyz/upload/images/1080.m3u8")),
        (HIGH2, cand(HIGH2, "AVA News  live kurdish tv", "https://ava2.store/upload/images/1080.m3u8")),
    ], catalog, policy)
    assert {g: len(v.members) for g, v in res.groups.items()} == {"channel-8": 2, "kurd-channel": 2, "mixkurdy": 2, "ava-media": 2}


def test_iptv_org_categories_are_understood():
    from kurdiptv.match import _category_from_tags

    assert _category_from_tags({"Religious"}) == "Religion"
    assert _category_from_tags({"Family"}) == "Entertainment"
    assert _category_from_tags({"Kids"}) == "Kids"
    assert _category_from_tags({"Undefined"}) == "General"
    assert _category_from_tags({"Culture", "General"}) == "Culture"
    assert _category_from_tags({"Culture", "General", "Religious"}) == "Culture"  # Sercem TV: culture first
    assert _category_from_tags({"Religious"}) == "Religion"


def test_original_source_is_recognised_by_domain_or_own_server(catalog):
    rudaw = catalog.by_id["rudaw-tv"]
    assert rudaw.is_original("https://live.rudaw.net/hls/rudaw-tv/master.m3u8")  # its own domain
    assert not rudaw.is_original("https://hlspackager.akamaized.net/live/DB/RUDAW/HLS/RUDAW.m3u8")  # somebody's copy
    assert not rudaw.is_original("https://rudaw.net.evil.example/x.m3u8")  # a look-alike host is not the domain
    waar = catalog.by_id["waar-tv"]
    assert waar.is_original("https://live.kwikmotion.com/waarmedialive/waarmedia.smil/playlist.m3u8")  # its own account ...
    assert not waar.is_original("https://live.kwikmotion.com/syriatvlive/syriatv.smil/playlist.m3u8")  # ... not the provider's
    assert not catalog.by_id["kurdsat-tv"].is_original("https://hlspackager.akamaized.net/live/DB/KURDSAT_HD/HLS/KURDSAT_HD.m3u8")


def test_shipped_catalog_is_consistent(catalog):
    assert len(catalog.channels) >= 35
    for ch in catalog.channels:
        assert ch.id == ch.id.lower() and " " not in ch.id
        assert ch.keys, ch.id
    # the most specific URL pattern wins
    assert catalog.by_url("https://5a3ed7a72ed4b.streamlock.net/zagrostv/SMIL:myStream.smil/playlist.m3u8").id == "zagros-tv"
    assert catalog.by_url("https://5a3ed7a72ed4b.streamlock.net/live/SMIL:myStream.smil/playlist.m3u8").id == "kurdistan-tv"
    assert catalog.by_url("https://unknown.example/x.m3u8") is None


def test_stream_addresses_of_the_kurdmax_and_gali_kurdistan_families_map_to_the_right_channel(catalog):
    """The four Kurdmax live pages and the two Gali Kurdistan pages name these addresses; each belongs to exactly one channel and
    counts as the broadcaster's own. A default port in the address (":443") must not change the answer."""
    cases = {
        "https://stream.kurdmax.tv/app/kurdmax-sorani1/ts:playlist.m3u8": ("kurdmax-sorani", True),
        "https://stream.kurdmax.tv/app/kurdmax-kurmanji2/ts:playlist.m3u8": ("kurdmax-kurmanci", True),
        "https://stream.kurdmaxshow.tv/app/kurdmaxshow-sorani/llhls.m3u8": ("kurdmax-show", True),
        "https://stream.kurdmaxshow.tv/app/kurdmaxshow-kurmanji/llhls.m3u8": ("kurdmax-show-kurmanci", True),
        "https://6476e46b58f91.streamlock.net:443/music/MUSIC2402/playlist.m3u8": ("kurdmax-music", True),
        "https://6476e46b58f91.streamlock.net/music/MUSIC2402/playlist.m3u8": ("kurdmax-music", True),  # not Med Muzik
        "https://live.host247.net/gk/gksat/playlist.m3u8": ("gali-kurdistan-tv", True),
        "https://live.host247.net/gk/gkplus/playlist.m3u8": ("gali-kurdistan-plus", True),
        "https://hlspackager.akamaized.net/live/DB/MED_MUSIC/HLS/MED_MUSIC.m3u8": ("med-muzik", False),  # a distributor, not Med's own
    }
    for url, (channel_id, original) in cases.items():
        found = catalog.by_url(url)
        assert found is not None and (found.id, found.is_original(url)) == (channel_id, original), url


def test_gali_kurdistan_labels_do_not_collapse_onto_the_plus_channel(catalog):
    # "GK+" and "GK" normalise to the same key; the plain abbreviation is the main channel
    assert catalog.by_name("GK").id == "gali-kurdistan-tv" and catalog.by_name("GK Sat").id == "gali-kurdistan-tv"
    assert catalog.by_name("GK Plus").id == "gali-kurdistan-plus"


def test_nrt_patterns_do_not_pull_in_the_sport_or_fourth_channel(catalog):
    nrt = "https://live20.bozztv.com/giatv/giatv-nrt/nrt/chunks.m3u8"
    assert catalog.by_url(nrt).id == "nrt-tv"
    assert catalog.by_url("https://hlspackager.akamaized.net/live/DB/NRT_HD/HLS/NRT_HD.m3u8").id == "nrt-tv"
    assert catalog.by_url("https://live20.bozztv.com/giatv/giatv-NRT-SPORT/NRT-SPORT/chunks.m3u8") is None  # sport: never NRT TV
    assert catalog.by_url("https://live20.bozztv.com/giatv/giatv-nrt4stream/nrt4stream/chunks.m3u8") is None


def test_relay_platforms_are_never_published_for_any_channel(catalog, policy):
    """Community lists often point at a stranger's proxy (a Cloudflare Worker, a free app host) that re-serves a stream whose
    owner only allows playback on its own site. Such a link is not the channel's stream and passes on the circumvention."""
    for url in (
        "https://kurdmaxshow1.worldtvplus.workers.dev/live.m3u8",
        "https://kurdmax-relay.vercel.app/hls/playlist.m3u8",
        "https://someone.herokuapp.com/kurdmax.m3u8",
        "https://x.trycloudflare.com/a.m3u8",
    ):
        res = run([(HIGH, cand(HIGH, "Kurdmax Show", url))], catalog, policy)
        assert not res.groups and reasons(res) == {"Kurdmax Show": "blocklist"}, url
    res = run([(HIGH, cand(HIGH, "Kurdmax Show", "https://stream.kurdmaxshow.tv/app/kurdmaxshow-sorani/llhls.m3u8"))], catalog, policy)
    assert list(res.groups) == ["kurdmax-show"]  # the channel's own server is fine


def test_no_catalogued_channel_is_blocklisted_or_a_denied_brand(catalog, policy):
    """The catalogue and the policy must not contradict each other (a channel added to one that the other forbids is dead weight
    at best and a published film/sports/relay channel at worst)."""
    for ch in catalog.channels:
        assert policy.blocked(channel_id=ch.id, name=ch.name, url="") is None, f"{ch.id} is blocklisted"
        for alias in ch.aliases:
            assert policy.blocked(channel_id=None, name=alias, url="") is None, f"{ch.id}: alias {alias!r} is blocklisted"
        for pattern in ch.url_patterns:
            assert policy.denied_by_url("https://" + pattern.strip("/")) is None, f"{ch.id}: pattern {pattern!r} names a denied brand"
