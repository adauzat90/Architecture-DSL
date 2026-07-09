"""JSON bridge: catalog high-value .barn fixtures/examples."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import dumps, fixture_catalog, fixture_catalog_markdown  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    out = fixture_catalog(payload.get("paths") or ["examples"])
    if payload.get("out"):
        path = ROOT / payload["out"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fixture_catalog_markdown(out), encoding="utf-8")
    print(dumps(out))
    raise SystemExit(0 if out["ok"] else 1)
