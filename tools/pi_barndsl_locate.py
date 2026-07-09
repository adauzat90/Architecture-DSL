"""JSON bridge: locate codebase breadcrumbs for a barndsl concept."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import dumps, locate  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    out = locate(payload["query"], max_results=payload.get("maxResults", payload.get("max_results", 80)))
    print(dumps(out))
    raise SystemExit(0 if out["ok"] else 1)
