"""JSON bridge: run :mod:`barndsl.devtools` repo audit."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.devtools import dumps, repo_audit  # noqa: E402

if __name__ == "__main__":
    out = repo_audit()
    print(dumps(out))
    raise SystemExit(0 if out["ok"] else 1)
