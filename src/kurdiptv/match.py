"""Identify which channel every discovered stream belongs to, decide scope, and group duplicates.

Deterministic and explainable: every exclusion and every uncertain decision leaves a reason/warning that
ends up in the refresh report. Uncertain matches are never merged - they are reported for review.
"""

from __future__ import annotations

import re
import string
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .catalog import Catalog, CatalogChannel, url_target
from .config import SourceConfig
from .models import RawCandidate
from .normalize import display_name, is_token_subset, name_key, similarity
from .policy import Policy, credential_reason
from .util import slugify, url_key

_EVIDENCE = {"override": 5, "url-pattern": 4, "catalog-name": 3, "catalog-alias": 2, "derived": 1}
_CONFIDENCE = {"high": 3, "medium": 2, "low": 1}
_CATEGORY_WORDS = [
    ("kids", "Kids"), ("children", "Kids"), ("cartoon", "Kids"), ("news", "News"), ("music", "Music"),
    ("religion", "Religion"), ("islamic", "Religion"), ("education", "Education"), ("documentary", "Culture"),
    ("culture", "Culture"), ("entertainment", "Entertainment"), ("general", "General"),
]


@dataclass
class Member:
    cid: int  # candidates.id
    cand: RawCandidate
    trust: str
    channel_id: str | None = None
    how: str = ""
    catalog: CatalogChannel | None = None
    verdict: str = "in"  # in | out
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def source_id(self) -> str:
        return self.cand.source_id


@dataclass
class ChannelGroup:
    id: str
    name: str
    category: str
    language: str
    country: str
    catalog: CatalogChannel | None
    members: list[Member]
    warnings: list[str] = field(default_factory=list)

    @property
    def unknown(self) -> bool:
        return self.catalog is None

    @property
    def sources(self) -> set[str]:
        return {m.source_id for m in self.members}


@dataclass
class MatchResult:
    groups: dict[str, ChannelGroup] = field(default_factory=dict)
    excluded: list[Member] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def warn(self, kind: str, message: str, channel: str | None = None) -> None:
        self.warnings.append({"kind": kind, "message": message, "channel": channel})


def _category_from_tags(tags: set[str]) -> str:
    low = {t.lower() for t in tags}
    for word, category in _CATEGORY_WORDS:
        if word in low:
            return category
    return "General"


_GENERIC_WORDS = {
    "show", "music", "muzik", "news", "plus", "kids", "sport", "live", "kurd", "kurdish", "kurdistan", "channel",
    "family", "movies", "film", "radio", "drama", "stream", "media", "playlist", "index", "chunklist", "master",
}


def _corroborated(member: Member) -> bool:
    """The channel name shows up (roughly) in the stream URL itself: khak -> .../khakhd/playlist.m3u8.

    Only the primary name counts: tile titles on the low-trust pages are exactly what we do not believe.
    """
    url_tokens = {t for t in re.split(r"[^a-z0-9]+", url_target(member.cand.url)) if len(t) >= 4 and t not in _GENERIC_WORDS}
    for token in name_key(member.cand.name).split():
        if len(token) < 4 or token.isdigit() or token in _GENERIC_WORDS:
            continue
        for u in url_tokens:
            if token in u or u in token:
                return True
            common = 0
            for a, b in zip(token, u):
                if a != b:
                    break
                common += 1
            if common >= 5 or SequenceMatcher(None, token, u).ratio() >= 0.82:
                return True
    return False


def match_candidates(
    items: list[tuple[int, RawCandidate, SourceConfig]],
    catalog: Catalog,
    policy: Policy,
) -> MatchResult:
    result = MatchResult()
    members = [Member(cid=cid, cand=cand, trust=src.trust) for cid, cand, src in items]

    # ---- A. pre-filters that need nothing but the URL ------------------------------------------------------------
    live: list[Member] = []
    for m in members:
        reason = credential_reason(m.cand.url)
        if reason:
            _exclude(result, m, "unsafe-url", reason)
            continue
        reason = policy.denied_by_url(m.cand.url)
        if reason:
            _exclude(result, m, "identity-mismatch", reason)
            continue
        live.append(m)

    # ---- B. identity -----------------------------------------------------------------------------------------------
    for m in live:
        _identify(m, catalog, policy)
    live = [m for m in live if m.verdict == "in" or _record_exclusion(result, m)]

    # ---- C. one URL claimed by different channels -----------------------------------------------------------------
    _settle_url_conflicts(live, catalog, result)

    # ---- D. channel level scope ---------------------------------------------------------------------------------------
    by_channel: dict[str, list[Member]] = defaultdict(list)
    for m in live:
        by_channel[m.channel_id].append(m)  # type: ignore[index]
    for cid, group_members in sorted(by_channel.items()):
        group = _build_group(cid, group_members, catalog, policy)
        reason = _scope_reason(group, policy, result)
        if reason:
            for m in group.members:
                _exclude(result, m, reason[0], reason[1])
            continue
        # per-member checks inside an in-scope channel
        kept = []
        for m in group.members:
            r = policy.blocked(channel_id=group.id, name=group.name, url=m.cand.url)
            if r:
                _exclude(result, m, "blocklist", r)
                continue
            if group.unknown:
                r = policy.rights_reason("", m.cand.url)
                if r:
                    _exclude(result, m, "rights-sensitive", r)
                    continue
            kept.append(m)
        if not kept:
            continue
        group.members = kept
        result.groups[cid] = group
        for m in kept:
            group.warnings.extend(m.warnings)

    _possible_duplicates(result, policy)
    return result


# ------------------------------------------------------------------------------------------------------------------------

def _exclude(result: MatchResult, m: Member, kind: str, reason: str) -> None:
    m.verdict, m.reason = "out", f"{kind}: {reason}"
    if m not in result.excluded:
        result.excluded.append(m)


def _record_exclusion(result: MatchResult, m: Member) -> bool:
    if m not in result.excluded:
        result.excluded.append(m)
    return False


def _identify(m: Member, catalog: Catalog, policy: Policy) -> None:
    cand = m.cand
    keys = [name_key(cand.name)] + [name_key(a) for a in cand.aliases]

    forced = next((policy.force_alias[k] for k in keys if k and k in policy.force_alias), None)
    cat_url = catalog.by_url(cand.url)
    cat_name = catalog.by_name(cand.name)
    cat_alias = next((c for c in (catalog.by_name(a) for a in cand.aliases) if c), None)

    chosen: CatalogChannel | None = None
    if forced:
        chosen = catalog.by_id.get(forced)
        m.channel_id, m.how, m.catalog = forced, "override", chosen
    elif cat_url:
        chosen, m.how = cat_url, "url-pattern"
        if cat_name and cat_name.id != cat_url.id:
            m.warnings.append(
                f"label {cand.name!r} ({cand.source_id}) says {cat_name.name}, but the stream URL belongs to {cat_url.name}; published as {cat_url.name}"
            )
    elif cat_name:
        chosen, m.how = cat_name, "catalog-name"
        if m.trust == "low" and chosen.url_patterns:
            m.verdict, m.reason = "out", f"unconfirmed-identity: labelled {chosen.name} but not on a known {chosen.name} stream server"
            return
    elif cat_alias:
        chosen, m.how = cat_alias, "catalog-alias"
        if m.trust == "low" and chosen.url_patterns:
            m.verdict, m.reason = "out", f"unconfirmed-identity: only an alias points to {chosen.name} and the URL is not a known {chosen.name} server"
            return

    if chosen is not None:
        m.channel_id, m.catalog = chosen.id, chosen
        other = next((c for c in (catalog.by_name(a) for a in cand.aliases) if c and c.id != chosen.id), None)
        if other:
            m.warnings.append(f"entry from {cand.source_id} is also labelled {other.name}; published as {chosen.name}")
        return
    if forced:
        return  # forced to an id that is not in the catalog: use as given

    # not in the catalogue: derive an id from the normalised name
    key = next((k for k in keys if k), "")
    if not key:
        m.verdict, m.reason = "out", "unnamed: no usable channel name"
        return
    cid = slugify(key)
    if cid in catalog.by_id:  # never let a derived id shadow a catalogue channel
        cid = "x-" + cid
    m.channel_id, m.how = cid, "derived"


def _rank(m: Member) -> tuple:
    return (_EVIDENCE.get(m.how, 0), _CONFIDENCE.get(m.cand.name_confidence, 1), 2 if m.trust == "high" else 1)


def _settle_url_conflicts(live: list[Member], catalog: Catalog, result: MatchResult) -> None:
    by_url: dict[str, list[Member]] = defaultdict(list)
    for m in live:
        by_url[url_key(m.cand.url)].append(m)
    for members in by_url.values():
        ids = {m.channel_id for m in members}
        if len(ids) < 2:
            continue
        winner = max(members, key=_rank)
        claims = ", ".join(f"{m.channel_id} ({m.source_id}: {m.cand.name!r})" for m in members)
        result.warn(
            "label-conflict",
            f"one stream URL is claimed by different channels: {claims}; published as {winner.channel_id}. {winner.cand.url}",
            channel=winner.channel_id,
        )
        for m in members:
            if m is not winner:
                m.channel_id, m.catalog, m.how = winner.channel_id, winner.catalog, winner.how + "+vote"


def _build_group(cid: str, members: list[Member], catalog: Catalog, policy: Policy) -> ChannelGroup:
    cat = catalog.by_id.get(cid)
    if cat is not None:
        language, country, category, name = cat.language, cat.country, cat.category, cat.name
    else:
        names = Counter()
        for m in members:
            text = display_name(m.cand.name)
            if text.islower():
                text = string.capwords(text)
            names[text] += _CONFIDENCE.get(m.cand.name_confidence, 1) * (2 if m.trust == "high" else 1)
        name = names.most_common(1)[0][0] if names else cid
        tags = {t for m in members for t in m.cand.tags}
        category = _category_from_tags(tags)
        language = next((m.cand.language for m in members if m.cand.language), "") or (
            "ku" if policy.has_kurdish_marker(*tags, name) else ""
        )
        country = next((m.cand.country for m in members if m.cand.country), "")
    return ChannelGroup(id=cid, name=name, category=category, language=language, country=country, catalog=cat, members=members)


def _scope_reason(group: ChannelGroup, policy: Policy, result: MatchResult) -> tuple[str, str] | None:
    if group.catalog is not None:
        r = policy.blocked(channel_id=group.id, name=group.name, url="")
        return ("blocklist", r) if r else None
    r = policy.blocked(channel_id=group.id, name=group.name, url="")
    if r:
        return "blocklist", r
    r = policy.denied_by_name(group.name) or next(
        (x for x in (policy.denied_by_name(m.cand.name) for m in group.members) if x), None
    )
    if r:
        return "out-of-scope", r
    r = policy.rights_reason(group.name, "")
    if r:
        return "rights-sensitive", r
    tags = tuple(dict.fromkeys(t for m in group.members for t in m.cand.tags))
    foreign = policy.foreign_only(tags)
    if foreign and not policy.has_kurdish_marker(group.name):
        return "out-of-scope", f"source tags it as {foreign} only, no Kurdish marker"

    has_trusted = any(m.trust == "high" for m in group.members)
    if has_trusted:
        if policy.unknown_from_trusted == "review":
            return "needs-review", "not in the catalogue; policy asks for review before publishing"
        return None
    if not policy.require_corroboration:
        return None
    if len(group.sources) >= 2 or any(_corroborated(m) for m in group.members):
        return None
    return "uncorroborated", "only a low-trust source lists it and its name is not confirmed by the stream URL or another source"


def _possible_duplicates(result: MatchResult, policy: Policy) -> None:
    items = []
    for g in result.groups.values():
        key = name_key(g.name)
        if key:
            items.append((g, key))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (ga, ka), (gb, kb) = items[i], items[j]
            if ga.catalog and gb.catalog:
                continue  # the catalogue is authoritative
            if not policy.may_merge(ga.id, gb.id, ga.name, gb.name):
                continue
            sim = similarity(ka, kb)
            if sim >= 0.88 or is_token_subset(ka, kb):
                msg = f"{ga.name!r} and {gb.name!r} may be the same channel (similarity {sim:.2f}); kept separate - add an alias in catalog.yaml to merge"
                result.warn("possible-duplicate", msg, channel=ga.id)
                ga.warnings.append(msg)
                gb.warnings.append(msg)
