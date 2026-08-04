"""Build a self-contained guided review deck for a set of barndsl designs.

    python -m tools.design_review FILE.barn [FILE2.barn ...] --out review.html

Each design is compiled and rendered; the page keeps one large floor plan visible
while guiding the reviewer through buildability, rating, design notes, every
diagnostic, and new-rule questions. Answers autosave locally. The **Export
feedback** button serialises everything to JSON (copy or download) in a stable
shape that's easy to read back when refining the rules.

The page is a single static HTML file — no network, no build step. Open it in any
browser, review, export, and paste the JSON back.
"""

from __future__ import annotations

import argparse
import json
import os

from barndsl import compile_source, render_svg


def design_data(path: str) -> dict:
    """Compile + render one .barn file into the data the page needs."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    result = compile_source(src, name=os.path.basename(path))
    diagnostics = [
        {
            "severity": d.severity.value,
            "code": d.code,
            "message": d.message,
            "hint": d.hint or "",
            "room": d.room or "",
            "line": d.line,
        }
        for d in sorted(result.diagnostics, key=lambda i: (i.line or 0, i.col or 0))
    ]
    return {
        "id": os.path.splitext(os.path.basename(path))[0],
        "title": result.plan.name if result.plan is not None else os.path.basename(path),
        "ok": result.ok,
        "summary": {
            "errors": len(result.errors),
            "warnings": len(result.warnings),
            "infos": len(result.infos),
        },
        "source": src,
        "svg": render_svg(result.plan) if result.plan is not None else "",
        "diagnostics": diagnostics,
    }


def build_html(designs: list[dict]) -> str:
    # Embed as JSON in a <script type="application/json">. Escape '<' so a stray
    # '</script>' in any source can't close the tag early.
    data = json.dumps(designs).replace("<", "\\u003c")
    return _TEMPLATE.replace("/*__DATA__*/", data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help="one or more .barn design files")
    ap.add_argument("--out", default="review.html", help="output HTML path")
    args = ap.parse_args(argv)
    designs = [design_data(p) for p in args.files]
    html = build_html(designs)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    n = len(designs)
    print(f"Wrote {args.out} — {n} design(s).")
    return 0


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>barndsl — guided design review</title>
<style>
  :root {
    --err:#b42318; --warn:#b54708; --info:#175cd3; --ok:#067647;
    --accent:#6941c6; --accent-soft:#f4f0ff; --bg:#f2f4f7; --card:#fff;
    --line:#d0d5dd; --ink:#101828; --mut:#667085; --shadow:0 12px 32px rgba(16,24,40,.10);
  }
  * { box-sizing:border-box; }
  html, body { height:100%; }
  body { margin:0; overflow:hidden; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:var(--ink); background:var(--bg); }
  button, select, textarea { font:inherit; }
  button, .btn { cursor:pointer; border:1px solid var(--line); color:var(--ink); background:#fff; border-radius:9px; padding:9px 13px; font-weight:600; }
  button:hover:not(:disabled), .btn:hover { border-color:#98a2b3; background:#f9fafb; }
  button:focus-visible, select:focus-visible, textarea:focus-visible, .choice:focus-within { outline:3px solid rgba(105,65,198,.24); outline-offset:2px; }
  button:disabled { cursor:not-allowed; opacity:.42; }
  .primary { color:#fff; background:var(--accent); border-color:var(--accent); }
  .primary:hover:not(:disabled) { color:#fff; background:#53389e; border-color:#53389e; }
  .topbar { min-height:72px; padding:11px 18px; display:grid; grid-template-columns:minmax(190px,auto) minmax(280px,1fr) auto; align-items:center; gap:18px; background:#fff; border-bottom:1px solid var(--line); }
  .brand h1 { margin:0; font-size:17px; line-height:1.2; }
  .brand p { margin:2px 0 0; color:var(--mut); font-size:12px; }
  .review-position { min-width:0; display:flex; align-items:center; justify-content:center; gap:12px; }
  .review-position select { min-width:210px; max-width:360px; padding:8px 32px 8px 10px; border:1px solid var(--line); border-radius:9px; background:#fff; color:var(--ink); }
  .position-copy { color:var(--mut); font-size:13px; white-space:nowrap; }
  .top-actions { display:flex; align-items:center; justify-content:flex-end; gap:8px; }
  #save-status { min-width:78px; color:var(--ok); font-size:12px; text-align:right; }
  .progress-track { height:4px; background:#eaecf0; }
  .progress-bar { width:0; height:100%; background:linear-gradient(90deg,#7f56d9,#9e77ed); transition:width .22s ease; }
  #app { height:calc(100% - 76px); min-height:0; padding:14px; }
  .review-shell { height:100%; min-height:0; display:grid; grid-template-columns:minmax(0,2.15fr) minmax(340px,.85fr); gap:14px; }
  .plan-pane, .question-pane { min-height:0; background:var(--card); border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); overflow:hidden; }
  .plan-pane { display:grid; grid-template-rows:auto minmax(0,1fr) auto; }
  .plan-head { padding:12px 14px; display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--line); }
  .plan-head h2 { min-width:0; margin:0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:17px; }
  .pill { display:inline-flex; align-items:center; border-radius:999px; padding:3px 9px; color:#fff; font-size:11px; font-weight:700; white-space:nowrap; }
  .pill.e, .pill.error { background:var(--err); }
  .pill.w, .pill.warning { background:var(--warn); }
  .pill.i, .pill.info { background:var(--info); }
  .pill.ok { background:var(--ok); }
  .plan-viewport { min-height:0; overflow:auto; position:relative; display:flex; align-items:flex-start; justify-content:flex-start; padding:12px; background-color:#fcfcfd; background-image:linear-gradient(#f2f4f7 1px,transparent 1px),linear-gradient(90deg,#f2f4f7 1px,transparent 1px); background-size:24px 24px; cursor:default; }
  .plan-viewport.can-pan { cursor:grab; }
  .plan-viewport.is-panning { cursor:grabbing; user-select:none; }
  .plan-canvas { flex:none; margin:auto; display:flex; align-items:center; justify-content:center; transition:width .16s ease; }
  .plan-canvas.is-fit { width:100%; height:100%; }
  .plan-canvas.is-fit svg { width:100% !important; height:100% !important; max-width:100%; max-height:100%; }
  .plan-canvas.is-zoomed { min-height:100%; }
  .plan-canvas.is-zoomed svg { display:block; width:100% !important; height:auto !important; max-width:none; max-height:none; }
  .plan-canvas svg [data-room] { transition:filter .18s ease,stroke .18s ease,stroke-width .18s ease; }
  .plan-canvas svg [data-room].review-focus { stroke:var(--accent) !important; stroke-width:5px !important; filter:drop-shadow(0 0 5px rgba(105,65,198,.72)); }
  .empty-plan { margin:auto; color:var(--mut); text-align:center; }
  .plan-tools { min-height:50px; padding:8px 12px; display:flex; align-items:center; gap:7px; border-top:1px solid var(--line); background:#fff; }
  .plan-tools button { min-width:39px; padding:7px 10px; }
  .zoom-value { min-width:48px; color:var(--mut); font-size:12px; text-align:center; }
  .focus-room { min-width:0; margin-left:auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--accent); font-size:12px; font-weight:700; }
  .question-pane { display:grid; grid-template-rows:auto minmax(0,1fr) auto; }
  .question-head { padding:14px 17px; border-bottom:1px solid var(--line); background:#fcfcfd; }
  .eyebrow { color:var(--accent); font-size:11px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
  .question-head h2 { margin:4px 0 2px; font-size:21px; line-height:1.25; }
  .question-head p { margin:0; color:var(--mut); font-size:13px; }
  .question-body { min-height:0; overflow:auto; padding:18px; }
  .question-body > :first-child { margin-top:0; }
  .lead { color:#344054; font-size:16px; }
  .overview-stats, .completion-stats { margin-top:20px; display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
  .stat { padding:12px 8px; border:1px solid var(--line); border-radius:10px; background:#fcfcfd; text-align:center; }
  .stat strong { display:block; font-size:22px; line-height:1.1; }
  .stat span { color:var(--mut); font-size:11px; text-transform:uppercase; }
  .choices { margin-top:18px; display:grid; gap:10px; }
  .choices.columns-3 { grid-template-columns:repeat(3,1fr); }
  .choices.columns-5 { grid-template-columns:repeat(5,1fr); }
  .choice { min-height:62px; position:relative; display:flex; align-items:center; justify-content:center; gap:7px; padding:12px; border:1px solid var(--line); border-radius:11px; background:#fff; color:#344054; cursor:pointer; text-align:center; font-weight:700; }
  .choice:hover { border-color:#98a2b3; background:#f9fafb; }
  .choice.selected { color:#53389e; border-color:#9e77ed; background:var(--accent-soft); box-shadow:0 0 0 1px #9e77ed inset; }
  .choice input { position:absolute; opacity:0; pointer-events:none; }
  .rating-choice { min-height:74px; flex-direction:column; gap:1px; font-size:20px; }
  .rating-choice small { color:var(--mut); font-size:10px; font-weight:500; }
  .field-label { display:block; margin:18px 0 7px; font-size:13px; font-weight:700; }
  textarea { width:100%; min-height:132px; padding:11px 12px; resize:vertical; border:1px solid var(--line); border-radius:10px; color:var(--ink); background:#fff; }
  .optional { color:var(--mut); font-size:11px; font-weight:500; }
  .diag-card { padding:15px; border:1px solid var(--line); border-radius:12px; background:#fcfcfd; }
  .diag-meta { display:flex; align-items:center; flex-wrap:wrap; gap:7px; }
  code.code { padding:3px 7px; border-radius:6px; background:#eaecf0; font-size:12px; }
  .location { color:var(--mut); font-size:12px; }
  .diag-message { margin:13px 0 0; font-size:16px; line-height:1.45; }
  .hint { margin-top:9px; padding-top:9px; border-top:1px solid #eaecf0; color:var(--mut); font-size:12px; }
  .clean { margin:18px 0; padding:13px; border:1px solid #abefc6; border-radius:10px; color:var(--ok); background:#ecfdf3; }
  .answer-status { margin-top:16px; color:var(--mut); font-size:12px; }
  .answer-status strong { color:var(--ink); }
  .question-foot { padding:11px 14px; display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:10px; border-top:1px solid var(--line); background:#fcfcfd; }
  .question-foot .middle { color:var(--mut); font-size:12px; text-align:center; }
  .shortcut { margin-left:6px; color:#98a2b3; }
  dialog { width:min(800px,94vw); max-height:88vh; padding:0; border:none; border-radius:14px; box-shadow:0 24px 64px rgba(16,24,40,.28); }
  dialog::backdrop { background:rgba(16,24,40,.48); }
  .dhead { padding:14px 17px; display:flex; align-items:center; gap:10px; border-bottom:1px solid var(--line); font-weight:700; }
  .dhead span { flex:1; }
  .dbody { padding:16px; overflow:auto; }
  .dbody textarea { height:48vh; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .source-code { max-height:62vh; margin:0; overflow:auto; padding:15px; border-radius:10px; color:#d6deeb; background:#0d1117; font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; white-space:pre; }
  .dfoot { padding:0 16px 16px; display:flex; justify-content:flex-end; gap:9px; }
  .btn { display:inline-flex; align-items:center; text-decoration:none; }
  @media (max-width:900px) {
    body { height:auto; min-height:100%; overflow:auto; }
    .topbar { grid-template-columns:1fr auto; }
    .review-position { grid-column:1/-1; grid-row:2; justify-content:flex-start; }
    #app { height:auto; padding:10px; }
    .review-shell { min-height:1100px; grid-template-columns:1fr; grid-template-rows:minmax(520px,58vh) minmax(560px,auto); }
    .plan-pane, .question-pane { min-height:0; }
  }
  @media (max-width:560px) {
    .topbar { display:flex; flex-wrap:wrap; gap:9px; }
    .brand { flex:1 1 180px; }
    .review-position { order:3; width:100%; }
    .review-position select { min-width:0; flex:1; }
    .position-copy { display:none; }
    #save-status, .shortcut { display:none; }
    .top-actions button { padding:8px 10px; }
    .review-shell { min-height:1050px; grid-template-rows:440px minmax(570px,auto); }
    .plan-head { flex-wrap:wrap; }
    .choices.columns-3 { grid-template-columns:1fr; }
    .choices.columns-5 { grid-template-columns:repeat(5,1fr); gap:5px; }
    .rating-choice { min-height:62px; padding:7px; }
    .rating-choice small { display:none; }
    .question-foot { grid-template-columns:1fr 1fr; }
    .question-foot .middle { grid-column:1/-1; grid-row:1; }
  }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><h1>barndsl design review</h1><p>Guided layout evaluation</p></div>
  <div class="review-position">
    <select id="design-picker" aria-label="Choose a design"></select>
    <span class="position-copy" id="position-copy"></span>
  </div>
  <div class="top-actions">
    <span id="save-status" aria-live="polite">Saved locally</span>
    <button id="source-open">View DSL</button>
    <button id="export" class="primary">Export feedback</button>
  </div>
</header>
<div class="progress-track" aria-hidden="true"><div class="progress-bar" id="progress-bar"></div></div>
<main id="app"></main>

<dialog id="export-dialog">
  <div class="dhead"><span>Feedback JSON — copy this back into the chat</span><button data-close="export-dialog" aria-label="Close">×</button></div>
  <div class="dbody"><textarea id="out" readonly></textarea></div>
  <div class="dfoot"><button id="copy">Copy to clipboard</button><a class="btn" id="download">Download .json</a><button data-close="export-dialog">Close</button></div>
</dialog>

<dialog id="source-dialog">
  <div class="dhead"><span id="source-title">DSL source</span><button data-close="source-dialog" aria-label="Close">×</button></div>
  <div class="dbody"><pre class="source-code" id="source-code"></pre></div>
  <div class="dfoot"><button data-close="source-dialog">Close</button></div>
</dialog>

<script id="data" type="application/json">/*__DATA__*/</script>
<script>
const designs = JSON.parse(document.getElementById('data').textContent);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const defaults = designs.map(d => ({
  id:d.id, title:d.title, summary:d.summary,
  rating:'', buildable:'', notes:'', ruleIdeas:'',
  diagnostics:d.diagnostics.map(x => ({severity:x.severity, code:x.code, message:x.message, verdict:'', note:''}))
}));
const storageKey = `barndsl-design-review:v1:${designs.map(d => d.id).join('|')}`;
let state = defaults;
let designIndex = 0;
let stepIndex = 0;
const zooms = designs.map(() => 1);
const app = document.getElementById('app');

function hydrate(saved) {
  if (!saved || !Array.isArray(saved.designs)) return;
  state = defaults.map(base => {
    const prior = saved.designs.find(item => item && item.id === base.id);
    if (!prior) return base;
    return {
      ...base,
      rating:String(prior.rating || ''), buildable:String(prior.buildable || ''),
      notes:String(prior.notes || ''), ruleIdeas:String(prior.ruleIdeas || ''),
      diagnostics:base.diagnostics.map((diag, j) => ({
        ...diag,
        verdict:String(prior.diagnostics?.[j]?.verdict || ''),
        note:String(prior.diagnostics?.[j]?.note || '')
      }))
    };
  });
  designIndex = Math.max(0, Math.min(designs.length - 1, Number(saved.designIndex) || 0));
  stepIndex = Math.max(0, Number(saved.stepIndex) || 0);
}

try { hydrate(JSON.parse(localStorage.getItem(storageKey))); } catch (_) {}

function persist() {
  const status = document.getElementById('save-status');
  try {
    localStorage.setItem(storageKey, JSON.stringify({version:1, designs:state, designIndex, stepIndex}));
    status.textContent = 'Saved locally'; status.style.color = 'var(--ok)';
  } catch (_) {
    status.textContent = 'Autosave unavailable'; status.style.color = 'var(--warn)';
  }
}

function stepsFor(d) {
  return [
    {kind:'overview', eyebrow:'First impression', title:'Take a clear look at the layout'},
    {kind:'buildable', eyebrow:'Overall assessment', title:'Does this layout feel buildable?'},
    {kind:'rating', eyebrow:'Overall assessment', title:'How would you rate this design?'},
    {kind:'notes', eyebrow:'Design critique', title:'What works well, and what would you change?'},
    ...d.diagnostics.map((_, diagIndex) => ({kind:'diagnostic', diagIndex, eyebrow:`Diagnostic ${diagIndex + 1} of ${d.diagnostics.length}`, title:'Is this diagnostic correct?'})),
    {kind:'ruleIdeas', eyebrow:'Rule discovery', title:'What new design rules does this suggest?'},
    {kind:'complete', eyebrow:'Design complete', title:'Review this design before moving on'}
  ];
}

function requiredStatus(s) {
  const total = 2 + s.diagnostics.length;
  const answered = Number(Boolean(s.buildable)) + Number(Boolean(s.rating)) + s.diagnostics.filter(x => x.verdict).length;
  return {answered, total, missing:total - answered};
}

function firstUnansweredStep(s) {
  if (!s.buildable) return 1;
  if (!s.rating) return 2;
  const missingDiag = s.diagnostics.findIndex(x => !x.verdict);
  return missingDiag < 0 ? null : 4 + missingDiag;
}

function pills(summary) {
  return [
    summary.errors ? `<span class="pill e">${summary.errors} err</span>` : '',
    summary.warnings ? `<span class="pill w">${summary.warnings} warn</span>` : '',
    summary.infos ? `<span class="pill i">${summary.infos} info</span>` : '',
    (!summary.errors && !summary.warnings && !summary.infos) ? '<span class="pill ok">0/0/0</span>' : ''
  ].join('');
}

function choice(name, value, label, selected, extra='') {
  return `<label class="choice ${selected===value?'selected':''} ${extra}"><input type="radio" name="${name}" value="${value}" ${selected===value?'checked':''}>${label}</label>`;
}

function questionContent(d, s, step) {
  if (step.kind === 'overview') {
    return `<p class="lead">Inspect the plan before reading individual diagnostics. Check circulation, room proportions, privacy, daylight, and whether the overall arrangement feels coherent.</p>
      ${d.diagnostics.length ? `<p class="answer-status">The compiler found <strong>${d.diagnostics.length}</strong> item${d.diagnostics.length===1?'':'s'} to review after your first impression.</p>` : '<div class="clean">✓ This design has no compiler diagnostics.</div>'}
      <div class="overview-stats"><div class="stat"><strong>${d.summary.errors}</strong><span>Errors</span></div><div class="stat"><strong>${d.summary.warnings}</strong><span>Warnings</span></div><div class="stat"><strong>${d.summary.infos}</strong><span>Infos</span></div></div>`;
  }
  if (step.kind === 'buildable') {
    return `<p class="lead">Imagine handing this plan to a builder. Is the overall arrangement plausible without a major redesign?</p>
      <div class="choices columns-3" data-choice-group="buildable">
        ${choice('buildable','yes','✓ Yes',s.buildable)}
        ${choice('buildable','maybe','? Maybe',s.buildable)}
        ${choice('buildable','no','× No',s.buildable)}
      </div>`;
  }
  if (step.kind === 'rating') {
    const labels = ['Poor','Weak','Usable','Good','Excellent'];
    return `<p class="lead">Rate the complete layout, not just its compliance score.</p><div class="choices columns-5" data-choice-group="rating">${labels.map((label,i) => choice('rating',String(i+1),`${i+1}<small>${label}</small>`,s.rating,'rating-choice')).join('')}</div>`;
  }
  if (step.kind === 'notes') {
    return `<p class="lead">Capture your architectural judgment before reviewing compiler suggestions in detail.</p><label class="field-label" for="design-notes">Design notes <span class="optional">optional</span></label><textarea id="design-notes" data-field="notes" placeholder="What is strong? What feels awkward? What would you move, resize, connect, or separate?">${esc(s.notes)}</textarea>`;
  }
  if (step.kind === 'diagnostic') {
    const x = d.diagnostics[step.diagIndex];
    const a = s.diagnostics[step.diagIndex];
    const where = [x.room ? `Room: ${esc(x.room)}` : '', x.line ? `Line ${x.line}` : ''].filter(Boolean).join(' · ');
    return `<div class="diag-card"><div class="diag-meta"><span class="pill ${x.severity}">${esc(x.severity)}</span><code class="code">${esc(x.code)}</code>${where?`<span class="location">${where}</span>`:''}</div><p class="diag-message">${esc(x.message)}</p>${x.hint?`<div class="hint"><strong>Compiler hint:</strong> ${esc(x.hint)}</div>`:''}</div>
      <div class="choices columns-3" data-choice-group="diagnostic">
        ${choice(`diagnostic-${step.diagIndex}`,'correct','✓ Correct',a.verdict)}
        ${choice(`diagnostic-${step.diagIndex}`,'false_positive','× False positive',a.verdict)}
        ${choice(`diagnostic-${step.diagIndex}`,'unsure','? Unsure',a.verdict)}
      </div>
      <label class="field-label" for="diagnostic-note">Why? <span class="optional">optional</span></label><textarea id="diagnostic-note" data-field="diagnostic-note" placeholder="Add context that will help refine this rule.">${esc(a.note)}</textarea>`;
  }
  if (step.kind === 'ruleIdeas') {
    return `<p class="lead">Note any design principle or validation rule that this layout exposed.</p><label class="field-label" for="rule-ideas">New rule ideas <span class="optional">optional</span></label><textarea id="rule-ideas" data-field="ruleIdeas" placeholder="Example: rec rooms should be buffered from bedrooms by service space or circulation.">${esc(s.ruleIdeas)}</textarea>`;
  }
  const progress = requiredStatus(s);
  const missing = firstUnansweredStep(s);
  return `<p class="lead">You answered ${progress.answered} of ${progress.total} required questions for this design.</p>
    <div class="completion-stats"><div class="stat"><strong>${progress.answered}</strong><span>Answered</span></div><div class="stat"><strong>${progress.missing}</strong><span>Missing</span></div><div class="stat"><strong>${d.diagnostics.length}</strong><span>Diagnostics</span></div></div>
    ${missing===null?'<div class="clean">✓ Required review questions are complete. Optional notes are saved too.</div>':`<div class="answer-status"><strong>${progress.missing} required answer${progress.missing===1?' is':'s are'} still missing.</strong></div><p><button data-action="jump-unanswered">Go to first unanswered question</button></p>`}`;
}

function render() {
  const d = designs[designIndex];
  const s = state[designIndex];
  const steps = stepsFor(d);
  stepIndex = Math.max(0, Math.min(steps.length - 1, stepIndex));
  const step = steps[stepIndex];
  const zoom = zooms[designIndex];
  const room = step.kind === 'diagnostic' ? d.diagnostics[step.diagIndex].room : '';
  const atStart = designIndex === 0 && stepIndex === 0;
  const atEnd = designIndex === designs.length - 1 && stepIndex === steps.length - 1;
  const nextLabel = stepIndex === steps.length - 1 && designIndex < designs.length - 1 ? 'Next design →' : 'Next →';

  document.getElementById('design-picker').value = String(designIndex);
  document.getElementById('position-copy').textContent = `Design ${designIndex + 1} of ${designs.length} · Step ${stepIndex + 1} of ${steps.length}`;
  const totalSteps = designs.reduce((total, design) => total + stepsFor(design).length, 0);
  const priorSteps = designs.slice(0, designIndex).reduce((total, design) => total + stepsFor(design).length, 0);
  document.getElementById('progress-bar').style.width = `${((priorSteps + stepIndex + 1) / totalSteps) * 100}%`;
  app.innerHTML = `<section class="review-shell">
    <section class="plan-pane" aria-label="Floor plan">
      <div class="plan-head"><h2>${esc(d.title)}</h2>${pills(d.summary)}</div>
      <div class="plan-viewport ${zoom>1?'can-pan':''}" id="plan-viewport">
        <div class="plan-canvas ${zoom===1?'is-fit':'is-zoomed'}" id="plan-canvas" style="width:${zoom*100}%">${d.svg || '<div class="empty-plan">No renderable plan is available.</div>'}</div>
      </div>
      <div class="plan-tools"><button data-action="fit" title="Fit plan">Fit</button><button data-action="zoom-out" aria-label="Zoom out">−</button><span class="zoom-value">${Math.round(zoom*100)}%</span><button data-action="zoom-in" aria-label="Zoom in">+</button><span class="focus-room">${room?`Highlighting ${esc(room)}`:'Drag to pan when zoomed'}</span></div>
    </section>
    <aside class="question-pane" id="question-panel" aria-live="polite">
      <div class="question-head"><div class="eyebrow">${esc(step.eyebrow)}</div><h2>${esc(step.title)}</h2><p>${step.kind==='diagnostic'?'Judge the rule independently of your overall rating.':'Your answers save automatically on this device.'}</p></div>
      <div class="question-body">${questionContent(d,s,step)}</div>
      <div class="question-foot"><button data-action="previous" ${atStart?'disabled':''}>← Back</button><div class="middle">Step ${stepIndex + 1} of ${steps.length}<span class="shortcut">← / → keys</span></div><button class="primary" data-action="next" ${atEnd?'disabled':''}>${nextLabel}</button></div>
    </aside>
  </section>`;
  focusRoom(room);
  bindPan();
  persist();
}

function focusRoom(room) {
  document.querySelectorAll('#plan-canvas [data-room]').forEach(element => {
    element.classList.toggle('review-focus', Boolean(room) && element.getAttribute('data-room') === room);
  });
}

function bindPan() {
  const viewport = document.getElementById('plan-viewport');
  if (!viewport || zooms[designIndex] <= 1) return;
  let dragging = false, startX = 0, startY = 0, scrollLeft = 0, scrollTop = 0;
  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    dragging = true; startX = event.clientX; startY = event.clientY;
    scrollLeft = viewport.scrollLeft; scrollTop = viewport.scrollTop;
    viewport.classList.add('is-panning'); viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener('pointermove', event => {
    if (!dragging) return;
    viewport.scrollLeft = scrollLeft - (event.clientX - startX);
    viewport.scrollTop = scrollTop - (event.clientY - startY);
  });
  const stop = event => { if (!dragging) return; dragging = false; viewport.classList.remove('is-panning'); try { viewport.releasePointerCapture(event.pointerId); } catch (_) {} };
  viewport.addEventListener('pointerup', stop); viewport.addEventListener('pointercancel', stop);
}

function move(direction) {
  const steps = stepsFor(designs[designIndex]);
  if (direction > 0) {
    if (stepIndex < steps.length - 1) stepIndex += 1;
    else if (designIndex < designs.length - 1) { designIndex += 1; stepIndex = 0; }
  } else if (stepIndex > 0) stepIndex -= 1;
  else if (designIndex > 0) { designIndex -= 1; stepIndex = stepsFor(designs[designIndex]).length - 1; }
  render();
}

function changeZoom(delta) {
  zooms[designIndex] = Math.max(1, Math.min(3, Math.round((zooms[designIndex] + delta) * 4) / 4));
  render();
}

app.addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  if (action === 'next') move(1);
  else if (action === 'previous') move(-1);
  else if (action === 'fit') { zooms[designIndex] = 1; render(); }
  else if (action === 'zoom-in') changeZoom(.25);
  else if (action === 'zoom-out') changeZoom(-.25);
  else if (action === 'jump-unanswered') { const missing = firstUnansweredStep(state[designIndex]); if (missing !== null) { stepIndex = missing; render(); } }
});

app.addEventListener('input', event => {
  const target = event.target;
  const current = state[designIndex];
  const step = stepsFor(designs[designIndex])[stepIndex];
  if (target.type === 'radio' && !target.checked) return;
  if (target.name === 'buildable') current.buildable = target.value;
  else if (target.name === 'rating') current.rating = target.value;
  else if (target.name && target.name.startsWith('diagnostic-')) current.diagnostics[step.diagIndex].verdict = target.value;
  else if (target.dataset.field === 'notes') current.notes = target.value;
  else if (target.dataset.field === 'ruleIdeas') current.ruleIdeas = target.value;
  else if (target.dataset.field === 'diagnostic-note') current.diagnostics[step.diagIndex].note = target.value;
  else return;
  persist();
  if (target.type === 'radio') render();
});

const picker = document.getElementById('design-picker');
designs.forEach((d, i) => picker.insertAdjacentHTML('beforeend', `<option value="${i}">${i + 1}. ${esc(d.title)} · ${esc(d.id)}</option>`));
picker.addEventListener('change', () => { designIndex = Number(picker.value); stepIndex = 0; render(); });

document.getElementById('source-open').onclick = () => {
  document.getElementById('source-title').textContent = `${designs[designIndex].title} — DSL source`;
  document.getElementById('source-code').textContent = designs[designIndex].source;
  document.getElementById('source-dialog').showModal();
};

document.getElementById('export').onclick = () => {
  const payload = {tool:'barndsl-design-review', version:1, designs:state};
  const text = JSON.stringify(payload, null, 2);
  document.getElementById('out').value = text;
  const link = document.getElementById('download');
  link.href = URL.createObjectURL(new Blob([text], {type:'application/json'}));
  link.download = 'design-feedback.json';
  document.getElementById('export-dialog').showModal();
};

document.getElementById('copy').onclick = async event => {
  try { await navigator.clipboard.writeText(document.getElementById('out').value); event.target.textContent = 'Copied'; }
  catch (_) { document.getElementById('out').select(); event.target.textContent = 'Select and copy'; }
};

document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => document.getElementById(button.dataset.close).close());
document.addEventListener('keydown', event => {
  if (document.querySelector('dialog[open]')) return;
  if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) return;
  if (event.key === 'ArrowRight') { event.preventDefault(); move(1); }
  else if (event.key === 'ArrowLeft') { event.preventDefault(); move(-1); }
  else if (event.key === '+' || event.key === '=') { event.preventDefault(); changeZoom(.25); }
  else if (event.key === '-') { event.preventDefault(); changeZoom(-.25); }
});

render();
</script>
</body>
</html>
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
