"""Diagnostic output must not depend on ``PYTHONHASHSEED``.

Several design-quality checks name a neighbouring room picked out of an
adjacency ``set`` (BED_PRIVACY, ALARM_HALL, PRIVATE_PASSTHROUGH) or emit one
issue per set member (GARAGE_BEDROOM, BATH_OVERSIZE). A ``set``'s iteration
order varies with the process hash seed, so without a deterministic tie-break
the message text — and the issue ordering — would drift run to run. These
subprocess runs compile one fixture that triggers every such site under three
different seeds and assert the diagnostics come out byte-identical.
"""

import os
import subprocess
import sys

# A bedroom opening onto two public rooms (BED_PRIVACY must name a stable one),
# a garage opening into two bedrooms (GARAGE_BEDROOM order must be stable), and
# a garage reachable only through a bedroom (PRIVATE_PASSTHROUGH names a stable
# gateway) — every set-derived diagnostic site in one plan.
_FIXTURE = """\
plan "Determinism"
envelope 48 x 32
ceiling 9
room living: living at 0,0 size 24 x 16
room kitchen: kitchen at 24,0 size 24 x 16
room bed_a: bedroom at 0,16 size 12 x 16
room bed_b: bedroom at 12,16 size 12 x 16
room garage: garage at 24,16 size 24 x 16
entry living south width 3 offset 4
door living - kitchen
door living - bed_a
door kitchen - bed_a
door garage - bed_a
door garage - bed_b
"""


def _compile(path: str, seed: str) -> str:
    """Compile ``path`` in a fresh process with ``PYTHONHASHSEED=seed`` set."""
    env = dict(os.environ)
    src_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONHASHSEED"] = seed
    proc = subprocess.run(
        [sys.executable, "-m", "barndsl.cli", "compile", path],
        capture_output=True, text=True, env=env,
    )
    return proc.stdout + proc.stderr


def test_diagnostics_are_hash_seed_independent(tmp_path):
    plan = tmp_path / "determ.barn"
    plan.write_text(_FIXTURE)

    outputs = {seed: _compile(str(plan), seed) for seed in ("0", "1", "42")}

    # The set-derived diagnostics must actually fire, or the test proves nothing.
    baseline = outputs["0"]
    assert "BED_PRIVACY" in baseline
    assert "GARAGE_BEDROOM" in baseline
    assert "PRIVATE_PASSTHROUGH" in baseline

    assert outputs["0"] == outputs["1"] == outputs["42"], (
        "diagnostics differ across PYTHONHASHSEED values:\n"
        f"--- seed 0 ---\n{outputs['0']}\n--- seed 1 ---\n{outputs['1']}\n"
        f"--- seed 42 ---\n{outputs['42']}"
    )
