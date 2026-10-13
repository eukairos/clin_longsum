# clin_longsum - tiered longitudinal clinical summarization
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
b2_build_ledger.py
==================
Phase B2 -- project per-admission facts into the per-patient must-retain ledger.

DETERMINISTIC projection (no LLM). Collapses cross-admission facts into a current-state ledger:

  1. On load, promote incidental/suspicious findings to negative_kind=surveillance via
     the deterministic detector (tax.is_surveillance_finding) - a ?malignancy
     under surveillance cannot drop on a prompt miss.
  2. Gather candidates (per is_ledger_member, plus superseded facts so a de-label
     clusters with the original it amends). recurrent_episode -> rollups;
     ruled_out -> considered_excluded.
  3. Cluster by overlap coefficient on content tokens within retention_class, requiring
     >=1 shared SPECIFIC (non-qualifier) token so distinct conditions sharing only
     boilerplate (chronic/stable/...) never chain; latest mention wins for lifecycle/status.
     Each cluster is then SPLIT by condition identity (_split_cluster) so a compound fact
     naming two conditions cannot let one speak for the other; the same guard runs
     again on consolidation groups (_split_group). code_status_directive is exempt from
     both - see _SPLIT_EXEMPT.
  4. Route each cluster by the latest fact:
       superseded               -> amended       (RETRACTED: record withdrawn, NOT served.
                                                  `amended` is a BUCKET NAME, not a lifecycle
                                                  value - there are only four of those, and
                                                  this bucket holds the `superseded` ones.
                                                  Safety classes are exempt, see step 4b.)
       confirmed                -> nodes        (confirmed current state)
       suspected / patient_reported -> flagged  (under investigation / patient-reported)
  5. recurrent_episode (T4) -> EpisodeRollup (count + date range).
  6. Anything matching NONE of the above - not pinned, not ruled_out, not superseded,
     not recurrent_episode - is recorded in `dropped` with the reason returned by
     tax.prune_reason(). The prune itself is usually correct; it is auditable.

Input:  data/interim/retention_facts.jsonl, data/interim/cohort_admissions.csv
Output: data/output/retention_ledger.jsonl   (one RetentionLedger per patient)

Deterministic recompute: safe to rerun anytime (re-derives over all facts, so it
reflects the current taxonomy even without re-extraction).

HOLDOUT: b2 does NOT filter facts by holdout - b1 extracts the established span
only, so retention_facts.jsonl never carries a held-out admission. b2 still derives each
patient's established admission COUNT from the cohort via holdout.established (excludes
admissions still pending physician review, in dev and production alike; a no-op only when
HOLDOUT_LAST_ADMISSION is False), so n_admissions matches d_coordinator's window math.

    cd ~/clin_longsum
    python b2_build_ledger.py                      # whole cohort (writes retention_ledger.jsonl)
    python b2_build_ledger.py --preview 90000035   # project ONE subject, print only (no write)
    python b2_build_ledger.py --subject 90000035   # re-project ONE subject, MERGE into the ledger
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from pydantic import BaseModel, Field

import cohort
import config
import holdout
import io_utils
import retention_taxonomy as tax
from roles.contracts import RetentionFact

FACTS_JSONL = config.RETENTION_FACTS_JSONL
COHORT_CSV = config.INTERIM_DIR / "cohort_admissions.csv"
LEDGER_JSONL = config.OUTPUT_DIR / "retention_ledger.jsonl"

CLUSTER_THRESH = 0.6
_MIN_SHARED = 1
_STOPWORDS = frozenset({"on", "the", "of", "with", "a", "an", "and", "to", "in", "for", "at", "by"})
# Generic / qualifier tokens that must NOT, on their own, justify a same-condition
# cluster. Two DISTINCT conditions phrased "Chronic and stable X" / "Chronic and stable Y"
# share only {chronic, stable}; without this, the overlap coefficient chained COPD/CAD
# into a hypertension node (and alcohol-use into seizure). _cluster requires >=1
# shared token that is NOT in this set. 
_QUALIFIERS = frozenset({
    "chronic", "acute", "disease", "disorder", "syndrome", "condition", "stage", "recurrent",
    "history", "known", "recent", "prior", "longstanding", "long", "standing", "old", "remote",
    "stable", "active", "ongoing", "current", "controlled", "managed", "uncontrolled",
    "poorly", "well", "compensated", "decompensated",
    "possible", "suspected", "probable", "likely", "presumed",
    "mild", "moderate", "severe", "significant", "diffuse", "essential", "primary", "secondary",
})
_TIER_RANK = {"T1": 1, "T2": 2, "T3": 3, "T4": 4, "T5": 5}
JACCARD_THRESH = 0.6
ABBREV_TSV = config.ABBREV_TSV        # curated dict, ships under src/resources/

# Only nodes in the same compatibility group may consolidate - a surgery/device node
# must never absorb a disease node (aggressive merge collapsed active recurrent HCC into
# a transplant-history line). Classes not listed merge only within their own class.
_MERGE_GROUP = {
    "chronic_disease_state": "disease",
    "permanent_condition": "disease",
    "sentinel_event": "disease",
    "anatomy_altering_surgery": "procedure",
    "implanted_device": "procedure",
}

# Curated canonical condition keys: explicit, safe same-condition merge across wording
# variants that token overlap can't catch (HCC facets, cirrhosis variants). Keys are
# specific (not "carcinoma") so distinct cancers never merge. Extend deliberately;
# SNOMED grounding may be used to generalise this in future - not in current version.
_CONDITION_KEYS = [
    (re.compile(r"hepatocellular carcinoma", re.I), "hepatocellular_carcinoma"),
    (re.compile(r"\bcirrhosis\b", re.I), "cirrhosis"),
]

# Classes exempt from identity splitting (_split_cluster / _split_group). A code-status
# directive legitimately MUTATES over time and the most recent statement is the one that
# reflects the patient's wishes, so latest-wins collapse is correct there and splitting
# would surface every superseded directive as its own entry. do not
# extend without one - for every other class, a silently swallowed identity is a defect.
_SPLIT_EXEMPT = frozenset({"code_status_directive"})

# Taxonomy guard makes a retraction win outright by taking the superseding fact's negative_kind
# but that is only as good as b1's labelling. When the retraction carries negative_kind=None the guard propagates the null, 
# producing exactly the state the guard exists to prevent: current_lifecycle=superseded AND negative_kind=None, i.e. the
# reader is told the fact was withdrawn and simultaneously that there is no negative, so the
# review UI renders no de-label badge. Supersession IS the negative, so derive one when the
# label is missing. A retraction with no explicit kind is treated as absent_or_negative, never as "present".
SUPERSEDED_DEFAULT_NK = "absent_or_negative"


def _load_abbrev() -> dict:
    """abbreviation (original case) -> set of expansion content tokens. Case-sensitive
    key so 'COPD'/'AFib'/'ACEi' expand but the common word 'all' never maps to 'ALL'."""
    m: dict = {}
    if not ABBREV_TSV.exists():
        print(f"WARN: abbreviation table not found at {ABBREV_TSV} -- no expansion")
        return m
    for ln in ABBREV_TSV.read_text(encoding="utf-8").splitlines()[1:]:
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        abbr, exp = parts[0].strip(), parts[1].strip()
        toks = set(re.findall(r"[a-z0-9]+", exp.lower())) - _STOPWORDS
        if abbr and toks:
            m[abbr] = toks
    return m


_ABBR = _load_abbrev()


# ── Projection output models (POC-local; move to contracts.py / D3 later) ─────

class LedgerNode(BaseModel):
    retention_class: str
    salience_tier: str
    text: str
    certainty: str = "confirmed"               # confirmed | suspected | patient_reported
    current_lifecycle: str = "active"
    negative_kind: Optional[str] = None        # None | absent_or_negative | resolved_relevant | surveillance
    onset_date: Optional[str] = None
    last_confirmed_date: Optional[str] = None
    n_mentions: int = 1
    source_hadm_ids: list[int] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    # PER-ATTRIBUTE PROVENANCE. A node is assembled from DIFFERENT members of its
    # cluster - salience_tier from the strongest mention, text/certainty/lifecycle/class
    # from the latest, negative_kind from the merge-priority winner - so `fact_ids` alone
    # cannot answer "where did this node's tier come from". Maps attribute -> fact_id.
    # Audit only: nothing routes or displays on it. Does NOT require re-extraction (b1),
    # since it is derived from fact_ids already present in retention_facts.jsonl.
    provenance: dict[str, str] = Field(default_factory=dict)


class EpisodeRollup(BaseModel):
    parent_condition: str
    count: int
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    source_hadm_ids: list[int] = Field(default_factory=list)


class DroppedFact(BaseModel):
    """A fact that matched no routing branch in _project_patient - recorded so the
    prune is inspectable in-band instead of vanishing (FM-5). Audit only: never served,
    never counted in recall. `reason` comes from tax.prune_reason()."""
    fact_id: Optional[str] = None
    text: str
    retention_class: str
    salience_tier: str
    certainty: str
    lifecycle: str
    negative_kind: Optional[str] = None
    source_hadm_id: Optional[int] = None
    admit_seq: Optional[int] = None
    reason: str


class RetentionLedger(BaseModel):
    subject_id: int
    n_admissions: int
    taxonomy_version: str
    generated_at: str
    nodes: list[LedgerNode] = Field(default_factory=list)            # confirmed current state
    flagged: list[LedgerNode] = Field(default_factory=list)          # provisional / patient-reported
    episodes: list[EpisodeRollup] = Field(default_factory=list)      # T4 rollups
    # RETRACTED entries: the RECORD was corrected, so these are withheld from the served
    # ledger. `amended` names the ACT while its contents carry the STATE (`current_lifecycle ==
    # "superseded"`), which is why readers mistake it for a fifth lifecycle value. There are
    # only four (tax.LIFECYCLE). Not loaded by d3; c2 counts it as REPRESENTED. 
    # Safety-robust classes are exempt and stay served. See GLOSSARY.md.
    amended: list[LedgerNode] = Field(default_factory=list)
    considered_excluded: list[str] = Field(default_factory=list)     # ruled-out negatives
    dropped: list[DroppedFact] = Field(default_factory=list)         # pruned, with reason (audit)


# ── helpers ───────────────────────────────────────────────────────────────────

# Surface forms that the [A-Za-z0-9]+ tokenizer would otherwise shatter into
# meaningless single letters. Both produced a bare "s" token, and "s" is in neither
# _STOPWORDS nor _QUALIFIERS, so it survived _specific() and _identity_tokens():
#   "<Eponym>'s disease"          -> {eponym, s, disease}
#   "<condition> s/p <procedure>" -> {condition, s, p, procedure}
# A possessive eponym and an "s/p" procedural fact therefore shared exactly the
# specific token {s}, at an overlap above CLUSTER_THRESH, so two unrelated conditions
# clustered. _split_cluster could not separate them because its identity tokens
# carried the same "s", and _build_node's latest-wins text then served one condition
# under the other's name. See the FM-7 residual in References/KNOWN_FAILURE_MODES.md.
# Stripping the possessive rather than folding it to "...s" keeps "<Eponym>'s disease"
# and "<Eponym> disease" on the same token, which the fold would have split.
_POSSESSIVE_RE = re.compile(r"([A-Za-z])['’]s\b")
_SP_ABBREV_RE = re.compile(r"\bs/p\b", re.IGNORECASE)


def _normalize_surface(text: str) -> str:
    """Applied by BOTH tokenizers below so token sets and phrases stay consistent."""
    return _SP_ABBREV_RE.sub(" ", _POSSESSIVE_RE.sub(r"\1", text))


def _content_tokens(text: str) -> set[str]:
    """Content tokens with abbreviations expanded to canonical words, so 'COPD' and
    'chronic obstructive pulmonary disease' share tokens. Case-sensitive abbreviation
    match avoids expanding common words (e.g. 'all' != 'ALL')."""
    out: set[str] = set()
    for t in re.findall(r"[A-Za-z0-9]+", _normalize_surface(text)):
        if t in _ABBR:
            out |= _ABBR[t]
        else:
            out.add(t.lower())
    return out - _STOPWORDS


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _specific(shared: set[str]) -> set[str]:
    """Shared tokens minus generic/qualifier boilerplate - the condition-identity overlap."""
    return shared - _QUALIFIERS


def _identity_tokens(text: str) -> set[str]:
    """One entry's condition-identity tokens: content tokens minus qualifier boilerplate.
    Two entries describing the SAME condition share at least one of these."""
    return _content_tokens(text) - _QUALIFIERS


def _read_facts() -> dict[int, list[RetentionFact]]:
    """Load facts; tax.normalize_fact applies the surveillance promotion and the
    disease-course reclassification on the way in (shared with c1/b3/db_persist)."""
    by_pid: dict[int, list[RetentionFact]] = {}
    if not FACTS_JSONL.exists():
        raise SystemExit(f"{FACTS_JSONL} not found. Run b1_retention_extractor.py first.")
    promoted = 0
    reclassified = 0
    for rec in io_utils.read_jsonl(FACTS_JSONL):
        pid = int(rec["subject_id"])
        for fd in rec.get("facts", []):
            p, r = tax.normalize_fact(fd)
            promoted += p
            reclassified += r
            by_pid.setdefault(pid, []).append(RetentionFact(**fd))
    if promoted:
        print(f"surveillance promotion: {promoted} facts -> negative_kind=surveillance")
    if reclassified:
        print(f"disease-course reclassification: {reclassified} recurrent_episode -> sentinel_event")
    return by_pid


# Cohort dates now come from the shared loader (cohort.py); alias kept for any
# importer of the old private helper.
_hadm_dates = cohort.hadm_dates


def _cluster(facts: list[RetentionFact]) -> list[list[RetentionFact]]:
    clusters: list[list[RetentionFact]] = []
    cluster_tokens: list[set[str]] = []
    for f in facts:
        toks = _content_tokens(f.text)
        placed = False
        for i, ct in enumerate(cluster_tokens):
            # require >=1 shared SPECIFIC (non-qualifier) token so distinct conditions
            # sharing only "chronic"/"stable"/etc. do not chain together; overlap gate as before.
            if len(_specific(toks & ct)) >= _MIN_SHARED and _overlap(toks, ct) >= CLUSTER_THRESH:
                clusters[i].append(f)
                cluster_tokens[i] = ct | toks
                placed = True
                break
        if not placed:
            clusters.append([f])
            cluster_tokens.append(set(toks))
    return clusters


def _seq_key(f: RetentionFact) -> int:
    return f.admit_seq if f.admit_seq is not None else -1


def _split_cluster(cluster: list[RetentionFact]) -> list[list[RetentionFact]]:
    """Partitions a cluster so every member shares an identity token with the member that
    will supply the node's displayed text.

    _cluster accumulates tokens as members join (`ct | toks`), so a COMPOUND fact naming
    two conditions ('Coronary artery disease and peripheral vascular disease') bridges
    them into one cluster. _build_node then takes text/class/certainty/lifecycle from the
    LATEST member, and the other condition loses its identity while its fact_id survives
    inside the node - not a prune, so no dropped[] entry and no prune_reason fires.

    Latest-wins is preserved WITHIN each identity: the latest member seeds a partition,
    everything sharing an identity token with it joins, the remainder recurses. This can
    only ever INCREASE the node count - no merge is created that _cluster did not already
    make - so nothing new can be swallowed.
    """
    if any(f.retention_class in _SPLIT_EXEMPT for f in cluster):
        return [cluster]
    parts: list[list[RetentionFact]] = []
    pending = sorted(cluster, key=_seq_key)
    while pending:
        seed = pending[-1]                       # the member _build_node would pick
        stoks = _identity_tokens(seed.text)
        part = [f for f in pending if f is seed or (_identity_tokens(f.text) & stoks)]
        taken = {id(f) for f in part}
        parts.append(part)
        pending = [f for f in pending if id(f) not in taken]
    return parts


def _build_node(cluster: list[RetentionFact], dates: dict[int, str]) -> LedgerNode:
    """Assemble one ledger node from a same-condition cluster.

    SPLIT PROVENANCE IS DELIBERATE:
      salience_tier   STRONGEST across the cluster - a chronic condition mis-tagged T4/T5
                      at a later mention must not fall out of the ledger (same intent as
                      tax.floor_tier). Attributed to the MOST RECENT mention holding that
                      tier, so the trail points at a live justification, not the oldest one.
      text / class / certainty / current_lifecycle  from the LATEST mention (current state).
      negative_kind   the merge-priority winner (tax.merge_negative_kind), EXCEPT when the
                      latest mention is lifecycle=superseded - a retraction wins outright,
                      or merge priority (None ranks highest) would strip the negative and
                      render a de-labelled allergy as live. 
    Each is attributed in `provenance`, so a trace can ask which fact supplied which
    attribute instead of tabulating every fact_id by hand.
    """
    cluster = sorted(cluster, key=_seq_key)
    latest = cluster[-1]
    # reversed(): min() returns the FIRST minimal element, so scanning newest-first
    # attributes the tier to the most recent mention that actually held it.
    strongest = min(reversed(cluster), key=lambda c: _TIER_RANK.get(c.salience_tier, 9))
    tier = strongest.salience_tier
    # merge priority ranks None (present) HIGHEST - correct in general
    # ("if any sibling says the condition is present, the node is present"), and wrong for
    # a retraction. An allergy de-label cluster (original allergy negative-kind = None + de-label
    # nk=absent_or_negative) would resolve to None, lose the "(absent)" badge in the review
    # UI, and render as a LIVE allergy. When the latest mention supersedes, it wins outright.
    if latest.lifecycle == "superseded":
        # fall back to SUPERSEDED_DEFAULT_NK rather than propagate a
        # None the extractor never set. See the constant for the rationale.
        nk, nk_src = latest.negative_kind or SUPERSEDED_DEFAULT_NK, latest
    else:
        nk = tax.merge_negative_kind(c.negative_kind for c in cluster)
        nk_src = next((c for c in reversed(cluster) if c.negative_kind == nk), latest)
    hadm_ids = [c.source_hadm_id for c in cluster if c.source_hadm_id is not None]
    node_dates = [dates.get(h) for h in hadm_ids if dates.get(h)]
    return LedgerNode(
        retention_class=latest.retention_class,
        salience_tier=tier,
        text=latest.text,
        certainty=latest.certainty,
        current_lifecycle=latest.lifecycle,
        negative_kind=nk,
        onset_date=min(node_dates) if node_dates else None,
        last_confirmed_date=max(node_dates) if node_dates else None,
        n_mentions=len(cluster),
        source_hadm_ids=sorted(set(hadm_ids)),
        fact_ids=[c.fact_id for c in cluster if c.fact_id],
        provenance={
            "salience_tier": strongest.fact_id or "",
            "retention_class": latest.fact_id or "",
            "text": latest.fact_id or "",
            "certainty": latest.fact_id or "",
            "current_lifecycle": latest.fact_id or "",
            "negative_kind": nk_src.fact_id or "",
        },
    )


def _group(cls: str) -> str:
    return _MERGE_GROUP.get(cls, cls)


def _condition_key(text: str):
    for rx, key in _CONDITION_KEYS:
        if rx.search(text):
            return key
    return None


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _phrase(text: str) -> str:
    toks: list[str] = []
    for t in re.findall(r"[A-Za-z0-9]+", _normalize_surface(text)):
        toks.extend(sorted(_ABBR[t]) if t in _ABBR else [t.lower()])
    return " ".join(toks)


def _phrase_contained(a: str, b: str) -> bool:
    """True if the shorter node's full phrase appears verbatim in the longer (same
    condition + qualifiers, e.g. 'essential hypertension' in 'essential hypertension
    managed with ...'). >=2 tokens so a bare word can't swallow a distinct condition."""
    pa, pb = _phrase(a), _phrase(b)
    short, long = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    return len(short.split()) >= 2 and short in long


def _recency_key(nd: LedgerNode) -> str:
    """Newest-mention key for a node. last_confirmed_date is the max() of its cluster's
    admission dates; a node with no resolvable date sorts oldest."""
    return nd.last_confirmed_date or ""


def _rep_of(group: list[LedgerNode]) -> LedgerNode:
    """Group representative: strongest tier, then prefer confirmed, then MOST RECENT.
    Sorting newest-first and taking min() (stable, returns the first minimal element)
    resolves ties to the latest mention, consistent with _build_node."""
    ordered = sorted(group, key=_recency_key, reverse=True)          # newest first
    return min(ordered, key=lambda nd: (_TIER_RANK.get(nd.salience_tier, 9),
                                        nd.certainty != "confirmed"))


def _split_group(group: list[LedgerNode]) -> list[list[LedgerNode]]:
    """The same identity guard as _split_cluster, one stage later.

    A union-find group may not keep a representative that orphans a member. Both the
    curated condition-key and the phrase-containment branches merge on a MENTION of a
    condition, so a node can be absorbed by one whose displayed text never names it (a
    cirrhosis node absorbed into 'Chronic thrombocytopenia' via 'Chronic thrombocytopenia
    likely secondary to cirrhosis'). Members that share no identity token with the
    representative are re-grouped around their own.
    """
    if any(nd.retention_class in _SPLIT_EXEMPT for nd in group):
        return [group]
    parts: list[list[LedgerNode]] = []
    pending = list(group)
    while pending:
        rep = _rep_of(pending)
        rtoks = _identity_tokens(rep.text)
        part = [nd for nd in pending if nd is rep or (_identity_tokens(nd.text) & rtoks)]
        taken = {id(nd) for nd in part}
        parts.append(part)
        pending = [nd for nd in pending if id(nd) not in taken]
    return parts


def _merge_group(group: list[LedgerNode]) -> LedgerNode:
    """Collapse same-condition nodes into one.

    Representative selection: strongest tier, then prefer confirmed, then MOST RECENT.
    Sorting newest-first and taking min() (stable, returns the first minimal element)
    means ties resolve to the latest mention, consistent with _build_node.
    """
    rep = _rep_of(group)
    ordered = sorted(group, key=_recency_key, reverse=True)          # newest first
    # _build_node makes a retraction win outright; _merge_group did not.
    # tax.merge_negative_kind ranks None HIGHEST - correct in general ("if any sibling says
    # present, the node is present") and wrong for a retraction, so a live sibling stripped
    # the negative off a de-labelled node and the review UI rendered a LIVE allergy. Only
    # severe_adr_allergy and code_status_directive can reach here superseded (every other
    # class routes to `amended` before _consolidate), but for those two this is a potential failure mode.
    #
    # The test is LATEST-is-superseded, matching _build_node - NOT any-member-superseded.
    # A de-label that is OLDER than a live re-assertion has itself been superseded: allergy
    # de-labelled 2020, re-documented 2023 means the allergy is present, and letting the
    # stale retraction win would suppress a live one. ordered[0] is the newest member.
    retraction = ordered[0] if ordered and ordered[0].current_lifecycle == "superseded" else None
    if retraction is not None:
        nk = retraction.negative_kind or SUPERSEDED_DEFAULT_NK
        nk_src = retraction
        lifecycle = "superseded"
    else:
        nk = tax.merge_negative_kind(nd.negative_kind for nd in group)
        nk_src = next((nd for nd in ordered if nd.negative_kind == nk), rep)
        lifecycle = rep.current_lifecycle
    onsets = [nd.onset_date for nd in group if nd.onset_date]
    lasts = [nd.last_confirmed_date for nd in group if nd.last_confirmed_date]
    # Every served attribute except negative_kind comes from `rep`, so its provenance
    # carries through unchanged; negative_kind is re-attributed to the priority winner.
    prov = dict(rep.provenance)
    prov["negative_kind"] = nk_src.provenance.get("negative_kind", "")
    if retraction is not None:      # lifecycle no longer came from rep
        prov["current_lifecycle"] = retraction.provenance.get("current_lifecycle", "")
        prov["negative_kind"] = retraction.provenance.get("negative_kind", "")
    return LedgerNode(
        retention_class=rep.retention_class, salience_tier=rep.salience_tier,
        text=rep.text, certainty=rep.certainty, current_lifecycle=lifecycle,
        negative_kind=nk,
        onset_date=min(onsets) if onsets else None,
        last_confirmed_date=max(lasts) if lasts else None,
        n_mentions=sum(nd.n_mentions for nd in group),
        source_hadm_ids=sorted({h for nd in group for h in nd.source_hadm_ids}),
        fact_ids=sorted({f for nd in group for f in nd.fact_ids}),
        provenance=prov,
    )


def _consolidate_group(nodes: list[LedgerNode]) -> list[LedgerNode]:
    n = len(nodes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    toks = [_content_tokens(nd.text) for nd in nodes]
    keys = [_condition_key(nd.text) for nd in nodes]
    for i in range(n):
        for j in range(i + 1, n):
            if (_jaccard(toks[i], toks[j]) >= JACCARD_THRESH
                    or (keys[i] is not None and keys[i] == keys[j])
                    or _phrase_contained(nodes[i].text, nodes[j].text)):
                parent[find(i)] = find(j)
    groups: dict[int, list[LedgerNode]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(nodes[i])
    out: list[LedgerNode] = []
    for g in groups.values():
        out.extend(_merge_group(part) for part in _split_group(g))
    return out


def _consolidate(nodes: list[LedgerNode]) -> list[LedgerNode]:
    """Safe consolidation: merge only WITHIN a compatibility group (diseases together,
    procedures/devices together, other classes only with their own), and only when
    Jaccard >= JACCARD_THRESH, a curated condition key matches, or the shorter node's
    phrase is contained verbatim in the longer. No containment/min-overlap and no
    cross-anatomy bigram chaining, so a surgery node can't absorb diseases.
    fact_ids/source_hadm_ids are unioned so C2 recall is preserved."""
    by_group: dict[str, list[LedgerNode]] = {}
    for nd in nodes:
        by_group.setdefault(_group(nd.retention_class), []).append(nd)
    out: list[LedgerNode] = []
    for grp in by_group.values():
        out.extend(_consolidate_group(grp))
    return out


def _is_candidate(f: RetentionFact) -> bool:
    """Eligible for a ledger node. Includes superseded (to amend a cluster)."""
    if f.certainty == "ruled_out":
        return False
    if f.retention_class == "recurrent_episode":
        return False
    if f.lifecycle == "superseded":
        return True
    return f.in_ledger


def _project_patient(pid: int, facts: list[RetentionFact], dates: dict[int, str],
                     n_admissions: int) -> RetentionLedger:
    candidates, episodes_raw, excluded, dropped = [], [], [], []
    for f in facts:
        if f.certainty == "ruled_out":
            excluded.append(f)
        elif f.retention_class == "recurrent_episode":
            episodes_raw.append(f)
        elif _is_candidate(f):
            candidates.append(f)
        else:
            # The prune is recorded with the reason the taxonomy gate declined it, 
            # so an audit can tell a pruned fact from a lost one.
            dropped.append(DroppedFact(
                fact_id=f.fact_id, text=f.text, retention_class=f.retention_class,
                salience_tier=f.salience_tier, certainty=f.certainty,
                lifecycle=f.lifecycle, negative_kind=f.negative_kind,
                source_hadm_id=f.source_hadm_id, admit_seq=f.admit_seq,
                reason=tax.prune_reason(f.salience_tier, f.certainty, f.lifecycle,
                                        f.negative_kind, f.retention_class) or "unclassified"))

    nodes, flagged, amended = [], [], []
    by_class: dict[str, list[RetentionFact]] = {}
    for f in candidates:
        by_class.setdefault(f.retention_class, []).append(f)
    for cls_facts in by_class.values():
        for cluster in _cluster(cls_facts):
            for part in _split_cluster(cluster):       # identity guard 
                node = _build_node(part, dates)
                # a safety-class node is SERVED even when superseded. A
                # de-labelled allergy must reach the reader as an explicit negative
                # ("penicillin allergy - de-labelled on challenge"), not vanish into
                # `amended`, which D3 does not load and C2 counts as represented. Formal
                # disproof (certainty=ruled_out) still routes to considered_excluded at
                # branch 1 of _project_patient.
                if node.current_lifecycle == "superseded" and \
                        not tax.is_safety_robust(node.retention_class):
                    amended.append(node)
                elif node.certainty == "confirmed":
                    nodes.append(node)
                else:                              # suspected / patient_reported
                    flagged.append(node)
    # Safe consolidation: collapse same-condition nodes within a compatibility group
    # (diseases / procedures-devices / own-class), via _consolidate. fact_ids unioned
    # so C2 recall holds.
    consolidated = _consolidate(nodes + flagged)
    nodes = [x for x in consolidated if x.certainty == "confirmed"]
    flagged = [x for x in consolidated if x.certainty != "confirmed"]
    nodes.sort(key=lambda n: _TIER_RANK.get(n.salience_tier, 9))
    flagged.sort(key=lambda n: _TIER_RANK.get(n.salience_tier, 9))

    # T4 episode rollups by normalized parent_condition.
    ep_groups: dict[str, list[RetentionFact]] = {}
    for f in episodes_raw:
        ep_groups.setdefault((f.parent_condition or f.text).strip().lower(), []).append(f)
    episodes = []
    for fs in ep_groups.values():
        hadm_ids = [x.source_hadm_id for x in fs if x.source_hadm_id is not None]
        ds = [dates.get(h) for h in hadm_ids if dates.get(h)]
        episodes.append(EpisodeRollup(
            parent_condition=(fs[0].parent_condition or fs[0].text),
            count=len(fs),
            first_date=min(ds) if ds else None,
            last_date=max(ds) if ds else None,
            source_hadm_ids=sorted(set(hadm_ids)),
        ))
    episodes.sort(key=lambda e: e.count, reverse=True)

    return RetentionLedger(
        subject_id=pid, n_admissions=n_admissions,
        taxonomy_version=tax.TAXONOMY_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        nodes=nodes, flagged=flagged, episodes=episodes, amended=amended,
        considered_excluded=sorted({f.text for f in excluded}),
        dropped=sorted(dropped, key=lambda d: (d.reason, d.admit_seq or -1, d.text)),
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=int, default=None, metavar="SUBJECT_ID",
                    help="project ONE subject and PRINT it (no write); use --subject to persist")
    ap.add_argument("--subject", type=int, default=None,
                    help="project ONE subject and MERGE into the ledger file (fold-in; writes)")
    args = ap.parse_args()
    config.check_paths()

    by_pid = _read_facts()
    dates = _hadm_dates()
    # No holdout fact-filtering here: b1 extracts the established span only, so
    # retention_facts.jsonl already excludes held-out admissions.
    counts = {}
    if COHORT_CSV.exists():
        cdf = pd.read_csv(COHORT_CSV,
                          usecols=lambda c: c in {"subject_id", "hadm_id", "is_holdout"})
        cdf = holdout.established(cdf)        # established admission COUNT (excludes pending review)
        counts = cdf.groupby("subject_id")["hadm_id"].nunique().to_dict()

    if args.subject is not None:
        pids = [args.subject]
    elif args.preview is not None:
        pids = [args.preview]
    else:
        pids = sorted(by_pid)
    ledgers = []
    for pid in pids:
        if pid not in by_pid:
            print(f"no facts for subject_id={pid}")
            continue
        ledgers.append(_project_patient(pid, by_pid[pid], dates, int(counts.get(pid, 0))))

    if args.preview is not None and ledgers:
        print(ledgers[0].model_dump_json(indent=2))
        return

    if args.subject is not None:
        io_utils.merge_jsonl(LEDGER_JSONL,
                             [json.loads(l.model_dump_json()) for l in ledgers],
                             lambda r: int(r["subject_id"]))
        print(f"B2 subject {args.subject}: merged raw ledger record into {LEDGER_JSONL.name} "
              f"(b4 --subject will consolidate it)")
        return

    LEDGER_JSONL.write_text("".join(l.model_dump_json() + "\n" for l in ledgers), encoding="utf-8")

    tot_nodes = sum(len(l.nodes) for l in ledgers)
    tot_flagged = sum(len(l.flagged) for l in ledgers)
    tot_amended = sum(len(l.amended) for l in ledgers)
    tot_episodes = sum(len(l.episodes) for l in ledgers)
    tot_excl = sum(len(l.considered_excluded) for l in ledgers)
    tot_dropped = sum(len(l.dropped) for l in ledgers)
    by_reason: dict[str, int] = {}
    for l in ledgers:
        for d in l.dropped:
            by_reason[d.reason] = by_reason.get(d.reason, 0) + 1
    by_tier: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for l in ledgers:
        for n in l.nodes + l.flagged:
            by_tier[n.salience_tier] = by_tier.get(n.salience_tier, 0) + 1
            by_class[n.retention_class] = by_class.get(n.retention_class, 0) + 1
    print("\n" + "=" * 55)
    print(f"RETENTION LEDGER  ({len(ledgers)} patients)")
    print(f"  confirmed nodes    : {tot_nodes}  ({tot_nodes / max(len(ledgers),1):.1f}/patient)")
    print(f"  flagged (provisional/reported): {tot_flagged}")
    print(f"  amended (retracted, unserved): {tot_amended}")
    print(f"  episode rollups    : {tot_episodes}")
    print(f"  considered/excluded: {tot_excl}")
    print(f"  dropped (pruned)   : {tot_dropped}  {dict(sorted(by_reason.items(), key=lambda x: -x[1]))}")
    print(f"  by tier            : {dict(sorted(by_tier.items()))}")
    print(f"  by class           : {dict(sorted(by_class.items(), key=lambda x: -x[1]))}")
    print(f"\nSaved -> {LEDGER_JSONL}")
    print("\nB2 done. Spot-check a deep-fold / transplant / cancer patient with --preview <id>.")


if __name__ == "__main__":
    main()
