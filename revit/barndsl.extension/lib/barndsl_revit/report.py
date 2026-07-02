# -*- coding: utf-8 -*-
"""Build report and options for the Revit builder — **pure Python, no Revit**.

Kept Revit-free (like :mod:`barndsl_revit.exchange`) so it unit-tests under
ordinary CPython and the builder's bookkeeping/formatting can be verified without
a running Revit. The builder records every element it creates / keeps (diff
re-build) / skips / fails here; the report then renders to **markdown** (for the
pyRevit output panel) and
to **JSON** (for a build-log file you can attach when debugging a real run).

Written for broad interpreter compatibility (no f-strings, no dataclasses) so it
also runs on the CPython engines older pyRevit builds ship.
"""

CREATED = "created"
#: A diff rebuild left the element from the previous build untouched (same
#: exchange record → same Revit element id, so user annotations survive).
KEPT = "kept"
SKIPPED = "skipped"
FAILED = "failed"
STATUSES = (CREATED, KEPT, SKIPPED, FAILED)

#: The element kinds the builder reports on, in display order.
KINDS = (
    "level", "wall", "door", "window", "opening", "room", "ceiling", "column",
    "framing", "slab", "porch", "stair", "grid", "roof", "footing", "fixture",
    "project",
    # documentation (the document() pass)
    "view", "tag", "schedule", "sheet", "dimension", "elevation", "section",
)

#: Names of the optional type/family overrides a build can specify, so the same
#: list drives :meth:`BuildOptions.from_dict`, ``to_dict`` and the config docs.
_OVERRIDE_KEYS = (
    "exterior_wall_type",
    "interior_wall_type",
    #: Wall types for interior walls a `wall` statement declared. Unset, the
    #: builder auto-picks a basic wall type whose name reads plumbing/wet,
    #: rated/fire, or bearing, falling back to the interior wall type.
    "plumbing_wall_type",
    "rated_wall_type",
    "bearing_wall_type",
    "door_family",
    #: A door family for kind == "overhead" openings (sectional garage doors).
    #: Unset, the builder auto-picks a door family whose name contains
    #: garage/overhead/sectional, falling back to the standard door family.
    "garage_door_family",
    "window_family",
    "floor_type",
    "roof_type",
    "column_family",
    "beam_family",
    "plumbing_family",
    "appliance_family",
    "foundation_family",
    #: How walls sit on the barndsl edge: "centerline" (default — rooms tile on
    #: centrelines) or "finish_face_exterior" (lands the outside finish on the
    #: footprint line so the building's overall dimension is exact).
    "location_line",
    #: How a ``replace`` re-build treats the previous barndsl build: "diff"
    #: (default — keep elements whose exchange record is unchanged, preserving
    #: their Revit ids and any user annotations on them; delete/recreate only
    #: what changed) or "full" (purge everything managed and recreate — the old
    #: behaviour). Ignored when ``replace`` is false.
    "rebuild",
)
_FLAG_KEYS = (
    "structure", "size_families", "porches", "stairs", "slabs", "grids", "roof",
    "fixtures", "foundation", "ceilings",
    "replace", "dry_run", "verbose",
    # document() pass
    "views", "tags", "schedules", "sheets", "dimensions", "elevations", "sections",
)


class BuildOptions(object):
    """Knobs for a build: which passes run, family sizing, dry-run, and optional
    overrides that map a pass to a **named** type/family in the project template
    (instead of auto-picking the first available)."""

    def __init__(
        self,
        structure=True,
        size_families=True,
        porches=True,
        stairs=True,
        slabs=True,
        grids=True,
        roof=True,
        fixtures=True,
        foundation=True,
        ceilings=True,
        replace=True,
        dry_run=False,
        verbose=False,
        views=True,
        tags=True,
        schedules=True,
        sheets=True,
        dimensions=True,
        elevations=True,
        sections=True,
        exterior_wall_type=None,
        interior_wall_type=None,
        plumbing_wall_type=None,
        rated_wall_type=None,
        bearing_wall_type=None,
        door_family=None,
        garage_door_family=None,
        window_family=None,
        floor_type=None,
        roof_type=None,
        column_family=None,
        beam_family=None,
        plumbing_family=None,
        appliance_family=None,
        foundation_family=None,
        location_line=None,
        rebuild="diff",
    ):
        self.structure = bool(structure)
        self.size_families = bool(size_families)
        self.porches = bool(porches)
        self.stairs = bool(stairs)
        self.slabs = bool(slabs)
        self.grids = bool(grids)
        self.roof = bool(roof)
        self.fixtures = bool(fixtures)
        self.foundation = bool(foundation)
        self.ceilings = bool(ceilings)
        self.replace = bool(replace)
        self.dry_run = bool(dry_run)
        self.verbose = bool(verbose)
        self.views = bool(views)
        self.tags = bool(tags)
        self.schedules = bool(schedules)
        self.sheets = bool(sheets)
        self.dimensions = bool(dimensions)
        self.elevations = bool(elevations)
        self.sections = bool(sections)
        self.exterior_wall_type = exterior_wall_type
        self.interior_wall_type = interior_wall_type
        self.plumbing_wall_type = plumbing_wall_type
        self.rated_wall_type = rated_wall_type
        self.bearing_wall_type = bearing_wall_type
        self.door_family = door_family
        self.garage_door_family = garage_door_family
        self.window_family = window_family
        self.floor_type = floor_type
        self.roof_type = roof_type
        self.column_family = column_family
        self.beam_family = beam_family
        self.plumbing_family = plumbing_family
        self.appliance_family = appliance_family
        self.foundation_family = foundation_family
        self.location_line = location_line
        rebuild = str(rebuild or "diff").strip().lower()
        self.rebuild = rebuild if rebuild in ("diff", "full") else "diff"

    @classmethod
    def from_dict(cls, data):
        """Build options from a plain dict (a sidecar ``config.json``).

        Unknown keys are ignored so a config can carry comments/extra fields.
        """
        data = data or {}
        kwargs = {}
        for key in _FLAG_KEYS + _OVERRIDE_KEYS:
            if key in data:
                kwargs[key] = data[key]
        return cls(**kwargs)

    def to_dict(self):
        out = {}
        for key in _FLAG_KEYS:
            out[key] = getattr(self, key)
        for key in _OVERRIDE_KEYS:
            out[key] = getattr(self, key)
        return out


class BuildRecord(object):
    """One element's outcome: its kind, the exchange source id, a status, the
    created Revit element id (when any), and a human-readable message."""

    def __init__(self, kind, source, status, revit_id=None, message=""):
        self.kind = kind
        self.source = source
        self.status = status
        self.revit_id = revit_id
        self.message = message

    def to_dict(self):
        return {
            "kind": self.kind,
            "source": self.source,
            "status": self.status,
            "revit_id": self.revit_id,
            "message": self.message,
        }

    def __repr__(self):
        return "BuildRecord(%r, %r, %r)" % (self.kind, self.source, self.status)


class BuildReport(object):
    """Accumulates per-element :class:`BuildRecord`s plus pre-build notes.

    ``dry_run`` only changes how the report *describes* itself (the builder does
    a real build then rolls it back for a dry run, so the records are accurate
    predictions). ``resources`` captures which types/families were chosen, for
    the diagnostics view. ``problems`` holds the exchange-validation cautions.
    """

    def __init__(self, dry_run=False):
        self.dry_run = bool(dry_run)
        self.records = []
        self.notes = []
        self.problems = []
        self.resources = {}

    # -- recording ---------------------------------------------------------

    def record(self, kind, source, status, revit_id=None, message=""):
        rec = BuildRecord(kind, source, status, revit_id, message)
        self.records.append(rec)
        return rec

    def created(self, kind, source, revit_id=None, message=""):
        return self.record(kind, source, CREATED, revit_id, message)

    def kept(self, kind, source, revit_id=None, message=""):
        return self.record(kind, source, KEPT, revit_id, message)

    def skipped(self, kind, source, message=""):
        return self.record(kind, source, SKIPPED, message=message)

    def failed(self, kind, source, message=""):
        return self.record(kind, source, FAILED, message=message)

    def note(self, message):
        self.notes.append(message)

    # -- queries -----------------------------------------------------------

    def count(self, status=None, kind=None):
        n = 0
        for r in self.records:
            if status is not None and r.status != status:
                continue
            if kind is not None and r.kind != kind:
                continue
            n += 1
        return n

    def counts_by_kind(self):
        out = {}
        for kind in KINDS:
            row = {}
            for st in STATUSES:
                c = self.count(status=st, kind=kind)
                if c:
                    row[st] = c
            if row:
                out[kind] = row
        return out

    def summary_line(self):
        verb = "would create" if self.dry_run else "created"
        parts = []
        for kind in KINDS:
            c = self.count(status=CREATED, kind=kind)
            if c:
                parts.append("%d %s" % (c, kind))
        made = ", ".join(parts) if parts else "nothing"
        bits = []
        nkept = self.count(status=KEPT)
        if nkept:
            bits.append("%d kept" % nkept)
        nfail = self.count(status=FAILED)
        nskip = self.count(status=SKIPPED)
        if nfail or nskip:
            bits.append("%d failed, %d skipped" % (nfail, nskip))
        tail = " (%s)" % "; ".join(bits) if bits else ""
        prefix = "[dry run] " if self.dry_run else ""
        return "%s%s %s%s" % (prefix, verb, made, tail)

    # -- serialisation -----------------------------------------------------

    def to_dict(self):
        return {
            "dry_run": self.dry_run,
            "summary": self.summary_line(),
            "counts": self.counts_by_kind(),
            "resources": self.resources,
            "problems": list(self.problems),
            "notes": list(self.notes),
            "records": [r.to_dict() for r in self.records],
        }

    def to_json(self, indent=2):
        import json

        return json.dumps(self.to_dict(), indent=indent)

    def to_markdown(self, include_created=False):
        """A compact report for the pyRevit output panel.

        Always lists the things that need attention — failures and skips — since
        those are what you debug. Set ``include_created`` (verbose) to also list
        every created element with its Revit id.
        """
        lines = []
        title = "Dry run (nothing was committed)" if self.dry_run else "Build report"
        lines.append("### %s" % title)
        lines.append("**%s**" % self.summary_line())

        counts = self.counts_by_kind()
        if counts:
            lines.append("")
            lines.append("| element | created | kept | skipped | failed |")
            lines.append("|---|---:|---:|---:|---:|")
            for kind in KINDS:
                row = counts.get(kind)
                if not row:
                    continue
                lines.append(
                    "| %s | %d | %d | %d | %d |"
                    % (kind, row.get(CREATED, 0), row.get(KEPT, 0),
                       row.get(SKIPPED, 0), row.get(FAILED, 0))
                )

        if self.resources:
            lines.append("")
            lines.append("**Using:** " + ", ".join(
                "%s = %s" % (k, v) for k, v in sorted(self.resources.items())
            ))

        attention = [r for r in self.records if r.status in (FAILED, SKIPPED)]
        if attention:
            lines.append("")
            lines.append("**%d need attention:**" % len(attention))
            for r in attention:
                lines.append("- `%s` %s — %s: %s" % (r.kind, r.source, r.status, r.message))

        if self.problems:
            lines.append("")
            lines.append("**Exchange cautions:**")
            for p in self.problems:
                lines.append("- %s" % p)

        if self.notes:
            lines.append("")
            lines.append("**Notes:**")
            for n in self.notes:
                lines.append("- %s" % n)

        if include_created:
            made = [r for r in self.records if r.status == CREATED]
            if made:
                lines.append("")
                lines.append("**Created:**")
                for r in made:
                    rid = "" if r.revit_id is None else " (id %s)" % r.revit_id
                    lines.append("- `%s` %s%s" % (r.kind, r.source, rid))

        return "\n".join(lines)
