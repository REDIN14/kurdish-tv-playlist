"""The refresh pipeline: discover -> store -> normalise/match -> probe -> rank -> logos -> validate -> publish.

`refresh()` is the full run (startup / every 6 h / on demand). `health_check()` is the cheap 15-minute pass
that re-probes only published streams and promotes a healthy fallback when the primary keeps failing.
Neither can leave the published playlist worse than it was: invalid or shrunken results are held back.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections import Counter, defaultdict

from . import __version__
from .adapters import ADAPTERS, run_adapter
from .app import App
from .fetcher import SourceFetcher
from .health import CandState, due_for_full_probe, load_states, record_probe
from .match import ChannelGroup, MatchResult, match_candidates
from .models import SourceResult
from .playlist import PublishedChannel, PublishedStream, render_m3u, validate_m3u
from .probe import ProbeResult
from .rank import Choice, choose
from .report import iso, render_report_text
from .store import load_active_candidates, record_source_run, save_assignment, upsert_candidate
from .util import now, url_key

log = logging.getLogger(__name__)


class RefreshBusy(RuntimeError):
    """Another refresh or health check is running."""


class Offline(RuntimeError):
    """The machine lost internet access mid-run; results would be meaningless."""


# ---- helpers -----------------------------------------------------------------------------------------------------------

def _stream_from_state(s: CandState) -> PublishedStream:
    return PublishedStream(
        cid=s.cid, url=s.url, source_id=s.source_id, height=s.height, width=s.width, fps=s.fps, bitrate=s.bitrate,
        verified=s.verified, latency_ms=s.latency_ms, vcodec=s.vcodec, acodec=s.acodec,
    )


def channel_to_dict(ch: PublishedChannel, candidate_ids: list[int]) -> dict:
    d = dataclasses.asdict(ch)
    d["candidate_ids"] = candidate_ids
    return d


def channel_from_dict(d: dict) -> tuple[PublishedChannel, list[int]]:
    d = dict(d)
    ids = list(d.pop("candidate_ids", []))
    streams = [PublishedStream(**s) for s in d.pop("streams", [])]
    return PublishedChannel(streams=streams, **d), ids


def _trust_map(app: App) -> dict[str, str]:
    return {s.id: s.trust for s in app.settings.sources}


def _states_for(app: App, cids: list[int]) -> list[CandState]:
    """Candidate states for ranking: one per distinct stream URL (the same URL from two sources is one stream)."""
    trust = _trust_map(app)
    loaded = sorted(load_states(app.db, cids).values(), key=lambda s: s.cid)
    for s in loaded:
        s.trust = trust.get(s.source_id, "low")
    best: dict[str, CandState] = {}
    for s in loaded:
        key = url_key(s.url)
        known = best.get(key)
        if known is None or (s.trust == "high" and known.trust != "high"):
            best[key] = s
    states = list(best.values())
    # A Wowza ".../chunklist.m3u8" is one rendition of the master ".../playlist.m3u8" beside it: the same stream. While the
    # master works the rendition is redundant; if the master stops working the rendition remains as the way in.
    working_masters = {s.url for s in states if s.url.endswith("/playlist.m3u8") and s.last_ok}
    return [
        s for s in states
        if not (s.url.endswith("/chunklist.m3u8") and s.url[: -len("chunklist.m3u8")] + "playlist.m3u8" in working_masters)
    ]


def _choose(app: App, channel_id: str, states: list[CandState], ts: float) -> Choice:
    pol = app.policy
    return choose(
        states, app.settings.probe, ts,
        preferred_sources=pol.preferred_source.get(channel_id),
        pin_url=pol.pin_url.get(channel_id),
    )


def _new_report(run_id: int, trigger: str, started: float) -> dict:
    return {
        "run_id": run_id, "trigger": trigger, "version": __version__, "started_at": iso(started), "finished_at": None,
        "duration_s": None, "status": "running", "error": "", "sources": [], "totals": {},
        "channels": {"new": [], "removed": [], "changed": [], "merged": [], "stale": [], "unverified": [], "unreviewed": []},
        "excluded": {"by_reason": {}, "examples": []}, "warnings": [], "problems": [], "probe_failures": {},
    }


def _failure_class(error: str) -> str:
    return (error.split(" (")[0].split(":")[0] or "unknown")[:48]


# ---- full refresh --------------------------------------------------------------------------------------------------------

async def refresh(app: App, trigger: str = "manual", *, discover: bool = True, force: bool = False) -> dict:
    if app.lock.locked():
        raise RefreshBusy("a refresh or health check is already running")
    async with app.lock:
        app.state.refreshing = True
        try:
            return await _refresh(app, trigger, discover, force)
        finally:
            app.state.refreshing = False


async def _refresh(app: App, trigger: str, discover: bool, force: bool) -> dict:
    db = app.db
    started = now()
    t0 = time.monotonic()
    run_id = int(db.execute("INSERT INTO runs (trigger, started_at, status) VALUES (?, ?, 'running')", (trigger, started)).lastrowid)
    report = _new_report(run_id, trigger, started)
    snapshot_id = None
    try:
        if not await app.online():
            report["status"], report["error"] = "offline", "no internet connection: nothing was changed, the last playlist stays published"
            log.warning(report["error"])
        else:
            if discover:
                await _discover(app, report, force)
            match = _match(app, report)
            await _probe(app, match, report, force)
            channels, id_map = _rank(app, match, report)
            await _logos(app, channels, match)
            snapshot_id = _publish(app, report, channels, id_map, trigger, run_id)
    except Offline:
        report["status"], report["error"] = "offline", "internet connection lost during the run: nothing was changed"
        log.warning(report["error"])
    except Exception as exc:  # last line of defence: a crash must never touch the published playlist
        log.exception("refresh crashed")
        report["status"], report["error"] = "failed", f"{type(exc).__name__}: {exc}"
    report["finished_at"] = iso(now())
    report["duration_s"] = round(time.monotonic() - t0, 1)
    if snapshot_id:
        app.publisher.update_report(snapshot_id, report, render_report_text(report))
    db.execute(
        "UPDATE runs SET finished_at = ?, status = ?, snapshot_id = ?, report_json = ? WHERE id = ?",
        (now(), report["status"], snapshot_id, json.dumps(report, ensure_ascii=False), run_id),
    )
    app.state.last_refresh = _summary(report)
    log.info("refresh %s: %s in %.0fs - %s", run_id, report["status"], report["duration_s"], report["totals"])
    return report


def _summary(report: dict) -> dict:
    return {
        "run_id": report["run_id"], "trigger": report["trigger"], "status": report["status"], "error": report["error"],
        "finished_at": report["finished_at"], "duration_s": report["duration_s"], "totals": report["totals"],
        "problems": report["problems"][:5],
    }


async def _discover(app: App, report: dict, force: bool) -> None:
    cfg = app.settings
    tasks, missing = [], []
    for src in cfg.sources:
        if not src.enabled:
            continue
        adapter_cls = ADAPTERS.get(src.adapter)
        if adapter_cls is None:
            missing.append(SourceResult(src.id, "failed", error=f"unknown adapter {src.adapter!r}"))
            continue
        fetcher = SourceFetcher(app.http, app.db, src, app.robots, respect_robots=cfg.http.respect_robots, force_refresh=force)
        tasks.append(run_adapter(adapter_cls(src, fetcher)))
    results: list[SourceResult] = [*await asyncio.gather(*tasks), *missing]
    ts = now()
    new_by_source: Counter = Counter()
    with app.db.tx():
        for res in results:
            for cand in res.candidates:
                _, is_new = upsert_candidate(app.db, cand, ts)
                if is_new:
                    new_by_source[res.source_id] += 1
            record_source_run(app.db, res, ts)
    report["sources"] = [{**res.summary(), "new_candidates": new_by_source[res.source_id]} for res in results]
    bad = [r for r in results if r.status in {"failed", "blocked"}]
    for r in bad:
        log.warning("source %s: %s - %s", r.source_id, r.status, r.error)


def _match(app: App, report: dict) -> MatchResult:
    ts = now()
    items = load_active_candidates(app.db, app.settings.sources, ts)
    match = match_candidates(items, app.catalog, app.policy)
    with app.db.tx():
        for g in match.groups.values():
            for m in g.members:
                save_assignment(app.db, m.cid, g.id, "in", m.how)
        for m in match.excluded:
            save_assignment(app.db, m.cid, m.channel_id, "out", m.reason)
    by_reason: Counter = Counter(m.reason.split(":", 1)[0] for m in match.excluded)
    report["excluded"] = {
        "by_reason": dict(by_reason.most_common()),
        "examples": [
            {"name": m.cand.name, "source": m.source_id, "url": m.cand.url, "reason": m.reason}
            for m in sorted(match.excluded, key=lambda x: (x.reason, x.cand.name))
        ][:400],
    }
    report["warnings"] = [{"kind": w["kind"], "message": w["message"], "channel": w["channel"]} for w in match.warnings]
    for g in match.groups.values():
        for w in g.warnings:
            if not any(w == x["message"] for x in report["warnings"]):
                report["warnings"].append({"kind": "match", "message": w, "channel": g.id})
    report["totals"].update(
        candidates_active=len(items), candidates_excluded=len(match.excluded), channels_in_scope=len(match.groups)
    )
    return match


def _identity_veto(app: App, res: ProbeResult) -> ProbeResult:
    """A stream that announces itself as a foreign network (MPEG-TS service name "ZDF") is not what the label claims."""
    if res.ok and res.service:
        reason = app.policy.denied_by_name(res.service)
        if reason:
            return dataclasses.replace(res, ok=False, error=f"identity mismatch: stream announces itself as {res.service!r}")
    return res


async def _probe(app: App, match: MatchResult, report: dict, force: bool) -> None:
    ts = now()
    urls = {m.cid: m.cand.url for g in match.groups.values() for m in g.members}
    due = due_for_full_probe(app.db, urls, app.settings.probe, ts, force)
    # The same stream URL listed by several sources is probed once and the result shared.
    by_key: dict[str, list[int]] = defaultdict(list)
    for cid in due:
        by_key[url_key(urls[cid])].append(cid)
    keys = list(by_key)
    log.info("probing %d distinct streams (%d candidates, %d reused from recent probes)", len(keys), len(urls), len(urls) - len(due))
    results = await asyncio.gather(*[app.prober.probe(urls[by_key[k][0]], "full") for k in keys])
    if keys and not await app.online():
        raise Offline()
    failures: Counter = Counter()
    ok_count = 0
    ts = now()
    for key, res in zip(keys, results):
        res = _identity_veto(app, res)
        effective = False
        for cid in by_key[key]:
            effective = record_probe(app.db, cid, res, app.settings.probe, ts)
        if effective:
            ok_count += 1
        else:
            failures[_failure_class(res.error or "stalled")] += 1
    report["probe_failures"] = dict(failures.most_common())
    report["totals"].update(probed=len(keys), probe_ok=ok_count, probe_failed=len(keys) - ok_count, probe_reused=len(urls) - len(due))


def _primary_changed(app: App, old_cid: int | None, new_primary: CandState) -> bool:
    """True when the published primary is a different stream (not merely a different row for the same URL)."""
    if old_cid == new_primary.cid:
        return False
    row = app.db.one("SELECT url FROM candidates WHERE id = ?", (old_cid,)) if old_cid else None
    return row is None or url_key(row["url"]) != url_key(new_primary.url)


def _rank(app: App, match: MatchResult, report: dict) -> tuple[list[PublishedChannel], dict[str, list[int]]]:
    ts = now()
    db = app.db
    prev = {r["channel_id"]: r for r in db.query("SELECT * FROM channel_state WHERE status IN ('published', 'stale')")}
    catalog_order = {c.id: i for i, c in enumerate(app.catalog.channels)}
    channels: list[PublishedChannel] = []
    id_map: dict[str, list[int]] = {}
    chans = report["channels"]

    for gid, group in sorted(match.groups.items()):
        cids = [m.cid for m in group.members]
        states = _states_for(app, cids)
        by_cid = {s.cid: s for s in states}
        choice = _choose(app, gid, states, ts)
        if choice.primary is None:
            errors = Counter(_failure_class(s.last_error) for s in states if s.last_error)
            chans["unverified"].append(
                {"id": gid, "name": group.name, "candidates": len(states), "sources": sorted(group.sources),
                 "errors": dict(errors.most_common(3))}
            )
            continue
        ch = PublishedChannel(
            id=gid, name=group.name, group=group.category, language=group.language, country=group.country,
            streams=[_stream_from_state(s) for s in choice.ordered], status=choice.status,
            in_catalog=group.catalog is not None, order=catalog_order.get(gid, 10_000),
        )
        channels.append(ch)
        id_map[gid] = cids

        entry = {"id": gid, "name": group.name, "sources": sorted(group.sources), "streams": len(ch.streams), "status": choice.status}
        if choice.status in {"stale", "degraded"}:
            chans["stale"].append({**entry, "note": "; ".join(choice.notes) or "primary stream is failing"})
        if len(group.sources) >= 2 or len(group.members) >= 2:
            chans["merged"].append(
                {**entry, "name": group.name, "urls": [{"url": s.url, "source": s.source_id, "height": s.height, "verified": s.verified} for s in ch.streams]}
            )
        before = prev.get(gid)
        if before is None:
            chans["new"].append(entry)
            if group.unknown:
                chans["unreviewed"].append({**entry, "note": "not in catalog.yaml - published automatically, please review the name"})
        elif _primary_changed(app, before["primary_candidate_id"], choice.primary):
            old_state = by_cid.get(before["primary_candidate_id"])
            if old_state is None:
                loaded = _states_for(app, [before["primary_candidate_id"]])
                old_state = loaded[0] if loaded else None
            if old_state is None:
                reason = "previous primary stream is no longer a candidate"
            elif old_state.consecutive_failures >= app.settings.probe.fail_threshold or not old_state.last_ok:
                reason = f"previous primary failing ({old_state.consecutive_failures} consecutive failures: {old_state.last_error or 'unknown'})"
            else:
                reason = "a better stream was ranked first (resolution/bitrate/latency)"
            chans["changed"].append({**entry, "from": old_state.url if old_state else None, "to": choice.primary.url, "reason": reason})

    for gid, before in prev.items():
        if gid not in id_map:
            reason = "no stream passed verification" if gid in match.groups else "no longer listed by any source / out of scope"
            chans["removed"].append({"id": gid, "name": before["name"], "reason": reason})
    report["totals"].update(channels_published=len(channels), streams_published=sum(len(c.streams) for c in channels))
    return channels, id_map


async def _logos(app: App, channels: list[PublishedChannel], match: MatchResult) -> None:
    await app.logos.prepare()
    sem = asyncio.Semaphore(4)

    async def one(ch: PublishedChannel) -> None:
        group = match.groups[ch.id]
        members = sorted(group.members, key=lambda m: (m.trust != "high", m.cand.name_confidence != "high"))
        source_urls = list(dict.fromkeys(m.cand.logo_url for m in members if m.cand.logo_url))
        names = [group.name]
        if group.catalog:
            names += list(group.catalog.aliases)
        names += [m.cand.name for m in members]
        async with sem:
            try:
                info = await app.logos.resolve(
                    ch.id, ch.name, names, source_urls, group.catalog.logo if group.catalog else "", app.policy.logo.get(ch.id, "")
                )
                ch.logo_version = info.version
            except Exception as exc:  # a logo problem must never block publication
                log.warning("logo for %s failed: %s", ch.id, exc)

    await asyncio.gather(*[one(ch) for ch in channels])


def _publish(app: App, report: dict, channels: list[PublishedChannel], id_map: dict[str, list[int]], trigger: str, run_id: int) -> str | None:
    cfg = app.settings.publish
    db = app.db
    prev_count = int(db.scalar("SELECT COUNT(*) FROM channel_state WHERE status IN ('published', 'stale')", default=0))
    playlist = render_m3u(channels, cfg)
    problems = validate_m3u(playlist, cfg, prev_count)
    if problems:
        report["problems"] = problems
        report["status"] = "held"
        report["error"] = "new playlist failed validation; the previous playlist stays published"
        log.error("%s: %s", report["error"], problems[:3])
        return None
    ts = now()
    rows = [channel_to_dict(c, id_map[c.id]) for c in channels]
    current = app.publisher.current()
    if current is not None and current.playlist == playlist:
        # Nothing a viewer would notice changed: keep the snapshot (and the rollback history), note the re-check.
        app.publisher.touch(current.snapshot_id, iso(ts), rows)
        _save_channel_state(app, channels, id_map, ts)
        report["status"], report["snapshot_id"] = "unchanged", current.snapshot_id
        return current.snapshot_id
    sid = app.publisher.new_snapshot_id()
    report["status"] = "published"
    report["snapshot_id"] = sid
    meta = {"created_at": iso(ts), "checked_at": iso(ts), "run_id": run_id, "trigger": trigger, "channels": len(channels),
            "streams": sum(len(c.streams) for c in channels), "version": __version__}
    app.publisher.publish(sid, playlist, rows, report, meta)
    _save_channel_state(app, channels, id_map, ts)
    return sid


def _save_channel_state(app: App, channels: list[PublishedChannel], id_map: dict[str, list[int]], ts: float) -> None:
    db = app.db
    with db.tx():
        live = {c.id for c in channels}
        for ch in channels:
            verified = ts if ch.status in {"healthy", "degraded"} else None
            db.execute(
                "INSERT INTO channel_state (channel_id, name, group_title, first_seen_at, last_published_at, last_verified_at, "
                "primary_candidate_id, candidate_ids_json, status) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (channel_id) DO UPDATE SET name = excluded.name, group_title = excluded.group_title, "
                "last_published_at = excluded.last_published_at, "
                "last_verified_at = COALESCE(excluded.last_verified_at, channel_state.last_verified_at), "
                "primary_candidate_id = excluded.primary_candidate_id, candidate_ids_json = excluded.candidate_ids_json, "
                "status = excluded.status",
                (ch.id, ch.name, ch.group, ts, ts, verified, ch.streams[0].cid, json.dumps(id_map.get(ch.id, [])),
                 "stale" if ch.status == "stale" else "published"),
            )
        for row in db.query("SELECT channel_id FROM channel_state WHERE status IN ('published', 'stale')"):
            if row["channel_id"] not in live:
                db.execute("UPDATE channel_state SET status = 'dropped' WHERE channel_id = ?", (row["channel_id"],))


# ---- 15-minute health check -----------------------------------------------------------------------------------------------

async def health_check(app: App) -> dict:
    """Light re-probe of every published stream; republish only when the order of a channel's streams changes."""
    if app.lock.locked():
        return {"status": "skipped", "reason": "a refresh is running"}
    async with app.lock:
        app.state.checking = True
        try:
            return await _health_check(app)
        finally:
            app.state.checking = False


async def _health_check(app: App) -> dict:
    t0 = time.monotonic()
    out: dict = {"status": "ok", "checked_at": iso(now()), "streams": 0, "failing": 0, "changes": []}
    try:
        rows = app.publisher.channels()
        if not rows:
            out["status"] = "skipped"
            out["reason"] = "nothing published yet"
        elif not await app.online():
            out["status"] = "offline"
        else:
            await _health_probe_and_rerank(app, rows, out)
    except Exception as exc:
        log.exception("health check crashed")
        out["status"], out["error"] = "failed", f"{type(exc).__name__}: {exc}"
    out["duration_s"] = round(time.monotonic() - t0, 1)
    app.state.last_health = out
    return out


async def _health_probe_and_rerank(app: App, rows: list[dict], out: dict) -> None:
    parsed = [channel_from_dict(r) for r in rows]
    streams = {s.cid: s.url for ch, _ in parsed for s in ch.streams}
    results = await asyncio.gather(*[app.prober.probe(url, "light") for url in streams.values()])
    if not await app.online():
        out["status"] = "offline"
        return
    ts = now()
    failing = 0
    for cid, res in zip(streams, results):
        res = _identity_veto(app, res)
        ok = record_probe(app.db, cid, res, app.settings.probe, ts)
        for row in app.db.query("SELECT id FROM candidates WHERE url_key = ? AND id != ?", (url_key(streams[cid]), cid)):
            record_probe(app.db, row["id"], res, app.settings.probe, ts)  # same stream listed by another source
        if not ok:
            failing += 1
    out["streams"], out["failing"] = len(streams), failing

    new_channels: list[PublishedChannel] = []
    id_map: dict[str, list[int]] = {}
    for ch, cids in parsed:
        id_map[ch.id] = cids
        choice = _choose(app, ch.id, _states_for(app, cids), ts)
        if choice.primary is None:
            out["changes"].append({"id": ch.id, "change": "dropped", "reason": "no working stream for too long"})
            continue
        new_urls = [s.url for s in choice.ordered]
        if new_urls != [s.url for s in ch.streams]:
            out["changes"].append(
                {"id": ch.id, "change": "failover" if new_urls[0] != ch.streams[0].url else "reordered",
                 "from": ch.streams[0].url, "to": new_urls[0], "status": choice.status}
            )
        # stream details and health status are kept current either way; only a different URL order republishes
        new_channels.append(dataclasses.replace(ch, streams=[_stream_from_state(s) for s in choice.ordered], status=choice.status))
    if not out["changes"]:
        current = app.publisher.current()
        if current is not None:
            app.publisher.touch(current.snapshot_id, iso(ts), [channel_to_dict(c, id_map[c.id]) for c in new_channels])
            _save_channel_state(app, new_channels, id_map, ts)
        return

    cfg = app.settings.publish
    prev_count = int(app.db.scalar("SELECT COUNT(*) FROM channel_state WHERE status IN ('published', 'stale')", default=0))
    playlist = render_m3u(new_channels, cfg)
    problems = validate_m3u(playlist, cfg, prev_count)
    if problems:
        out["status"], out["problems"] = "held", problems
        log.error("health-check playlist failed validation, keeping the current one: %s", problems[:3])
        return
    sid = app.publisher.new_snapshot_id()
    meta = {"created_at": iso(ts), "checked_at": iso(ts), "trigger": "health", "channels": len(new_channels),
            "streams": sum(len(c.streams) for c in new_channels), "version": __version__}
    report = {"trigger": "health", "status": "published", "snapshot_id": sid, "changes": out["changes"]}
    app.publisher.publish(sid, playlist, [channel_to_dict(c, id_map[c.id]) for c in new_channels], report, meta)
    _save_channel_state(app, new_channels, id_map, ts)
    out["snapshot_id"] = sid
    log.info("health check republished %d channel(s): %s", len(out["changes"]), [c["id"] for c in out["changes"]][:8])
