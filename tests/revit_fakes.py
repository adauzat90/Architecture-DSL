# -*- coding: utf-8 -*-
"""A minimal fake of the Revit / pyRevit API, enough to import and exercise
``barndsl_revit.builder`` in plain CPython.

The builder can't run against a real Revit here, but its *logic* — which wall
type it picks, that a dry run rolls back, that door families are duplicated and
sized, that openings host on the matched wall, that ``read_model`` reads rooms
and normalises the origin, that ``diagnose`` reports readiness — is ordinary code
that only needs the API *shape*. This module supplies that shape: the handful of
DB classes/enums, ``FilteredElementCollector``, ``Transaction``, the ``doc.Create``
factory, the Architecture (stairs) and ``System.Collections.Generic`` bits the
builder imports, and a :class:`FakeDocument` to assemble a synthetic model.

Fidelity is deliberately shallow — it proves the builder drives the API
correctly, not that Revit does the right thing with the calls. ``install()`` must
run **before** ``barndsl_revit.builder`` is imported.
"""

import sys
import types


# --- ids, parameters, base element ------------------------------------------


class _Id:
    _counter = 1000

    def __init__(self):
        _Id._counter += 1
        self.Value = _Id._counter  # Revit 2024+/2025 64-bit id (no IntegerValue)

    def __hash__(self):
        return self.Value

    def __eq__(self, other):
        return isinstance(other, _Id) and other.Value == self.Value


class FakeParam:
    def __init__(self, value=0.0, storage="Double", read_only=False):
        self._v = value
        self.StorageType = storage
        self.IsReadOnly = read_only

    def Set(self, v):
        if self.IsReadOnly:
            raise Exception("parameter is read only")
        self._v = v
        return True

    def AsDouble(self):
        return self._v

    def AsString(self):
        return self._v


class FakeElement:
    def __init__(self, name="", doc=None):
        self.Id = _Id()
        self._name = name
        self._params = {}
        if doc is not None:
            doc._register(self)

    @property
    def Name(self):
        return self._name

    @Name.setter
    def Name(self, value):
        self._name = value

    def set_param(self, key, value, storage="Double", read_only=False):
        self._params[key] = FakeParam(value, storage, read_only)

    def get_Parameter(self, bip):
        return self._params.get(bip)

    def LookupParameter(self, name):
        return self._params.get(name)


# --- enums / namespaces (string values: hashable + comparable) ---------------


class BuiltInParameter:
    FAMILY_WIDTH_PARAM = "FAMILY_WIDTH_PARAM"
    FAMILY_HEIGHT_PARAM = "FAMILY_HEIGHT_PARAM"
    INSTANCE_SILL_HEIGHT_PARAM = "INSTANCE_SILL_HEIGHT_PARAM"
    ROOM_NAME = "ROOM_NAME"


class BuiltInCategory:
    OST_Doors = "OST_Doors"
    OST_Windows = "OST_Windows"
    OST_StructuralColumns = "OST_StructuralColumns"
    OST_StructuralFraming = "OST_StructuralFraming"
    OST_Rooms = "OST_Rooms"


class WallKind:
    Basic = "Basic"


class WallFunction:
    Exterior = "Exterior"
    Interior = "Interior"


class StorageType:
    Double = "Double"
    String = "String"
    Integer = "Integer"


class FailureProcessingResult:
    Continue = "Continue"


class IFailuresPreprocessor:  # builder subclasses this; a plain base is fine
    pass


class _StructuralType:
    NonStructural = "NonStructural"
    Column = "Column"
    Beam = "Beam"


class Structure:
    StructuralType = _StructuralType


# --- geometry ----------------------------------------------------------------


class XYZ:
    def __init__(self, x, y, z=0.0):
        self.X = float(x)
        self.Y = float(y)
        self.Z = float(z)


class UV:
    def __init__(self, u, v):
        self.U = float(u)
        self.V = float(v)


class Line:
    def __init__(self, p1, p2):
        self.p1 = p1
        self.p2 = p2

    @staticmethod
    def CreateBound(p1, p2):
        if abs(p1.X - p2.X) < 1e-9 and abs(p1.Y - p2.Y) < 1e-9 and abs(p1.Z - p2.Z) < 1e-9:
            raise Exception("degenerate curve")
        return Line(p1, p2)


class CurveLoop:
    def __init__(self):
        self.curves = []

    def Append(self, c):
        self.curves.append(c)


class _BBox:
    def __init__(self, x, y, w, l):
        self.Min = XYZ(x, y, 0.0)
        self.Max = XYZ(x + w, y + l, 0.0)


# --- element types -----------------------------------------------------------


class Element:
    class Name:  # only the GetValue fallback path uses this
        @staticmethod
        def GetValue(e):
            return getattr(e, "_name", "")


class Level(FakeElement):
    def __init__(self, elevation, name="Level", doc=None):
        super().__init__(name, doc)
        self.Elevation = float(elevation)

    @staticmethod
    def Create(doc, elevation):
        lv = Level(elevation, "Level %d" % (len(doc.levels) + 1), doc)
        doc.levels.append(lv)
        return lv


class WallType(FakeElement):
    def __init__(self, name, kind="Basic", function=None, doc=None):
        super().__init__(name, doc)
        self.Kind = kind
        self.Function = function


class FloorType(FakeElement):
    def __init__(self, name, foundation=False, doc=None):
        super().__init__(name, doc)
        self.IsFoundationSlab = foundation


class Family:
    def __init__(self, name, ids_provider):
        self.Name = name
        self._ids_provider = ids_provider

    def GetFamilySymbolIds(self):
        return self._ids_provider()


class FamilySymbol(FakeElement):
    def __init__(self, family_name, name, category, doc, width=3.0, height=6.667):
        super().__init__(name, doc)
        self.IsActive = False
        self._category = category
        self._doc = doc
        self.Family = Family(family_name, lambda: doc._symbol_ids_of_family(family_name))
        self.set_param(BuiltInParameter.FAMILY_WIDTH_PARAM, width)
        self.set_param(BuiltInParameter.FAMILY_HEIGHT_PARAM, height)

    def Activate(self):
        self.IsActive = True

    def Duplicate(self, new_name):
        dup = FamilySymbol(self.Family.Name, new_name, self._category, self._doc)
        self._doc.add_symbol(dup, self._category)
        return dup


class Wall(FakeElement):
    @staticmethod
    def Create(doc, curve, wtype_id, level_id, height, offset, flip, structural):
        w = Wall("wall", doc)
        w.curve, w.height, w.level_id, w.wtype_id = curve, height, level_id, wtype_id
        doc.created.append(("wall", w))
        return w


class Floor(FakeElement):
    @staticmethod
    def Create(doc, loops, ftype_id, level_id):
        f = Floor("floor", doc)
        doc.created.append(("floor", f))
        return f


class FamilyInstance(FakeElement):
    pass


class Room(FakeElement):
    def __init__(self, name, x, y, w, l, level, doc):
        super().__init__(name, doc)
        self._bb = _BBox(x, y, w, l)
        self.Area = w * l
        self.Location = object()  # truthy = placed
        self.LevelId = level.Id
        self.set_param(BuiltInParameter.ROOM_NAME, name, "String")

    def get_BoundingBox(self, view):
        return self._bb


# --- collector / transaction / creator ---------------------------------------


class FilteredElementCollector:
    def __init__(self, doc):
        self.doc = doc
        self._cls = None
        self._cat = None
        self._notype = False

    def OfClass(self, cls):
        self._cls = cls
        return self

    def OfCategory(self, bic):
        self._cat = bic
        return self

    def WhereElementIsNotElementType(self):
        self._notype = True
        return self

    def ToElements(self):
        return self.doc.query(self._cls, self._cat, self._notype)


class Transaction:
    def __init__(self, doc, name):
        self.doc = doc
        self.name = name
        self._started = False
        self._ended = False

    def Start(self):
        self._started = True

    def Commit(self):
        self._ended = True
        self.doc.commits.append(self.name)

    def RollBack(self):
        self._ended = True
        self.doc.rollbacks.append(self.name)

    def HasStarted(self):
        return self._started

    def HasEnded(self):
        return self._ended


class _Creator:
    def __init__(self, doc):
        self.doc = doc

    def NewFamilyInstance(self, *args):
        if self.doc.fail_family_instance:
            raise Exception("forced NewFamilyInstance failure")
        inst = FamilyInstance("instance", self.doc)
        inst.set_param(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM, 0.0)
        self.doc.created.append(("instance", inst, args))
        return inst

    def NewRoom(self, level, uv):
        if self.doc.room_unplaced:
            return None
        rm = FakeElement("placed-room", self.doc)
        rm.set_param(BuiltInParameter.ROOM_NAME, "", "String")
        self.doc.created.append(("room", rm))
        return rm


class _Application:
    VersionNumber = "2025"
    VersionName = "Autodesk Revit 2025"
    VersionBuild = "20240101_fake"


# --- stairs (Architecture) ---------------------------------------------------


class StairsRunJustification:
    Center = "Center"


class StairsRun:
    @staticmethod
    def CreateStraightRun(doc, stairs_id, line, justification):
        r = FakeElement("run", doc)
        doc.created.append(("stair_run", r))
        return r


class StairsLanding:
    @staticmethod
    def CreateAutomaticLanding(doc, id1, id2):
        l = FakeElement("landing", doc)
        doc.created.append(("stair_landing", l))
        return l


class StairsEditScope:
    def __init__(self, doc, name):
        self.doc = doc
        self.IsActive = False

    def Start(self, base_id, top_id):
        self.IsActive = True
        self._id = _Id()
        return self._id

    def Commit(self, preprocessor):
        self.IsActive = False
        self.doc.stair_commits += 1

    def Cancel(self):
        self.IsActive = False
        self.doc.stair_cancels += 1


# --- the document ------------------------------------------------------------


class FakeDocument:
    """A synthetic Revit document. Add resources, then hand it to the builder."""

    def __init__(self, title="Fake Project"):
        self.Title = title
        self.Application = _Application()
        self.Create = _Creator(self)
        self.levels = []
        self.wall_types = []
        self.floor_types = []
        self.symbols = {}  # category -> [FamilySymbol]
        self.placed_rooms = []
        self.instances = {}  # category -> [FamilyInstance-ish]
        self.created = []  # (kind, element[, args]) appended as the builder builds
        self.commits = []
        self.rollbacks = []
        self.stair_commits = 0
        self.stair_cancels = 0
        self._by_id = {}
        # toggles for exercising failure paths
        self.fail_family_instance = False
        self.room_unplaced = False

    # registration / lookup
    def _register(self, elem):
        self._by_id[elem.Id.Value] = elem

    def GetElement(self, eid):
        return self._by_id.get(eid.Value)

    def Regenerate(self):
        pass

    def _symbol_ids_of_family(self, family_name):
        out = []
        for syms in self.symbols.values():
            for s in syms:
                if s.Family.Name == family_name:
                    out.append(s.Id)
        return out

    # builders for tests
    def add_level(self, elevation, name=None):
        lv = Level(elevation, name or ("Level %d" % (len(self.levels) + 1)), self)
        self.levels.append(lv)
        return lv

    def add_wall_type(self, name, function=None):
        wt = WallType(name, "Basic", function, self)
        self.wall_types.append(wt)
        return wt

    def add_floor_type(self, name, foundation=False):
        ft = FloorType(name, foundation, self)
        self.floor_types.append(ft)
        return ft

    def add_symbol(self, symbol, category):
        self.symbols.setdefault(category, []).append(symbol)
        return symbol

    def add_family(self, category, family_name, name=None, width=3.0, height=6.667):
        sym = FamilySymbol(family_name, name or family_name, category, self, width, height)
        return self.add_symbol(sym, category)

    def add_placed_room(self, name, x, y, w, l, level):
        rm = Room(name, x, y, w, l, level, self)
        self.placed_rooms.append(rm)
        return rm

    def add_instance(self, category, x, y, from_room=None, to_room=None, width=3.0, height=6.667, sill=0.0):
        inst = FamilyInstance(category, self)
        inst.Location = types.SimpleNamespace(Point=XYZ(x, y, 0.0))
        inst.Symbol = FamilySymbol("Fam-%s" % category, "Type", category, self, width, height)
        inst.FromRoom = from_room
        inst.ToRoom = to_room
        inst.set_param(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM, sill)
        self.instances.setdefault(category, []).append(inst)
        return inst

    # the collector query
    def query(self, cls, cat, notype):
        if cls is Level:
            return list(self.levels)
        if cls is WallType:
            return list(self.wall_types)
        if cls is FloorType:
            return list(self.floor_types)
        if cls is FamilySymbol:
            return list(self.symbols.get(cat, []))
        if cat == BuiltInCategory.OST_Rooms:
            return list(self.placed_rooms)
        if cat in (BuiltInCategory.OST_Doors, BuiltInCategory.OST_Windows):
            return list(self.instances.get(cat, []))
        return []


# --- install the fake modules ------------------------------------------------


def _make_db_module():
    db = types.ModuleType("Autodesk.Revit.DB")
    for obj in (
        BuiltInParameter, BuiltInCategory, WallKind, WallFunction, StorageType,
        FailureProcessingResult, IFailuresPreprocessor, Structure, XYZ, UV, Line,
        CurveLoop, Element, Level, WallType, FloorType, Family, FamilySymbol, Wall,
        Floor, FamilyInstance, FilteredElementCollector, Transaction,
    ):
        setattr(db, obj.__name__, obj)
    return db


def install():
    """Register the fake modules in ``sys.modules``. Idempotent."""
    db = _make_db_module()

    pyrevit = types.ModuleType("pyrevit")
    pyrevit.DB = db
    pyrevit.revit = types.SimpleNamespace(doc=None, uidoc=None)
    script_mod = types.ModuleType("pyrevit.script")

    class _NullLogger:
        def _noop(self, *a, **k):
            pass

        debug = info = warning = error = _noop

    script_mod.get_logger = lambda: _NullLogger()
    pyrevit.script = script_mod

    arch = types.ModuleType("Autodesk.Revit.DB.Architecture")
    arch.StairsEditScope = StairsEditScope
    arch.StairsRun = StairsRun
    arch.StairsLanding = StairsLanding
    arch.StairsRunJustification = StairsRunJustification

    # System.Collections.Generic.List[T]() → a .Add-able list
    class _List(list):
        def Add(self, x):
            self.append(x)

    class _ListFactory:
        def __getitem__(self, T):
            return lambda: _List()

    sysgen = types.ModuleType("System.Collections.Generic")
    sysgen.List = _ListFactory()

    mods = {
        "pyrevit": pyrevit,
        "pyrevit.script": script_mod,
        "Autodesk": types.ModuleType("Autodesk"),
        "Autodesk.Revit": types.ModuleType("Autodesk.Revit"),
        "Autodesk.Revit.DB": db,
        "Autodesk.Revit.DB.Architecture": arch,
        "System": types.ModuleType("System"),
        "System.Collections": types.ModuleType("System.Collections"),
        "System.Collections.Generic": sysgen,
    }
    sys.modules.update(mods)
    return db
