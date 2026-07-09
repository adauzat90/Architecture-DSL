"""JSON bridge: smoke-test pure LSP helpers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import dumps, lsp_smoke  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    out = lsp_smoke(payload.get("path"), strict_composed=payload.get("strictComposed", False))
    print(dumps(out))
    raise SystemExit(0 if out["ok"] else 1)
