import dataclasses
import json

import pytest

from kurdiptv.config import PublishSettings
from kurdiptv.m3u import parse_m3u
from kurdiptv.playlist import BASE_TOKEN, PublishedChannel, PublishedStream, render_m3u, validate_m3u
from kurdiptv.publish import Publisher

CFG = PublishSettings()


def stream(n, host="h"):
    return PublishedStream(cid=n, url=f"https://{host}{n}.example/live/{n}.m3u8", source_id="s", height=720)


def channel(cid="rudaw-tv", name="Rudaw TV", group="News", streams=None, **kw):
    return PublishedChannel(id=cid, name=name, group=group, language="ckb", country="IQ", logo_version="abc12345",
                            streams=streams or [stream(1)], **kw)


# ---- rendering ------------------------------------------------------------------------------------------------------------------------

def test_every_required_attribute_is_present():
    text = render_m3u([channel()], CFG)
    [entry] = parse_m3u(text)
    assert text.startswith("#EXTM3U\n")
    assert entry.name == "Rudaw TV"
    assert entry.attrs["tvg-id"] == "rudaw-tv" and entry.attrs["tvg-name"] == "Rudaw TV"
    assert entry.attrs["tvg-logo"] == f"{BASE_TOKEN}/logos/rudaw-tv.png?v=abc12345"
    assert entry.attrs["group-title"] == "News"
    assert entry.attrs["tvg-language"] == "Kurdish (Sorani)" and entry.attrs["tvg-country"] == "IQ"
    assert entry.url == "https://h1.example/live/1.m3u8"


def test_fallbacks_go_to_a_backup_group_by_default():
    text = render_m3u([channel(streams=[stream(1), stream(2), stream(3)]), channel("zagros-tv", "Zagros TV", "General")], CFG)
    entries = parse_m3u(text)
    assert [e.name for e in entries] == ["Rudaw TV", "Zagros TV", "Rudaw TV (Alt 2)", "Rudaw TV (Alt 3)"]
    alt = entries[2]
    assert alt.attrs["group-title"] == "Backup streams" and alt.attrs["tvg-id"] == "rudaw-tv-alt2"
    assert alt.attrs["tvg-logo"].endswith("rudaw-tv.png?v=abc12345")
    assert alt.url == "https://h2.example/live/2.m3u8"  # the ordered fallback list is preserved


def test_inline_and_none_fallback_modes():
    ch = channel(streams=[stream(1), stream(2)], order=1)
    inline = parse_m3u(render_m3u([ch, channel("b", "B TV", "News")], dataclasses.replace(CFG, fallback_mode="inline")))
    assert [e.name for e in inline] == ["Rudaw TV", "Rudaw TV (Alt 2)", "B TV"] and inline[1].attrs["group-title"] == "News"
    none = parse_m3u(render_m3u([ch], dataclasses.replace(CFG, fallback_mode="none")))
    assert [e.name for e in none] == ["Rudaw TV"]


def test_ordering_groups_then_catalogue_position_then_name():
    chs = [
        channel("z", "Zeta", "Music", order=5), channel("a", "Alpha", "News", order=10_000), channel("n", "Nrt", "News", order=2),
        channel("b", "Beta", "News", order=10_000), channel("k", "Kids TV", "Kids", order=1),
    ]
    names = [e.name for e in parse_m3u(render_m3u(chs, CFG))]
    assert names == ["Nrt", "Alpha", "Beta", "Zeta", "Kids TV"]


def test_hostile_names_cannot_break_the_format():
    ch = channel("x", 'Evil", tvg-id="hack\nhttp://attacker/stream.m3u8', "News")
    text = render_m3u([ch], CFG)
    [entry] = parse_m3u(text)
    assert text.count("#EXTINF") == 1 and "attacker" not in entry.url
    assert entry.attrs["tvg-id"] == "x"


def test_channels_without_streams_are_skipped():
    empty = PublishedChannel(id="x", name="X", group="News", streams=[])
    assert parse_m3u(render_m3u([empty], CFG)) == []


def test_epg_header_only_when_asked():
    assert "url-tvg" not in render_m3u([channel()], CFG)
    assert f'url-tvg="{BASE_TOKEN}/epg.xml"' in render_m3u([channel()], CFG, epg=True).splitlines()[0]


# ---- validation -------------------------------------------------------------------------------------------------------------------------

def many(n):
    return [channel(f"c{i}", f"Channel {i}", "News", streams=[stream(i)]) for i in range(n)]


def test_a_good_playlist_validates():
    assert validate_m3u(render_m3u(many(8), CFG), CFG) == []


def test_validation_gates():
    cfg = CFG
    assert any("EXTM3U" in p for p in validate_m3u("nonsense", cfg))
    assert any("only 0 channels" in p for p in validate_m3u("#EXTM3U\n", cfg))
    assert any("only 3 channels" in p for p in validate_m3u(render_m3u(many(3), cfg), cfg))
    # shrinking: 12 -> 5 is below 50 %
    assert any("dropped from 12 to 5" in p for p in validate_m3u(render_m3u(many(5), cfg), cfg, previous_primary_count=12))
    assert validate_m3u(render_m3u(many(8), cfg), cfg, previous_primary_count=12) == []


def test_validation_catches_malformed_entries():
    good = render_m3u(many(6), CFG)
    dup = good + '#EXTINF:-1 tvg-id="c0" tvg-name="Dup" tvg-logo="x" group-title="News",Dup\nhttps://z.example/z.m3u8\n'
    assert any("duplicate tvg-id" in p for p in validate_m3u(dup, CFG))
    creds = good + '#EXTINF:-1 tvg-id="c99" tvg-name="C" tvg-logo="x" group-title="News",C\nhttp://u:p@z.example/z.m3u8\n'
    assert any("credentials" in p for p in validate_m3u(creds, CFG))
    notnet = good + '#EXTINF:-1 tvg-id="c98" tvg-name="C" tvg-logo="x" group-title="News",C\nrtmp://z.example/live\n'
    assert any("not http" in p for p in validate_m3u(notnet, CFG))
    nologo = good + '#EXTINF:-1 tvg-id="c97" tvg-name="C" group-title="News",C\nhttps://z.example/z.m3u8\n'
    assert any("missing tvg-logo" in p for p in validate_m3u(nologo, CFG))


def test_backup_entries_do_not_count_as_channels():
    text = render_m3u([channel(streams=[stream(1), stream(2), stream(3)])], CFG)
    assert any("only 1 channels" in p for p in validate_m3u(text, CFG))


# ---- publishing ---------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def publisher(settings):
    return Publisher(settings)


def publish(pub, n_channels=8, label="run"):
    text = render_m3u(many(n_channels), CFG)
    sid = pub.new_snapshot_id()
    pub.publish(sid, text, [{"id": f"c{i}"} for i in range(n_channels)], {"label": label}, {"created_at": "2026-01-01T00:00:00Z", "channels": n_channels})
    return sid, text


def test_nothing_published_initially(publisher):
    assert publisher.current() is None and publisher.channels() == []


def test_publish_makes_the_playlist_current(publisher):
    sid, text = publish(publisher)
    cur = publisher.current()
    assert cur.playlist == text and cur.snapshot_id == sid and cur.meta["channels"] == 8
    assert publisher.channels()[0] == {"id": "c0"}
    assert json.loads((publisher.published / "report.json").read_text(encoding="utf-8")) == {"label": "run"}


def test_only_the_last_three_snapshots_are_kept(publisher):
    ids = [publish(publisher)[0] for _ in range(6)]
    assert publisher.list_snapshots() == sorted(ids, reverse=True)[:3]
    assert len(set(ids)) == 6  # ids never collide even within one second


def test_rollback_restores_an_earlier_playlist(publisher):
    first_id, first_text = publish(publisher, 8)
    second_id, second_text = publish(publisher, 10)
    assert publisher.current().playlist == second_text
    target = publisher.rollback()
    assert target == first_id and publisher.current().playlist == first_text
    assert publisher.rollback(second_id) == second_id and publisher.current().playlist == second_text
    with pytest.raises(RuntimeError):
        publisher.rollback("nope")


def test_corrupt_live_files_are_treated_as_no_playlist(publisher):
    publish(publisher)
    (publisher.published / "playlist.m3u").write_text("garbage", encoding="utf-8")
    assert publisher.current() is None


def test_update_report_writes_text_and_json(publisher):
    sid, _ = publish(publisher)
    publisher.update_report(sid, {"final": True}, "plain text report\n")
    assert (publisher.published / "report.txt").read_text(encoding="utf-8") == "plain text report\n"
    assert json.loads((publisher.snapshots / sid / "report.json").read_text(encoding="utf-8")) == {"final": True}
