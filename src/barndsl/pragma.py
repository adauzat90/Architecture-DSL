"""Diagnostic suppression pragmas — the audited escape hatch (``accept``).

Real projects have justified deviations. A pragma lets an author waive a *specific*
diagnostic without deleting it: the diagnostic is DOWNGRADED to an accepted INFO
(so the design score stops deducting) while the audit trail — the code and the
author's reason — survives for an AHJ reviewer.

Syntax (a comment)::

    # barndsl: accept <CODE> ["reason"]

Placement (ruff's ``noqa`` vs isort's ``skip`` conventions):

* **Trailing** a statement line — the primary form — suppresses ``CODE`` for
  diagnostics anchored to *that* line only.
* On **its own line** — the secondary form — suppresses ``CODE`` for the
  *immediately following* statement line.

Semantics:

* Suppress means *downgrade, not delete*: severity becomes ``INFO``, the message
  gains a ``(accepted: "<reason>")`` suffix, and ``Issue.accepted`` is set.
* **Errors cannot be accepted** — an error must be fixed. An ``accept`` naming a
  code that fired as an error keeps the error and adds an ``ACCEPT_DENIED``
  warning.
* An unknown code is an ``ACCEPT_UNKNOWN`` warning (with did-you-mean candidates).
* A pragma matching no diagnostic on its target line is an ``ACCEPT_UNUSED`` info
  (a stale pragma to clean up).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .compiler import comment_start
from .diagnostics import REGISTRY
from .issues import Issue, Severity

#: Structural design-flaw codes that an `accept` pragma may NOT waive, even
#: though they fire as warnings/infos rather than errors. These describe a broken
#: *building* — a plan you can compile but shouldn't build — not a jurisdiction
#: judgement call, so silencing them out of the design score would let a
#: structurally-broken plan score high on a technicality. An `accept` naming one
#: is refused exactly like an accept on an error (ACCEPT_DENIED), so the
#: diagnostic keeps its severity and keeps deducting. Kept small and deliberate.
ACCEPT_DENIED_CODES: frozenset[str] = frozenset({"GARAGE_PASSTHROUGH"})

#: ``barndsl: accept <CODE> ["reason"]`` — matched against a comment's text (the
#: part after ``#``). The code is an identifier; the reason is an optional quoted
#: string (backslash escapes recognised, mirroring the lexer).
_ACCEPT_RE = re.compile(
    r"""^\s*barndsl:\s*accept\s+
        (?P<code>[A-Za-z_][A-Za-z0-9_]*)\s*
        (?:"(?P<reason>(?:[^"\\]|\\.)*)")?\s*$""",
    re.VERBOSE,
)


@dataclass
class AcceptPragma:
    """One parsed ``accept`` pragma and the line it targets."""

    code: str
    reason: str | None
    #: The statement line the pragma suppresses on (``None`` if a standalone
    #: pragma has no following statement).
    target_line: int | None
    #: The line the pragma comment itself sits on (where ACCEPT_* diagnostics
    #: about the pragma are anchored).
    pragma_line: int


def _is_statement(line: str) -> bool:
    """True if ``line`` carries a statement (non-blank content before any
    comment). Matches ``compile_source``'s own notion of a statement line."""
    cut = comment_start(line)
    head = line if cut is None else line[:cut]
    return bool(head.strip())


def parse_pragmas(source: str) -> list[AcceptPragma]:
    """Extract every ``accept`` pragma from ``source`` and resolve its target line.

    A trailing pragma targets its own line; a standalone pragma targets the next
    statement line (``None`` when none follows)."""
    lines = source.splitlines()
    pragmas: list[AcceptPragma] = []
    for idx, raw in enumerate(lines):
        lineno = idx + 1
        cut = comment_start(raw)
        if cut is None:
            continue
        comment = raw[cut + 1:]
        m = _ACCEPT_RE.match(comment)
        if m is None:
            continue
        reason = m.group("reason")
        if reason is not None:
            reason = reason.replace('\\"', '"').replace("\\\\", "\\")
        if _is_statement(raw):
            target: int | None = lineno  # trailing pragma → this line
        else:
            # Standalone pragma → the next statement line.
            target = None
            for j in range(idx + 1, len(lines)):
                if _is_statement(lines[j]):
                    target = j + 1
                    break
        pragmas.append(AcceptPragma(m.group("code"), reason, target, lineno))
    return pragmas


def apply_pragmas(diagnostics: list[Issue], pragmas: list[AcceptPragma]) -> None:
    """Apply each pragma to ``diagnostics`` in place: downgrade the matched
    warnings/infos, and append ACCEPT_DENIED / ACCEPT_UNKNOWN / ACCEPT_UNUSED for
    the pragmas that can't be honoured or fire on nothing."""
    for p in pragmas:
        code = p.code.upper()
        if code not in REGISTRY:
            hits = difflib.get_close_matches(code, list(REGISTRY), n=3)
            dym = f" Did you mean {', '.join(hits)}?" if hits else ""
            diagnostics.append(
                Issue(
                    Severity.WARNING,
                    "ACCEPT_UNKNOWN",
                    f"Unknown diagnostic code '{p.code}' in an accept pragma.{dym}",
                    line=p.pragma_line,
                    hint="Only a real diagnostic code can be accepted — see "
                    "`barndsl explain` or the registry for the exact spelling.",
                )
            )
            continue
        if p.target_line is None:
            diagnostics.append(
                Issue(
                    Severity.INFO,
                    "ACCEPT_UNUSED",
                    f"An accept pragma for {code} stands alone with no following "
                    "statement to apply to.",
                    line=p.pragma_line,
                    hint="Put the pragma on (or right before) the statement whose "
                    "diagnostic you mean to accept.",
                )
            )
            continue
        matched = [
            d
            for d in diagnostics
            if d.line == p.target_line and d.code == code and not d.accepted
        ]
        # An error can never be accepted; nor can a structural design-flaw code on
        # the denylist (a broken *building*, not a judgement call) — both keep
        # their severity and draw an ACCEPT_DENIED. Everything else downgrades.
        denied = code in ACCEPT_DENIED_CODES
        errs = [
            d for d in matched if d.severity is Severity.ERROR or denied
        ]
        downgradable = (
            [] if denied else [d for d in matched if d.severity is not Severity.ERROR]
        )
        if not matched:
            diagnostics.append(
                Issue(
                    Severity.INFO,
                    "ACCEPT_UNUSED",
                    f"The accept pragma for {code} matches no diagnostic on line "
                    f"{p.target_line} — nothing to accept.",
                    line=p.pragma_line,
                    hint="The code didn't fire on that line; remove the stale "
                    "pragma, or move it to the line the diagnostic anchors to.",
                )
            )
            continue
        if errs:
            reason = (
                f"{code} is a structural design flaw and can't be waived — it "
                f"marks a broken building, not a judgement call"
                if denied
                else f"{code} fired as an error on line {p.target_line} and an "
                "error can't be accepted — only fixed"
            )
            diagnostics.append(
                Issue(
                    Severity.WARNING,
                    "ACCEPT_DENIED",
                    f"{reason}.",
                    line=p.pragma_line,
                    room=errs[0].room,
                    hint="Resolve the underlying problem; `accept` only downgrades "
                    "warnings and infos, and never a structural design flaw.",
                )
            )
        for d in downgradable:
            d.accepted = True
            d.accept_reason = p.reason
            d.severity = Severity.INFO
            suffix = (
                f' (accepted: "{p.reason}")' if p.reason else " (accepted)"
            )
            d.message = d.message + suffix
