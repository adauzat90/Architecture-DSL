"""JSON bridge: build diagnostic registry/emitter/tests/docs/example matrix."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import diagnostic_matrix, diagnostic_matrix_markdown, dumps  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    out = diagnostic_matrix(payload.get("paths") or ["examples"], max_hits=payload.get("maxHits", payload.get("max_hits", 6)))
    if payload.get("out"):
        path = ROOT / payload["out"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(diagnostic_matrix_markdown(out), encoding="utf-8")
    print(dumps(out))
