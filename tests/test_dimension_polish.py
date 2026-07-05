"""Feet-and-inches display formatting and the PROJECT SUMMARY legend-overflow fix.

The DSL keeps lengths in decimal feet (the single source of truth); these cover
the *display* layer: the shared ``fmt_ft_in`` formatter and the guarantee that a
long legend never clips off the plan SVG.
"""

from __future__ import annotations

import re

from barndsl import compile_source
from barndsl.render import fmt_ft_in, render_svg


def test_fmt_ft_in_whole_feet_drop_the_inch_part():
    # Whole feet render as a bare foot mark — this preserves the many pinned
    # label strings elsewhere (e.g. "33′", "24′") that predate ft-in.
    assert fmt_ft_in(18) == "18′"
    assert fmt_ft_in(33.0) == "33′"
    assert fmt_ft_in(1) == "1′"


def test_fmt_ft_in_fractional_uses_feet_dash_inches():
    assert fmt_ft_in(18.5) == "18′-6″"
    assert fmt_ft_in(21.7) == "21′-8″"  # rounds to the nearest inch (260.4 → 260)
    assert fmt_ft_in(2.67) == "2′-8″"  # a 2'-8" door


def test_fmt_ft_in_sub_foot_is_inches_alone():
    assert fmt_ft_in(0.75) == "9″"
    assert fmt_ft_in(0.5) == "6″"


def test_fmt_ft_in_zero_and_rounding_up_to_a_foot():
    assert fmt_ft_in(0) == "0′"
    # 11.99 ft rounds to 144 inches → a clean 12′, never "11′-12″".
    assert fmt_ft_in(11.99) == "12′"
    assert fmt_ft_in(0.96) == "1′"  # 11.52 in → 12 in → 1′


def test_fmt_ft_in_negative_does_not_crash():
    # Negatives shouldn't occur in a plan, but the formatter must be total.
    assert fmt_ft_in(-0.5) == "-6″"
    assert fmt_ft_in(-3.5) == "-3′-6″"


def test_fmt_ft_in_uses_the_prime_glyphs_not_ascii():
    s = fmt_ft_in(6.5)
    assert "′" in s and "″" in s  # U+2032 / U+2033
    assert "'" not in s and '"' not in s


# -- legend overflow ------------------------------------------------------

_SEVEN_TYPES = """\
plan "Many Types"
envelope 40 x 20
ceiling 9
room living:  living   at 0,0    size 14 x 12
room kitchen: kitchen  at 14,0   size 12 x 12
room office:  office   at 26,0   size 14 x 12
room bed:     bedroom  at 0,12    size 12 x 8
room bath:    bathroom at 12,12   size 8 x 8
room laundry: laundry  at 20,12   size 8 x 8
room pantry:  pantry   at 28,12   size 12 x 8
"""


def _max_drawn_y(svg: str) -> float:
    ys = [float(m) for m in re.findall(r'(?:\by=|\bcy=|\by1=|\by2=)"([0-9.]+)"', svg)]
    return max(ys)


def test_long_legend_is_never_clipped_below_the_canvas():
    plan = compile_source(_SEVEN_TYPES).plan
    present = {r.type for r in plan.rooms}
    assert len(present) >= 7  # enough legend rows to overrun a short plan
    svg = render_svg(plan)
    height = float(re.search(r'height="(\d+)"', svg).group(1))
    # Every drawn coordinate — including the last legend row — sits on the canvas.
    assert _max_drawn_y(svg) < height


def test_legend_fix_leaves_a_tall_plan_untouched():
    # A plan whose drawing is already taller than its summary panel must keep the
    # panel bounded to the plan height (the grow-to-fit only ever adds, via max()).
    src = 'plan "Tall"\nenvelope 30 x 60\nceiling 9\nroom big: living at 0,0 size 30 x 60\n'
    plan = compile_source(src).plan
    svg = render_svg(plan)
    height = float(re.search(r'height="(\d+)"', svg).group(1))
    assert _max_drawn_y(svg) < height
