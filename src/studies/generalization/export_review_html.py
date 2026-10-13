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
export_review_html.py  --  read-only qualitative review page for a cohort
=========================================================================
Once-off utility: dumps the served tiered record (TL;DR + current episode + prior-
admissions prose + per-admission episode paragraphs + retention ledger + the B2 prune
audit trail) for every patient in a database into a SINGLE self-contained HTML file.
No server, no FastAPI, no approve/edit workflow -- open the file in a browser and read.

Intended use: hands-on manual review of summarisation quality on the generalisation
cohort, side by side with the raw bhc_text (data_<ns>/interim/cohort_admissions.csv).

Data source: tiered_view / tiered_episode / tiered_ledger / retention_dropped from the
database named by PGDATABASE (falls back to .env). Point it at the gen DB.

OUTPUT goes to the DATA tree, never the repo: the page embeds patient-derived text and
the repo is not a DUA-compliant location. Namespace-aware via config.
    -> <data root>/output/studies/generalization/gen_review.html

    cd ~/clin_longsum/src
    $env:LONGSUM_NAMESPACE="gen"; $env:PGDATABASE="longitudinal_bhc_gen"
    python studies/generalization/export_review_html.py

Options:
    --out PATH             override the output file
    --db  NAME             override PGDATABASE for this run
    --subjects ...         restrict to specific subject_ids
    --episode-order {desc,asc}
                           order of the Episode Paragraphs section (default: desc, i.e. NEWEST
                           first). desc puts the admission that was just demoted into the hot
                           window -- the one a fold-in review most needs to check -- right under
                           Current, with no scrolling past a long cold tail to reach it.

Differences from the tiered_longsum original: output lands in the data tree; the
INCOMING (held-out) admission is rendered when present, so a k=1 gen cohort can be read
as "record vs the discharge it has not seen"; and the B2 dropped-fact audit is shown per
patient so a reviewer can see what the ledger deliberately pruned and why.

The styling and the inline md() renderer are lifted from the physician UI
(tiered_review.html) so the qualitative read matches what a physician would see, minus
the incoming-review / sign-off machinery.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # src/ on the path

import config

OUT_DIR = config.OUTPUT_DIR / "studies" / "generalization"
DEFAULT_OUT = OUT_DIR / "gen_review.html"


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def connect(db_override: str | None):
    load_dotenv(override=False)
    params = dict(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        dbname=db_override or os.getenv("PGDATABASE", "longitudinal_bhc"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )
    return psycopg2.connect(**params), params["dbname"]


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{name}",))
    return bool(cur.fetchone()["ok"])


def fetch_all(conn, subjects: list[int] | None, episode_order: str = "desc") -> list[dict]:
    """Assemble one nested dict per patient: prose + incoming + episodes + ledger + dropped.

    episode_order controls only the EPISODE PARAGRAPHS list (per-admission paragraphs, hot and
    cold together); the ledger, TL;DR, and compacted-prose sections are unaffected -- there is
    nothing to reorder in a single narrative blob or a ledger already sorted by tier.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where, params = "", []
    if subjects:
        where, params = " WHERE subject_id = ANY(%s)", [list(subjects)]

    cur.execute(f"""
        SELECT subject_id, n_total_admissions, first_date::text, last_date::text,
               current_service, current_seq, tldr_md, current_episode_md,
               prior_admissions_md, taxonomy_version,
               incoming_hadm_id, incoming_seq, incoming_date::text, incoming_service,
               incoming_summary_md, incoming_bhc_text
        FROM tiered_view{where} ORDER BY subject_id
    """, params)
    patients = {r["subject_id"]: dict(r, episodes=[], ledger=[], flagged=[], dropped=[])
                for r in cur.fetchall()}

    cur.execute(f"""
        SELECT subject_id, hadm_id, admit_seq, admit_date::text, service,
               paragraph_md, in_hot_window
        FROM tiered_episode{where}
        ORDER BY subject_id, admit_seq {"DESC" if episode_order == "desc" else "ASC"}
    """, params)
    for r in cur.fetchall():
        p = patients.get(r["subject_id"])
        if p is not None:
            p["episodes"].append(dict(r))

    cur.execute(f"""
        SELECT subject_id, retention_class, salience_tier, text, certainty,
               current_lifecycle, negative_kind, provisional,
               onset_date::text, last_confirmed_date::text, n_mentions
        FROM tiered_ledger{where}
        ORDER BY subject_id,
                 CASE upper(salience_tier) WHEN 'T1' THEN 1 WHEN 'T2' THEN 2
                      WHEN 'T3' THEN 3 WHEN 'T4' THEN 4 WHEN 'T5' THEN 5 ELSE 6 END,
                 onset_date          NULLS LAST,
                 last_confirmed_date NULLS LAST,
                 retention_class,
                 text
    """, params)
    for r in cur.fetchall():
        p = patients.get(r["subject_id"])
        if p is None:
            continue
        (p["flagged"] if r["provisional"] else p["ledger"]).append(dict(r))

    # B2 prune audit trail -- present since tax v2.11; tolerate an older DB without it.
    if _table_exists(cur, "retention_dropped"):
        cur.execute(f"""
            SELECT subject_id, salience_tier, retention_class, text, reason
            FROM retention_dropped{where} ORDER BY subject_id, salience_tier, text
        """, params)
        for r in cur.fetchall():
            p = patients.get(r["subject_id"])
            if p is not None:
                p["dropped"].append(dict(r))

    cur.close()
    return list(patients.values())


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def build_html(patients: list[dict], db_name: str, ns: str, episode_order: str) -> str:
    # Escape "<" so a stray "</script>" or "<!--" inside any bhc-derived text cannot
    # break out of the <script> block that embeds the data.
    data_json = json.dumps(patients, default=str).replace("<", "\\u003c")
    return (_TEMPLATE.replace("__DB_NAME__", db_name)
                     .replace("__NS__", ns or "(default)")
                     .replace("__N_PATIENTS__", str(len(patients)))
                     .replace("__EPISODE_ORDER__", episode_order)
                     .replace("__DATA_JSON__", data_json))


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LongSum &ndash; Read-only Review (__DB_NAME__)</title>
<style>
:root {
  --bg:#0f1117; --surface:#181c27; --surface2:#1e2332; --border:#2a3042; --border2:#3a4255;
  --text:#e2e8f4; --text-muted:#8a94a9; --text-dim:#5a6478;
  --accent:#4f8ef7; --accent-dim:#1e3a6e;
  --high:#f59c38; --high-bg:#2a1e0a; --high-border:#6b3f0a;
  --low:#3ecf7a; --low-bg:#0a2015; --low-border:#1a5030;
  --fail:#f0524d; --fail-bg:#2a0d0c; --fail-border:#6b1714;
  --neg:#a78bfa;
  --t1:#f0524d; --t2:#f59c38; --t3:#4f8ef7;
  --mono: ui-monospace, 'Cascadia Code', Consolas, 'SF Mono', Menlo, monospace;
  --sans: system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  --radius:5px;
}
* { box-sizing:border-box; margin:0; padding:0; }
html, body { height:100%; overflow:hidden; }
body { font-family:var(--sans); font-size:15px; background:var(--bg); color:var(--text); display:flex; flex-direction:column; }
#hdr { height:50px; background:var(--surface); border-bottom:1px solid var(--border); display:flex; align-items:center; padding:0 18px; gap:14px; flex-shrink:0; }
#hdr .logo { font-family:var(--mono); font-size:15px; font-weight:500; color:var(--accent); letter-spacing:0.02em; }
#hdr .sep { color:var(--border2); }
#hdr .title { font-size:14px; color:var(--text-muted); }
#hdr .spacer { flex:1; }
#hdr .db { font-family:var(--mono); font-size:12px; padding:4px 11px; background:var(--surface2); border:1px solid var(--border2); border-radius:var(--radius); color:var(--text-muted); }
#hdr .count { font-family:var(--mono); font-size:13px; padding:4px 11px; background:var(--accent-dim); border:1px solid var(--accent); border-radius:var(--radius); color:var(--accent); }

#layout { flex:1; display:flex; overflow:hidden; }
#sidebar { width:250px; flex-shrink:0; background:var(--surface); border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden; }
#sidebar-hdr { padding:11px 15px 9px; border-bottom:1px solid var(--border); font-family:var(--mono); font-size:11px; color:var(--text-dim); letter-spacing:0.08em; text-transform:uppercase; flex-shrink:0; }
#queue-list { flex:1; overflow-y:auto; }
#queue-list::-webkit-scrollbar { width:4px; }
#queue-list::-webkit-scrollbar-thumb { background:var(--border2); border-radius:2px; }
.q-item { padding:11px 15px 10px; border-bottom:1px solid var(--border); cursor:pointer; border-left:3px solid transparent; }
.q-item:hover { background:var(--surface2); }
.q-item.active { background:var(--surface2); border-left-color:var(--accent); }
.q-row1 { display:flex; align-items:center; gap:6px; margin-bottom:3px; }
.q-sid { font-family:var(--mono); font-size:14px; font-weight:500; }
.q-row2 { font-size:13px; color:var(--text-muted); }
.q-counts { display:flex; gap:5px; margin-top:5px; flex-wrap:wrap; }
.chip { font-family:var(--mono); font-size:11px; padding:2px 6px; border-radius:3px; border:1px solid; letter-spacing:0.02em; }
.chip.node { color:var(--low); background:var(--low-bg); border-color:var(--low-border); }
.chip.epi  { color:var(--accent); background:var(--accent-dim); border-color:#2a4a8a; }
.chip.flag { color:var(--fail); background:var(--fail-bg); border-color:var(--fail-border); }
.chip.drop { color:var(--text-dim); background:var(--surface2); border-color:var(--border2); }

#main { flex:1; display:flex; flex-direction:column; overflow:hidden; min-width:0; }
#placeholder { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; color:var(--text-dim); }
#placeholder .icon { font-size:32px; opacity:0.25; }
#content { display:none; flex:1; flex-direction:column; overflow:hidden; }
#content.visible { display:flex; }
#pt-header { padding:11px 18px 9px; background:var(--surface); border-bottom:1px solid var(--border); flex-shrink:0; display:flex; align-items:center; gap:11px; flex-wrap:wrap; }
#pt-id { font-family:var(--mono); font-size:17px; font-weight:500; }
.pt-meta { font-size:14px; color:var(--text-muted); display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.pt-meta .sep { color:var(--border2); }
.svc-tag { font-size:12px; font-family:var(--mono); padding:2px 7px; background:var(--surface2); border:1px solid var(--border2); border-radius:3px; }
#tax-tag { font-size:12px; font-family:var(--mono); padding:2px 7px; color:var(--text-dim); border:1px solid var(--border2); border-radius:3px; margin-left:auto; }
#scroll { flex:1; min-height:0; overflow-y:auto; padding:16px 20px 24px; }
#scroll::-webkit-scrollbar { width:6px; }
#scroll::-webkit-scrollbar-thumb { background:var(--border2); border-radius:2px; }

.section { margin-bottom:18px; }
.section-hdr { font-family:var(--mono); font-size:11px; letter-spacing:0.08em; text-transform:uppercase; color:var(--text-muted); margin-bottom:7px; padding-bottom:4px; border-bottom:1px solid var(--border); }
.section-hdr .n { color:var(--text-dim); }
.card { background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius); padding:13px 16px; }
.tldr-card { border-color:var(--accent-dim); }
.incoming-card { border-color:var(--high-border); }
.epi { background:var(--surface2); border:1px solid var(--border); border-radius:var(--radius); padding:12px 15px; margin-bottom:9px; }
.epi-hdr { display:flex; align-items:center; gap:9px; margin-bottom:6px; font-size:13px; color:var(--text-muted); }
.epi-seq { font-family:var(--mono); font-size:12px; color:var(--accent); background:var(--accent-dim); border:1px solid #2a4a8a; border-radius:3px; padding:1px 7px; }
.epi-cold { opacity:0.85; }
details.raw { margin-top:9px; }
details.raw summary { cursor:pointer; font-family:var(--mono); font-size:12px; color:var(--text-muted); }
details.raw pre { white-space:pre-wrap; font-family:var(--mono); font-size:12.5px; line-height:1.6; color:var(--text-muted); margin-top:8px; padding:11px; background:var(--bg); border:1px solid var(--border); border-radius:var(--radius); max-height:420px; overflow-y:auto; }

.md { font-size:15px; }
.md h1,.md h2 { font-size:16px; font-weight:600; margin:10px 0 6px; }
.md h3 { font-size:14px; font-weight:600; margin:8px 0 4px; color:var(--accent); font-family:var(--mono); }
.md p { margin-bottom:7px; line-height:1.75; }
.md ul,.md ol { margin:4px 0 7px 20px; }
.md li { margin-bottom:3px; line-height:1.7; }
.md hr { border:none; border-top:1px solid var(--border); margin:8px 0; }
.md code { font-family:var(--mono); font-size:13px; background:var(--surface); padding:1px 4px; border-radius:3px; }

.led-wrap { overflow-x:auto; border:1px solid var(--border); border-radius:var(--radius); }
table.led { width:100%; border-collapse:collapse; font-size:14px; }
table.led th { text-align:left; font-family:var(--mono); font-size:11px; letter-spacing:0.04em; text-transform:uppercase; color:var(--text-dim); padding:8px 11px; border-bottom:1px solid var(--border); background:var(--surface); }
table.led td { padding:8px 11px; border-bottom:1px solid var(--border); vertical-align:top; }
table.led tr:last-child td { border-bottom:none; }
table.led tr.flagged { background:var(--fail-bg); }
.tier { font-family:var(--mono); font-size:11px; font-weight:500; padding:1px 6px; border-radius:3px; border:1px solid; }
.tier.T1 { color:var(--t1); border-color:var(--fail-border); background:var(--fail-bg); }
.tier.T2 { color:var(--t2); border-color:var(--high-border); background:var(--high-bg); }
.tier.T3 { color:var(--t3); border-color:#2a4a8a; background:var(--accent-dim); }
.tier.other { color:var(--text-dim); border-color:var(--border2); }
.led-cls { font-family:var(--mono); font-size:12px; color:var(--text-muted); }
.muted { color:var(--text-dim); }
.led-sub { font-family:var(--mono); font-size:11px; letter-spacing:0.06em; text-transform:uppercase; color:var(--fail); margin:13px 0 7px; }
.led-sub.dim { color:var(--text-dim); }
</style>
</head>
<body>
<div id="hdr">
  <span class="logo">LongSum</span><span class="sep">/</span><span class="title">Read-only Review</span>
  <div class="spacer"></div>
  <span class="db">ns: __NS__</span>
  <span class="db">db: __DB_NAME__</span>
  <span class="count">__N_PATIENTS__ patients</span>
</div>
<div id="layout">
  <div id="sidebar">
    <div id="sidebar-hdr">Cohort</div>
    <div id="queue-list"></div>
  </div>
  <div id="main">
    <div id="placeholder"><div class="icon">&#9776;</div><p>Select a patient to review</p></div>
    <div id="content">
      <div id="pt-header">
        <span id="pt-id"></span>
        <div class="pt-meta">
          <span id="pt-admissions"></span><span class="sep">|</span>
          <span id="pt-dates"></span><span id="pt-service" class="svc-tag"></span>
        </div>
        <span id="tax-tag"></span>
      </div>
      <div id="scroll"></div>
    </div>
  </div>
</div>
<script>
const PATIENTS = __DATA_JSON__;
const EPISODE_ORDER = "__EPISODE_ORDER__";
const byId = {}; PATIENTS.forEach(p => byId[p.subject_id] = p);
let current = null;
renderList();

function renderList() {
  const el = document.getElementById('queue-list');
  el.innerHTML = PATIENTS.map(p => {
    const active = current && current.subject_id === p.subject_id ? ' active' : '';
    const nLed = (p.ledger||[]).length, nFlag = (p.flagged||[]).length;
    const nEpi = (p.episodes||[]).length, nDrop = (p.dropped||[]).length;
    return `<div class="q-item${active}" onclick="selectPatient(${p.subject_id})">
      <div class="q-row1"><span class="q-sid">${p.subject_id}</span></div>
      <div class="q-row2">${p.n_total_admissions != null ? p.n_total_admissions + ' adm' : ''}${p.current_service ? ' &middot; ' + esc(p.current_service) : ''}</div>
      <div class="q-counts"><span class="chip node">${nLed} ledger</span><span class="chip epi">${nEpi} epi</span>${nFlag ? `<span class="chip flag">${nFlag} flag</span>` : ''}${nDrop ? `<span class="chip drop">${nDrop} dropped</span>` : ''}</div>
    </div>`;
  }).join('');
}

function selectPatient(sid) {
  current = byId[sid];
  document.getElementById('placeholder').style.display = 'none';
  document.getElementById('content').classList.add('visible');
  renderList();
  renderDetail();
  document.getElementById('scroll').scrollTop = 0;
}

function renderDetail() {
  const p = current;
  document.getElementById('pt-id').innerHTML = `Patient <span style="font-family:var(--mono)">${p.subject_id}</span>`;
  document.getElementById('pt-admissions').textContent =
    `${p.n_total_admissions != null ? p.n_total_admissions : '?'} admissions`;
  const span = (p.first_date && p.last_date) ? `${p.first_date.slice(0,10)} \u2192 ${p.last_date.slice(0,10)}` : '';
  document.getElementById('pt-dates').textContent = span;
  document.getElementById('pt-service').textContent = p.current_service || '';
  document.getElementById('tax-tag').textContent = p.taxonomy_version ? `taxonomy ${p.taxonomy_version}` : '';

  const out = [];
  if ((p.tldr_md||'').trim()) out.push(sec('TL;DR', `<div class="card tldr-card md">${md(p.tldr_md)}</div>`));

  if ((p.incoming_summary_md||'').trim() || (p.incoming_bhc_text||'').trim()) {
    const hdr = `<div class="epi-hdr" style="margin-bottom:9px">
        ${p.incoming_seq != null ? `<span class="epi-seq">seq ${p.incoming_seq}</span>` : ''}
        <span style="font-family:var(--mono)">${p.incoming_date ? p.incoming_date.slice(0,10) : ''}</span>
        ${p.incoming_service ? '<span class="svc-tag">'+esc(p.incoming_service)+'</span>' : ''}
        <span class="muted" style="font-size:11px">held out &ndash; the record has NOT seen this</span></div>`;
    const raw = (p.incoming_bhc_text||'').trim()
      ? `<details class="raw"><summary>raw clinician BHC for the incoming admission</summary><pre>${esc(p.incoming_bhc_text)}</pre></details>` : '';
    out.push(sec('Incoming admission (held out)',
      `<div class="card incoming-card">${hdr}<div class="md">${md(p.incoming_summary_md)}</div>${raw}</div>`));
  }

  if ((p.current_episode_md||'').trim()) out.push(sec('Current / Most Recent Admission', `<div class="card md">${md(p.current_episode_md)}</div>`));

  const epis = p.episodes || [];
  if (epis.length) {
    const blocks = epis.map(e => `<div class="epi${e.in_hot_window ? '' : ' epi-cold'}"><div class="epi-hdr">
        <span class="epi-seq">seq ${e.admit_seq != null ? e.admit_seq : '?'}</span>
        <span style="font-family:var(--mono)">${e.admit_date ? e.admit_date.slice(0,10) : ''}</span>
        ${e.service ? '<span class="svc-tag">'+esc(e.service)+'</span>' : ''}
        ${e.in_hot_window ? '' : '<span class="muted" style="font-size:11px">cold</span>'}</div>
        <div class="md">${md(e.paragraph_md)}</div></div>`).join('');
    out.push(sec(`Episode Paragraphs (${EPISODE_ORDER === 'desc' ? 'newest first' : 'oldest first'})`, blocks, epis.length));
  }

  if ((p.prior_admissions_md||'').trim()) out.push(sec('Prior Admissions (compacted)', `<div class="card md">${md(p.prior_admissions_md)}</div>`));

  const led = p.ledger || [], flg = p.flagged || [], drp = p.dropped || [];
  if (led.length || flg.length) {
    let body = led.length ? ledgerTable(led, false) : '';
    if (flg.length) { body += `<div class="led-sub">Flagged &ndash; provisional, ${flg.length}</div>` + ledgerTable(flg, true); }
    out.push(sec('Retention Ledger', body, led.length + flg.length));
  }
  if (drp.length) {
    const rows = drp.map(r => {
      const t = (r.salience_tier||'').toUpperCase();
      const tc = (t==='T1'||t==='T2'||t==='T3') ? t : 'other';
      return `<tr><td><span class="tier ${tc}">${esc(r.salience_tier||'?')}</span></td>
        <td>${esc(r.text||'')}</td><td class="led-cls">${esc(r.retention_class||'')}</td>
        <td class="led-cls">${esc(r.reason||'')}</td></tr>`;
    }).join('');
    out.push(sec('Dropped at ledger build (audit only, never served)',
      `<div class="led-wrap"><table class="led"><thead><tr><th>Tier</th><th>Fact</th><th>Class</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table></div>`,
      drp.length));
  }
  document.getElementById('scroll').innerHTML = out.join('');
}

function sec(title, inner, n) { const c = (n!=null)?`<span class="n">${n}</span>`:''; return `<div class="section"><div class="section-hdr">${esc(title)} ${c}</div>${inner}</div>`; }

function negBadge(nk) {
  const m = {
    absent_or_negative: ['var(--neg)', 'absent'],
    resolved_relevant:  ['#b07400', 'resolved'],
    surveillance:       ['#1a6fb0', 'surveillance'],
  }[nk];
  return m ? ` <span style="color:${m[0]};font-size:12px">(${m[1]})</span>` : '';
}

function ledgerTable(rows, flagged) {
  const head = `<thead><tr><th>Tier</th><th>Fact</th><th>Class</th><th>Cert.</th><th>Lifecycle</th><th>Onset</th><th>Last</th><th>n</th></tr></thead>`;
  const body = rows.map(r => {
    const t = (r.salience_tier||'').toUpperCase();
    const tc = (t==='T1'||t==='T2'||t==='T3') ? t : 'other';
    return `<tr class="${flagged?'flagged':''}">
      <td><span class="tier ${tc}">${esc(r.salience_tier||'?')}</span></td>
      <td>${esc(r.text||'')}${negBadge(r.negative_kind)}</td>
      <td class="led-cls">${esc(r.retention_class||'')}</td>
      <td>${esc(r.certainty||'')}</td>
      <td>${esc(r.current_lifecycle||'')}</td>
      <td class="muted">${r.onset_date?r.onset_date.slice(0,10):'&mdash;'}</td>
      <td class="muted">${r.last_confirmed_date?r.last_confirmed_date.slice(0,10):'&mdash;'}</td>
      <td>${r.n_mentions != null ? r.n_mentions : '&mdash;'}</td></tr>`;
  }).join('');
  return `<div class="led-wrap"><table class="led">${head}<tbody>${body}</tbody></table></div>`;
}

function md(t) {
  const inline = s => esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+?)`/g, '<code>$1</code>');
  const lines = String(t || '').replace(/\r/g, '').split('\n');
  let html = '', inList = false, lt = '';
  const close = () => { if (inList) { html += lt === 'ol' ? '</ol>' : '</ul>'; inList = false; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { close(); continue; }
    if (/^---+$/.test(line)) { close(); html += '<hr>'; continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { close(); const lv = Math.min(m[1].length, 3); html += `<h${lv}>${inline(m[2])}</h${lv}>`; continue; }
    if ((m = line.match(/^[-*]\s+(.*)$/))) { if (!inList || lt !== 'ul') { close(); html += '<ul>'; inList = true; lt = 'ul'; } html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = line.match(/^\d+\.\s+(.*)$/))) { if (!inList || lt !== 'ol') { close(); html += '<ol>'; inList = true; lt = 'ol'; } html += `<li>${inline(m[1])}</li>`; continue; }
    close(); html += `<p>${inline(line)}</p>`;
  }
  close();
  return html;
}
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Export a read-only review HTML for a cohort.")
    ap.add_argument("--out", type=Path, default=None, help=f"output HTML (default: {DEFAULT_OUT})")
    ap.add_argument("--db", default=None, help="override PGDATABASE for this run")
    ap.add_argument("--subjects", type=int, nargs="*", default=None, help="restrict to these ids")
    ap.add_argument("--episode-order", choices=["desc", "asc"], default="desc",
                    help="Episode Paragraphs order: desc = newest first (default), "
                         "asc = oldest first (the original chronological reading order)")
    args = ap.parse_args()

    conn, db_name = connect(args.db)
    try:
        patients = fetch_all(conn, args.subjects, args.episode_order)
    finally:
        conn.close()

    if not patients:
        print(f"No rows in tiered_view for db '{db_name}'. "
              "Has database/d3_load_tiers.py run against this database?")
        return 1

    out = args.out or DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(patients, db_name, config.RUN_NAMESPACE, args.episode_order),
                   encoding="utf-8")

    n_epi = sum(len(p["episodes"]) for p in patients)
    n_led = sum(len(p["ledger"]) + len(p["flagged"]) for p in patients)
    n_drop = sum(len(p["dropped"]) for p in patients)
    n_inc = sum(1 for p in patients if (p.get("incoming_summary_md") or "").strip())
    print(f"namespace     : {config.RUN_NAMESPACE or '(default)'}")
    print(f"db            : {db_name}")
    print(f"patients      : {len(patients)}")
    print(f"episodes      : {n_epi}")
    print(f"ledger nodes  : {n_led}")
    print(f"dropped facts : {n_drop}")
    print(f"with incoming : {n_inc}")
    print(f"episode order : {args.episode_order} ({'newest first' if args.episode_order == 'desc' else 'oldest first'})")
    print(f"-> {out}")
    print("Open the file in a browser (no server needed). Contains patient text -- keep it "
          "inside the DUA-covered data tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
