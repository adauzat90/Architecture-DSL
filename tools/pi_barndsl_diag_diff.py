"""JSON bridge: diagnostic-code diff across plan sets."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import diagnostic_diff, dumps  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin)
    out = diagnostic_diff(
        payload.get("before") or [],
        payload.get("after"),
        profile=payload.get("profile"),
    )
    print(dumps(out))
    raise SystemExit(0)
