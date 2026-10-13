# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
evidence.py
===========
Deterministic evidence collector. The only component that touches BOTH databases:
it reads a card from longsum_support and gathers facts about it from the clinical
cohort, read-only.

WHY THIS IS CODE AND NOT A MODEL
--------------------------------
The forensic procedure it automates says, in as many words, to EXECUTE the
deterministic taxonomy functions rather than predict them, and to locate a fact
stage by stage through the artifacts rather than reason about where it probably
went. Those are instructions for a program. A model given tool access would
reproduce them unreliably and attach a confident-looking trace to a card, which
is worse than attaching nothing.

What it produces is evidence, not a verdict. The owners still decide.

THE S1 DECISION TREE
--------------------
S1 ("a fact is missing") is the commonest report and the ambiguous one - an
extraction miss, a taxonomy prune and a consolidation absorption are
indistinguishable to the physician. Four indexed queries separate them
completely, with no model and no file access:

    absent from retention_facts .................. S1a  b1 extraction miss
    present in retention_dropped ................. S1b  taxonomy prune
    in tiered_ledger_raw but not tiered_ledger ... S1c  absorbed by b4
    in tiered_ledger ............................. S1d  present after all

The ordering matters: it walks the pipeline forwards, so the FIRST stage that
lost the fact is the one named. Reversing it would attribute every loss to the
last stage that could have carried it.

FACT IDENTITY
-------------
fact_id = sha1(f"{source_hadm_id}|{text}")[:16], per contracts.RetentionFact.
assign_fact_id. Reports anchored on a ledger row carry fact_ids already. Reports
anchored on a Completeness-tab flag carry only (fact_text, source_hadm_id), so the
id is reconstructed - and, because b1 text and physician-quoted text may differ by
whitespace or punctuation, a normalised-text match is tried as a fallback before
concluding a fact was never extracted. Without that fallback S1a would be
over-diagnosed, and S1a is the most expensive disposition in the system.

READ-ONLY, ENFORCED AT THE SESSION
----------------------------------
The clinical connection is opened with psycopg2's read-only session flag, so a
coding error here cannot write to a cohort. The support connection is separate and
is the only one written to.

Usage
-----
    cd ~/clin_longsum/src
    python -m support.evidence 1247            # collect and print evidence for feedback card 1247
    python -m support.evidence 1247 --attach   # also persist to feedback_evidence and re-run triage
    python -m support.evidence 1247 --json     # print raw JSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from typing import Any, Optional

try:
    from support import service as svc
except ImportError:  # pragma: no cover - direct execution from inside support/
    import service as svc

COLLECTOR_VERSION = "evidence.v1"

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


# ── identity ───────────────────────────────────────────────────────────────────

def fact_id_for(source_hadm_id: Optional[int], text: str) -> str:
    """Reproduce contracts.RetentionFact.assign_fact_id exactly."""
    return hashlib.sha1(f"{source_hadm_id}|{text}".encode("utf-8")).hexdigest()[:16]


def _norm(text: Optional[str]) -> str:
    """Aggressive normalisation for FALLBACK matching only.

    Strips punctuation as well as case and whitespace, which is too lossy for a
    dedup key but right here: the question is only "did b1 ever produce something
    that is recognisably this fact", and a false negative sends the card down the
    most expensive disposition in the system.
    """
    if not text:
        return ""
    return _WS.sub(" ", _PUNCT.sub(" ", text.casefold())).strip()


# ── connections ────────────────────────────────────────────────────────────────

def clinical_conn(cohort: str):
    """Read-only connection to a named clinical database.

    Connects by explicit dbname rather than through database.pg.get_conn(), which
    resolves PGDATABASE: a card names the cohort it refers to, and the collector
    must honour that rather than whatever the ambient shell happens to point at.
    Read-only is set on the session, so nothing here can write to a cohort even
    by mistake.
    """
    import psycopg2
    from dotenv import load_dotenv
    try:
        import config
        load_dotenv(config.PROJECT_ROOT / ".env")
    except Exception:
        load_dotenv()
    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        dbname=cohort,
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


# ── probes ─────────────────────────────────────────────────────────────────────

def _resolve_fact_ids(cur, subject_id: int, fact_ids, fact_text, source_hadm_ids) -> dict:
    """Settle which fact_id(s) this card is about."""
    out = {"given": list(fact_ids or []), "reconstructed": [], "text_matched": [],
           "resolved": [], "method": None}
    if fact_ids:
        out["resolved"] = list(fact_ids)
        out["method"] = "anchor"
        return out

    for h in (source_hadm_ids or [None]):
        out["reconstructed"].append(fact_id_for(h, fact_text or ""))
    cur.execute("""SELECT fact_id FROM retention_facts
                    WHERE subject_id=%s AND fact_id = ANY(%s);""",
                (subject_id, out["reconstructed"]))
    hit = [r[0] for r in cur.fetchall()]
    if hit:
        out["resolved"], out["method"] = hit, "reconstructed"
        return out

    # Fallback: normalised text. Guards against over-diagnosing S1a when the
    # physician's quoted wording differs trivially from b1's.
    cur.execute("""SELECT fact_id, text FROM retention_facts WHERE subject_id=%s;""",
                (subject_id,))
    target = _norm(fact_text)
    if target:
        matches = [fid for fid, txt in cur.fetchall() if _norm(txt) == target]
        if matches:
            out["text_matched"], out["resolved"], out["method"] = matches, matches, "text"
            return out
    out["method"] = "unresolved"
    return out


def _s1_walk(cur, subject_id: int, fact_ids: list[str]) -> dict:
    """Walk the pipeline forwards and name the first stage that lost the fact."""
    ev: dict[str, Any] = {"checked_fact_ids": fact_ids}
    if not fact_ids:
        ev["s1_subclass"] = None
        ev["s1_reason"] = ("fact could not be resolved to any fact_id - it may never "
                           "have been extracted, or the quoted text may differ too "
                           "much to match. Needs a human look.")
        return ev

    cur.execute("""SELECT fact_id, text, retention_class, salience_tier, certainty,
                          lifecycle, negative_kind, in_ledger
                     FROM retention_facts
                    WHERE subject_id=%s AND fact_id = ANY(%s);""", (subject_id, fact_ids))
    facts = cur.fetchall()
    ev["in_retention_facts"] = len(facts)
    if not facts:
        ev["s1_subclass"] = "S1a"
        ev["s1_reason"] = ("absent from retention_facts - b1 never produced this fact. "
                           "Prompt-level defect; cohort-wide re-extraction to fix.")
        return ev
    ev["extracted"] = [
        {"fact_id": f[0], "text": f[1], "retention_class": f[2], "salience_tier": f[3],
         "certainty": f[4], "lifecycle": f[5], "negative_kind": f[6], "in_ledger": f[7]}
        for f in facts]

    cur.execute("""SELECT fact_id, reason, taxonomy_version, salience_tier,
                          retention_class, certainty, lifecycle, negative_kind
                     FROM retention_dropped
                    WHERE subject_id=%s AND fact_id = ANY(%s);""", (subject_id, fact_ids))
    dropped = cur.fetchall()
    if dropped:
        ev["s1_subclass"] = "S1b"
        ev["dropped"] = [
            {"fact_id": d[0], "reason": d[1], "taxonomy_version": d[2],
             "salience_tier": d[3], "retention_class": d[4], "certainty": d[5],
             "lifecycle": d[6], "negative_kind": d[7]} for d in dropped]
        ev["s1_reason"] = ("pruned by the taxonomy gate; prune_reason() names the "
                           "branch. Cheap to fix - retention_facts is already extracted.")
        return ev

    cur.execute("""SELECT count(*) FROM tiered_ledger
                    WHERE subject_id=%s AND fact_ids && %s;""", (subject_id, fact_ids))
    in_ledger = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM tiered_ledger_raw
                    WHERE subject_id=%s AND fact_ids && %s;""", (subject_id, fact_ids))
    in_raw = cur.fetchone()[0]
    ev["in_tiered_ledger"], ev["in_tiered_ledger_raw"] = in_ledger, in_raw

    if in_ledger:
        ev["s1_subclass"] = "S1d"
        ev["s1_reason"] = ("present in the served ledger. Not a retention loss - "
                           "a display problem, or the row was read past.")
        cur.execute("""SELECT text, salience_tier, retention_class, certainty,
                              current_lifecycle, negative_kind, n_mentions
                         FROM tiered_ledger
                        WHERE subject_id=%s AND fact_ids && %s;""", (subject_id, fact_ids))
        ev["ledger_rows"] = [
            {"text": r[0], "salience_tier": r[1], "retention_class": r[2],
             "certainty": r[3], "current_lifecycle": r[4], "negative_kind": r[5],
             "n_mentions": r[6]} for r in cur.fetchall()]
        return ev
    if in_raw:
        ev["s1_subclass"] = "S1c"
        ev["s1_reason"] = ("in the lossless pre-consolidation ledger but not the "
                           "served one - absorbed by b4 tier-aware consolidation.")
        return ev

    ev["s1_subclass"] = None
    ev["s1_reason"] = ("extracted, not dropped, and in neither ledger. Does not fit "
                       "the decision tree - needs a human trace.")
    return ev


def s1_subclass(cur, subject_id: int, fact_ids: list[str]) -> dict:
    """Walk the pipeline, then surface the fact's TRUE axes as `discovered_axes`.

    This is what makes a free-text "something is missing" report triageable. Such
    a report has no row on screen, so the card carries no salience_tier - and
    U-1, the gate that makes a missing T1 fact urgent, keys on exactly that. The
    physician cannot supply it: tiers are an internal construct.

    So the collector recovers the axes from wherever the fact actually sits -
    retention_dropped records them as they stood at the gate, the ledger as
    served, retention_facts as b1 assigned them - and triage overlays them onto
    the card before evaluating urgency. A missing anaphylaxis allergy therefore
    reaches 'urgent' by the same rule and the same evidence as one reported from
    a visible row.

    When the fact was never extracted at all (S1a) there are no axes to recover.
    That is honest rather than a gap: the card stays routine and dual-owned, and
    says plainly that it needs a human.
    """
    ev = _s1_walk(cur, subject_id, fact_ids)
    axes_keys = ("salience_tier", "retention_class", "certainty", "negative_kind")
    # Precedence: the gate's own record, then the served ledger, then b1.
    for source, key, lifecycle_key in ((ev.get("dropped"), "dropped", "lifecycle"),
                                       (ev.get("ledger_rows"), "ledger_rows", "current_lifecycle"),
                                       (ev.get("extracted"), "extracted", "lifecycle")):
        if source:
            row = source[0]
            axes = {k: row.get(k) for k in axes_keys if row.get(k)}
            if row.get(lifecycle_key):
                axes["current_lifecycle"] = row[lifecycle_key]
            if axes:
                ev["discovered_axes"] = axes
                ev["discovered_axes_source"] = key
                break
    return ev


# Cues that a mention is NEGATED. A match inside one of these windows is worse
# than no match: it would credit the source notes with asserting something they
# explicitly deny.
_NEGATION_CUES = ("no ", "not ", "non ", "without ", "denies", "denied", "negative",
                  "ruled out", "absence of", "no evidence of", "free of",
                  "unremarkable")

# Words too common to identify a clinical concept. Kept deliberately short: an
# over-eager stoplist silently weakens every match.
_STOP = {"the", "and", "with", "from", "that", "this", "was", "were", "has", "have",
         "had", "for", "his", "her", "patient", "admission", "admissions", "last",
         "noted", "found", "during", "there", "been", "also", "which", "into"}


def _terms(text: Optional[str]) -> list[str]:
    """Content tokens, singular-folded.

    WORD BOUNDARIES MATTER MORE THAN THEY LOOK. A substring search for 'adrenal'
    matches 'hypoadrenalism' - a different concept, and in the case that prompted
    this function, a NEGATED one in an unrelated admission. That single false
    match would have diagnosed an extraction miss and sent someone to re-run b1
    across the cohort, which is the most expensive disposition in the system.
    """
    out = []
    for t in _norm(text).split():
        if len(t) <= 3 or t in _STOP:
            continue
        out.append(t[:-1] if len(t) > 4 and t.endswith("s") else t)
    return out


def _coverage(terms: list[str], body: str) -> tuple[float, list[str]]:
    if not terms:
        return 0.0, []
    toks = set()
    for t in body.split():
        toks.add(t)
        if len(t) > 4 and t.endswith("s"):
            toks.add(t[:-1])
    found = [t for t in terms if t in toks]
    return round(len(found) / len(terms), 2), found


def _negated_near(body: str, term: str, window: int = 70) -> bool:
    i = body.find(term)
    while i != -1:
        if any(c in body[max(0, i - window):i] for c in _NEGATION_CUES):
            return True
        i = body.find(term, i + 1)
    return False


def missing_fact_probe(cur, subject_id: int, fact_text: Optional[str]) -> dict:
    """Where, if anywhere, does a reportedly-missing fact actually appear?

    Runs when a report says something is missing and no fact_id could be
    resolved. Without it the card reads only "unresolved - needs a human look",
    which leaves the commonest report type answered with the least information.

    THE DISCRIMINATOR IS STRUCTURAL, NOT SEMANTIC. The ledger is built from PRIOR
    admissions. A fact appearing only in the INCOMING admission is not missing at
    all - it has not been folded in yet. From the physician's seat that is
    indistinguishable from an extraction miss, but telling them apart needs no
    model, only knowing which admission the text sat in. Hence deterministic.

    Verdicts:
      in_incoming_only     - not a defect. Expected until fold-in. Close by_design.
      in_prior_unextracted - notes have it, b1 never extracted it: S1a.
      negated_only         - the notes DENY this. Not a retention failure.
      not_in_source        - absent from every note; the pipeline cannot retain
                             what the record does not contain.
      ambiguous            - partial signal; a human should read the notes.

    Limits, stated plainly: this is token overlap, not a faithfulness judgement.
    It will miss synonymy and paraphrase ('adrenal masses' for 'adrenal nodules').
    It errs toward `ambiguous`, which routes to a human - the safe direction,
    since the costly error is wrongly exonerating the pipeline.
    """
    out: dict[str, Any] = {"terms": _terms(fact_text)}
    if not out["terms"]:
        out["verdict"] = "ambiguous"
        out["reason"] = "reported text carries no distinctive terms to search for"
        return out

    cur.execute("""SELECT a.hadm_id, a.admit_seq, a.bhc_text,
                          (a.hadm_id = tv.incoming_hadm_id) AS is_incoming
                     FROM admissions a
                     LEFT JOIN tiered_view tv ON tv.subject_id = a.subject_id
                    WHERE a.subject_id = %s ORDER BY a.admit_seq;""", (subject_id,))
    per, best_prior, best_incoming = [], 0.0, 0.0
    for hadm_id, seq, bhc, is_incoming in cur.fetchall():
        body = _norm(bhc)
        cov, found = _coverage(out["terms"], body)
        if not cov:
            continue
        negated = bool(found) and all(_negated_near(body, t) for t in found)
        per.append({"hadm_id": hadm_id, "admit_seq": seq, "is_incoming": bool(is_incoming),
                    "coverage": cov, "matched": found, "negated": negated})
        if not negated:
            if is_incoming:
                best_incoming = max(best_incoming, cov)
            else:
                best_prior = max(best_prior, cov)
    per.sort(key=lambda p: -p["coverage"])
    out["per_admission"] = per[:6]
    out["best_prior_coverage"] = best_prior
    out["best_incoming_coverage"] = best_incoming

    # Near misses in retention_facts: if b1 DID extract something similar, the
    # report is probably about wording, merging or tier rather than a loss.
    cur.execute("""SELECT fact_id, text, salience_tier, retention_class
                     FROM retention_facts WHERE subject_id=%s;""", (subject_id,))
    near = []
    for fid, txt, tier, cls in cur.fetchall():
        cov, _ = _coverage(out["terms"], _norm(txt))
        if cov >= 0.5:
            near.append({"fact_id": fid, "text": txt, "salience_tier": tier,
                         "retention_class": cls, "coverage": cov})
    near.sort(key=lambda n: -n["coverage"])
    out["near_misses"] = near[:5]

    if near:
        out["verdict"] = "ambiguous"
        out["reason"] = ("a similar fact WAS extracted - more likely about wording, "
                         "merging or tier than a retention loss")
    elif best_incoming >= 0.6 and best_prior < 0.6:
        out["verdict"] = "in_incoming_only"
        out["reason"] = ("this appears only in the INCOMING admission. The ledger is "
                         "built from prior admissions, so it is correctly absent and "
                         "will appear once this admission is folded in. Not a defect "
                         "- close by_design, and consider whether the UI makes the "
                         "distinction clear enough.")
    elif best_prior >= 0.6:
        out["verdict"] = "in_prior_unextracted"
        out["reason"] = ("present in a prior admission's notes but never extracted into "
                         "retention_facts - a b1 extraction miss (S1a). Expensive: a "
                         "prompt fix means cohort-wide re-extraction.")
    elif per and all(p["negated"] for p in per):
        out["verdict"] = "negated_only"
        out["reason"] = ("the only mentions in the source notes are negated - the notes "
                         "deny this rather than assert it. Not a retention failure.")
    elif not per:
        out["verdict"] = "not_in_source"
        out["reason"] = ("no mention in any admission's notes for this patient. Not a "
                         "pipeline defect: the pipeline cannot retain what the source "
                         "record does not contain.")
    else:
        out["verdict"] = "ambiguous"
        out["reason"] = "partial matches only - a human should read the notes"
    return out


def bhc_support(cur, subject_id: int, fact_text: Optional[str],
                source_hadm_ids) -> dict:
    """Is the reported text supported by the source notes? Feeds S2.

    Token overlap only - a cheap screen, not a faithfulness judgement. Most
    reported fabrications turn out to be synonymy, and this catches the obvious
    half so an owner is not reading whole discharge summaries by hand.
    """
    out: dict[str, Any] = {"searched_hadm_ids": list(source_hadm_ids or [])}
    if not fact_text:
        return out
    if source_hadm_ids:
        cur.execute("""SELECT hadm_id, bhc_text FROM admissions
                        WHERE subject_id=%s AND hadm_id = ANY(%s);""",
                    (subject_id, list(source_hadm_ids)))
    else:
        cur.execute("""SELECT hadm_id, bhc_text FROM admissions WHERE subject_id=%s;""",
                    (subject_id,))
    rows = cur.fetchall()
    terms = [t for t in _norm(fact_text).split() if len(t) > 3]
    if not terms:
        return out
    hits = []
    for hadm_id, bhc in rows:
        body = _norm(bhc)
        if not body:
            continue
        found = [t for t in terms if t in body]
        if found:
            hits.append({"hadm_id": hadm_id, "matched_terms": len(found),
                         "of_terms": len(terms),
                         "coverage": round(len(found) / len(terms), 2)})
    hits.sort(key=lambda h: -h["coverage"])
    out["term_hits"] = hits[:5]
    out["best_coverage"] = hits[0]["coverage"] if hits else 0.0
    out["verdict"] = ("likely supported" if hits and hits[0]["coverage"] >= 0.7 else
                      "partially supported" if hits and hits[0]["coverage"] >= 0.4 else
                      "no clear support in source notes")
    return out


def taxonomy_check(card: dict) -> dict:
    """Run the live taxonomy over the card's own axes. Feeds S3.

    EXECUTED, never predicted. Answers the question an owner actually has on a
    wrong-label report: under the CURRENT taxonomy, would this row still be a
    ledger member, and if not, which branch drops it.
    """
    out: dict[str, Any] = {}
    try:
        import retention_taxonomy as tax
    except ImportError as e:
        out["error"] = f"retention_taxonomy unavailable: {e}"
        return out

    out["taxonomy_version_now"] = tax.TAXONOMY_VERSION
    out["taxonomy_version_at_capture"] = card.get("taxonomy_version")
    out["version_drift"] = (card.get("taxonomy_version") not in (None, "")
                            and card["taxonomy_version"] != tax.TAXONOMY_VERSION)
    axes = dict(salience_tier=card.get("salience_tier"),
                certainty=card.get("certainty"),
                lifecycle=card.get("current_lifecycle"),
                negative_kind=card.get("negative_kind"),
                retention_class=card.get("retention_class"))
    out["axes"] = axes
    if not axes["salience_tier"]:
        return out
    try:
        out["is_ledger_member_now"] = bool(tax.is_ledger_member(
            axes["salience_tier"], axes["certainty"] or "confirmed",
            axes["lifecycle"] or "active", axes["negative_kind"],
            axes["retention_class"]))
    except Exception as e:
        out["is_ledger_member_error"] = str(e)
    try:
        out["prune_reason_now"] = tax.prune_reason(
            axes["salience_tier"], axes["certainty"] or "confirmed",
            axes["lifecycle"] or "active", axes["negative_kind"],
            axes["retention_class"])
    except Exception as e:
        out["prune_reason_error"] = str(e)
    return out


def artifact_drift(cur, subject_id: int, card: dict) -> dict:
    """Was the reported record superseded by a later regeneration or a newer taxonomy?

    TWO BASELINES, NOT ONE. Comparing the card's snapshot against `tiered_view`
    alone answers "has this patient been regenerated since?" - which is silent in
    the common case where the whole cohort is stale. The taxonomy in the code has
    usually moved on while every stored artifact still carries the older version,
    so an entire class of already-fixed defects reads as current. Comparing
    against retention_taxonomy.TAXONOMY_VERSION as well catches that, and it is
    the cheapest disposition available: `already_fixed` costs a re-run, not an
    investigation.
    """
    out: dict[str, Any] = {}
    try:
        import retention_taxonomy as tax
        out["code_taxonomy_version"] = tax.TAXONOMY_VERSION
    except Exception:
        out["code_taxonomy_version"] = None

    cur.execute("""SELECT taxonomy_version, generated_at FROM tiered_view
                    WHERE subject_id=%s;""", (subject_id,))
    row = cur.fetchone()
    if not row:
        out["note"] = "no tiered_view row - patient may not be loaded in this cohort"
        return out
    out["current_taxonomy_version"] = row[0]
    out["current_generated_at"] = str(row[1]) if row[1] else None
    out["reported_taxonomy_version"] = card.get("taxonomy_version")
    out["reported_generated_at"] = (str(card["artifact_generated_at"])
                                    if card.get("artifact_generated_at") else None)
    out["regenerated_since_report"] = bool(
        card.get("artifact_generated_at") and row[1]
        and row[1] > card["artifact_generated_at"].replace(tzinfo=None))
    out["taxonomy_moved_since_report"] = bool(
        card.get("taxonomy_version") and row[0] and row[0] != card["taxonomy_version"])
    # The stored artifact itself is behind the code. Nothing about this patient
    # has changed, but a re-run would apply gate changes the report may predate.
    out["artifact_behind_code"] = bool(
        out["code_taxonomy_version"] and row[0]
        and row[0] != out["code_taxonomy_version"])
    out["stale_versus"] = (
        f"artifact {row[0]} vs code {out['code_taxonomy_version']}"
        if out["artifact_behind_code"] else None)
    return out


# ── orchestration ──────────────────────────────────────────────────────────────

CARD_COLS = ("feedback_id", "cohort", "subject_id", "symptom", "fact_text", "fact_ids",
             "source_hadm_ids", "salience_tier", "retention_class", "certainty",
             "current_lifecycle", "negative_kind", "disputed_field",
             "taxonomy_version", "prompt_version", "artifact_generated_at")


# Probe verdicts that settle the S1 sub-class on their own. The other verdicts
# (negated_only, not_in_source, ambiguous) deliberately do NOT map: they mean the
# report is not a retention failure at all, or that a human has to read the
# notes, and inventing a sub-class for either would put a confident label on an
# unresolved card.
_PROBE_TO_SUBCLASS = {
    "in_incoming_only": "S1e",
    "in_prior_unextracted": "S1a",
}


def collect(support_conn, feedback_id: int) -> dict:
    """Gather all evidence for one card. Read-only against the cohort."""
    cur = support_conn.cursor()
    cur.execute(f"SELECT {', '.join(CARD_COLS)} FROM feedback WHERE feedback_id=%s;",
                (feedback_id,))
    row = cur.fetchone()
    if not row:
        raise svc.ServiceError(f"no such report: {feedback_id}")
    card = dict(zip(CARD_COLS, row))

    ev: dict[str, Any] = {"collector_version": COLLECTOR_VERSION,
                          "cohort": card["cohort"], "symptom": card["symptom"]}
    cconn = clinical_conn(card["cohort"])
    try:
        ccur = cconn.cursor()
        ids = _resolve_fact_ids(ccur, card["subject_id"], card["fact_ids"],
                                card["fact_text"], card["source_hadm_ids"])
        ev["fact_identity"] = ids

        # S1 is the expensive ambiguity, so resolve it for any symptom that could
        # turn out to be a retention loss, not only when S1 was picked.
        if card["symptom"] in ("S1", "S6", "S99"):
            ev.update(s1_subclass(ccur, card["subject_id"], ids["resolved"]))
            # Unresolved is the commonest S1 outcome and the least informative.
            # The probe turns it into an actionable finding - in particular it
            # separates "not folded in yet" from a genuine extraction miss.
            if not ev.get("s1_subclass"):
                probe = missing_fact_probe(ccur, card["subject_id"], card["fact_text"])
                ev["missing_probe"] = probe
                sub = _PROBE_TO_SUBCLASS.get(probe.get("verdict"))
                if sub:
                    # The probe is the ONLY route to S1a and S1e. S1a was
                    # otherwise unreachable: it requires a fact_id that resolved
                    # but is absent from retention_facts, and resolution falls
                    # back to matching against retention_facts itself, so genuine
                    # extraction misses landed as "unresolved" instead.
                    ev["s1_subclass"] = sub
                    ev["s1_reason"] = probe.get("reason")
                    ev["s1_source"] = "missing_fact_probe"
        if card["symptom"] in ("S2", "S99"):
            ev["bhc_support"] = bhc_support(ccur, card["subject_id"], card["fact_text"],
                                            card["source_hadm_ids"])
        if card["symptom"] in ("S3", "S4", "S99"):
            ev["taxonomy"] = taxonomy_check(card)
        ev["artifact"] = artifact_drift(ccur, card["subject_id"], card)
    finally:
        cconn.close()
    return ev


def collect_and_attach(support_conn, feedback_id: int, *, retriage: bool = True) -> dict:
    ev = collect(support_conn, feedback_id)
    svc.attach_evidence(support_conn, feedback_id, COLLECTOR_VERSION, ev,
                        retriage=retriage)
    return ev


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect forensic evidence for a report.")
    ap.add_argument("feedback_id", type=int)
    ap.add_argument("--attach", action="store_true",
                    help="Persist to feedback_evidence and re-run triage.")
    ap.add_argument("--json", action="store_true", help="Print raw JSON.")
    args = ap.parse_args()

    conn = svc.connect()
    try:
        ev = (collect_and_attach(conn, args.feedback_id) if args.attach
              else collect(conn, args.feedback_id))
    finally:
        conn.close()

    if args.json:
        print(json.dumps(ev, indent=2, default=str))
        return
    print(f"report #{args.feedback_id}  {ev['symptom']}  cohort={ev['cohort']}")
    fi = ev.get("fact_identity", {})
    print(f"  fact identity: {fi.get('method')} -> {fi.get('resolved') or 'unresolved'}")
    if "s1_subclass" in ev:
        print(f"  S1 sub-class : {ev['s1_subclass'] or 'unresolved'}")
        print(f"                 {ev.get('s1_reason', '')}")
    if "bhc_support" in ev:
        b = ev["bhc_support"]
        print(f"  source notes : {b.get('verdict')} (best coverage {b.get('best_coverage')})")
    if "taxonomy" in ev:
        t = ev["taxonomy"]
        print(f"  taxonomy     : now {t.get('taxonomy_version_now')}, "
              f"ledger member now = {t.get('is_ledger_member_now')}, "
              f"prune_reason = {t.get('prune_reason_now')}")
    a = ev.get("artifact", {})
    if a.get("taxonomy_moved_since_report") or a.get("regenerated_since_report"):
        print(f"  artifact     : SUPERSEDED since the report "
              f"(now {a.get('current_taxonomy_version')} @ {a.get('current_generated_at')}) "
              f"- consider disposition 'already_fixed'")


if __name__ == "__main__":
    main()
