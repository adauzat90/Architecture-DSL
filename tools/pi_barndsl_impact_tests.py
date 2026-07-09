"""JSON bridge: map changed files to likely pytest targets and optionally run them."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import dumps, impact_tests  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    out = impact_tests(payload.get("changed"), run=payload.get("run", False), quiet=payload.get("quiet", True))
    print(dumps(out))
    raise SystemExit(int(out.get("exit", 0) or 0))
