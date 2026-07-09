"""JSON bridge: generate lightweight scaffolds/checklists."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import dumps, feature_scaffold, rule_scaffold  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin)
    if payload.get("kind") == "rule":
        out = rule_scaffold(
            payload["code"],
            payload.get("severity", "info"),
            payload.get("testFile"),
            write=payload.get("write", False),
        )
    elif payload.get("kind") == "feature":
        out = feature_scaffold(
            payload["name"],
            payload.get("out"),
            write=payload.get("write", True),
        )
    else:
        raise SystemExit("payload.kind must be 'rule' or 'feature'")
    print(dumps(out))
    raise SystemExit(0)
