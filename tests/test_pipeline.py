"""End-to-end: the whole refresh/health pipeline against a fake internet (the acceptance criteria)."""

import asyncio
import dataclasses
import json

import pytest
from PIL import Image

from kurdiptv.m3u import parse_m3u
from kurdiptv.pipeline import RefreshBusy, health_check, refresh
from kurdiptv.playlist import validate_m3u
from kurdiptv.report import render_report_text
from kurdiptv.server import _scheduler
from kurdiptv import util

from helpers import advance
from world import (CANARY, KANALD, KSAT_A, KSAT_B, MIHRAB, NEWTV, RUDAW_A, RUDAW_B, SPEDA_DEAD, ZAGROS, build_world, fake_ffprobe)


@pytest.fixture
def world(web):
    return build_world(web)


@pytest.fixture
def app(make_app, world, settings):
    small = dataclasses.replace(settings, publish=dataclasses.replace(settings.publish, min_channels=3))  # the fake world is tiny
    return make_app(settings_override=small, canary_urls=(CANARY,))


def entries(app):
    return parse_m3u(app.publisher.current().playlist)


def primaries(app):
    return {e.attrs["tvg-id"]: e for e in entries(app) if "-alt" not in e.attrs["tvg-id"]}


def backups(app):
    return {e.attrs["tvg-id"]: e for e in entries(app) if "-alt" in e.attrs["tvg-id"]}


# ---- first run: discovery, dedup, selection, logos, report -----------------------------------------------------------------------------

async def test_first_refresh_publishes_a_deduplicated_verified_playlist(app, world, web):
    report = await refresh(app, "test")
    assert report["status"] == "published", report

    prim = primaries(app)
    assert set(prim) == {"rudaw-tv", "kurdsat-tv", "zagros-tv", "mihrab"}
    # duplicate channels from several sources -> one entry each, the rest kept as ordered fallbacks
    assert prim["rudaw-tv"].url == RUDAW_A and prim["kurdsat-tv"].url == KSAT_A
    alt = backups(app)
    assert alt["rudaw-tv-alt2"].url == RUDAW_B and alt["kurdsat-tv-alt2"].url == KSAT_B
    assert all(e.attrs["group-title"] == "Backup streams" for e in alt.values())
    assert web.count("rudaw-tv/master.m3u8") == 1  # listed by two sources, probed once

    # nothing that should be excluded was even requested
    assert web.count("kanald") == 0 and web.count("daserste") == 0
    assert validate_m3u(app.publisher.current().playlist, app.settings.publish) == []


async def test_every_entry_has_the_required_metadata_and_a_local_logo(app, world):
    await refresh(app, "test")
    for e in entries(app):
        for attr in ("tvg-id", "tvg-name", "tvg-logo", "group-title"):
            assert e.attrs[attr], (e.name, attr)
        assert e.name
    for cid in primaries(app):
        png = app.settings.logos_dir / f"{cid}.png"
        assert Image.open(png).size[0] > 0
    kinds = {r["channel_id"]: r["kind"] for r in app.db.query("SELECT channel_id, kind FROM logo_state")}
    assert kinds["rudaw-tv"] == "source"  # the source supplied a valid one
    assert kinds["zagros-tv"] == "placeholder" and kinds["mihrab"] == "placeholder"  # nothing valid: generated text tile


async def test_report_explains_every_decision(app, world):
    report = await refresh(app, "test")
    src = {s["source"]: s for s in report["sources"]}
    assert src["kurdtvs"]["status"] == "ok" and src["kurditv"]["status"] == "ok" and src["site123"]["status"] == "ok"
    assert src["karwan"]["status"] == "blocked" and "challenge" in src["karwan"]["error"]
    ch = report["channels"]
    assert {c["id"] for c in ch["new"]} == {"rudaw-tv", "kurdsat-tv", "zagros-tv", "mihrab"}
    assert [c["id"] for c in ch["unreviewed"]] == ["mihrab"]  # not in catalog.yaml: flagged for a human look
    assert {c["id"] for c in ch["merged"]} >= {"rudaw-tv", "kurdsat-tv"}
    rudaw = next(c for c in ch["merged"] if c["id"] == "rudaw-tv")
    assert [u["url"] for u in rudaw["urls"]] == [RUDAW_A, RUDAW_B] and set(rudaw["sources"]) == {"kurdtvs", "kurditv", "site123"}
    assert [c["id"] for c in ch["unverified"]] == ["speda-tv"] and ch["unverified"][0]["errors"] == {"http-404": 1}
    by_reason = report["excluded"]["by_reason"]
    assert by_reason == {"identity-mismatch": 1}  # Kanal D (its stream URL names the brand); the ARD post is in a foreign category
    assert report["probe_failures"] == {"http-404": 1}
    assert report["totals"]["channels_published"] == 4 and report["totals"]["streams_published"] == 6

    text = render_report_text(report)
    for needle in ("Sources", "New channels", "Merged / with fallbacks", "Excluded streams by reason", "karwan", "blocked"):
        assert needle in text
    assert (app.settings.published_dir / "report.txt").read_text(encoding="utf-8").startswith("Refresh #1")


async def test_selection_uses_measured_quality_not_claims_or_url_text(app, world, monkeypatch):
    fake_ffprobe(monkeypatch)
    app.prober.ffprobe = "fake-ffprobe"
    liar = "https://waar-a.example/live-1080p-hd/master.m3u8"  # URL and playlist both say 1080p ...
    honest = "https://waar-b.example/live/master.m3u8"
    world.stream(liar, height=480, claim=1080)  # ... the video is 480p
    world.stream(honest, height=720)
    world.m3u_entries += [["Waar TV", "Kurdish", liar], ["Waar TV", "Kurdish", honest]]
    world.publish_sources()
    await refresh(app, "test")
    assert primaries(app)["waar-tv"].url == honest
    assert backups(app)["waar-tv-alt2"].url == liar
    row = app.db.one("SELECT height, verified FROM health h JOIN candidates c ON c.id = h.candidate_id WHERE c.url = ?", (liar,))
    assert (row["height"], row["verified"]) == (480, 1)  # what was measured, not what was claimed


async def test_the_broadcasters_own_stream_leads_even_when_a_copy_is_sharper(app, world):
    # Kurdsat: the sharper copy (1080p, akamai) is listed by an aggregator; the broadcaster's own server serves 720p
    app.catalog.by_id["kurdsat-tv"] = dataclasses.replace(app.catalog.by_id["kurdsat-tv"], sites=("kurdsat.akamaized.net",))
    await refresh(app, "test")
    assert primaries(app)["kurdsat-tv"].url == KSAT_B  # original source first ...
    assert backups(app)["kurdsat-tv-alt2"].url == KSAT_A  # ... the copy is the fallback
    rows = json.loads((app.settings.published_dir / "channels.json").read_text(encoding="utf-8"))
    assert next(c for c in rows if c["id"] == "kurdsat-tv")["streams"][0]["official"] is True  # exposed for auditing


async def test_a_stream_found_on_the_broadcasters_own_page_is_the_original(make_app, world, settings):
    world.stream("https://waar-a.example/live/master.m3u8", 1080)
    world.stream("https://waar-b.example/live/master.m3u8", 480)
    world.m3u_entries += [["Waar TV", "Kurdish", "https://waar-a.example/live/master.m3u8"]]  # the aggregator's list
    world.kurditv_posts += [(4, "Waar TV ZINDI", "waar-tv-zindi", 3, ["https://waar-b.example/live/master.m3u8"])]
    world.publish_sources()
    flagged = dataclasses.replace(
        settings,
        publish=dataclasses.replace(settings.publish, min_channels=3),
        sources=tuple(dataclasses.replace(s, official=(s.id == "kurditv")) for s in settings.sources),  # pretend kurditv is Waar's own page
    )
    app = make_app(settings_override=flagged, canary_urls=(CANARY,))
    await refresh(app, "test")
    assert primaries(app)["waar-tv"].url == "https://waar-b.example/live/master.m3u8"
    assert backups(app)["waar-tv-alt2"].url == "https://waar-a.example/live/master.m3u8"


async def test_stream_that_announces_itself_as_another_network_is_rejected(app, world, monkeypatch):
    fake_ffprobe(monkeypatch)
    app.prober.ffprobe = "fake-ffprobe"
    world.streams[MIHRAB].service = "ZDF HD"
    report = await refresh(app, "test")
    assert "mihrab" not in primaries(app)
    assert any("identity mismatch" in k for k in report["probe_failures"])


# ---- discovery keeps working --------------------------------------------------------------------------------------------------------------

async def test_a_new_source_channel_appears_after_the_next_refresh_without_any_edit(app, world):
    await refresh(app, "test")
    assert "brand-new-kurdish" not in primaries(app)
    world.stream(NEWTV, 720)
    world.m3u_entries.append(["Brand New Kurdish TV", "Kurdish", NEWTV])
    world.publish_sources()
    advance(13 * 3600)  # cached source pages have expired
    report = await refresh(app, "test")
    assert report["status"] == "published"
    assert primaries(app)["brand-new-kurdish"].url == NEWTV
    assert "brand-new-kurdish" in [c["id"] for c in report["channels"]["new"]]
    assert {"rudaw-tv", "kurdsat-tv", "zagros-tv", "mihrab"} <= set(primaries(app))  # nothing else disturbed


async def test_a_rendition_playlist_and_its_master_are_one_stream_and_the_master_wins(app, world):
    base = "https://wowza.example/live/app/"
    world.stream(base + "playlist.m3u8", 720)  # master
    media = world.web.add_stream(base + "chunklist.m3u8", master=False, variants=[])  # one rendition of it
    world.streams[base + "chunklist.m3u8"] = media
    world.m3u_entries.append(["Chunk Test Kurdish TV", "Kurdish", base + "chunklist.m3u8"])  # the page lists only the rendition
    world.publish_sources()
    await refresh(app, "test")
    assert primaries(app)["chunk-test-kurdish"].url == base + "playlist.m3u8"  # the master was derived and preferred
    assert "chunk-test-kurdish-alt2" not in backups(app)  # not a second stream, just another manifest of the same one

    world.streams[base + "playlist.m3u8"].alive = False  # master dies: the rendition still works and takes over
    for _ in range(3):
        advance(7 * 3600)
        await refresh(app, "test")
    assert primaries(app)["chunk-test-kurdish"].url == base + "chunklist.m3u8"


async def test_a_packager_rendition_gets_its_master_tried_and_the_master_wins(app, world):
    base = "https://packager.example/live/DB/NEWS_HD/HLS/"
    rendition = base + "NEWS_HD-avc1_2500000=10002,mp4a_128000=20000.m3u8"
    world.stream(base + "NEWS_HD.m3u8", 1080)  # the channel's master, listed by no source
    media = world.web.add_stream(rendition, master=False, variants=[])
    world.streams[rendition] = media
    world.m3u_entries.append(["Packager Test Kurdish TV", "Kurdish", rendition])  # the list carries only one rendition of it
    world.publish_sources()
    await refresh(app, "test")
    assert primaries(app)["packager-test-kurdish"].url == base + "NEWS_HD.m3u8"
    assert "packager-test-kurdish-alt2" not in backups(app)

    world.streams[base + "NEWS_HD.m3u8"].alive = False  # master dies: the rendition takes over
    for _ in range(3):
        advance(7 * 3600)
        await refresh(app, "test")
    assert primaries(app)["packager-test-kurdish"].url == rendition


async def test_the_report_names_known_unavailable_channels_in_plain_words(app, world):
    world.streams[KSAT_A].alive = False
    world.streams[KSAT_B].alive = False  # Kurdsat: every address dead
    report = await refresh(app, "test")
    gone = {c["id"]: c for c in report["channels"]["unverified"]}
    assert gone["kurdsat-tv"]["in_catalog"] is True and "official_errors" in gone["kurdsat-tv"]
    from kurdiptv.report import unavailable_channels

    assert dict(unavailable_channels(report))["Kurdsat TV"] == "its stream address no longer exists"
    assert "Speda TV" in dict(unavailable_channels(report))  # also catalogued and dead (404)


async def test_the_reason_comes_from_the_broadcasters_own_address_even_when_other_sources_list_it_too(make_app, world, web, settings):
    own = "https://kurdsat-official.example/hls/kurdsat.m3u8"
    world.stream(own, 720).status = 403  # the broadcaster's server refuses every player but its own website
    world.stream(KSAT_A, 1080, alive=False)
    world.stream(KSAT_B, 720, alive=False)  # the copies on aggregator pages are simply gone
    world.m3u_entries.append(["Kurdsat Tv", "News General Local Kurdish", own])  # an ordinary source lists the same address as well
    world.publish_sources()
    web.add("https://kurdsat-official.example/robots.txt", text="", content_type="text/plain")
    web.add("https://kurdsat-official.example/live", text=f'<html><script>player.load("{own}")</script></html>')
    sources = tuple(
        dataclasses.replace(src, options={"channels": [{"name": "Kurdsat TV", "pages": ["https://kurdsat-official.example/live"]}]})
        if src.id == "official" else src
        for src in settings.sources
    )
    small = dataclasses.replace(settings, sources=sources, publish=dataclasses.replace(settings.publish, min_channels=3))
    app = make_app(settings_override=small, canary_urls=(CANARY,))
    report = await refresh(app, "test")
    entry = next(c for c in report["channels"]["unverified"] if c["id"] == "kurdsat-tv")
    assert entry["official_errors"] == {"http-403": 1} and "http-404" in entry["errors"]
    from kurdiptv.report import unavailable_channels

    assert dict(unavailable_channels(report))["Kurdsat TV"] == "only plays on its own website or app and refuses other players"


async def test_unchanged_refresh_reuses_probes_and_keeps_the_snapshot(app, world):
    first = await refresh(app, "test")
    snap = app.publisher.current().snapshot_id
    created = app.publisher.current().meta["created_at"]
    advance(120)
    second = await refresh(app, "test")
    assert second["status"] == "unchanged" and second["snapshot_id"] == snap
    assert second["totals"]["probed"] == 0 and second["totals"]["probe_reused"] > 0
    assert app.publisher.list_snapshots() == [snap]
    meta = app.publisher.current().meta
    assert meta["created_at"] == created and meta["checked_at"] != created


async def test_streams_with_embedded_credentials_never_appear(app, world):
    world.m3u_entries.append(["Paid Panel TV", "Kurdish", "http://185.166.25.198:25461/live/user/secretpass/1576.m3u8"])
    world.m3u_entries.append(["Tokened TV", "Kurdish", "https://cdn.example/a.m3u8?token=abc"])
    world.publish_sources()
    report = await refresh(app, "test")
    text = app.publisher.current().playlist
    assert "secretpass" not in text and "token=" not in text
    assert report["excluded"]["by_reason"]["unsafe-url"] == 2
    # ... and the rejected URLs are not republished in any report either
    import json

    everything = json.dumps(report) + render_report_text(report)
    for f in app.settings.published_dir.glob("report.*"):
        everything += f.read_text(encoding="utf-8")
    assert "secretpass" not in everything and "token=abc" not in everything
    shown = {e["name"]: e["url"] for e in report["excluded"]["examples"]}
    assert shown["Paid Panel TV"] == "http://185.166.25.198:25461/(credentials removed)"


# ---- outages ----------------------------------------------------------------------------------------------------------------------------------

async def test_source_outage_leaves_the_playlist_intact(app, world):
    await refresh(app, "test")
    before = app.publisher.current().playlist
    world.sources_down |= {"kurdtvs", "kurditv", "site123"}
    world.publish_sources()
    advance(25 * 3600)  # every cached page (12 h / 24 h) has expired
    report = await refresh(app, "test")
    assert {s["status"] for s in report["sources"] if s["source"] != "karwan"} == {"failed"}
    assert report["status"] in {"unchanged", "published"}
    assert app.publisher.current().playlist == before  # candidates are remembered while their source is down
    state = {r["source_id"]: r["consecutive_failures"] for r in app.db.query("SELECT * FROM source_state")}
    assert state["kurdtvs"] == 1 and state["kurditv"] == 1


async def test_one_failing_source_does_not_stop_the_others(make_app, world, settings):
    tiny = dataclasses.replace(settings, publish=dataclasses.replace(settings.publish, min_channels=1))
    app = make_app(settings_override=tiny, canary_urls=(CANARY,))
    world.sources_down |= {"kurdtvs"}
    world.publish_sources()
    report = await refresh(app, "test")
    src = {s["source"]: s["status"] for s in report["sources"]}
    assert src["kurdtvs"] == "failed" and src["kurditv"] == "ok" and src["site123"] == "ok"
    assert report["status"] == "published" and "rudaw-tv" in primaries(app)  # via kurditv + site123


async def test_everything_dead_never_replaces_the_playlist_with_an_empty_one(app, world):
    await refresh(app, "test")
    before = app.publisher.current().playlist
    for s in world.streams.values():
        s.alive = False
    advance(100 * 3600)  # far beyond the 72 h the last known good streams are kept
    report = await refresh(app, "test", force=True)
    assert report["status"] == "held" and report["problems"]
    assert app.publisher.current().playlist == before
    assert app.state.last_refresh["status"] == "held"


async def test_a_sudden_collapse_of_the_channel_list_is_held_back(app, world):
    extra = []
    for i in range(12):
        url = f"https://cdn{i}.example/live/master.m3u8"
        world.stream(url, 720)
        world.m3u_entries.append([f"Test Kurdish {chr(65 + i)} TV", "Kurdish", url])
        extra.append(url)
    world.publish_sources()
    await refresh(app, "test")
    n_before = len(primaries(app))
    assert n_before == 16
    before = app.publisher.current().playlist
    for url in extra[:11]:
        world.streams[url].alive = False
    advance(100 * 3600)
    report = await refresh(app, "test", force=True)
    assert report["status"] == "held" and any("dropped from 16" in p for p in report["problems"])
    assert app.publisher.current().playlist == before


async def test_stale_channels_survive_a_short_total_outage_of_their_streams(app, world):
    await refresh(app, "test")
    for s in world.streams.values():
        s.alive = False
    advance(6 * 3600)
    report = await refresh(app, "test", force=True)
    assert {"rudaw-tv", "kurdsat-tv", "zagros-tv", "mihrab"} <= set(primaries(app))  # kept (last known good)
    assert report["channels"]["stale"], "reported as running on failing streams"


async def test_losing_internet_changes_nothing(app, world, web):
    await refresh(app, "test")
    before = app.publisher.current().playlist
    probes_before = app.db.scalar("SELECT COUNT(*) FROM probes")
    failures_before = app.db.scalar("SELECT SUM(consecutive_failures) FROM health")
    web.offline = True
    advance(7 * 3600)
    report = await refresh(app, "test", force=True)
    assert report["status"] == "offline"
    assert app.publisher.current().playlist == before
    assert app.db.scalar("SELECT COUNT(*) FROM probes") == probes_before  # no poisoned health data
    assert app.db.scalar("SELECT SUM(consecutive_failures) FROM health") == failures_before
    health = await health_check(app)
    assert health["status"] == "offline"


async def test_internet_lost_midway_through_probing_discards_the_run(app, world, web):
    await refresh(app, "test")
    calls = {"n": 0}
    original = app.online

    async def flaky_online():
        calls["n"] += 1
        return calls["n"] == 1  # reachable at the start, gone when probing has finished

    app.online = flaky_online
    advance(7 * 3600)
    report = await refresh(app, "test", force=True)
    assert report["status"] == "offline" and "lost during the run" in report["error"]
    app.online = original


async def test_concurrent_refresh_is_refused(app, world):
    first = asyncio.create_task(refresh(app, "a"))
    await asyncio.sleep(0)
    with pytest.raises(RefreshBusy):
        await refresh(app, "b")
    assert (await first)["status"] == "published"


async def test_a_crash_inside_the_pipeline_cannot_touch_the_playlist(app, world, monkeypatch):
    await refresh(app, "test")
    before = app.publisher.current().playlist
    import kurdiptv.pipeline as pl

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(pl, "_rank", boom)
    advance(7 * 3600)
    report = await refresh(app, "test", force=True)
    assert report["status"] == "failed" and "bug" in report["error"]
    assert app.publisher.current().playlist == before


# ---- failover -------------------------------------------------------------------------------------------------------------------------------------

async def test_health_check_promotes_a_fallback_after_repeated_failures(app, world):
    await refresh(app, "test")
    snap0 = app.publisher.current().snapshot_id
    assert primaries(app)["rudaw-tv"].url == RUDAW_A
    world.streams[RUDAW_A].alive = False

    for _ in range(2):  # one or two failures: tolerated, nothing changes
        advance(900)
        out = await health_check(app)
        assert out["status"] == "ok" and out["failing"] == 1 and out["changes"] == []
        assert primaries(app)["rudaw-tv"].url == RUDAW_A and app.publisher.current().snapshot_id == snap0

    advance(900)  # third consecutive failure: promote the healthy fallback
    out = await health_check(app)
    assert [c["change"] for c in out["changes"]] == ["failover"] and out["changes"][0]["to"] == RUDAW_B
    assert primaries(app)["rudaw-tv"].url == RUDAW_B
    assert app.publisher.current().snapshot_id != snap0 and "rudaw-tv-alt2" not in backups(app)
    assert primaries(app)["kurdsat-tv"].url == KSAT_A  # untouched channels stay as they were

    # when the old primary recovers, the next full refresh puts the better stream back first
    world.streams[RUDAW_A].alive = True
    advance(7 * 3600)
    await refresh(app, "test", force=True)
    assert primaries(app)["rudaw-tv"].url == RUDAW_A and backups(app)["rudaw-tv-alt2"].url == RUDAW_B


async def test_full_refresh_also_promotes_after_repeated_failures(app, world):
    await refresh(app, "test")
    world.streams[KSAT_A].alive = False
    for i in range(3):
        advance(7 * 3600)
        await refresh(app, "test")
        expected = KSAT_A if i < 2 else KSAT_B
        assert primaries(app)["kurdsat-tv"].url == expected, i


async def test_frozen_stream_is_treated_as_failed(app, world):
    await refresh(app, "test")
    world.streams[RUDAW_A].advancing = False  # keeps serving the same playlist forever
    # FakeStream keeps seq constant unless changed, so a frozen playlist is simply "not changing"
    for _ in range(4):
        advance(900)
        out = await health_check(app)
    assert out["failing"] >= 1


async def test_health_check_without_a_playlist_is_a_noop(app, world):
    out = await health_check(app)
    assert out["status"] == "skipped"


# ---- startup behaviour ---------------------------------------------------------------------------------------------------------------------------

async def run_scheduler(app, seconds=3.0):
    task = asyncio.create_task(_scheduler(app))
    try:
        for _ in range(int(seconds / 0.05)):
            await asyncio.sleep(0.05)
            if app.state.last_refresh:
                break
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_startup_refresh_publishes_when_nothing_exists_yet(app, world):
    await run_scheduler(app)
    assert app.state.last_refresh["trigger"] == "startup" and app.state.last_refresh["status"] == "published"
    assert app.publisher.current() is not None and app.state.next_refresh_at and app.state.next_health_at


async def test_startup_refresh_with_sources_down_keeps_the_last_playlist(app, world, web):
    await refresh(app, "test")
    before = app.publisher.current().playlist
    app.state.last_refresh = None
    world.sources_down |= {"kurdtvs", "kurditv", "site123"}
    world.publish_sources()
    advance(13 * 3600)
    await run_scheduler(app)
    assert app.state.last_refresh["trigger"] == "startup"
    assert app.publisher.current().playlist == before


async def test_startup_while_offline_never_publishes_and_retries_soon(app, world, web):
    web.offline = True
    await run_scheduler(app)
    assert app.state.last_refresh["status"] == "offline" and app.publisher.current() is None
    assert 0 < app.state.next_refresh_at - util.now() <= 15 * 60 + 5  # try again in 15 min, not after 6 h


async def test_startup_refresh_can_be_disabled(make_app, settings, world):
    s = dataclasses.replace(settings, service=dataclasses.replace(settings.service, refresh_on_startup=False))
    app = make_app(settings_override=s, canary_urls=(CANARY,))
    task = asyncio.create_task(_scheduler(app))
    await asyncio.sleep(0.2)
    task.cancel()
    assert app.state.last_refresh is None


# ---- takedown --------------------------------------------------------------------------------------------------------------------------------------

async def test_blocklisted_channel_disappears_at_the_next_refresh(app, world):
    await refresh(app, "test")
    assert "zagros-tv" in primaries(app)
    app.policy = dataclasses.replace(app.policy, block_ids=frozenset({"zagros-tv"}))
    advance(7 * 3600)
    report = await refresh(app, "test")
    assert "zagros-tv" not in primaries(app)
    assert any(c["id"] == "zagros-tv" for c in report["channels"]["removed"])
    assert report["excluded"]["by_reason"]["blocklist"] >= 1
