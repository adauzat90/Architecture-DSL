"""JSON bridge: run the barndsl developer doctor gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import doctor, dumps  # noqa: E402

if __name__ == "__main__":
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    out = doctor(
        paths=payload.get("paths"),
        lsp_strict=payload.get("lspStrict", True),
        run_impact=payload.get("runImpact", False),
        export_plan=payload.get("exportPlan"),
        profile=payload.get("profile"),
    )
    print(dumps(out))
    raise SystemExit(0 if out["ok"] else 1)
