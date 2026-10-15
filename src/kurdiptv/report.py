"""Human-readable refresh report (the JSON report holds the same data for tools)."""

from __future__ import annotations

import time


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def render_report_text(report: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"Refresh #{report.get('run_id')} ({report.get('trigger')}) - {report.get('status', '?').upper()}")
    add(f"started {report.get('started_at')}, took {report.get('duration_s')} s")
    if report.get("error"):
        add(f"NOTE: {report['error']}")
    t = report.get("totals") or {}
    if t:
        add("")
        add("Totals: " + ", ".join(f"{k}={v}" for k, v in t.items()))

    add("")
    add("Sources")
    for s in report.get("sources", []):
        extra = f" - {s['error']}" if s.get("error") else ""
        add(f"  {s['source']:10} {s['status']:8} http={s.get('http_status')} requests={s['requests']} cached={s['cache_hits']} "
            f"found={s['candidates']} new={s.get('new_candidates', 0)} {s['duration_ms'] / 1000:.0f}s{extra}")
        if s.get("skipped"):
            add("             skipped entries: " + ", ".join(f"{k}={v}" for k, v in s["skipped"].items()))

    ch = report.get("channels") or {}
    for title, key in (("New channels", "new"), ("Removed channels", "removed"), ("Primary stream changed", "changed"),
                       ("Running on stale/degraded streams", "stale"), ("In scope but no verified stream", "unverified"),
                       ("New, not in catalog.yaml (please review)", "unreviewed")):
        items = ch.get(key) or []
        if not items:
            continue
        add("")
        add(f"{title} ({len(items)})")
        for it in items:
            detail = it.get("reason") or it.get("note") or (f"errors: {it['errors']}" if it.get("errors") else "")
            add(f"  {it['id']:28} {it.get('name', '')[:30]:30} {detail}")

    merged = ch.get("merged") or []
    if merged:
        add("")
        add(f"Merged / with fallbacks ({len(merged)})")
        for it in merged:
            add(f"  {it['id']:28} {len(it['urls'])} stream(s) from {', '.join(it['sources'])}")
            for u in it["urls"]:
                q = f"{u['height']}p{'*' if u['verified'] else '?'}" if u.get("height") else "?"
                add(f"      {q:7} {u['url']}  [{u['source']}]")

    ex = report.get("excluded") or {}
    if ex.get("by_reason"):
        add("")
        add("Excluded streams by reason: " + ", ".join(f"{k}={v}" for k, v in ex["by_reason"].items()))
    if report.get("probe_failures"):
        add("Probe failures: " + ", ".join(f"{k}={v}" for k, v in report["probe_failures"].items()))
    warnings = report.get("warnings") or []
    if warnings:
        add("")
        add(f"Review warnings ({len(warnings)})")
        for w in warnings[:60]:
            add(f"  [{w['kind']}] {w['message']}")
    if report.get("problems"):
        add("")
        add("VALIDATION PROBLEMS (playlist was NOT replaced):")
        for p in report["problems"]:
            add(f"  - {p}")
    return "\n".join(lines) + "\n"
