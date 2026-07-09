# Fixture catalog

Generated from `barndsl dev fixtures`. Prefer these fixtures before inventing new large plans in tests.

## Roles

| Role | Path/source | Purpose |
| --- | --- | --- |
| `minimal_valid_inline` | inline source | Smallest useful whole-plan smoke fixture for parser/diagnostic probes. |
| `canonical_example` | examples/cedar_ridge.barn | Full hand-authored sample with house + shop geometry. |
| `gallery_export` | examples/gallery/lshape.barn | Stable whole plan for render/export parity and scoring checks. |
| `composed_smoke` | examples/composed/cedar_ridge.barn | Composition/stamped-id smoke fixture with accepted deviations. |
| `composed_nested_parametric` | examples/composed/cedar_ridge_v2.barn | Nested use/parametric/multi-level composition fixture. |
| `multi_level` | examples/gallery/two_story.barn | Two-level stair/loft/alarm fixture. |
| `fragment_part` | examples/composed/parts/master_suite.barn | Headerless part fixture; compile as fragment. |
| `parametric_part` | examples/composed/parts/flex_bath.barn | Part with params for `use ... with` coverage. |
| `shop_loft_part` | examples/composed/parts/shop_loft.barn | Multi-level shop/loft fragment for composed export and validation. |

## Files

| Path | Kind | Features | Codes | Score | Purpose |
| --- | --- | --- | --- | --- | --- |
| examples/cedar_ridge.barn | whole | whole_plan, shop | ALARM_CO:1, BED_SOUND:2, DOOR_CENTERED:3, ENVELOPE_MODULE:1, GARAGE_NO_ENTRY:1, GARAGE_SEPARATION:1, HALL_DEADEND:1, HALL_TIGHT:1, LOW_STORAGE:1, NAT_LIGHT:1, NO_BACK_DOOR:1, NO_CLOSET:3, ROOM_CLEAR:1, SHOP_DEPTH:1, VENT_AREA:1, WINDOW_TEMPERED:1 | 54.7 | canonical whole-plan example |
| examples/composed/cedar_ridge.barn | whole | whole_plan, composed, accepted_diagnostics | BATH_VENT:1, BED_PRIVACY:1, HALL_DEADEND:1 | 100.0 | composition host fixture |
| examples/composed/cedar_ridge_v2.barn | whole | whole_plan, composed, multi_level, shop, accepted_diagnostics | ALARM_CO:1, BATH_VENT:2, BED_PRIVACY:1, DOOR_CENTERED:3, ENVELOPE_MODULE:1, GARAGE_DOOR:1, GARAGE_SEPARATION:1, LOFT_GUARD:1, LOW_STORAGE:1, NO_BACK_DOOR:1, NO_CLOSET:1, STAIR_HANDRAIL:1 | 82.0 | composition host fixture |
| examples/composed/parts/bath_core.barn | fragment | fragment | — | — | headerless composition part fixture |
| examples/composed/parts/flex_bath.barn | fragment | fragment, parametric_part | — | — | headerless composition part fixture |
| examples/composed/parts/guest_wing.barn | fragment | fragment, composed | — | — | headerless composition part fixture |
| examples/composed/parts/kitchen_l.barn | fragment | fragment | — | — | headerless composition part fixture |
| examples/composed/parts/laundry_core.barn | fragment | fragment | — | — | headerless composition part fixture |
| examples/composed/parts/master_suite.barn | fragment | fragment | — | — | headerless composition part fixture |
| examples/composed/parts/shop_loft.barn | fragment | fragment, parametric_part, multi_level, shop | — | — | headerless composition part fixture |
| examples/frame_demo.barn | whole | whole_plan, shop | ALARM_CO:1, BED_SOUND:2, DOOR_CENTERED:3, DOOR_NO_LANDING:1, ENVELOPE_MODULE:1, GARAGE_NO_ENTRY:1, GARAGE_SEPARATION:1, HALL_DEADEND:1, HALL_TIGHT:1, LOW_STORAGE:1, NAT_LIGHT:1, NO_CLOSET:3, ROOM_CLEAR:1, SHOP_DEPTH:1, VENT_AREA:1, WINDOW_TEMPERED:1 | 46.7 | example plan fixture |
| examples/gallery/cottage.barn | whole | whole_plan | — | 99.2 | gallery quality fixture |
| examples/gallery/hall_spine.barn | whole | whole_plan | — | 99.3 | gallery quality fixture |
| examples/gallery/homestead.barn | whole | whole_plan | — | 99.3 | gallery quality fixture |
| examples/gallery/lshape.barn | whole | whole_plan, export_parity_candidate | — | 100.0 | export/render parity fixture |
| examples/gallery/two_story.barn | whole | whole_plan, multi_level, accepted_diagnostics | STAIR_HANDRAIL:1 | 100.0 | multi-level stair/loft fixture |
| examples/lshape.barn | whole | whole_plan, export_parity_candidate | ALARM_CO:1, BATH_DISTANCE:2, BATH_VENT:1, BED_PRIVACY:1, BED_SOUND:1, DOOR_CENTERED:7, DOOR_NO_LANDING:1, DOOR_SWING_UNSET:2, DOOR_THRESHOLD:1, ENVELOPE_MODULE:1, HALL_DEADEND:1, KITCHEN_TRIANGLE:1, NO_BACK_DOOR:1, NO_CLOSET:2 | 79.1 | example plan fixture |
