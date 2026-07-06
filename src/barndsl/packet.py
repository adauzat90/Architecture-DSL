"""A single client / permit-sketch deliverable for a compiled plan.

`barndsl packet FILE -o plan.html` binds the capabilities that already exist —
`metrics()`, the deterministic design score, the dimensioned SVG render, the
room/door/window schedules, the compiler diagnostics, and the `cost` estimate —
into one self-contained, print-ready HTML document: the "hand it to a
builder / lender / county" artifact, and the non-Revit user's equivalent of the
Revit *Document* pass.

**Format.** A single self-contained HTML file, styled for print with a CSS
page-break before each section (cover, floor plan, schedules, cost, diagnostics).
Every asset is embedded — the SVG is inlined, there are no external requests, and
it works fully offline. No new dependency is added: `cairosvg` (which only turns
one SVG into one PDF and cannot lay out a multi-section document) is **not**
required. To get a PDF, open the HTML and *Print → Save as PDF* in any browser;
the page-break CSS paginates it into the sections above.

    from barndsl import compile_file, build_packet
    open("plan.html", "w").write(build_packet(compile_file("plan.barn")))
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

from .cost import estimate_cost
from .render import RenderConfig, render_site_svg, render_svg, sheet_scale
from .schedule import _schedules
from .score import design_score

_CSS = """
:root { --ink:#222; --muted:#666; --line:#ddd; --accent:#8A4B12; }
* { box-sizing: border-box; }
body { font-family: Helvetica, Arial, sans-serif; color: var(--ink); margin: 0;
       line-height: 1.4; }
.page { padding: 40px 48px; page-break-after: always; }
.page:last-child { page-break-after: auto; }
h1 { font-size: 30px; margin: 0 0 4px; }
h2 { font-size: 20px; margin: 0 0 16px; border-bottom: 2px solid var(--accent);
     padding-bottom: 6px; }
h3 { font-size: 15px; margin: 20px 0 8px; color: var(--muted); }
.sub { color: var(--muted); font-size: 15px; margin: 0 0 28px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 6px 0 18px; }
th, td { text-align: left; padding: 5px 9px; border-bottom: 1px solid var(--line); }
th { background: #f6f6f6; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.metrics td:first-child { color: var(--muted); }
.metrics td:last-child { text-align: right; font-weight: bold; }
.metrics { max-width: 460px; }
.score-total { font-size: 40px; font-weight: bold; }
.score-total small { font-size: 18px; color: var(--muted); font-weight: normal; }
.svgwrap { overflow-x: auto; border: 1px solid var(--line); padding: 10px;
           background: #fff; }
.svgwrap svg { max-width: 100%; height: auto; }
/* The true-scale floor-plan wrapper is sized in physical inches; the SVG must
   fill it exactly — max-width alone shrinks an oversized drawing but would
   never grow one, silently printing below the stated scale. content-box keeps
   the inch width on the content itself (border-box would fold the wrapper's
   padding/border into it, shaving ~2% off the printed scale). */
.svgwrap.scaled { box-sizing: content-box; }
.svgwrap.scaled svg { width: 100%; height: auto; display: block; }
.diag { font-size: 13px; margin: 4px 0; padding: 8px 10px; border-left: 4px solid; }
.diag.error { border-color: #c0392b; background: #fdeceb; }
.diag.warning { border-color: #d68910; background: #fef6e9; }
.diag.info { border-color: #2874a6; background: #eaf2f9; }
.diag .code { font-weight: bold; font-family: monospace; }
.diag .hint { color: var(--muted); }
.total-row td { font-weight: bold; border-top: 2px solid var(--ink);
                border-bottom: none; }
.note { color: var(--muted); font-size: 12px; font-style: italic; margin-top: 12px; }
.badge { display: inline-block; font-size: 12px; color: var(--muted);
         margin-left: 8px; }
"""


def _tag(text: str) -> str:
    return escape(str(text))


def _cover(result: Any, plan: Any, est: dict[str, Any]) -> str:
    m = plan.metrics()
    score = design_score(result)
    program = (
        f"{int(m['bedroom_count'])} bed / {m['bathroom_count']:g} bath · "
        f"{m['interior_sqft']:.0f} sq ft interior · "
        f"{m['habitable_sqft']:.0f} sq ft habitable · "
        f"footprint {m['footprint_sqft']:.0f} sq ft"
    )
    rows = [
        ("Footprint", f"{m['footprint_sqft']:.0f} sq ft"),
        ("Interior (conditioned)", f"{m['interior_sqft']:.0f} sq ft"),
        ("Habitable", f"{m['habitable_sqft']:.0f} sq ft"),
        ("Bedrooms", f"{int(m['bedroom_count'])}"),
        ("Bathrooms", f"{m['bathroom_count']:g}"),
        ("Ceiling", f"{plan.ceiling_height:g} ft"),
        ("Exterior wall area", f"{m['exterior_wall_area_sqft']:.0f} sq ft"),
        ("Roof area (approx)", f"{m['roof_area_sqft']:.0f} sq ft"),
        ("Foundation concrete", f"{m['foundation_concrete_yd3']:.1f} cu yd"),
    ]
    if m.get("counter_linear_ft", 0.0) > 0:
        rows.append(("Countertops", f"{m['counter_linear_ft']:.0f} lf"))
    rows.append(("Estimated cost", f"${est['total']['expected']:,.0f}"))
    metric_rows = "\n".join(
        f"<tr><td>{_tag(k)}</td><td>{_tag(v)}</td></tr>" for k, v in rows
    )
    comp_rows = "\n".join(
        f"<tr><td>{_tag(name)}</td><td class='num'>-{points:g}</td>"
        f"<td>{_tag(score.details.get(name, ''))}</td></tr>"
        for name, points in score.components.items()
        if points
    ) or "<tr><td colspan='3'>No deductions — a clean plan.</td></tr>"
    c = score.counts
    return f"""
<section class="page">
  <h1>{_tag(plan.name)}</h1>
  <p class="sub">Permit-sketch packet · {_tag(program)}</p>
  <h3>Key metrics</h3>
  <table class="metrics">{metric_rows}</table>
  <h3>Design score</h3>
  <p class="score-total">{score.total:g}<small> / 100</small>
     <span class="badge">{c['error']} error(s), {c['warning']} warning(s),
     {c['info']} info(s)</span></p>
  <table>
    <tr><th>Component</th><th class="num">Deduction</th><th>Cause</th></tr>
    {comp_rows}
  </table>
</section>
"""


#: Feet (′) and inches (″) glyphs for the scale statement.
_FT_GLYPH, _IN_GLYPH = "′", "″"


def _floor_plan(plan: Any, sheet: str = "Letter") -> str:
    # render_svg draws the dimensioned plan (overall dimension lines + per-room
    # W x L, and one stacked block per level for a multi-story plan). For the
    # permit sheet — the drawing an architect submits — pick the largest standard
    # architectural scale that fits the target sheet, size the embedded SVG in
    # physical inches so the plan prints at that true scale, state the scale, and
    # draw a graphic scale bar (which survives any reprographic resize).
    ipf, label, css_w = sheet_scale(plan, sheet=sheet)
    statement = f"SCALE: {label} = 1{_FT_GLYPH}-0{_IN_GLYPH} ({sheet})"
    cfg = RenderConfig(scale_bar=True, scale_note=statement)
    svg = render_svg(plan, cfg)
    levels = plan.levels()
    note = (
        f"One block per level ({len(levels)} levels)."
        if len(levels) > 1
        else "Drawn to architectural scale."
    )
    # Inline physical width (inches) so the plan prints at true scale; the graphic
    # scale bar in the SVG is the reprographic-safe backup (browser print margins
    # can't be guaranteed to the pixel — the scale statement + bar are the answer).
    return f"""
<section class="page">
  <h2>Floor Plan</h2>
  <p class="sub">{_tag(statement)}</p>
  <div class="svgwrap scaled" style="width:{css_w:.2f}in; max-width:100%;">{svg}</div>
  <p class="note">{_tag(note)} Verify against the graphic scale bar and stated
     dimensions.</p>
</section>
"""


def _has_devices(plan: Any) -> bool:
    """True when the plan declares any receptacle / switch / light layout."""
    return bool(plan.outlets or plan.switches or plan.lights)


def _has_alarms(plan: Any) -> bool:
    return bool(getattr(plan, "alarms", None))


def _has_electrical(plan: Any) -> bool:
    """Whether an Electrical Plan sheet should be produced at all.

    Three states drive the sheet (see :func:`_electrical_plan`): a device layout
    (full sheet), alarms only (sheet + an "alarms only" note), or nothing at all
    (no sheet — never a 0/0/0 device table an examiner would bounce)."""
    return _has_devices(plan) or _has_alarms(plan)


def _electrical_plan(plan: Any, sheet: str = "Letter") -> str:
    """The Electrical Plan sheet: the floor plan with the electrical layer on, a
    small legend, and an outlet/switch/light count table. Only included when the
    plan declares electrical items."""
    ipf, label, css_w = sheet_scale(plan, sheet=sheet)
    statement = f"SCALE: {label} = 1{_FT_GLYPH}-0{_IN_GLYPH} ({sheet})"
    cfg = RenderConfig(scale_bar=True, scale_note=statement, show_electrical=True)
    svg = render_svg(plan, cfg)
    n_out = len(plan.outlets)
    n_gfci = sum(1 for o in plan.outlets if o.gfci)
    n_sw = len(plan.switches)
    n_light = len(plan.lights)
    alarms = getattr(plan, "alarms", None) or []
    n_smoke = sum(1 for a in alarms if a.is_smoke)
    n_co = sum(1 for a in alarms if a.is_co)
    count_rows = "\n".join(
        f"<tr><td>{_tag(name)}</td><td class='num'>{n}</td></tr>"
        for name, n in (
            ("Receptacles (outlets)", n_out),
            ("— of which GFCI", n_gfci),
            ("Wall switches", n_sw),
            ("Ceiling lights", n_light),
            ("Smoke alarms", n_smoke),
            ("CO alarms", n_co),
        )
    )
    legend = (
        "<span class='badge'>⊙ receptacle · ⊙ GFCI = ground-fault · "
        "S = switch · ⊗ = ceiling light · SD/CO = smoke/CO alarm</span>"
    )
    # Alarms-only: a plan that declares smoke/CO alarms but no receptacle/switch/
    # lighting layout still earns a sheet (the alarms must be shown), but the sheet
    # says so plainly rather than pretending a 0/0/0 device table is the design.
    alarms_only = not _has_devices(plan)
    alarms_note = (
        '<p class="note">No receptacle/switch/lighting layout declared — alarms '
        "only.</p>"
        if alarms_only
        else ""
    )
    return f"""
<section class="page">
  <h2>Electrical Plan</h2>
  <p class="sub">{_tag(statement)} · devices shown in violet. {legend}</p>
  <div class="svgwrap scaled" style="width:{css_w:.2f}in; max-width:100%;">{svg}</div>
  {alarms_note}
  <h3>Device count</h3>
  <table class="metrics">{count_rows}</table>
  <p class="note">Schematic device layout — verify circuiting, GFCI/AFCI
     protection and switched-lighting coverage against IRC E39xx on the final
     electrical plan.</p>
</section>
"""


def _site_clearance_rows(plan: Any) -> list[tuple[str, str, str, bool]]:
    """``(side, required, actual, ok)`` for each lot side — the building's real
    yard vs its required setback. Empty when the building isn't placeable on the
    lot. ``ok`` is True when there's no requirement or the yard meets it."""
    from .render import fmt_ft_in

    ss = plan.site_spec
    origin = plan.building_origin_on_lot()
    if origin is None:
        return []
    bx, by = origin
    minx, miny, maxx, maxy = plan.bounds()
    for p in plan.porches:
        minx, miny = min(minx, p.x), min(miny, p.y)
        maxx, maxy = max(maxx, p.x + p.width), max(maxy, p.y + p.length)
    lot_w, lot_l = ss.width, ss.length
    sides = [
        ("Front (south)", ss.front, by + miny),
        ("Rear (north)", ss.rear, lot_l - (by + maxy)),
        ("West side", ss.side, bx + minx),
        ("East side", ss.side, lot_w - (bx + maxx)),
    ]
    rows = []
    for label, req, actual in sides:
        req_str = "—" if req is None else fmt_ft_in(req)
        ok = req is None or actual >= req - 1e-6
        rows.append((label, req_str, fmt_ft_in(actual), ok))
    return rows


def _site_plan(plan: Any) -> str:
    """The Site Plan sheet: the lot, setback lines, site features (drive/well/
    septic/service) and building footprint placed on the lot, with dimensions, a
    yard-clearance table and site notes. Only included when the plan declares a
    ``site``."""
    ss = plan.site_spec
    svg = render_site_svg(plan)
    rows = [("Lot", f"{ss.width:g}′ × {ss.length:g}′")]
    for label, val in (("Front setback", ss.front), ("Rear setback", ss.rear),
                       ("Side setback", ss.side)):
        if val is not None:
            rows.append((label, f"{val:g}′"))
    if ss.has_building:
        rows.append(("Building at", f"{ss.building_x:g}′, {ss.building_y:g}′ (SW corner)"))
    if plan.grade is not None:
        from .render import fmt_ft_in
        rows.append(("Grade", f"finish floor {fmt_ft_in(plan.grade)} above grade"))
    dim_rows = "\n".join(
        f"<tr><td>{_tag(k)}</td><td>{_tag(v)}</td></tr>" for k, v in rows
    )

    # Yard-clearance table: required setback vs the building's actual distance.
    clearance = _site_clearance_rows(plan)
    clearance_html = ""
    if clearance:
        body = "\n".join(
            f"<tr><td>{_tag(side)}</td><td>{_tag(req)}</td><td>{_tag(act)}</td>"
            f"<td>{'✓' if ok else '✗ short'}</td></tr>"
            for side, req, act, ok in clearance
        )
        clearance_html = (
            "<h3>Yard clearances</h3>"
            "<table class='metrics'><tr><th>Side</th><th>Required</th>"
            f"<th>Actual</th><th>OK</th></tr>{body}</table>"
        )

    # Site-feature notes (drive/well/septic/service), only when present.
    notes = []
    for d in ss.drives:
        notes.append(f"Driveway: {d.width:g}′ × {d.length:g}′ {d.surface}.")
    if ss.walks:
        notes.append("Walkway from an entry to the drive.")
    for _wl in ss.wells:
        notes.append("Private water well (confirm 100 ft septic separation).")
    for sp in ss.septics:
        notes.append(
            "Septic tank" + (" + drain field" if sp.has_field else "")
            + " (allowance; requires a perc test)."
        )
    for sv in ss.services:
        notes.append(f"Service: {sv.utility} from the {sv.side.value}.")
    notes_html = ""
    if notes:
        items = "\n".join(f"<li>{_tag(n)}</li>" for n in notes)
        notes_html = f"<h3>Site notes</h3><ul class='notes'>{items}</ul>"

    return f"""
<section class="page">
  <h2>Site Plan</h2>
  <p class="sub">Lot boundary, required setbacks (dashed), site features, and building footprint.</p>
  <div class="svgwrap">{svg}</div>
  <h3>Lot &amp; setbacks</h3>
  <table class="metrics">{dim_rows}</table>
  {clearance_html}
  {notes_html}
  <p class="note">Schematic, fit-to-page — not drawn to a fixed engineering scale
     (a limitation; the floor-plan sheet carries the true architectural scale).
     Not a substitute for a surveyed site plan.</p>
</section>
"""


def _schedule_tables(plan: Any) -> str:
    blocks = []
    for title, columns, rows in _schedules(plan, True, True, True):
        head = "".join(f"<th>{_tag(c.header)}</th>" for c in columns)
        if rows:
            body = "\n".join(
                "<tr>" + "".join(f"<td>{_tag(c.get(row))}</td>" for c in columns) + "</tr>"
                for row in rows
            )
        else:
            body = f"<tr><td colspan='{len(columns)}'>None.</td></tr>"
        blocks.append(
            f"<h3>{_tag(title)} ({len(rows)})</h3>"
            f"<table><tr>{head}</tr>{body}</table>"
        )
    return f"""
<section class="page">
  <h2>Schedules</h2>
  {''.join(blocks)}
</section>
"""


def _cost_section(est: dict[str, Any]) -> str:
    rows = "\n".join(
        f"<tr><td>{_tag(ln['group'])}</td><td>{_tag(ln['item'])}</td>"
        f"<td class='num'>{ln['quantity']:g} {_tag(ln['unit'])}</td>"
        f"<td class='num'>${ln['unit_cost']:,.0f}</td>"
        f"<td class='num'>${ln['cost']:,.0f}</td>"
        f"<td>{_tag(ln['source'])}</td></tr>"
        for ln in est["assemblies"]
    )
    t = est["total"]
    mult = (
        f" · regional multiplier x{est['multiplier']:g}"
        if est["multiplier"] != 1.0
        else ""
    )
    return f"""
<section class="page">
  <h2>Cost Estimate</h2>
  <p class="sub">Assembly takeoff{_tag(mult)}. Every line is quantity x unit cost.</p>
  <table>
    <tr><th>Assembly</th><th>Item</th><th class="num">Qty</th>
        <th class="num">Unit cost</th><th class="num">Cost</th><th>Source</th></tr>
    {rows}
    <tr class="total-row"><td colspan="4">Estimated total (expected)</td>
        <td class="num">${t['expected']:,.0f}</td><td></td></tr>
    <tr class="total-row"><td colspan="4">Range (+/-{est['band_pct']:g}%)</td>
        <td class="num">${t['low']:,.0f} – ${t['high']:,.0f}</td><td></td></tr>
  </table>
  <p class="note">{_tag(est.get('exclusions', ''))}</p>
  <p class="note">{_tag(est['disclaimer'])}</p>
</section>
"""


def _diagnostics(result: Any) -> str:
    diags = sorted(result.diagnostics, key=lambda i: (i.line or 0, i.col or 0))
    # Accepted deviations (downgraded by a `# barndsl: accept CODE` pragma) get
    # their own audit subsection — the documented, deliberate deviations an AHJ
    # reviewer reads — separate from the diagnostics still needing attention.
    accepted = [d for d in diags if getattr(d, "accepted", False)]
    active = [d for d in diags if not getattr(d, "accepted", False)]
    if not active:
        body = "<p>No diagnostics — the plan compiles clean.</p>"
    else:
        items = []
        for d in active:
            sev = d.severity.value
            where = f" ({_tag(d.room)})" if d.room else ""
            loc = f"line {d.line}: " if d.line else ""
            hint = f"<div class='hint'>hint: {_tag(d.hint)}</div>" if d.hint else ""
            items.append(
                f"<div class='diag {sev}'>{loc}<span class='code'>{_tag(d.code)}</span>"
                f"{where} — {_tag(d.message)}{hint}</div>"
            )
        body = "\n".join(items)
    accepted_html = ""
    if accepted:
        rows = []
        for d in accepted:
            where = f" ({_tag(d.room)})" if d.room else ""
            loc = f"line {d.line}: " if d.line else ""
            reason = getattr(d, "accept_reason", None)
            reason_html = (
                f"<div class='hint'>reason: {_tag(reason)}</div>"
                if reason
                else "<div class='hint'>reason: (none given)</div>"
            )
            rows.append(
                f"<div class='diag info accepted'>{loc}"
                f"<span class='code'>{_tag(d.code)}</span>{where} — "
                f"accepted deviation{reason_html}</div>"
            )
        accepted_html = f"""
  <h3>Accepted deviations</h3>
  <p class="sub">{len(accepted)} deviation(s) waived by an `accept` pragma — a
     documented, deliberate departure recorded for review, not a defect.</p>
  {"".join(rows)}
"""
    c = result.to_dict()["counts"]
    return f"""
<section class="page">
  <h2>Diagnostics Appendix</h2>
  <p class="sub">{c['error']} error(s), {c['warning']} warning(s),
     {c['info']} info(s){f' — including {len(accepted)} accepted' if accepted else ''}</p>
  {body}
  {accepted_html}
</section>
"""


def build_packet(
    result: Any,
    *,
    costs: dict[str, float] | None = None,
    multiplier: float = 1.0,
    sheet: str = "Letter",
) -> str:
    """Return the full permit-sketch packet as a self-contained HTML string.

    ``result`` is a :class:`~barndsl.compiler.CompileResult` (needed for the
    diagnostics appendix); its ``plan`` must be non-``None``. ``costs`` and
    ``multiplier`` are passed straight to :func:`~barndsl.cost.estimate_cost`.
    ``sheet`` selects the print sheet the floor plan is scaled to fit — one of
    :data:`~barndsl.render.SHEETS` (``"Letter"`` default, ``"Tabloid"`` for
    11×17); the largest standard architectural scale that fits is chosen and
    stated on the sheet with a graphic scale bar.
    """
    plan = getattr(result, "plan", None)
    if plan is None:
        raise ValueError("cannot build a packet: the source did not compile to a plan")
    est = estimate_cost(plan, overrides=costs, multiplier=multiplier)
    sections = (
        _cover(result, plan, est)
        + _floor_plan(plan, sheet=sheet)
        + (_electrical_plan(plan, sheet=sheet) if _has_electrical(plan) else "")
        + (
            _site_plan(plan)
            if plan.site_spec is not None and plan.site_spec.has_dims
            else ""
        )
        + _schedule_tables(plan)
        + _cost_section(est)
        + _diagnostics(result)
    )
    return (
        f"<!doctype html>\n<html lang=\"en\">\n<head>\n"
        f"<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_tag(plan.name)} — Permit Packet</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n{sections}\n</body>\n</html>\n"
    )


def save_packet(
    result: Any,
    path: str,
    *,
    costs: dict[str, float] | None = None,
    multiplier: float = 1.0,
    sheet: str = "Letter",
) -> str:
    """Write :func:`build_packet` to ``path``. Returns the path."""
    html = build_packet(result, costs=costs, multiplier=multiplier, sheet=sheet)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
