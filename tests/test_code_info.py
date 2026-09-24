"""Everything about a diagnostic code lives on its registry entry (TD-6b).

A new code used to mean editing four places: the registry, the ``_VARYING``
set, ``compose.PART_LOCAL_CODES`` and ``pragma.ACCEPT_DENIED_CODES``. Now the
entry carries a general fix hint (as ADR 0002 promised), every severity the code
can fire at, and whether a part reports it for itself or a pragma may waive it.
The old names are derived from the entries.
"""

from __future__ import annotations

import pytest

from barndsl import compile_source
from barndsl.compose import PART_LOCAL_CODES
from barndsl.diagnostics import REGISTRY, CodeInfo, explain
from barndsl.issues import Severity
from barndsl.pragma import ACCEPT_DENIED_CODES


def test_every_entry_has_a_general_hint():
    for code, info in REGISTRY.items():
        assert len(info.hint.strip()) >= 20, code


def test_an_entry_must_allow_its_usual_severity():
    with pytest.raises(ValueError, match="usual severity"):
        CodeInfo("X_CODE", Severity.ERROR, "Title", "An explanation long enough.",
                 "A hint long enough to pass.", frozenset({Severity.WARNING}))


def test_explain_shows_the_hint_and_every_severity():
    text = explain("NO_ACCESS")
    assert text.startswith("NO_ACCESS [error, or warning by context]")
    assert f"How to fix: {REGISTRY['NO_ACCESS'].hint}" in text
    assert explain("DUP_ID").startswith("DUP_ID [error] ")


def test_the_old_code_sets_are_read_off_the_entries():
    assert PART_LOCAL_CODES == {c for c, i in REGISTRY.items() if i.part_local}
    assert ACCEPT_DENIED_CODES == {c for c, i in REGISTRY.items() if i.accept_denied}
    # A room's own shape is the part's business; reachability depends on placement.
    assert {"DUP_ID", "OVERLAP", "ROOM_SIZE"} <= PART_LOCAL_CODES
    assert "NO_ACCESS" not in PART_LOCAL_CODES
    assert ACCEPT_DENIED_CODES == {"GARAGE_PASSTHROUGH"}


def test_the_severity_a_code_fires_at_is_one_its_entry_allows():
    # A closet with no way in is a warning, a bedroom an error: both allowed.
    src = """\
plan "P"
envelope 30 x 20
ceiling 9
room living: living at 0,0 size 20 x 20
room bed: bedroom at 20,0 size 10 x 12
room clo: closet at 20,12 size 10 x 8
entry living south width 3
"""
    fired = {(d.code, d.severity) for d in compile_source(src).diagnostics if d.code == "NO_ACCESS"}
    assert fired == {("NO_ACCESS", Severity.ERROR), ("NO_ACCESS", Severity.WARNING)}
    assert {s for _, s in fired} <= REGISTRY["NO_ACCESS"].severities
