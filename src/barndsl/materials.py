"""A small palette of named architectural materials for the 3D views.

The 2D drawing set speaks in flat fill colours (:mod:`barndsl.render`); the 3D
model wants a little more — a metal roof should read as *metal* (bright, tight
specular), a wood floor as planks, a concrete slab as a speckled matte slab. This
module is that vocabulary: a registry of :class:`Material` records, each carrying
the handful of parameters both the glTF exporter and the inline WebGL viewer need
to shade a surface — a base colour, physically-based ``roughness``/``metallic``
factors, and a procedural ``pattern`` id (with a feature ``scale`` in **feet**)
the renderers turn into a texture.

The DSL never spells materials out directly. Instead the plan carries free-text
*finish hints* — ``finish siding "metal" roof "standing-seam"`` and a per-room
``floor "tile"`` — and this module fuzzy-matches those hints against the palette
(:func:`wall_material`, :func:`roof_material`, :func:`floor_material`). Matching is
deliberately forgiving (substring keywords, not an enum) so an author writes what
they mean; an unmatched *floor* hint is a teachable warning, not an error (see the
validator's ``FLOOR_FINISH`` check). Unset hints fall back to sensible
barndominium defaults: a ribbed-metal shell, a standing-seam metal roof, and a
per-room-type floor (wet rooms tile, garage/shop concrete, everywhere else wood).

Keeping this as data (not colours scattered through the exporter) means the two
renderers stay in lockstep: the glTF ``pbrMetallicRoughness`` factors and the
viewer's shader read the *same* ``roughness``/``metallic``/``pattern`` off one
record, so an external glTF viewer and the built-in one agree on what a surface is.
"""

from __future__ import annotations

from dataclasses import dataclass

from .elements import RoomType

#: The procedural texture families a material can request. Each is a *detail map*
#: the renderers generate at runtime (a corrugation, a plank seam, a grout grid…)
#: and modulate the base colour with — never an image file, so everything stays
#: offline. ``none`` is a flat, untextured surface.
PATTERNS = ("rib", "batten", "shingle", "plank", "tile", "speckle", "none")


@dataclass(frozen=True)
class Material:
    """A named surface finish: base colour + PBR factors + a procedural pattern.

    ``color`` is an authoring-friendly ``#rrggbb`` sRGB hex (converted to a linear
    ``baseColorFactor`` on emit, exactly like the plan palette). ``roughness`` and
    ``metallic`` are the standard 0–1 metallic-roughness factors. ``pattern`` is
    one of :data:`PATTERNS`, and ``pattern_scale`` is the world size in **feet** of
    one texture repeat — metal ribs every ``0.75`` ft, plank seams every ``0.5`` ft
    — so features read at true scale on the geometry (which is modelled in feet).
    """

    name: str
    color: str  # #rrggbb sRGB, the authoring colour
    roughness: float
    metallic: float
    pattern: str
    pattern_scale: float = 1.0  # feet per texture repeat


#: The palette, keyed by a stable id. Colours are muted, drawing-set tones — the
#: 3D model is schematic, not a photoreal render, so metals are light greys rather
#: than mirror chrome and finishes read as their *type*, not a product swatch.
PALETTE: dict[str, Material] = {
    # --- exterior shell -------------------------------------------------------
    "metal_siding": Material("metal siding", "#B9BEC4", 0.35, 0.85, "rib", 0.75),
    "board_batten": Material("board & batten", "#D8D2C3", 0.80, 0.0, "batten", 1.0),
    "lap_siding": Material("lap siding", "#CEC8BB", 0.75, 0.0, "shingle", 0.55),
    # --- roof -----------------------------------------------------------------
    "standing_seam": Material("standing-seam metal", "#767C82", 0.30, 0.90, "rib", 1.5),
    "asphalt_shingle": Material("asphalt shingle", "#6B5C4B", 0.90, 0.0, "shingle", 1.0),
    # --- floors ---------------------------------------------------------------
    "concrete_slab": Material("concrete slab", "#B7B4AE", 0.85, 0.0, "speckle", 3.0),
    "wood_plank": Material("wood plank floor", "#B08A55", 0.55, 0.0, "plank", 0.5),
    "tile_floor": Material("tile floor", "#D7D1C4", 0.40, 0.0, "tile", 1.5),
    "carpet": Material("carpet", "#B4AA9C", 1.0, 0.0, "speckle", 0.3),
    # --- interior / structure / glazing --------------------------------------
    "drywall": Material("drywall", "#E7E3DA", 0.90, 0.0, "none", 1.0),
    "glass": Material("glass", "#AFC7D6", 0.10, 0.0, "none", 1.0),
    "timber_frame": Material("timber frame", "#8A5E2E", 0.70, 0.0, "plank", 1.2),
    # --- fixtures & furnishings ----------------------------------------------
    "porcelain": Material("porcelain", "#F3F4F2", 0.20, 0.0, "none", 1.0),
    "stainless": Material("stainless steel", "#C4C8CC", 0.35, 0.80, "none", 1.0),
    "fabric": Material("upholstery", "#9BA3A8", 0.95, 0.0, "speckle", 0.25),
}

#: Fixture/furnishing materials the 3D exporter lowers each fixture kind onto.
FIXTURE_PORCELAIN = PALETTE["porcelain"]
FIXTURE_STAINLESS = PALETTE["stainless"]
FIXTURE_FABRIC = PALETTE["fabric"]
FIXTURE_WOOD = PALETTE["wood_plank"]

#: The barndominium defaults, used when a finish hint is unset. A metal shell and a
#: standing-seam metal roof are the archetypal barndominium finishes.
DEFAULT_WALL = PALETTE["metal_siding"]
DEFAULT_ROOF = PALETTE["standing_seam"]

#: Non-authored structural/finish surfaces the exporter lowers directly (no hint).
FRAME_MATERIAL = PALETTE["timber_frame"]
SLAB_MATERIAL = PALETTE["concrete_slab"]
STAIR_MATERIAL = PALETTE["wood_plank"]
PORCH_MATERIAL = PALETTE["wood_plank"]
#: The header/sill boxes over and under openings: a painted interior reveal.
OPENING_MATERIAL = PALETTE["drywall"]

# --- fuzzy hint → material resolution ----------------------------------------

#: Substring keyword → palette key, tried in order (first hit wins). Ordering
#: matters where words overlap: "metal roof" is standing-seam, "wood siding" is lap.
_WALL_KEYWORDS: tuple[tuple[str, str], ...] = (
    # "batten" first (board-and-batten); "clapboard"/"lap"/"cedar"/"wood" before the
    # bare "board" so "clapboard" and "cedar siding" resolve to lap, not board.
    ("batten", "board_batten"),
    ("clapboard", "lap_siding"),
    ("lap", "lap_siding"),
    ("cedar", "lap_siding"),
    ("wood", "lap_siding"),
    ("board", "board_batten"),
    ("metal", "metal_siding"),
    ("steel", "metal_siding"),
    ("corrugat", "metal_siding"),
    ("rib", "metal_siding"),
    ("panel", "metal_siding"),
    ("tin", "metal_siding"),
)

_ROOF_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("seam", "standing_seam"),
    ("standing", "standing_seam"),
    ("shingle", "asphalt_shingle"),
    ("asphalt", "asphalt_shingle"),
    ("comp", "asphalt_shingle"),
    ("metal", "standing_seam"),
    ("steel", "standing_seam"),
    ("tin", "standing_seam"),
)

_FLOOR_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("tile", "tile_floor"),
    ("ceramic", "tile_floor"),
    ("porcelain", "tile_floor"),
    ("concrete", "concrete_slab"),
    ("slab", "concrete_slab"),
    ("polished", "concrete_slab"),
    ("epoxy", "concrete_slab"),
    ("carpet", "carpet"),
    ("rug", "carpet"),
    ("plank", "wood_plank"),
    ("wood", "wood_plank"),
    ("hardwood", "wood_plank"),
    ("oak", "wood_plank"),
    ("pine", "wood_plank"),
    ("laminate", "wood_plank"),
    ("vinyl", "wood_plank"),
    ("lvp", "wood_plank"),
    ("lvt", "wood_plank"),
)

#: Room types whose default (unhinted) floor is a wet-room tile.
_WET_ROOMS = frozenset(
    {
        RoomType.BATHROOM,
        RoomType.HALF_BATH,
        RoomType.LAUNDRY,
        RoomType.MUDROOM,
        RoomType.UTILITY,
    }
)
#: Room types that default to a bare concrete slab.
_SLAB_ROOMS = frozenset({RoomType.GARAGE, RoomType.SHOP})


def _match(hint: str | None, table: tuple[tuple[str, str], ...]) -> Material | None:
    """First palette material whose keyword is a substring of ``hint`` (or None)."""
    if not hint:
        return None
    low = hint.lower()
    for keyword, key in table:
        if keyword in low:
            return PALETTE[key]
    return None


def wall_material(plan) -> Material:
    """The exterior-wall material for ``plan`` from its ``siding`` hint.

    Falls back to :data:`DEFAULT_WALL` (ribbed metal) when the hint is unset or
    matches nothing — a barndominium's default shell.
    """
    return _match(getattr(plan, "siding", None), _WALL_KEYWORDS) or DEFAULT_WALL


def roof_material(plan) -> Material:
    """The roof material for ``plan`` from its ``roofing`` hint.

    Falls back to :data:`DEFAULT_ROOF` (standing-seam metal) when unset/unmatched.
    """
    return _match(getattr(plan, "roofing", None), _ROOF_KEYWORDS) or DEFAULT_ROOF


def match_floor(hint: str | None) -> Material | None:
    """Fuzzy-match a room ``floor`` hint to a palette material, or None if unset/
    unmatched. Exposed so the validator can warn on an unrecognised name."""
    return _match(hint, _FLOOR_KEYWORDS)


def floor_material(room) -> Material:
    """The floor material for ``room``: its ``floor`` hint if it matches, else a
    default by room type (wet rooms tile, garage/shop concrete, else wood plank)."""
    matched = match_floor(getattr(room, "floor", None))
    if matched is not None:
        return matched
    if room.type in _SLAB_ROOMS:
        return PALETTE["concrete_slab"]
    if room.type in _WET_ROOMS:
        return PALETTE["tile_floor"]
    return PALETTE["wood_plank"]


def known_floor_names() -> list[str]:
    """The floor-finish keywords the fuzzy matcher recognises, for friendly hints."""
    return sorted({keyword for keyword, _ in _FLOOR_KEYWORDS})
