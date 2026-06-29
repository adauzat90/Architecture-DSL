"""A starter `.barn` plan for `barndsl new` — a known-clean scaffold to edit.

The template is a trimmed version of the verified-clean cottage gallery plan, so
a newcomer's first ``barndsl compile`` lands on 0/0/0 and they have a working
shape to modify rather than a blank file.
"""

from __future__ import annotations

_TEMPLATE = '''\
# {name} — a starter barndominium plan. Edit freely, then `barndsl compile` it.
# Measurements are in feet; origin (0,0) is the south-west corner (x→east, y→north).
plan "{name}"
envelope 33 x 24
ceiling 9
program 1 bed 1 bath

room living:  living   at 0,0    size 18 x 14
room kitchen: kitchen  north-of living size 18 x 10
room hall:    hallway  at 18,0   size 4 x 24
room bed:     bedroom  at 22,0   size 11 x 12
room closet:  closet   north-of bed size 11 x 3
room bath:    bathroom at 22,15  size 11 x 9

open living - kitchen width 8
door living - hall width 3 offset 0.5
door hall - bed width 3 offset 0.5
door hall - bath width 2.67 offset 5.83
door bed - closet width 2.5 offset 0.5

entry living south width 3 offset 13
entry kitchen west width 3 offset 3
window living south width 8 offset 2
window kitchen north width 6 offset 6
window bed  south width 4 offset 3
window bath north width 3 offset 4
'''


def starter_dsl(name: str = "My Barndo") -> str:
    """Return starter DSL source for a plan named ``name``."""
    # Strip quotes the user might wrap the name in; the template quotes it itself.
    clean = name.strip().strip('"')
    return _TEMPLATE.format(name=clean)
