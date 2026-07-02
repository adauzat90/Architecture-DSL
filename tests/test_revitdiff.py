"""`barndsl revit-diff`: drift between a Revit model export and the authored plan."""

from __future__ import annotations

import json

from barndsl import compile_source, diff_plans, diff_text, to_revit_model
from barndsl.cli import main
from barndsl.revitdiff import load_diff_input

# A fully-connected four-room plan that compiles clean (no errors, no recovery).
BASE = """\
plan "P"
envelope 40 x 12
ceiling 9
room a: living at 0,0 size 10 x 12
room b: kitchen at 10,0 size 10 x 12
room c: dining at 20,0 size 10 x 12
room d: office at 30,0 size 10 x 12
door a - b width 3
door b - c width 3
door c - d width 3
entry a west width 3
window b south width 4 offset 3
"""


def _res(src):
    r = compile_source(src)
    assert r.ok, [e.code for e in r.errors]
    return r


# --- identical -----------------------------------------------------------------


def test_identical_plans_show_no_drift():
    d = diff_plans(_res(BASE), _res(BASE))
    assert d["drift"] is False
    assert d["totals"]["added"] == 0
    assert d["totals"]["removed"] == 0
    assert d["totals"]["moved"] == 0
    assert d["totals"]["changed"] == 0
    assert "No drift" in diff_text(d)


# --- moved room + resized window ----------------------------------------------


def test_moved_room_and_resized_window_report_deltas():
    # Slide the a/b shared wall +2 ft (a grows, b shrinks and shifts) and widen
    # the window on b from 4 to 6 — both tiling-preserving so only these change.
    model_src = (
        BASE.replace("room a: living at 0,0 size 10 x 12", "room a: living at 0,0 size 12 x 12")
        .replace("room b: kitchen at 10,0 size 10 x 12", "room b: kitchen at 12,0 size 8 x 12")
        .replace("window b south width 4 offset 3", "window b south width 6 offset 1")
    )
    d = diff_plans(_res(model_src), _res(BASE), names=("model", "authored"))
    assert d["drift"] is True

    rooms_moved = {e["id"]: e for e in d["kinds"]["rooms"]["moved"]}
    # Room a keeps its origin but grows 2 ft wide; room b shifts +2 and shrinks.
    assert rooms_moved["a"]["deltas"]["width"]["delta"] == 2
    assert rooms_moved["b"]["deltas"]["x"]["delta"] == 2
    assert rooms_moved["b"]["deltas"]["width"]["delta"] == -2

    win_moved = d["kinds"]["windows"]["moved"]
    assert len(win_moved) == 1
    assert win_moved[0]["deltas"]["width"]["delta"] == 2

    text = diff_text(d)
    assert "Windows:" in text and "width 4→6" in text


# --- added / removed door ------------------------------------------------------


def test_added_door_detected():
    # The model has an extra exterior door on room d; exterior doors number last,
    # so the added door is a clean append (o4) with no id aliasing.
    model_src = BASE + "entry d east width 3\n"
    d = diff_plans(_res(model_src), _res(BASE), names=("model", "authored"))
    added = d["kinds"]["doors"]["added"]
    assert len(added) == 1
    assert d["kinds"]["doors"]["removed"] == []
    assert "added" in diff_text(d)


def test_removed_door_detected():
    # Reverse: the authored plan has the extra door, the model dropped it.
    authored_src = BASE + "entry d east width 3\n"
    d = diff_plans(_res(BASE), _res(authored_src), names=("model", "authored"))
    removed = d["kinds"]["doors"]["removed"]
    assert len(removed) == 1
    assert d["kinds"]["doors"]["added"] == []


# --- kind change ---------------------------------------------------------------


def test_room_kind_change_detected():
    # Same room id, different room type → a "changed" (rekinded) room.
    model_src = BASE.replace("room d: office", "room d: loft")
    d = diff_plans(_res(model_src), _res(BASE), names=("model", "authored"))
    changed = {e["id"]: e for e in d["kinds"]["rooms"]["changed"]}
    assert "d" in changed
    assert changed["d"]["changes"]["kind"] == {"from": "office", "to": "loft"}


# --- position fallback when ids differ ----------------------------------------


def test_position_fallback_matches_renamed_room():
    # A single-room plan; the model renames the room (different stable id) and
    # nudges it 1 ft. Id-matching fails, so the room is matched by centroid and
    # reported as moved "by position" — not as an add + remove.
    authored = """\
plan "S"
envelope 20 x 21
ceiling 9
room living: living at 0,0 size 20 x 20
entry living west width 3
"""
    model = """\
plan "S"
envelope 20 x 21
ceiling 9
room lounge: living at 0,1 size 20 x 20
entry lounge west width 3
"""
    d = diff_plans(_res(model), _res(authored), names=("model", "authored"))
    rooms = d["kinds"]["rooms"]
    assert rooms["added"] == [] and rooms["removed"] == []
    assert len(rooms["moved"]) == 1
    moved = rooms["moved"][0]
    assert moved["matched_by"] == "position"
    assert moved["deltas"]["y"]["delta"] == 1
    assert moved["authored_id"] == "living"
    assert "by position" in diff_text(d)


# --- exchange-dict input vs .barn input parity --------------------------------


def test_exchange_dict_and_barn_inputs_agree():
    res = _res(BASE)
    ex = to_revit_model(res.plan).to_dict()

    d_barn = diff_plans(res, res)
    d_dict = diff_plans(ex, ex)
    assert d_barn["kinds"] == d_dict["kinds"]
    assert d_dict["drift"] is False

    # Cross: a plan against its own lowered exchange is a fixed point → no drift.
    d_cross = diff_plans(ex, res)
    assert d_cross["drift"] is False


# --- determinism ---------------------------------------------------------------


def test_diff_is_json_able_and_deterministic():
    model_src = BASE + "entry d east width 3\n"
    one = diff_plans(_res(model_src), _res(BASE))
    two = diff_plans(_res(model_src), _res(BASE))
    assert one == two
    assert json.loads(json.dumps(one)) == one


def test_score_delta_present_when_both_compile():
    d = diff_plans(_res(BASE), _res(BASE))
    assert d["score"] is not None
    assert d["score"]["delta"] == 0


# --- CLI -----------------------------------------------------------------------


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_cli_no_drift_exit_zero(tmp_path, capsys):
    a = _write(tmp_path, "a.barn", BASE)
    b = _write(tmp_path, "b.barn", BASE)
    assert main(["revit-diff", a, b]) == 0
    out = capsys.readouterr().out
    assert "Drift: a.barn vs b.barn" in out
    assert "No drift" in out


def test_cli_drift_exit_one_and_json(tmp_path, capsys):
    model = _write(tmp_path, "model.barn", BASE + "entry d east width 3\n")
    authored = _write(tmp_path, "authored.barn", BASE)
    assert main(["revit-diff", model, authored]) == 1
    capsys.readouterr()

    assert main(["revit-diff", model, authored, "--json"]) == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["drift"] is True
    assert parsed["totals"]["added"] == 1


def test_cli_json_exchange_input(tmp_path, capsys):
    # A .json barndsl.revit/1 exchange on either side is accepted.
    res = _res(BASE)
    ex = to_revit_model(res.plan).to_json()
    model = _write(tmp_path, "model.json", ex)
    authored = _write(tmp_path, "authored.barn", BASE)
    assert main(["revit-diff", model, authored]) == 0


def test_cli_uncompilable_barn_exit_two(tmp_path, capsys):
    good = _write(tmp_path, "good.barn", BASE)
    broken = _write(tmp_path, "broken.barn", "not a plan at all\n")
    assert main(["revit-diff", broken, good]) == 2
    assert main(["revit-diff", good, broken]) == 2


def test_cli_bad_json_exit_two(tmp_path, capsys):
    good = _write(tmp_path, "good.barn", BASE)
    notex = _write(tmp_path, "notex.json", json.dumps({"schema": "something/else"}))
    assert main(["revit-diff", notex, good]) == 2


def test_cli_tolerance_flag_changes_verdict(tmp_path, capsys):
    # A 0.3 ft shift of the a/b wall: within the 0.5 ft default (no drift), but a
    # tighter --tolerance 0.1 surfaces it.
    model_src = (
        BASE.replace("room a: living at 0,0 size 10 x 12", "room a: living at 0,0 size 10.3 x 12")
        .replace("room b: kitchen at 10,0 size 10 x 12", "room b: kitchen at 10.3,0 size 9.7 x 12")
    )
    model = _write(tmp_path, "model.barn", model_src)
    authored = _write(tmp_path, "authored.barn", BASE)
    assert main(["revit-diff", model, authored]) == 0  # default 0.5 ft tolerance
    capsys.readouterr()
    assert main(["revit-diff", model, authored, "--tolerance", "0.1"]) == 1


def test_load_diff_input_sniffs_content(tmp_path):
    # No .json/.barn extension: a leading "{" reads as JSON, else as .barn source.
    res = _res(BASE)
    ex = to_revit_model(res.plan).to_json()
    j = tmp_path / "exchange.txt"
    j.write_text(ex, encoding="utf-8")
    assert isinstance(load_diff_input(str(j)), dict)

    b = tmp_path / "plan.txt"
    b.write_text(BASE, encoding="utf-8")
    loaded = load_diff_input(str(b))
    assert loaded.plan is not None


# --- review fixes: positional ids, malformed input, negative tolerance ---------


def test_deleting_the_first_window_does_not_alias_the_rest():
    """Opening ids are positional, so id-matching after a non-last delete pairs
    different physical windows and fabricates moves. Position-first matching
    must report exactly one removed window and NO moves."""
    three = BASE.replace(
        "window b south width 4 offset 3",
        "window b south width 4 offset 3\n"
        "window c south width 4 offset 3\n"
        "window d south width 4 offset 3",
    )
    model = three.replace("window b south width 4 offset 3\n", "")
    d = diff_plans(_res(model), _res(three))
    w = d["kinds"]["windows"]
    assert len(w["removed"]) == 1
    assert w["moved"] == []
    assert w["unchanged"] == 2
    # The removed one is reported at the FIRST window's position (on room b).
    assert 10 <= w["removed"][0]["at"][0] <= 20


def test_negative_tolerance_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        diff_plans(_res(BASE), _res(BASE), tolerance=-1)


def test_cli_rejects_a_schema_valid_but_malformed_exchange(tmp_path, capsys):
    bad = tmp_path / "model.json"
    bad.write_text(
        json.dumps({"schema": "barndsl.revit/1", "rooms": "not-a-list"}),
        encoding="utf-8",
    )
    plan = _write(tmp_path, "p.barn", BASE)
    assert main(["revit-diff", str(bad), str(plan)]) == 2
    err = capsys.readouterr().err
    assert "error" in err.lower() and "Traceback" not in err


def test_score_line_uses_the_given_names():
    model = BASE.replace("window b south width 4 offset 3\n", "")
    d = diff_plans(_res(model), _res(BASE), names=("edited.json", "source.barn"))
    text = diff_text(d)
    if d.get("score") is not None:
        assert "Score: source.barn" in text and "edited.json" in text


def test_big_move_note_explains_add_remove_pairs():
    # Move the window far beyond the match radius: it reads as removed+added,
    # and the text says so.
    model = BASE.replace(
        "window b south width 4 offset 3", "window d north width 4 offset 3"
    )
    d = diff_plans(_res(model), _res(BASE))
    w = d["kinds"]["windows"]
    assert len(w["added"]) == 1 and len(w["removed"]) == 1
    assert "reads as one removed + one added" in diff_text(d)
