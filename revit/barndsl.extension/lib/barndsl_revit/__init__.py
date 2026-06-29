# -*- coding: utf-8 -*-
"""Shared library for the barndsl pyRevit extension.

* :mod:`barndsl_revit.exchange` — pure, Revit-free load/validate of a
  ``barndsl.revit/1`` document (the seam with the barndsl core).
* :mod:`barndsl_revit.report` — pure, Revit-free build report + options.
* :mod:`barndsl_revit.naming` — pure, Revit-free name → type/id heuristics.
* :mod:`barndsl_revit.builder` — creates Revit elements from that document, and
  reads a model back into an exchange. Imports the Revit API, so it only loads
  inside Revit; import it lazily.
"""

from . import exchange  # noqa: F401  (Revit-free; safe to import anywhere)
from . import naming  # noqa: F401  (Revit-free; safe to import anywhere)
from . import report  # noqa: F401  (Revit-free; safe to import anywhere)

__all__ = ["exchange", "naming", "report", "builder"]
