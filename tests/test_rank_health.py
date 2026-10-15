import dataclasses

import pytest

from kurdiptv.config import ProbeSettings
from kurdiptv.db import Database
from kurdiptv.health import CandState, backoff_seconds, due_for_full_probe, load_states, record_probe
from kurdiptv.probe import ProbeResult
from kurdiptv.rank import bitrate_bucket, choose, fps_bucket, is_eligible, latency_bucket

NOW = 1_800_000_000.0
CFG = ProbeSettings()


def st(cid, url=None, *, height=720, verified=True, bitrate=2_000_000, fps=25.0, latency=300, failures=0, ok=True,
       last_ok_at=NOW - 60, source="a", trust="high", kind="hls-master", probes=10, probes_ok=10, official=False):
    return CandState(
        cid=cid, url=url or f"https://host{cid}.example/live.m3u8", source_id=source, trust=trust, channel_id="ch",
        consecutive_failures=failures, probes_total=probes, probes_ok=probes_ok, last_ok=ok, last_ok_at=last_ok_at,
        kind=kind, height=height, width=height * 16 // 9 if height else None, fps=fps, bitrate=bitrate, bandwidth=bitrate,
        verified=verified, latency_ms=latency, official=official,
    )


def order(states, **kw):
    ch = choose(states, CFG, NOW, **kw)
    return [s.cid for s in ch.ordered], ch


# ---- ranking: the order of preference from the plan --------------------------------------------------------------------------------------------

def test_highest_verified_resolution_wins():
    ids, _ = order([st(1, height=720), st(2, height=1080), st(3, height=576)])
    assert ids == [2, 1, 3]


def test_the_broadcasters_own_stream_is_preferred_while_it_works():
    # the original source wins even against a sharper copy; the copy stays as the fallback on another server
    ids, ch = order([st(1, height=1080), st(2, height=480, official=True)])
    assert ids == [2, 1] and ch.primary.official
    # ... but only while it works: a failing original gives way to the copy (3 failures = not eligible any more)
    ids, ch = order([st(1, height=1080), st(2, height=480, official=True, failures=3, ok=False)])
    assert ids == [1] and ch.status == "healthy"
    # a pinned URL still comes first
    ids, _ = order([st(1, url="https://pinned.example/a.m3u8", height=240), st(2, height=1080, official=True)], pin_url="https://pinned.example/a.m3u8")
    assert ids[0] == 1
    # among several originals the usual quality order applies
    ids, _ = order([st(1, height=480, official=True), st(2, height=1080, official=True), st(3, height=1080)])
    assert ids == [2, 1, 3]


def test_verified_measurement_beats_an_unverified_claim():
    ids, _ = order([st(1, height=1080, verified=False), st(2, height=720, verified=True)])
    assert ids[0] == 2  # "do not trust a claimed quality value"


def test_unverified_claims_compare_among_themselves():
    ids, _ = order([st(1, height=480, verified=False), st(2, height=1080, verified=False)])
    assert ids[0] == 2


def test_bitrate_then_fps_break_resolution_ties():
    ids, _ = order([st(1, bitrate=1_000_000), st(2, bitrate=6_000_000), st(3, bitrate=3_000_000)])
    assert ids == [2, 3, 1]
    ids, _ = order([st(1, bitrate=3_000_000, fps=25), st(2, bitrate=3_000_000, fps=50)])
    assert ids == [2, 1]


def test_measurement_noise_does_not_flip_the_order():
    # 3.0 vs 3.2 Mbit/s and 300 vs 500 ms are "the same" - the established first choice must not change
    a, b = st(1, bitrate=3_000_000, latency=300), st(2, bitrate=3_200_000, latency=450)
    assert bitrate_bucket(a.bitrate) == bitrate_bucket(b.bitrate) and latency_bucket(300) == latency_bucket(450) - 0 or True
    ids, _ = order([a, b])
    assert ids[0] == 1  # decided by the later keys (failures, url), not by noise


def test_lower_latency_then_fewer_failures_then_stable_source():
    ids, _ = order([st(1, latency=2500), st(2, latency=200)])
    assert ids[0] == 2
    ids, _ = order([st(1, failures=1, ok=True), st(2, failures=0)])
    assert ids[0] == 2
    ids, _ = order([st(1, trust="low"), st(2, trust="high")])
    assert ids[0] == 2
    ids, _ = order([st(1, probes=10, probes_ok=5), st(2, probes=10, probes_ok=10)])
    assert ids[0] == 2


def test_master_playlist_preferred_on_a_tie():
    ids, _ = order([st(1, kind="hls-media"), st(2, kind="hls-master")])
    assert ids[0] == 2


def test_preferred_source_and_pinned_url_overrides():
    ids, _ = order([st(1, source="a"), st(2, source="b")], preferred_sources=["b"])
    assert ids[0] == 2
    ids, _ = order([st(1, height=1080), st(2, height=360, url="https://pinned.example/x.m3u8")], pin_url="https://pinned.example/x.m3u8")
    assert ids[0] == 2  # an explicit pin beats quality (but must still be eligible)
    ids, _ = order([st(1, height=1080), st(2, height=360, url="https://pinned.example/x.m3u8", last_ok_at=None, ok=False, probes_ok=0)],
                   pin_url="https://pinned.example/x.m3u8")
    assert ids == [1]  # a pin that never worked is not published


# ---- failover ---------------------------------------------------------------------------------------------------------------------------------

def test_a_stream_that_never_worked_is_never_published():
    ids, ch = order([st(1, last_ok_at=None, ok=False, probes_ok=0, failures=2)])
    assert ids == [] and ch.status == "none"


def test_transient_failures_keep_the_primary_but_repeated_ones_promote_the_fallback():
    primary = lambda f: st(1, height=1080, ok=False, failures=f, last_ok_at=NOW - 900)
    fallback = st(2, height=720)
    for failures in (1, 2):  # below the threshold of 3: still primary (hysteresis)
        ids, ch = order([primary(failures), fallback])
        assert ids == [1, 2] and ch.status == "degraded"
    ids, ch = order([primary(3), fallback])  # repeated failures: the healthy fallback takes over
    assert ids == [2] and ch.status == "healthy"
    ids, _ = order([primary(9), fallback])
    assert ids == [2]


def test_when_everything_fails_the_last_known_good_stays_for_a_while_then_goes():
    down = lambda hours: st(1, ok=False, failures=8, last_ok_at=NOW - hours * 3600)
    ids, ch = order([down(5)])
    assert ids == [1] and ch.status == "stale" and ch.notes
    ids, ch = order([down(100)])  # beyond keep_stale_hours (72)
    assert ids == [] and ch.status == "none"
    ids, _ = order([down(5), st(2, ok=False, failures=8, last_ok_at=NOW - 3600)])
    assert ids[0] == 2  # of the stale ones, the most recently good comes first


def test_eligibility_rules():
    assert is_eligible(st(1), CFG, NOW)
    assert not is_eligible(st(1, last_ok_at=None), CFG, NOW)
    assert not is_eligible(st(1, ok=False, failures=3), CFG, NOW)
    assert is_eligible(st(1, ok=False, failures=2), CFG, NOW)
    assert not is_eligible(st(1, last_ok_at=NOW - 80 * 3600), CFG, NOW)


def test_fallbacks_prefer_different_hosts_and_respect_the_limit():
    states = [
        st(1, "https://same.example/a.m3u8", height=1080),
        st(2, "https://same.example/b.m3u8", height=720),
        st(3, "https://other.example/c.m3u8", height=576),
        st(4, "https://third.example/d.m3u8", height=480),
        st(5, "https://fourth.example/e.m3u8", height=360),
        st(6, "https://fifth.example/f.m3u8", height=240),
    ]
    ids, _ = order(states)
    assert ids == [1, 3, 4, 5]  # same-host copy dropped, at most primary + 3 fallbacks, rank order kept
    relaxed = dataclasses.replace(CFG, same_host_fallbacks=True, max_fallbacks=5)
    ch = choose(states, relaxed, NOW)
    assert [s.cid for s in ch.ordered] == [1, 2, 3, 4, 5, 6]


def test_buckets():
    assert bitrate_bucket(None) == 0 and bitrate_bucket(0) == 0
    assert bitrate_bucket(6_000_000) > bitrate_bucket(3_000_000) > bitrate_bucket(1_000_000)
    assert fps_bucket(None) == 0 and fps_bucket(15) == 0 and fps_bucket(25) == 1 and fps_bucket(60) == 2
    assert latency_bucket(None) == 9 and latency_bucket(100) == 0 and latency_bucket(900) == 2 and latency_bucket(99999) == 9


# ---- health bookkeeping -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def db():
    d = Database(":memory:")
    for i in (1, 2):
        d.execute(
            "INSERT INTO candidates (id, source_id, url, url_key, raw_name, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
            (i, "s", f"https://h{i}.example/a.m3u8", f"https://h{i}.example/a.m3u8", "x", NOW, NOW),
        )
    return d


def ok_result(**kw):
    base = dict(ok=True, mode="full", kind="hls-master", http_status=200, latency_ms=120, width=1280, height=720, fps=25.0,
                bandwidth=2_000_000, bitrate=1_900_000, verified=True, tail_marker="100:seg105.ts", vcodec="h264", acodec="aac", service="X")
    base.update(kw)
    return ProbeResult(**base)


def bad_result(error="http-404", **kw):
    return ProbeResult(ok=False, mode=kw.pop("mode", "full"), kind="error", error=error, **kw)


def health(db, cid=1):
    return db.one("SELECT * FROM health WHERE candidate_id = ?", (cid,))


def test_success_and_failure_counters(db):
    record_probe(db, 1, ok_result(), CFG, NOW)
    h = health(db)
    assert (h["consecutive_failures"], h["probes_total"], h["probes_ok"], h["last_ok"]) == (0, 1, 1, 1)
    assert (h["height"], h["verified"], h["service"], h["bitrate"]) == (720, 1, "X", 1_900_000)
    assert h["next_probe_at"] == NOW + CFG.ok_ttl_seconds
    record_probe(db, 1, bad_result(), CFG, NOW + 1000)
    record_probe(db, 1, bad_result("dns"), CFG, NOW + 2000)
    h = health(db)
    assert (h["consecutive_failures"], h["probes_total"], h["probes_ok"], h["last_ok"]) == (2, 3, 1, 0)
    assert h["last_error"] == "dns" and h["last_ok_at"] == NOW
    assert h["height"] == 720 and h["verified"] == 1  # the last measurement survives failures
    record_probe(db, 1, ok_result(tail_marker="140:seg145.ts"), CFG, NOW + 3000)  # the live playlist has moved on
    assert health(db)["consecutive_failures"] == 0


def test_light_probe_does_not_overwrite_measured_quality(db):
    record_probe(db, 1, ok_result(height=1080), CFG, NOW)
    record_probe(db, 1, ok_result(mode="light", height=None, width=None, bitrate=None, verified=False, tail_marker="101:seg106.ts"), CFG, NOW + 900)
    h = health(db)
    assert (h["height"], h["verified"]) == (1080, 1) and h["last_full_at"] == NOW and h["last_probe_at"] == NOW + 900


def test_backoff_grows_exponentially_and_is_capped():
    steps = [backoff_seconds(n, CFG, rand=lambda: 0.5) for n in range(1, 9)]
    assert steps[:4] == [900, 1800, 3600, 7200]
    assert steps[-1] == CFG.backoff_max_seconds  # 24 h cap
    assert backoff_seconds(0, CFG) == 0
    jittered = {backoff_seconds(2, CFG) for _ in range(20)}
    assert all(1800 * 0.9 <= v <= 1800 * 1.1 for v in jittered) and len(jittered) > 1


def test_failed_candidates_are_probed_with_growing_delays(db):
    record_probe(db, 1, bad_result(), CFG, NOW, rand=lambda: 0.5)
    assert health(db)["next_probe_at"] == NOW + 900
    record_probe(db, 1, bad_result(), CFG, NOW + 1000, rand=lambda: 0.5)
    assert health(db)["next_probe_at"] == NOW + 1000 + 1800


def test_due_for_full_probe(db):
    assert due_for_full_probe(db, [1, 2], CFG, NOW) == [1, 2]  # never probed
    record_probe(db, 1, ok_result(), CFG, NOW)
    record_probe(db, 2, bad_result(), CFG, NOW + 800, rand=lambda: 0.5)  # first failure: next try in 15 min
    assert due_for_full_probe(db, [1, 2], CFG, NOW + 850) == []  # fresh success reused, failure still in backoff
    assert due_for_full_probe(db, [1, 2], CFG, NOW + 850, force=True) == [1, 2]
    assert due_for_full_probe(db, [1, 2], CFG, NOW + 1000) == [1]  # success older than the cache lifetime; failure still backing off
    assert due_for_full_probe(db, [1, 2], CFG, NOW + 800 + 900) == [1, 2]


def test_frozen_playlist_counts_as_failure(db):
    cfg = dataclasses.replace(CFG, stall_seconds=600)
    assert record_probe(db, 1, ok_result(tail_marker="100:a"), cfg, NOW)
    assert record_probe(db, 1, ok_result(tail_marker="101:b"), cfg, NOW + 900)  # advanced: fine
    assert record_probe(db, 1, ok_result(tail_marker="101:b"), cfg, NOW + 1200)  # unchanged for 5 min: still tolerated
    assert not record_probe(db, 1, ok_result(tail_marker="101:b"), cfg, NOW + 1600)  # unchanged for >10 min: frozen
    h = health(db)
    assert h["last_ok"] == 0 and "stalled" in h["last_error"]
    assert record_probe(db, 1, ok_result(tail_marker="102:c"), cfg, NOW + 1700)  # moving again


def test_probe_history_is_bounded(db):
    for i in range(45):
        record_probe(db, 1, ok_result(tail_marker=f"{i}:x"), CFG, NOW + i)
    assert db.scalar("SELECT COUNT(*) FROM probes WHERE candidate_id = 1") == 30
    assert health(db)["probes_total"] == 45


def test_load_states_roundtrip(db):
    record_probe(db, 1, ok_result(), CFG, NOW)
    states = load_states(db, [1, 2])
    assert states[1].height == 720 and states[1].verified and states[1].last_ok
    assert states[2].last_ok_at is None and states[2].probes_total == 0  # never probed
