"""Translate a Revit build log into standard diagnostics.

The pyRevit *Build Plan* button writes a ``*.buildlog.json`` sidecar recording
every element's outcome (created / kept / skipped / failed, with reasons —
see ``revit/barndsl.extension/lib/barndsl_revit/report.py``). Nothing read it
back until now. :func:`buildlog_issues` turns that log into the same
:class:`~barndsl.issues.Issue` shape the compiler emits, so the target
Revit environment itself becomes part of the compile-fix loop:

- a **failed** element (an API error) is a ``REVIT_FAIL`` warning;
- a **skipped** element (missing level/host/family — the element is absent
  from the model) is a ``REVIT_SKIP`` warning;
- a build **note** that a stand-in was used (no garage-door family, no
  matching kind family, a gable profile that fell back flat) is a
  ``REVIT_NOTE`` info — the model built, but not the way the plan asked.

``barndsl revit-log BUILDLOG.json`` prints them like a compile report (and
``--json`` emits the same stable dict shape as ``compile --json``), so an
agent can fold "what the template couldn't build" into its next revision and
CI can gate on "everything built".
"""

from __future__ import annotations

import json
from typing import Any

from .issues import Issue, Severity

#: Build-note fragments that mean "a stand-in was used / a pass degraded" —
#: worth surfacing to the author; other notes are progress chatter.
_NOTE_MARKERS = (
    "stands in",
    "fallback",
    "falls back",
    "not found in project",
    "matched no",
    "no window family reads",
    "no door family reads",
    "purged",
    # Sizing/placement degradations: the element exists at the wrong size or
    # orientation — exactly "built, but not the way the plan asked".
    "no settable",
    "could not size",
    "default size",
    "not applied",
)


def _hint_for(message: str) -> str | None:
    """A fix hint for the common skip/fail reasons, matched on the message."""
    text = (message or "").lower()
    if "family" in text or "type" in text:
        return (
            "Load a matching family/type into the project template, or map the "
            "pass to a named one in the config.json sidecar."
        )
    if "no level" in text:
        return "The exchange references a level the project lacks; rebuild levels or re-export."
    if "no host wall" in text:
        return (
            "The opening's wall was not built (see its own record); fix the "
            "wall first and the opening will follow."
        )
    return None


def buildlog_issues(log: dict[str, Any]) -> list[Issue]:
    """The diagnostics a ``*.buildlog.json`` implies (pure, deterministic).

    Order: failures first, then skips, then stand-in notes — each in the
    log's own record order. Unknown/malformed records are ignored rather than
    raised on (the log is an external artifact).
    """
    issues: list[Issue] = []
    records = log.get("records") or []
    for status, severity, code in (
        ("failed", Severity.WARNING, "REVIT_FAIL"),
        ("skipped", Severity.WARNING, "REVIT_SKIP"),
    ):
        for rec in records:
            if not isinstance(rec, dict) or rec.get("status") != status:
                continue
            what = "%s %s" % (rec.get("kind", "element"), rec.get("source", "?"))
            reason = rec.get("message") or ""
            verb = "failed to build" if status == "failed" else "was skipped"
            message = f"{what} {verb} in Revit" + (f": {reason}" if reason else "")
            issues.append(
                Issue(severity, code, message, hint=_hint_for(reason))
            )
    for note in log.get("notes") or []:
        text = str(note)
        if any(m in text.lower() for m in _NOTE_MARKERS):
            issues.append(
                Issue(
                    Severity.INFO,
                    "REVIT_NOTE",
                    f"build note: {text}",
                    hint=_hint_for(text),
                )
            )
    return issues


def load_buildlog(path: str) -> dict[str, Any]:
    """Read a ``*.buildlog.json`` file (a thin, checked wrapper)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("not a barndsl build log (expected a JSON object)")
    return data


def issues_to_dict(issues: list[Issue]) -> dict[str, Any]:
    """The stable JSON view (same field shape as ``compile --json``)."""
    return {
        "diagnostics": [
            {
                "severity": i.severity.value,
                "code": i.code,
                "message": i.message,
                "room": i.room,
                "line": i.line,
                "hint": i.hint,
            }
            for i in issues
        ]
    }
