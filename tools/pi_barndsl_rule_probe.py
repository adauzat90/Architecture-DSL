"""JSON bridge: inline compile/assert helper for developing diagnostics."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import dumps, rule_probe  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin)
    out = rule_probe(
        payload.get("source", ""),
        payload.get("expect"),
        name=payload.get("name"),
        profile=payload.get("profile"),
    )
    print(dumps(out))
    raise SystemExit(0 if out["ok"] else 1)
