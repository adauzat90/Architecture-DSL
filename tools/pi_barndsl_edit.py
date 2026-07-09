"""Small JSON bridge for pi's barndsl_edit extension tool.

Reads one JSON object from stdin:

    {"path": "plan.barn", "edit": {"kind": "move_room", ...}}

Applies :func:`barndsl.edits.apply_edit` and writes a JSON result.  This stays
in Python so the pi TypeScript extension can reuse the project's own DSL-aware
edit engine instead of re-implementing source surgery in Node.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

# Running from an editable checkout should work even before `pip install -e .`.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from barndsl.edits import Edit, apply_edit  # noqa: E402


def _clean_edit(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {f.name for f in fields(Edit)}
    return {k: v for k, v in raw.items() if k in allowed}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        rel = payload.get("path")
        raw_edit = payload.get("edit")
        if not isinstance(rel, str) or not rel:
            raise ValueError("payload.path must be a non-empty string")
        if not isinstance(raw_edit, dict):
            raise ValueError("payload.edit must be an object")

        path = Path(rel[1:] if rel.startswith("@") else rel)
        if not path.is_absolute():
            path = Path.cwd() / path
        source = path.read_text(encoding="utf-8")
        edit = Edit(**_clean_edit(raw_edit))
        result = apply_edit(source, edit, base_dir=str(path.parent))

        if result.ok and result.changed:
            path.write_text(result.source, encoding="utf-8")

        out: dict[str, Any] = {
            "ok": result.ok,
            "changed": result.changed,
            "line": result.line,
            "summary": result.summary,
            "placed": result.placed,
            "path": os.path.relpath(path, Path.cwd()),
        }
        if result.error is not None:
            out["error"] = {"kind": result.error.kind, "message": result.error.message}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if result.ok else 2
    except Exception as exc:  # keep the bridge exception-free for the extension
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
