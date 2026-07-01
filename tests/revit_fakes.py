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

    def AsElementId(self):
        return self._v


class FakeElement:
    def __init__(self, name="", doc=None):
        self.Id = _Id()
        self._name = name
        self.Document = doc
        self._entity = None
        self._params = {}
        # Every Revit element carries instance Comments — the managed marker lives here.
        self._params[BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS] = FakeParam("", "String")
        self._params["Comments"] = self._params[BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS]
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

    # Extensible Storage: the managed marker lives here (not in Comments).
    def SetEntity(self, entity):
        self._entity = entity

    def GetEntity(self, schema):
        return self._entity if self._entity is not None else Entity(None)


# --- enums / namespaces (string values: hashable + comparable) ---------------


class BuiltInParameter:
    FAMILY_WIDTH_PARAM = "FAMILY_WIDTH_PARAM"
    FAMILY_HEIGHT_PARAM = "FAMILY_HEIGHT_PARAM"
    INSTANCE_SILL_HEIGHT_PARAM = "INSTANCE_SILL_HEIGHT_PARAM"
    ROOM_NAME = "ROOM_NAME"
    ROOM_NUMBER = "ROOM_NUMBER"
    ROOM_FINISH_FLOOR = "ROOM_FINISH_FLOOR"
    ROOM_FINISH_BASE = "ROOM_FINISH_BASE"
    ROOM_FINISH_CEILING = "ROOM_FINISH_CEILING"
    ROOM_FINISH_WALL = "ROOM_FINISH_WALL"
    CEILING_HEIGHTABOVELEVEL_PARAM = "CEILING_HEIGHTABOVELEVEL_PARAM"
    ALL_MODEL_INSTANCE_COMMENTS = "ALL_MODEL_INSTANCE_COMMENTS"
    # wall top constraint / location line
    WALL_HEIGHT_TYPE = "WALL_HEIGHT_TYPE"
    WALL_TOP_OFFSET = "WALL_TOP_OFFSET"
    WALL_KEY_REF_PARAM = "WALL_KEY_REF_PARAM"
    # structural-column base/top
    FAMILY_TOP_LEVEL_PARAM = "FAMILY_TOP_LEVEL_PARAM"
    FAMILY_TOP_LEVEL_OFFSET_PARAM = "FAMILY_TOP_LEVEL_OFFSET_PARAM"
    FAMILY_BASE_LEVEL_PARAM = "FAMILY_BASE_LEVEL_PARAM"
    FAMILY_BASE_LEVEL_OFFSET_PARAM = "FAMILY_BASE_LEVEL_OFFSET_PARAM"


class BuiltInCategory:
    OST_Doors = "OST_Doors"
    OST_Windows = "OST_Windows"
    OST_StructuralColumns = "OST_StructuralColumns"
    OST_StructuralFraming = "OST_StructuralFraming"
    OST_Rooms = "OST_Rooms"
    OST_RoomTags = "OST_RoomTags"
    OST_TitleBlocks = "OST_TitleBlocks"
    OST_PlumbingFixtures = "OST_PlumbingFixtures"
    OST_SpecialityEquipment = "OST_SpecialityEquipment"
    OST_StructuralFoundation = "OST_StructuralFoundation"


class ViewFamily:
    FloorPlan = "FloorPlan"


class TagMode:
    TM_ADDBY_CATEGORY = "TM_ADDBY_CATEGORY"


class TagOrientation:
    Horizontal = "Horizontal"


class ElementId:
    """A category/element id wrapper (distinct from the auto-id on elements)."""

    def __init__(self, value):
        self.Value = value


class Reference:
    def __init__(self, element):
        self.element = element


class LinkElementId:
    def __init__(self, element_id):
        self.LinkedElementId = element_id


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
    Footing = "Footing"


class Structure:
    StructuralType = _StructuralType


# --- extensible storage (the managed marker) --------------------------------


class _AccessLevel:
    Public = "Public"


class _Schema:
    _registry = {}  # guid string -> _Schema

    def __init__(self, guid, name):
        self.guid = str(guid)
        self._name = name

    @staticmethod
    def Lookup(guid):
        return _Schema._registry.get(str(guid))

    def GetField(self, name):
        return name


class _SchemaBuilder:
    def __init__(self, guid):
        self.guid = guid
        self.name = "schema"

    def SetSchemaName(self, n):
        self.name = n

    def SetReadAccessLevel(self, a):
        pass

    def SetWriteAccessLevel(self, a):
        pass

    def AddSimpleField(self, name, dotnet_type):
        return object()  # a real FieldBuilder; we don't need it

    def Finish(self):
        s = _Schema(self.guid, self.name)
        _Schema._registry[str(self.guid)] = s
        return s


class _EntityAccessor:
    """Backs ``entity.Set[T](field, value)`` / ``entity.Get[T](field)`` — the
    generic call shape Revit uses — and a plain callable form for convenience."""

    def __init__(self, entity, write):
        self._entity = entity
        self._write = write

    def __getitem__(self, dotnet_type):
        return self._op

    def __call__(self, *args):
        return self._op(*args)

    def _op(self, *args):
        if self._write:
            field, value = args
            self._entity._data[field] = value
            return None
        (field,) = args
        return self._entity._data.get(field)


class Entity:
    def __init__(self, schema=None):
        self.Schema = schema
        self._data = {}
        self._valid = schema is not None
        self.Set = _EntityAccessor(self, write=True)
        self.Get = _EntityAccessor(self, write=False)

    def IsValid(self):
        return self._valid


class ExtensibleStorage:
    AccessLevel = _AccessLevel
    Schema = _Schema
    SchemaBuilder = _SchemaBuilder
    Entity = Entity


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


class Curve:
    """Base curve type — only used as the generic parameter of ``List[DB.Curve]``
    when building a wall from a vertical profile (a gable-end wall)."""


class Line(Curve):
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


class RoofType(FakeElement):
    pass


class CeilingType(FakeElement):
    pass


class Ceiling(FakeElement):
    @staticmethod
    def Create(doc, loops, ctype_id, level_id):
        c = Ceiling("ceiling", doc)
        c.set_param(BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM, 0.0, "Double")
        doc.created.append(("ceiling", c))
        return c


class _ModelCurveArray:
    """Stand-in for the ``ModelCurveArray`` ``NewFootPrintRoof`` returns — one
    model curve per footprint edge, queried by ``Size`` / ``get_Item`` like Revit's."""

    def __init__(self, n):
        self._items = [FakeElement("model-curve", None) for _ in range(n)]

    @property
    def Size(self):
        return len(self._items)

    def get_Item(self, i):
        return self._items[i]


class FootPrintRoof(FakeElement):
    """A footprint roof that records which edges were made slope-defining, so a
    test can assert the eaves slope and the gable ends stay vertical."""

    def __init__(self, name, doc):
        super().__init__(name, doc)
        self.slopes = {}  # model-curve -> slope angle (radians)

    def set_DefinesSlope(self, model_curve, defines):
        if defines:
            self.slopes.setdefault(id(model_curve), 0.0)
        else:
            self.slopes.pop(id(model_curve), None)

    def set_SlopeAngle(self, model_curve, angle):
        self.slopes[id(model_curve)] = float(angle)


class Grid(FakeElement):
    @staticmethod
    def Create(doc, line):
        g = Grid("grid", doc)
        g.line = line
        doc.created.append(("grid", g))
        return g


class CurveArray:
    def __init__(self):
        self.curves = []

    def Append(self, c):
        self.curves.append(c)


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
    def Create(doc, first, *rest):
        """Two overloads, matched by arity (as Revit matches by signature):

        * line:    ``Create(doc, curve, wtypeId, levelId, height, offset, flip, structural)``
        * profile: ``Create(doc, IList[Curve], wtypeId, levelId, structural)`` — a
          wall from a vertical profile loop, used for a gable-end wall.
        """
        w = Wall("wall", doc)
        if len(rest) == 3:  # profile overload
            w.profile = list(first)
            w.curve = None
            w.height = None
            w.wtype_id, w.level_id, w.structural = rest
        else:  # line overload
            w.profile = None
            w.curve = first
            w.wtype_id, w.level_id, w.height = rest[0], rest[1], rest[2]
        # Top constraint / location line params a real wall carries, so the
        # builder can constrain the top and (optionally) set the location line.
        w.set_param(BuiltInParameter.WALL_HEIGHT_TYPE, None, "ElementId")
        w.set_param(BuiltInParameter.WALL_TOP_OFFSET, 0.0, "Double")
        w.set_param(BuiltInParameter.WALL_KEY_REF_PARAM, 0, "Integer")
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


class ViewFamilyType(FakeElement):
    def __init__(self, name, view_family, doc=None):
        super().__init__(name, doc)
        self.ViewFamily = view_family
        self.IsActive = True

    def Activate(self):
        self.IsActive = True


class ViewPlan(FakeElement):
    @staticmethod
    def Create(doc, vft_id, level_id):
        v = ViewPlan("view", doc)
        v.GenLevel = doc._by_id.get(level_id.Value)
        doc.views.append(v)
        doc.created.append(("view", v))
        return v


class ViewSchedule(FakeElement):
    @staticmethod
    def CreateSchedule(doc, category_id):
        s = ViewSchedule("schedule", doc)
        doc.schedules.append(s)
        doc.created.append(("schedule", s))
        return s


class ViewSheet(FakeElement):
    @staticmethod
    def Create(doc, title_block_id):
        sh = ViewSheet("sheet", doc)
        doc.sheets.append(sh)
        doc.created.append(("sheet", sh))
        return sh


class Viewport(FakeElement):
    @staticmethod
    def Create(doc, sheet_id, view_id, point):
        vp = Viewport("viewport", doc)
        doc.created.append(("viewport", vp))
        return vp


class IndependentTag(FakeElement):
    @staticmethod
    def Create(doc, view_id, reference, add_leader, tag_mode, orientation, point):
        t = IndependentTag("tag", doc)
        doc.created.append(("tag", t))
        return t


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
        # Structural-column base/top params (present on a real column instance),
        # so the builder can raise a post to the plate and a test can read it.
        inst.set_param(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, None, "ElementId")
        inst.set_param(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, 0.0, "Double")
        inst.set_param(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, None, "ElementId")
        inst.set_param(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, 0.0, "Double")
        # A placed instance is queryable by its symbol's category and carries a
        # location point — as a real Revit instance would, so tags can find it.
        sym = args[1] if len(args) >= 2 else None
        cat = getattr(sym, "_category", None)
        if args and isinstance(args[0], XYZ):
            inst.Location = types.SimpleNamespace(Point=args[0])
        if cat is not None:
            self.doc.instances.setdefault(cat, []).append(inst)
        self.doc.created.append(("instance", inst, args))
        return inst

    def NewRoom(self, level, uv):
        if self.doc.room_unplaced:
            return None
        rm = FakeElement("placed-room", self.doc)
        rm.set_param(BuiltInParameter.ROOM_NAME, "", "String")
        for _p in (
            BuiltInParameter.ROOM_NUMBER, BuiltInParameter.ROOM_FINISH_FLOOR,
            BuiltInParameter.ROOM_FINISH_BASE, BuiltInParameter.ROOM_FINISH_CEILING,
            BuiltInParameter.ROOM_FINISH_WALL,
        ):
            rm.set_param(_p, "", "String")
        rm.Area = 1.0
        rm.Location = types.SimpleNamespace(Point=XYZ(uv.U, uv.V, 0.0))
        rm.LevelId = level.Id
        # Queryable by a Rooms collector (and taggable), like a real placed room.
        self.doc.placed_rooms.append(rm)
        self.doc.created.append(("room", rm))
        return rm

    def NewFootPrintRoof(self, curve_array, level, roof_type):
        roof = FootPrintRoof("roof", self.doc)
        self.doc.created.append(("roof", roof))
        # Real Revit returns (roof, modelCurveMapping) — one model curve per
        # footprint edge; the builder sets slopes on the eave edges.
        mapping = _ModelCurveArray(len(getattr(curve_array, "curves", [])))
        return (roof, mapping)

    def NewRoomTag(self, link_element_id, uv, view_id):
        t = FakeElement("room-tag", self.doc)
        self.doc.created.append(("tag", t))
        return t


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
        self.roof_types = []
        self.ceiling_types = []
        self.symbols = {}  # category -> [FamilySymbol]
        self.placed_rooms = []
        self.instances = {}  # category -> [FamilyInstance-ish]
        self.view_family_types = []
        self.views = []
        self.schedules = []
        self.sheets = []
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

    def Delete(self, eid):
        self._by_id.pop(eid.Value, None)
        for lst in (
            self.views, self.schedules, self.sheets, self.levels, self.wall_types,
            self.floor_types, self.roof_types, self.view_family_types, self.placed_rooms,
        ):
            for x in list(lst):
                if x.Id.Value == eid.Value:
                    lst.remove(x)

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

    def add_roof_type(self, name):
        rt = RoofType(name, self)
        self.roof_types.append(rt)
        return rt

    def add_ceiling_type(self, name):
        ct = CeilingType(name, self)
        self.ceiling_types.append(ct)
        return ct

    def add_view_family_type(self, name, view_family=ViewFamily.FloorPlan):
        vft = ViewFamilyType(name, view_family, self)
        self.view_family_types.append(vft)
        return vft

    def add_title_block(self, family_name="A1 Title Block"):
        sym = FamilySymbol(family_name, "A1", BuiltInCategory.OST_TitleBlocks, self)
        return self.add_symbol(sym, BuiltInCategory.OST_TitleBlocks)

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
        if cls is None and cat is None and notype:
            # The "all instances" sweep the managed-element purge uses.
            return list(self._by_id.values())
        if cls is Level:
            return list(self.levels)
        if cls is WallType:
            return list(self.wall_types)
        if cls is FloorType:
            return list(self.floor_types)
        if cls is RoofType:
            return list(self.roof_types)
        if cls is CeilingType:
            return list(self.ceiling_types)
        if cls is ViewFamilyType:
            return list(self.view_family_types)
        if cls is ViewPlan:
            return list(self.views)
        if cls is ViewSchedule:
            return list(self.schedules)
        if cls is ViewSheet:
            return list(self.sheets)
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
        FailureProcessingResult, IFailuresPreprocessor, Structure, XYZ, UV, Curve, Line,
        CurveLoop, CurveArray, Element, Level, WallType, FloorType, RoofType,
        CeilingType, Ceiling, Grid,
        Family, FamilySymbol, Wall, Floor, FamilyInstance, FilteredElementCollector,
        Transaction, ViewFamily, ViewFamilyType, ViewPlan, ViewSchedule, ViewSheet,
        Viewport, IndependentTag, TagMode, TagOrientation, ElementId, Reference,
        LinkElementId,
    ):
        setattr(db, obj.__name__, obj)
    db.ExtensibleStorage = ExtensibleStorage
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

    system = types.ModuleType("System")

    class _Guid:
        def __init__(self, s):
            self.s = str(s)

        def __str__(self):
            return self.s

    system.Guid = _Guid
    system.String = str

    mods = {
        "pyrevit": pyrevit,
        "pyrevit.script": script_mod,
        "Autodesk": types.ModuleType("Autodesk"),
        "Autodesk.Revit": types.ModuleType("Autodesk.Revit"),
        "Autodesk.Revit.DB": db,
        "Autodesk.Revit.DB.Architecture": arch,
        "System": system,
        "System.Collections": types.ModuleType("System.Collections"),
        "System.Collections.Generic": sysgen,
    }
    sys.modules.update(mods)
    return db
