"""Human-readable refresh report (the JSON report holds the same data for tools)."""

from __future__ import annotations

import html
import re
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


# ---- what is not in the playlist, in plain words (shown on the landing page) ----------------------------------------------

DEFAULT_REASON = "no working stream at the moment"


def plain_reason(errors: dict | None) -> str:
    """The most frequent probe failure of a channel, in words a viewer understands."""
    if not errors:
        return DEFAULT_REASON
    kind = max(errors.items(), key=lambda kv: kv[1])[0].lower()
    match = re.search(r"http-(\d{3})", kind)
    code = int(match.group(1)) if match else 0
    if code in (401, 403):
        return "only plays on its own website or app and refuses other players"
    if code in (400, 404, 410):
        return "its stream address no longer exists"
    if code == 429:
        return "its server refuses repeated visits"
    if code >= 500 or "connect" in kind or "timeout" in kind:
        return "its stream server is down or not responding"
    if kind.startswith("dns"):
        return "its stream server no longer exists"
    if kind.startswith("tls"):
        return "its stream server has an invalid security certificate"
    if "not an hls" in kind or "not-hls" in kind:
        return "no direct stream address (a web page or embedded player only)"
    return DEFAULT_REASON


def load_report(published_dir) -> dict | None:
    """The latest refresh report as a dict, or None when there is none yet (or it cannot be read)."""
    import json
    from pathlib import Path

    try:
        return json.loads((Path(published_dir) / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def unavailable_channels(report: dict | None) -> list[tuple[str, str]]:
    """Catalogued channels that are known but have no working stream right now, with the reason. When the broadcaster's own
    address is among the candidates, its failure is the reason (old copies elsewhere are beside the point)."""
    out = []
    for item in ((report or {}).get("channels") or {}).get("unverified") or []:
        if not item.get("in_catalog"):
            continue
        out.append((item.get("name") or item.get("id") or "?", plain_reason(item.get("official_errors") or item.get("errors"))))
    return sorted(out, key=lambda x: x[0].lower())


def unavailable_html(report: dict | None) -> str:
    """The landing page section listing them ("" when every known channel works)."""
    items = unavailable_channels(report)
    if not items:
        return ""
    rows = "\n".join(f"<li><b>{html.escape(name)}</b> &ndash; {html.escape(reason)}</li>" for name, reason in items)
    return (
        '<h2 style="font-size:1.1rem;margin-top:2rem">Known channels that are not in the list right now</h2>\n'
        '<p class="muted" style="margin:.25rem 0 .5rem">Every one is checked again at each refresh and appears by itself as soon as '
        "its stream works for outside players.</p>\n"
        f"<ul>\n{rows}\n</ul>"
    )
