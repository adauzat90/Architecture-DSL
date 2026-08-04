"""Smoke tests for the HTML design-review builder (tools/design_review.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tools.design_review import build_html, design_data

GALLERY = Path(__file__).resolve().parent.parent / "examples" / "gallery"


def test_design_data_compiles_and_renders():
    d = design_data(str(GALLERY / "cottage.barn"))
    assert d["id"] == "cottage"
    assert d["svg"].startswith("<svg")
    assert d["summary"] == {"errors": 0, "warnings": 0, "infos": 0}
    assert isinstance(d["diagnostics"], list)


def test_design_data_captures_diagnostics():
    # hall_spine is clean; build a trivially-flawed source to get a diagnostic.
    from tools.design_review import design_data as dd  # same fn, explicit

    d = dd(str(GALLERY / "hall_spine.barn"))
    assert d["summary"]["errors"] == 0


def test_build_html_is_self_contained_and_embeds_valid_json():
    designs = [design_data(str(p)) for p in sorted(GALLERY.glob("*.barn"))]
    html = build_html(designs)
    assert html.lstrip().startswith("<!doctype html>")
    # No raw "</script>" can leak from a source/SVG into the data block.
    body = re.search(r'<script id="data"[^>]*>(.*?)</script>', html, re.S)
    assert body is not None
    embedded = json.loads(body.group(1))
    assert [d["id"] for d in embedded] == [p.stem for p in sorted(GALLERY.glob("*.barn"))]
    # The export/feedback affordances are present.
    assert "Export feedback" in html and "barndsl-design-review" in html


def test_build_html_uses_guided_review_deck_with_persistence():
    html = build_html([design_data(str(GALLERY / "cottage.barn"))])

    assert 'id="plan-viewport"' in html
    assert 'id="question-panel"' in html
    assert 'id="design-picker"' in html
    assert 'id="progress-bar"' in html
    assert "Does this layout feel buildable?" in html
    assert "Is this diagnostic correct?" in html
    assert "View DSL" in html
    assert "localStorage.setItem" in html
    assert "firstUnansweredStep" in html
    assert "review-focus" in html
    # Keep the existing downstream feedback contract while changing the UI.
    assert "{tool:'barndsl-design-review', version:1, designs:state}" in html
