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
build_review_ui.py  (scratch helper - NOT part of the pipeline)
================================================================
Bake a SELF-CONTAINED, deterministic HTML tool for eyeballing each loss_audit REVIEW
item against that patient's full retention ledger. Writes one HTML for your cohort that 
you open in a browser.

  data_<ns>/interim/loss_audit.md           -> REVIEW items (both sections)
  data_<ns>/output/retention_ledger.jsonl   -> per-subject ledger (raw backup if consolidated)
  data_<ns>/output/review_tools/loss_review.html  <- self-contained UI (data baked in)

Paths follow the RUNNING namespace via config (LONGSUM_DATA_ROOT + LONGSUM_NAMESPACE),
exactly like every pipeline stage - no cohort is hardcoded. The baked HTML carries
patient text, so it is written under the data root, not into the repo.

Re-run after any rebuild to refresh. In the UI: pick an item, eyeball the subject's whole
ledger (grouped, searchable, entries sharing words with the item are highlighted), mark
each item covered / needs-BHC (saved in your browser via localStorage), export to CSV.

    cd ~/clin_longsum/src/clinical_review_tools/b3_loss_audit_tool
    python build_review_ui.py
    python build_review_ui.py --ledger raw
    python build_review_ui.py --out ./loss_review.html   # beside the script instead
"""

import argparse
import json
import re
import sys
from pathlib import Path

# config lives in src/ (this tool is at src/clinical_review_tools/b3_loss_audit_tool/).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config

# All paths follow the running namespace. AUDIT_MD matches b3_loss_audit.py's OUT.
AUDIT_MD = config.INTERIM_DIR / "loss_audit.md"
SERVED = config.OUTPUT_DIR / "retention_ledger.jsonl"
RAW = config.OUTPUT_DIR / "retention_ledger.raw.jsonl"
OUT_HTML = config.OUTPUT_DIR / "review_tools" / "loss_review.html"

ITEM_RE = re.compile(r"^- \[([^\]]+)\] \(subj (\d+), hadm (\S+?)\) (.+)$")


def _read_jsonl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def parse_review_items(md_path: Path):
    lines = md_path.read_text(encoding="utf-8").splitlines()
    items, section, i = [], None, 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("## "):
            if "Must-retain" in ln:
                section = "must_retain"
            elif "Potential losses" in ln:
                section = "loss"
        m = ITEM_RE.match(ln)
        if m:
            bracket, subj, hadm, text = m.groups()
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            verdict = lines[j].strip()[2:].strip() if (j < len(lines)
                       and lines[j].strip().startswith("=>")) else ""
            if verdict.startswith("REVIEW"):
                parts = [p.strip() for p in bracket.split("/")]
                if len(parts) == 5:
                    tier, klass, cert, life, nk = parts[0], parts[1], parts[2], parts[3], parts[4].replace("nk=", "")
                elif len(parts) == 3:
                    tier, (klass, cert, life), nk = "?", parts, "-"
                else:
                    tier, klass, cert, life, nk = "?", (parts[0] if parts else "?"), "?", "?", "-"
                items.append({"section": section or "loss", "tier": tier, "klass": klass,
                              "certainty": cert, "lifecycle": life, "nk": nk,
                              "subject_id": int(subj), "hadm_id": hadm, "text": text.strip()})
            i = max(j, i + 1)
            continue
        i += 1
    return items


def load_ledgers(which: str, subjects: set[int]):
    served = _read_jsonl(SERVED)
    src = SERVED
    if which == "raw":
        src = RAW
    elif which == "auto" and served and served[0].get("consolidated") and RAW.exists():
        src = RAW
    out = {}
    for led in _read_jsonl(src):
        sid = int(led["subject_id"])
        if sid not in subjects:
            continue
        entries = []
        for home in ("nodes", "flagged", "amended"):
            for e in led.get(home, []) or []:
                entries.append({"bucket": home, "klass": e.get("retention_class", ""),
                                "tier": e.get("salience_tier", ""),
                                "life": e.get("current_lifecycle", ""),
                                "nk": e.get("negative_kind"), "n": e.get("n_mentions"),
                                "hadms": e.get("source_hadm_ids") or [], "text": e.get("text", "")})
        for ep in led.get("episodes", []) or []:
            entries.append({"bucket": "episode", "klass": "(episode)", "tier": "", "life": "",
                            "nk": None, "n": ep.get("count"), "hadms": ep.get("source_hadm_ids") or [],
                            "text": ep.get("parent_condition", "")})
        for t in led.get("considered_excluded", []) or []:
            entries.append({"bucket": "excluded", "klass": "(excluded)", "tier": "", "life": "",
                            "nk": None, "n": None, "hadms": [], "text": t})
        out[sid] = {"n_admissions": led.get("n_admissions"),
                    "consolidated": led.get("consolidated", False), "entries": entries}
    return out, src.name


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loss-audit REVIEW triage</title>
<style>
  :root{--bg:#f7f8fa;--panel:#fff;--ink:#1a1f29;--muted:#6b7280;--line:#e3e6eb;
        --accent:#1a6fb0;--cov:#1a7d4b;--bhc:#b4451f;--unset:#9aa3af;--hl:#fff3bf;--match:#e7f1fb}
  *{box-sizing:border-box} html,body{margin:0;height:100%}
  body{font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
  code,.mono{font-family:ui-monospace,Consolas,monospace}
  header{padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--line);
         display:flex;gap:14px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}
  header h1{font-size:15px;margin:0 10px 0 0;font-weight:650}
  header .sp{flex:1}
  .pill{padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600}
  .pill.cov{background:#e3f4ea;color:var(--cov)} .pill.bhc{background:#fbe8e1;color:var(--bhc)}
  .pill.unset{background:#eef0f3;color:var(--unset)}
  select,input[type=search],button{font:13px inherit;padding:5px 8px;border:1px solid var(--line);
         border-radius:6px;background:#fff;color:var(--ink)}
  button{cursor:pointer} button:hover{background:#f0f2f5}
  main{display:grid;grid-template-columns:minmax(320px,420px) 1fr;height:calc(100% - 49px)}
  #list{overflow:auto;border-right:1px solid var(--line);background:var(--panel)}
  .row{padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer}
  .row:hover{background:#f3f6fa} .row.sel{background:var(--match);border-left:3px solid var(--accent);padding-left:9px}
  .row .t{font-weight:600} .row .meta{color:var(--muted);font-size:12px;margin-top:2px}
  .row .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .dot.cov{background:var(--cov)} .dot.bhc{background:var(--bhc)} .dot.unset{background:var(--unset)}
  #detail{overflow:auto;padding:16px 20px}
  .item{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--accent);
        border-radius:8px;padding:12px 14px;margin-bottom:14px}
  .item .txt{font-size:17px;font-weight:650;margin:2px 0 8px}
  .tags span{display:inline-block;background:#eef2f6;color:#374151;border-radius:5px;
        padding:1px 7px;margin:2px 4px 2px 0;font-size:12px}
  .acts{margin-top:10px;display:flex;gap:8px;align-items:center}
  .acts button.on.cov{background:var(--cov);color:#fff;border-color:var(--cov)}
  .acts button.on.bhc{background:var(--bhc);color:#fff;border-color:var(--bhc)}
  .lhead{display:flex;align-items:center;gap:10px;margin:6px 0 8px}
  .lhead h2{font-size:14px;margin:0} .lhead .sub{color:var(--muted);font-size:12px}
  .grp{margin:0 0 10px} .grp h3{font-size:12px;color:var(--muted);text-transform:uppercase;
        letter-spacing:.04em;margin:10px 0 4px;border-bottom:1px solid var(--line);padding-bottom:2px}
  .e{padding:5px 8px;border:1px solid var(--line);border-radius:6px;margin:4px 0;background:#fff}
  .e.match{background:var(--match);border-color:#bcd8f0}
  .e .et{font-weight:560} .e .em{color:var(--muted);font-size:12px;margin-top:1px}
  .e .nk{font-weight:600} .nk-resolved_relevant{color:#b07400} .nk-absent_or_negative{color:#b4451f}
  .nk-surveillance{color:#1a6fb0}
  mark{background:var(--hl);padding:0 1px;border-radius:2px}
  .empty{color:var(--muted);font-style:italic;padding:20px}
  kbd{font:11px ui-monospace,monospace;background:#eef0f3;border:1px solid var(--line);
        border-bottom-width:2px;border-radius:4px;padding:0 5px}
</style></head>
<body>
<header>
  <h1>Loss-audit REVIEW triage</h1>
  <span id="counts" class="mono"></span>
  <span class="sp"></span>
  <label>section <select id="fSection"><option value="">all</option>
    <option value="loss">potential losses</option>
    <option value="must_retain">must-retain unpinned</option></select></label>
  <label><input type="checkbox" id="fUndecided"> undecided only</label>
  <input type="search" id="fText" placeholder="filter items...">
  <button id="export">Export CSV</button>
</header>
<main>
  <div id="list"></div>
  <div id="detail"><div class="empty">Select an item on the left. Keys: <kbd>j</kbd>/<kbd>k</kbd> move,
     <kbd>c</kbd> covered, <kbd>b</kbd> needs-BHC, <kbd>u</kbd> unset.</div></div>
</main>
<script>
const DATA = /*__DATA__*/;
const STOP = new Set("the of and with due to secondary acute chronic history on for from status disease syndrome episode recurrent left right bilateral severe mild moderate new old known patient reported suspected probable possible likely related associated requiring presenting".split(" "));
const KEY = "lossReviewStatus.v1";
let status = JSON.parse(localStorage.getItem(KEY) || "{}");
let sel = -1;

const idOf = it => `${it.subject_id}|${it.hadm_id}|${it.text}`;
const esc = s => (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const toks = s => new Set((s||"").toLowerCase().match(/[a-z0-9]+/g)?.filter(t=>t.length>=3&&!STOP.has(t))||[]);
function hl(s, terms){ let o=esc(s); for(const t of terms){ if(!t)continue;
  o=o.replace(new RegExp("("+t.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","ig"),"<mark>$1</mark>"); } return o; }

function filtered(){
  const sec=document.getElementById("fSection").value;
  const und=document.getElementById("fUndecided").checked;
  const q=document.getElementById("fText").value.toLowerCase();
  return DATA.items.map((it,i)=>({it,i})).filter(({it})=>{
    if(sec && it.section!==sec) return false;
    if(und && (status[idOf(it)]||"unset")!=="unset") return false;
    if(q && !(it.text.toLowerCase().includes(q) || String(it.subject_id).includes(q))) return false;
    return true; });
}
function counts(){
  let c=0,b=0,u=0; for(const it of DATA.items){const s=status[idOf(it)]||"unset";
    if(s==="cov")c++; else if(s==="bhc")b++; else u++;}
  document.getElementById("counts").innerHTML =
    `${DATA.items.length} REVIEW · <span class="pill bhc">${b} needs-BHC</span> `+
    `<span class="pill cov">${c} covered</span> <span class="pill unset">${u} undecided</span>`;
}
function renderList(){
  const L=document.getElementById("list"); const rows=filtered();
  L.innerHTML = rows.map(({it,i})=>{
    const s=status[idOf(it)]||"unset";
    return `<div class="row ${i===sel?'sel':''}" data-i="${i}">
      <div class="t"><span class="dot ${s}"></span>${esc(it.text)}</div>
      <div class="meta mono">subj ${it.subject_id} · hadm ${it.hadm_id} · ${it.tier}/${it.klass}</div></div>`;
  }).join("") || `<div class="empty">no items match the filter</div>`;
  [...L.querySelectorAll(".row")].forEach(r=>r.onclick=()=>{sel=+r.dataset.i; renderList(); renderDetail();});
  counts();
}
function setStatus(s){ if(sel<0)return; const it=DATA.items[sel];
  const k=idOf(it); if((status[k]||"unset")===s) delete status[k]; else status[k]=s;
  localStorage.setItem(KEY, JSON.stringify(status)); renderList(); renderDetail(); }

function renderDetail(){
  const D=document.getElementById("detail"); if(sel<0){return;}
  const it=DATA.items[sel]; const led=DATA.ledgers[it.subject_id]||{entries:[]};
  const it_tok=toks(it.text); const s=status[idOf(it)]||"unset";
  const order={nodes:0,flagged:1,amended:2,episode:3,excluded:4};
  const ents=[...led.entries].map(e=>({...e, shared:[...toks(e.text)].filter(t=>it_tok.has(t))}))
    .sort((a,b)=> (b.shared.length-a.shared.length) || (order[a.bucket]-order[b.bucket]));
  const groups={};
  for(const e of ents){ (groups[e.bucket]=groups[e.bucket]||[]).push(e); }
  const buckets=["nodes","flagged","amended","episode","excluded"];
  let body = buckets.filter(b=>groups[b]).map(b=>{
    const rows=groups[b].map(e=>{
      const nk=e.nk?` <span class="nk nk-${e.nk}">[${e.nk}]</span>`:"";
      const m=e.shared.length?" match":"";
      const meta=[e.klass,e.tier,e.life].filter(Boolean).join(" · ");
      const hd=e.hadms&&e.hadms.length?` · hadms ${e.hadms.join(",")}`:"";
      const nn=(e.n!=null)?` · n=${e.n}`:"";
      return `<div class="e${m}"><div class="et">${hl(e.text,[])}${nk}</div>
        <div class="em mono">${esc(meta)}${nn}${hd}</div></div>`;
    }).join("");
    return `<div class="grp"><h3>${b} (${groups[b].length})</h3>${rows}</div>`;
  }).join("") || `<div class="empty">no ledger entries for this subject</div>`;

  D.innerHTML = `
    <div class="item">
      <div class="txt">${esc(it.text)}</div>
      <div class="tags"><span>subj ${it.subject_id}</span><span>hadm ${it.hadm_id}</span>
        <span>${it.section==='must_retain'?'must-retain unpinned':'potential loss'}</span>
        <span>${it.tier}/${it.klass}</span><span>${it.certainty}/${it.lifecycle}</span>
        <span>nk=${it.nk}</span></div>
      <div class="acts">
        <button class="cov ${s==='cov'?'on':''}" data-s="cov">covered (skip BHC)</button>
        <button class="bhc ${s==='bhc'?'on':''}" data-s="bhc">needs BHC</button>
        <button data-s="unset">clear</button>
        <span class="sp" style="flex:1"></span>
        <input type="search" id="lsearch" placeholder="highlight in ledger (e.g. depress)">
      </div>
    </div>
    <div class="lhead"><h2>Ledger</h2><span class="sub mono">subject ${it.subject_id} ·
      ${led.entries.length} entries · ${led.n_admissions||'?'} admissions ·
      ${led.consolidated?'consolidated':'raw'}</span></div>
    <div id="ledgerBody">${body}</div>`;

  D.querySelectorAll(".acts button[data-s]").forEach(btn=>btn.onclick=()=>setStatus(btn.dataset.s));
  const ls=document.getElementById("lsearch");
  ls.oninput=()=>{ const terms=ls.value.split(/\s+/).filter(Boolean);
    document.querySelectorAll("#ledgerBody .et").forEach((el,k)=>{
      el.innerHTML = hl(ents[k].text,[]).replace(/<mark>|<\/mark>/g,"");  // reset
      el.innerHTML = hl(el.textContent, terms) + (ents[k].nk?` <span class="nk nk-${ents[k].nk}">[${ents[k].nk}]</span>`:""); });
  };
}
function move(d){ const rows=filtered(); if(!rows.length)return;
  let pos=rows.findIndex(r=>r.i===sel); pos=(pos<0?0:pos+d);
  pos=Math.max(0,Math.min(rows.length-1,pos)); sel=rows[pos].i; renderList(); renderDetail();
  document.querySelector(".row.sel")?.scrollIntoView({block:"nearest"}); }

document.addEventListener("keydown",e=>{
  if(/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName))return;
  if(e.key==="j"||e.key==="ArrowDown"){e.preventDefault();move(1);}
  else if(e.key==="k"||e.key==="ArrowUp"){e.preventDefault();move(-1);}
  else if(e.key==="c")setStatus("cov"); else if(e.key==="b")setStatus("bhc");
  else if(e.key==="u")setStatus("unset"); });
["fSection","fUndecided","fText"].forEach(id=>document.getElementById(id)
  .addEventListener(id==="fText"?"input":"change",renderList));
document.getElementById("export").onclick=()=>{
  const head=["needs_bhc_or_status","section","subject_id","hadm_id","tier","klass","certainty","lifecycle","nk","text"];
  const rows=DATA.items.map(it=>[status[idOf(it)]||"unset",it.section,it.subject_id,it.hadm_id,
    it.tier,it.klass,it.certainty,it.lifecycle,it.nk,it.text]);
  const csv=[head,...rows].map(r=>r.map(x=>`"${String(x).replace(/"/g,'""')}"`).join(",")).join("\n");
  const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  a.download="loss_review_decisions.csv"; a.click(); };

renderList();
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", choices=["auto", "served", "raw"], default="auto")
    ap.add_argument("--out", type=Path, default=OUT_HTML,
                    help=f"output HTML (default: {OUT_HTML})")
    args = ap.parse_args()

    print(f"namespace: {config.RUN_NAMESPACE or '(default)'}  |  data: {config.DATA_DIR}")
    if not AUDIT_MD.exists():
        raise SystemExit(f"{AUDIT_MD} not found -- run b3_loss_audit.py under this namespace first.")

    items = parse_review_items(AUDIT_MD)
    subjects = {it["subject_id"] for it in items}
    ledgers, src = load_ledgers(args.ledger, subjects)
    data = {"items": items, "ledgers": {str(k): v for k, v in ledgers.items()}}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(HTML.replace("/*__DATA__*/", blob), encoding="utf-8")

    n_cov = sum(1 for s in ledgers.values())
    print(f"wrote {args.out}")
    print(f"  REVIEW items: {len(items)}   subjects: {len(subjects)}   "
          f"ledgers embedded: {n_cov}   (source: {src})")
    print(f"  open {args.out.name} in a browser (double-click).")


if __name__ == "__main__":
    main()
