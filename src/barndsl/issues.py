"""The diagnostic types every stage reports with: :class:`Severity`,
:class:`Issue` and :class:`ValidationReport`, and :func:`report_order`, the
order a compile reports them in.

The compiler, the validator, composition, pragmas, the LSP and the Revit log
all produce or read these. They live here, with no barndsl imports, so a module
that only needs the types doesn't depend on the whole validator.
:mod:`barndsl.validation` still re-exports them for older callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Issue:
    """A single diagnostic. Used for both syntax (compiler) and semantic checks."""

    severity: Severity
    code: str
    message: str
    room: str | None = None
    line: int | None = None
    col: int | None = None
    end_col: int | None = None  # 1-based, exclusive — for column-accurate carets
    hint: str | None = None
    #: Set by an ``# barndsl: accept <CODE>`` pragma (see :mod:`barndsl.pragma`).
    #: An accepted diagnostic has been DOWNGRADED to an INFO — its ``severity`` is
    #: already ``INFO`` — but the flag records that it was a deliberate,
    #: documented deviation so the score stops deducting for it and the audit
    #: trail survives. ``accept_reason`` carries the quoted justification (if any).
    accepted: bool = False
    accept_reason: str | None = None
    #: Cross-file composition (the ``use`` statement). A *part-internal* diagnostic
    #: — one that fires inside a used part file regardless of where it's placed —
    #: carries ``file`` (the resolved part path) and ``part`` (the relative path as
    #: written in the ``use`` line). Its ``line`` anchors to the ``use`` statement in
    #: the host buffer (the nearest thing there), while the message names the part's
    #: own ``file:line``. ``None`` on an ordinary host diagnostic. See
    #: :mod:`barndsl.compose`.
    file: str | None = None
    part: str | None = None

    def __str__(self) -> str:
        loc = f"line {self.line}: " if self.line else ""
        where = f" ({self.room})" if self.room is not None else ""
        head = f"{loc}{self.severity.value}[{self.code}]{where}: {self.message}"
        if self.hint:
            head += f"\n    hint: {self.hint}"
        return head


_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def report_order(issue: Issue) -> tuple[bool, int, bool, int, int, str]:
    """Sort key for the order a compile reports diagnostics in: by line (a
    diagnostic with no line after those with one), then column (likewise), then
    severity (errors first), then code. Python's sort is stable, so ties keep
    the order they were emitted in."""
    return (
        issue.line is None, issue.line or 0,
        issue.col is None, issue.col or 0,
        _SEVERITY_RANK[issue.severity], issue.code,
    )


@dataclass
class ValidationReport:
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.INFO]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        e, w, n = len(self.errors), len(self.warnings), len(self.infos)
        status = "VALID" if self.is_valid else "INVALID"
        return f"{status} — {e} error(s), {w} warning(s), {n} info(s)"

    def __str__(self) -> str:
        return "\n".join([self.summary(), *(str(i) for i in self.issues)])
