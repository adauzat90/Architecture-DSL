"""Build a self-contained HTML review sheet for a set of barndsl designs.

    python -m tools.design_review FILE.barn [FILE2.barn ...] --out review.html

Each design is compiled and rendered; the page shows the floor plan, the DSL
source, and the diagnostics. For every diagnostic you can mark a verdict
(correct / false positive / unsure) and a note; per design you can add a rating,
a buildable yes/no/maybe, free notes, and "new rule ideas". The **Export
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
<title>barndsl — design review</title>
<style>
  :root { --err:#c0392b; --warn:#b9770e; --info:#1f6feb; --ok:#1a7f37; --bg:#f6f7f9; --card:#fff; --line:#e3e6ea; --ink:#1c2128; --mut:#6b7480; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; color:var(--ink); background:var(--bg); }
  header { position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line); padding:12px 20px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; }
  header p { margin:0; color:var(--mut); flex:1 1 320px; min-width:240px; }
  button, .btn { font:inherit; cursor:pointer; border:1px solid var(--line); background:#fff; border-radius:7px; padding:7px 12px; }
  #export { background:var(--ink); color:#fff; border-color:var(--ink); font-weight:600; }
  main { padding:20px; display:grid; gap:20px; grid-template-columns:repeat(auto-fill,minmax(440px,1fr)); align-items:start; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .head { display:flex; align-items:center; gap:8px; padding:12px 14px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .head h2 { font-size:15px; margin:0; flex:1; }
  .pill { font-size:12px; font-weight:600; border-radius:999px; padding:2px 9px; color:#fff; }
  .pill.e{background:var(--err)} .pill.w{background:var(--warn)} .pill.i{background:var(--info)} .pill.ok{background:var(--ok)}
  .plan { background:#fbfbfc; border-bottom:1px solid var(--line); padding:8px; text-align:center; }
  .plan svg { max-width:100%; height:auto; }
  .body { padding:12px 14px; }
  table.diags { width:100%; border-collapse:collapse; margin-bottom:12px; }
  table.diags td { vertical-align:top; padding:6px 6px; border-top:1px solid var(--line); font-size:13px; }
  .sev { font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:.03em; white-space:nowrap; }
  .sev.error{color:var(--err)} .sev.warning{color:var(--warn)} .sev.info{color:var(--info)}
  code.code { background:#eef1f4; border-radius:5px; padding:1px 6px; font-size:12px; }
  .msg { color:var(--ink); }
  .hint { color:var(--mut); font-size:12px; }
  .verdict { display:flex; gap:8px; flex-wrap:wrap; margin-top:4px; }
  .verdict label { font-size:12px; color:var(--mut); cursor:pointer; display:inline-flex; gap:3px; align-items:center; }
  .vnote { width:100%; margin-top:4px; }
  input[type=text], textarea, select { font:inherit; border:1px solid var(--line); border-radius:6px; padding:5px 7px; }
  textarea { width:100%; resize:vertical; }
  .clean { color:var(--ok); font-size:13px; padding:6px; }
  .meta { display:grid; gap:8px; border-top:1px solid var(--line); padding-top:10px; }
  .meta .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .meta label.k { font-weight:600; font-size:12px; min-width:74px; }
  details pre { background:#0d1117; color:#d6deeb; padding:12px; border-radius:8px; overflow:auto; font-size:12px; }
  dialog { border:none; border-radius:12px; padding:0; width:min(760px,92vw); }
  dialog .dhead { padding:12px 16px; border-bottom:1px solid var(--line); font-weight:600; }
  dialog .dbody { padding:16px; }
  dialog textarea { width:100%; height:46vh; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
  dialog .dfoot { display:flex; gap:10px; padding:0 16px 16px; }
</style>
</head>
<body>
<header>
  <h1>barndsl — design review</h1>
  <p>For each diagnostic, mark whether the call is right (✓ correct · ✗ false positive · ? unsure) and add a note. Rate each design, jot notes and new rule ideas, then <b>Export</b> and paste the JSON back.</p>
  <button id="export">⤓ Export feedback</button>
</header>
<main id="app"></main>

<dialog id="dlg">
  <div class="dhead">Feedback JSON — copy this back into the chat</div>
  <div class="dbody"><textarea id="out" readonly></textarea></div>
  <div class="dfoot"><button id="copy">Copy to clipboard</button><a class="btn" id="dl">Download .json</a><button id="close">Close</button></div>
</dialog>

<script id="data" type="application/json">/*__DATA__*/</script>
<script>
const designs = JSON.parse(document.getElementById('data').textContent);
const state = designs.map(d => ({
  id: d.id, title: d.title, summary: d.summary,
  rating: '', buildable: '', notes: '', ruleIdeas: '',
  diagnostics: d.diagnostics.map(x => ({severity:x.severity, code:x.code, message:x.message, verdict:'', note:''}))
}));
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const app = document.getElementById('app');

designs.forEach((d, i) => {
  const s = d.summary;
  const pills = [
    s.errors ? `<span class="pill e">${s.errors} err</span>` : '',
    s.warnings ? `<span class="pill w">${s.warnings} warn</span>` : '',
    s.infos ? `<span class="pill i">${s.infos} info</span>` : '',
    (!s.errors && !s.warnings && !s.infos) ? `<span class="pill ok">0/0/0</span>` : ''
  ].join('');
  const rows = d.diagnostics.map((x, j) => `
    <tr>
      <td class="sev ${x.severity}">${x.severity}</td>
      <td><code class="code">${esc(x.code)}</code></td>
      <td>
        <div class="msg">${esc(x.message)}${x.room?` <em>(${esc(x.room)})</em>`:''}</div>
        ${x.hint?`<div class="hint">${esc(x.hint)}</div>`:''}
        <div class="verdict">
          <label><input type="radio" name="v${i}_${j}" data-d="${i}" data-f="diag:${j}:verdict" value="correct">✓ correct</label>
          <label><input type="radio" name="v${i}_${j}" data-d="${i}" data-f="diag:${j}:verdict" value="false_positive">✗ false&nbsp;positive</label>
          <label><input type="radio" name="v${i}_${j}" data-d="${i}" data-f="diag:${j}:verdict" value="unsure">? unsure</label>
        </div>
        <input type="text" class="vnote" data-d="${i}" data-f="diag:${j}:note" placeholder="note (optional)">
      </td>
    </tr>`).join('');
  const diagTable = d.diagnostics.length
    ? `<table class="diags">${rows}</table>`
    : `<div class="clean">✓ No diagnostics — a clean 0/0/0 reference design.</div>`;
  const card = document.createElement('section');
  card.className = 'card';
  card.innerHTML = `
    <div class="head"><h2>${esc(d.title)}</h2>${pills}</div>
    <div class="plan">${d.svg}</div>
    <div class="body">
      ${diagTable}
      <div class="meta">
        <div class="row"><label class="k">Rating</label>
          <select data-d="${i}" data-f="rating">
            <option value="">—</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option>
          </select>
          <label class="k">Buildable</label>
          <label><input type="radio" name="b${i}" data-d="${i}" data-f="buildable" value="yes">yes</label>
          <label><input type="radio" name="b${i}" data-d="${i}" data-f="buildable" value="no">no</label>
          <label><input type="radio" name="b${i}" data-d="${i}" data-f="buildable" value="maybe">maybe</label>
        </div>
        <div class="row" style="display:block"><label class="k">Notes</label>
          <textarea rows="2" data-d="${i}" data-f="notes" placeholder="what's good / wrong with this design"></textarea></div>
        <div class="row" style="display:block"><label class="k">Rule ideas</label>
          <textarea rows="2" data-d="${i}" data-f="ruleIdeas" placeholder="new design rules this plan suggests"></textarea></div>
        <details><summary>DSL source</summary><pre>${esc(d.source)}</pre></details>
      </div>
    </div>`;
  app.appendChild(card);
});

app.addEventListener('input', ev => {
  const t = ev.target, di = t.dataset.d, f = t.dataset.f;
  if (di == null || !f) return;
  if (t.type === 'radio' && !t.checked) return;
  const s = state[di];
  if (f.startsWith('diag:')) { const [, j, key] = f.split(':'); s.diagnostics[j][key] = t.value; }
  else { s[f] = t.value; }
});

document.getElementById('export').onclick = () => {
  const payload = { tool: 'barndsl-design-review', version: 1, designs: state };
  const text = JSON.stringify(payload, null, 2);
  document.getElementById('out').value = text;
  const blob = new Blob([text], { type: 'application/json' });
  const dl = document.getElementById('dl');
  dl.href = URL.createObjectURL(blob); dl.download = 'design-feedback.json';
  document.getElementById('dlg').showModal();
};
document.getElementById('copy').onclick = () => navigator.clipboard.writeText(document.getElementById('out').value);
document.getElementById('close').onclick = () => document.getElementById('dlg').close();
</script>
</body>
</html>
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
