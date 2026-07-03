"""Jurisdiction profiles — the numeric thresholds the code checks compare against.

barndsl's building-code checks encode one rule set: an IRC-flavoured set of
defaults. But a real project answers to a county or state that *amends* those
numbers — a stricter minimum ceiling, a gentler stair, a larger egress window.
A :class:`Profile` bundles the handful of thresholds that genuinely vary by
authority-having-jurisdiction so "compile under these local rules" is one flag
(``--profile``) instead of mental math on every diagnostic.

Only the ~14 thresholds that are *genuinely jurisdiction-variable* live here.
Everything else the validator checks (geometry sanity, door/opening sizes,
fixture clearances, the design-quality nudges) keeps importing
:mod:`barndsl.constants` directly — a profile is not a place to reparameterise
the whole rule base.

    from barndsl import load_profile, compile_file
    profile = load_profile("strict")           # a built-in
    profile = load_profile("travis_county.json")  # a JSON override file
    result = compile_file("plan.barn", profile=profile)

.. warning::
   The non-default built-in profiles below are **ILLUSTRATIVE starting points**,
   not legal advice. The numbers are plausible amendments chosen to demonstrate
   the mechanism — they are *not* transcribed from any adopted code. Always
   confirm the thresholds your jurisdiction actually enforces with the authority
   having jurisdiction before relying on a compile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from typing import Any

from .constants import (
    COMFORT_HALLWAY_WIDTH,
    MAX_EGRESS_SILL,
    MAX_RISER_HEIGHT,
    MIN_BEDROOM_AREA,
    MIN_BEDROOM_DIMENSION,
    MIN_CEILING,
    MIN_EGRESS_AREA,
    MIN_EGRESS_AREA_GRADE,
    MIN_EGRESS_OPENING_HEIGHT,
    MIN_EGRESS_OPENING_WIDTH,
    MIN_HALLWAY_WIDTH,
    MIN_STAIR_WIDTH,
    MIN_TREAD_DEPTH,
    NATURAL_LIGHT_RATIO,
)

#: Printed by ``barndsl profiles`` and reproduced in the docs. The built-in
#: non-default profiles are teaching examples, not adopted code.
DISCLAIMER = (
    "Non-default profiles are ILLUSTRATIVE examples of how thresholds can be "
    "amended — NOT legal advice and NOT transcribed from any adopted code. "
    "Confirm the numbers your jurisdiction enforces with the authority having "
    "jurisdiction before relying on a compile."
)


@dataclass(frozen=True)
class Profile:
    """The jurisdiction-variable code thresholds a compile is checked against.

    Immutable. Build one with :func:`load_profile` (a built-in name or a JSON
    override file) or from :data:`DEFAULT` via :meth:`with_overrides`. All
    measurements are in feet unless noted; :data:`DEFAULT` reproduces the current
    :mod:`barndsl.constants` values exactly, so validating with the default
    profile is byte-identical to the pre-profile behaviour.
    """

    #: Human name (also the registry key for a built-in).
    name: str

    # -- habitability (IRC R304 / R305) --
    #: Minimum ceiling height for habitable space (ft). Jurisdictions amend this:
    #: the IRC allows 7 ft; some adopt the IBC's 7 ft 6 in for dwellings.
    min_ceiling_height: float
    #: Minimum floor area of a habitable room / bedroom (sq ft). Local codes have
    #: historically required a larger primary sleeping room (80–120 sq ft).
    min_bedroom_area: float
    #: Minimum horizontal dimension of a habitable room (ft).
    min_bedroom_dimension: float

    # -- circulation (IRC R311.6) --
    #: Hard minimum hallway width (ft). Accessibility-minded jurisdictions raise
    #: the 36 in code floor to 42 in for a more usable passage.
    min_hallway_width: float
    #: Comfort hallway width (ft) — the soft HALL_TIGHT nudge target, below which
    #: a hall is legal but cramped. A rural profile may not want the nudge at all.
    comfort_hallway_width: float

    # -- emergency escape (IRC R310) --
    #: Net clear escape-opening area on an upper floor (sq ft).
    min_egress_area: float
    #: Net clear escape-opening area at grade / level 0 (sq ft). The IRC relaxes
    #: this to 5.0; some jurisdictions hold every floor to 5.7.
    min_egress_area_grade: float
    #: Minimum clear opening width / height of an escape opening (ft).
    min_egress_opening_width: float
    min_egress_opening_height: float
    #: Maximum sill height of an escape opening above the floor (ft). A reach /
    #: aging-in-place amendment lowers the 44 in default.
    max_egress_sill: float

    # -- stairs (IRC R311.7) --
    #: Maximum riser height (ft). The IRC allows 7 3/4 in; gentler local amendments
    #: (or an accessible target) cut it to 7 in, older codes allowed 8 1/4 in.
    max_riser_height: float
    #: Minimum tread depth (ft). The IRC allows 10 in; the IBC and some
    #: jurisdictions require 11 in.
    min_tread_depth: float
    #: Minimum stair flight width (ft).
    min_stair_width: float

    # -- daylight (IRC R303) --
    #: Glazing as a fraction of a habitable room's floor area. Energy / daylight
    #: amendments raise the 8% floor.
    natural_light_ratio: float

    @property
    def is_default(self) -> bool:
        """True when every threshold equals the IRC baseline (the DEFAULT values).

        Compared numerically, not by name, so a JSON profile that happens to
        override nothing still reports honestly and prints baseline wording.
        """
        return all(
            getattr(self, f.name) == getattr(DEFAULT, f.name)
            for f in fields(self)
            if f.name != "name"
        )

    def with_overrides(self, name: str | None = None, **overrides: float) -> "Profile":
        """A copy with some thresholds replaced (unknown fields raise)."""
        unknown = sorted(set(overrides) - _FIELD_NAMES)
        if unknown:
            raise ValueError(
                "unknown profile threshold(s): %s — valid keys are: %s"
                % (", ".join(unknown), ", ".join(sorted(_FIELD_NAMES)))
            )
        return replace(self, name=name or self.name, **overrides)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view: ``{name, <threshold>: value, ...}``."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: The numeric threshold field names (everything but ``name``).
_FIELD_NAMES = frozenset(f.name for f in fields(Profile) if f.name != "name")


#: The IRC-flavoured baseline: DEFAULT reproduces the current constants exactly,
#: so `validate(plan)` and `validate(plan, DEFAULT)` are byte-identical.
DEFAULT = Profile(
    name="default",
    min_ceiling_height=MIN_CEILING,
    min_bedroom_area=MIN_BEDROOM_AREA,
    min_bedroom_dimension=MIN_BEDROOM_DIMENSION,
    min_hallway_width=MIN_HALLWAY_WIDTH,
    comfort_hallway_width=COMFORT_HALLWAY_WIDTH,
    min_egress_area=MIN_EGRESS_AREA,
    min_egress_area_grade=MIN_EGRESS_AREA_GRADE,
    min_egress_opening_width=MIN_EGRESS_OPENING_WIDTH,
    min_egress_opening_height=MIN_EGRESS_OPENING_HEIGHT,
    max_egress_sill=MAX_EGRESS_SILL,
    max_riser_height=MAX_RISER_HEIGHT,
    min_tread_depth=MIN_TREAD_DEPTH,
    min_stair_width=MIN_STAIR_WIDTH,
    natural_light_ratio=NATURAL_LIGHT_RATIO,
)


#: A tighter, accessibility-leaning example: taller ceilings, a larger and wider
#: sleeping room, a 42 in hall, a gentler 7 in / 11 in stair, a lower sill, a
#: full-5.7 egress on every floor, and 10% glazing. ILLUSTRATIVE — see DISCLAIMER.
_STRICT = DEFAULT.with_overrides(
    name="strict",
    min_ceiling_height=7.5,
    min_bedroom_area=80.0,
    min_bedroom_dimension=8.0,
    min_hallway_width=3.5,
    comfort_hallway_width=4.5,
    min_egress_area_grade=MIN_EGRESS_AREA,  # hold grade floors to 5.7 too
    max_egress_sill=42 / 12,
    max_riser_height=7.0 / 12,
    min_tread_depth=11 / 12,
    natural_light_ratio=0.10,
)


#: A looser rural example: an older-code stair (8 1/4 in riser / 9 in tread),
#: the 5.0 sq ft escape opening on every floor, a higher permitted sill, and no
#: comfort-width hallway nag. ILLUSTRATIVE — see DISCLAIMER.
_RURAL = DEFAULT.with_overrides(
    name="rural",
    comfort_hallway_width=MIN_HALLWAY_WIDTH,  # don't nudge on hall comfort width
    min_egress_area=MIN_EGRESS_AREA_GRADE,  # 5.0 everywhere
    max_egress_sill=48 / 12,
    max_riser_height=8.25 / 12,
    min_tread_depth=9 / 12,
)


#: Built-in profiles by name. Aliases (``irc-2021``) resolve to the same object.
BUILTIN_PROFILES: dict[str, Profile] = {
    "default": DEFAULT,
    "strict": _STRICT,
    "rural": _RURAL,
}

#: Alternate names an author can pass to ``--profile`` / :func:`load_profile`.
_ALIASES: dict[str, str] = {
    "irc": "default",
    "irc-2021": "default",
    "irc2021": "default",
    "baseline": "default",
}


def get_profile(name: str) -> Profile:
    """A built-in profile by name (case-insensitive, aliases resolved).

    Raises ``ValueError`` naming the available profiles if ``name`` is unknown.
    """
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[key]
    raise ValueError(
        "unknown profile '%s' — built-ins are: %s (or pass a path to a JSON "
        "override file)" % (name, ", ".join(sorted(BUILTIN_PROFILES)))
    )


def profile_from_dict(data: dict[str, Any], *, source: str = "<dict>") -> Profile:
    """Build a profile from a JSON-style mapping overriding a base profile.

    Recognised keys: any :class:`Profile` threshold field, plus ``name`` and
    ``extends`` (the built-in to layer the overrides onto — default ``default``).
    Any other key is rejected with a clear error, mirroring cost.py's override
    behaviour. Threshold values must be finite positive numbers.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expected a JSON object, got {type(data).__name__}")
    valid = _FIELD_NAMES | {"name", "extends"}
    unknown = sorted(set(data) - valid)
    if unknown:
        raise ValueError(
            "%s: unknown profile key(s): %s — valid keys are: name, extends, %s"
            % (source, ", ".join(unknown), ", ".join(sorted(_FIELD_NAMES)))
        )
    base_name = data.get("extends", "default")
    if not isinstance(base_name, str):
        raise ValueError(f"{source}: 'extends' must be a profile name (string)")
    base = get_profile(base_name)
    overrides: dict[str, float] = {}
    for key in _FIELD_NAMES:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{source}: '{key}' must be a number, got {value!r}")
        value = float(value)
        if not (value > 0.0) or value != value or value in (float("inf"),):
            raise ValueError(f"{source}: '{key}' must be a finite positive number")
        overrides[key] = value
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError(f"{source}: 'name' must be a string")
    return base.with_overrides(name=name or f"custom ({base.name}+)", **overrides)


def load_profile(path_or_name: str) -> Profile:
    """Resolve ``--profile``: a built-in name (or alias), or a JSON override file.

    A bare name that matches a built-in wins; otherwise the argument is treated
    as a path to a JSON file (``{"extends": "strict", "min_ceiling_height": 8}``).
    Unknown built-in names that aren't a readable file, unknown JSON keys, and
    malformed JSON all raise ``ValueError`` with an actionable message.
    """
    key = path_or_name.strip().lower()
    if _ALIASES.get(key, key) in BUILTIN_PROFILES:
        return get_profile(path_or_name)
    try:
        with open(path_or_name, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ValueError(
            "unknown profile '%s' — not a built-in (%s) and no such JSON file"
            % (path_or_name, ", ".join(sorted(BUILTIN_PROFILES)))
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read profile file '{path_or_name}': {exc}")
    return profile_from_dict(data, source=path_or_name)


def profiles_text() -> str:
    """A table of the built-in profiles and their thresholds (for `barndsl profiles`)."""
    labels = {
        "min_ceiling_height": "ceiling height (ft)",
        "min_bedroom_area": "bedroom area (sq ft)",
        "min_bedroom_dimension": "bedroom min dim (ft)",
        "min_hallway_width": "hallway width (ft)",
        "comfort_hallway_width": "hallway comfort (ft)",
        "min_egress_area": "egress area, upper (sq ft)",
        "min_egress_area_grade": "egress area, grade (sq ft)",
        "min_egress_opening_width": "egress width (in)",
        "min_egress_opening_height": "egress height (in)",
        "max_egress_sill": "egress max sill (in)",
        "max_riser_height": "stair max riser (in)",
        "min_tread_depth": "stair min tread (in)",
        "min_stair_width": "stair width (ft)",
        "natural_light_ratio": "daylight glazing (%)",
    }
    # Fields shown in inches / percent for readability.
    inches = {
        "min_egress_opening_width", "min_egress_opening_height", "max_egress_sill",
        "max_riser_height", "min_tread_depth",
    }
    names = list(BUILTIN_PROFILES)
    rows = []
    header = f"  {'threshold':<28}" + "".join(f"{n:>12}" for n in names)
    rows.append(header)
    rows.append("  " + "-" * (28 + 12 * len(names)))
    for key, label in labels.items():
        cells = ""
        for n in names:
            v = getattr(BUILTIN_PROFILES[n], key)
            if key == "natural_light_ratio":
                cell = f"{v * 100:g}%"
            elif key in inches:
                cell = f"{v * 12:g}"
            else:
                cell = f"{v:g}"
            cells += f"{cell:>12}"
        rows.append(f"  {label:<28}{cells}")
    return (
        "Built-in jurisdiction profiles\n\n"
        + "\n".join(rows)
        + "\n\nAliases: irc-2021, baseline -> default.\n"
        + "Pass a name to --profile, or a path to a JSON override file "
        + '(e.g. {"extends": "strict", "min_ceiling_height": 8}).\n\n'
        + "NOTE: " + DISCLAIMER
    )
