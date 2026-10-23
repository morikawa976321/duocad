"""曲面の上面・テーパー壁の接点を実面から求める、曲率半径を超える丸め。

球の中心は壁の法線上に置き、上面への射影距離が R となる壁の母線位置を求根する。
中心の道を母線方向に射影して燕尾を切り、交点は空間でも一致することを検査する。
残した道と両接点の線から半径一定の管を作り、燕尾の壁接点を境界に G1 の帽子を張る。
上面と壁は接点の線で切り、元の面を支えに使う。切断面より上に別の面がある形は断る。
"""
from __future__ import annotations

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepFeat import BRepFeat_SplitShape
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeFilling
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.GeomAPI import GeomAPI_Interpolate, GeomAPI_ProjectPointOnSurf
from OCP.GeomAbs import GeomAbs_Shape, GeomAbs_SurfaceType
from OCP.GeomFill import GeomFill_Pipe
from OCP.GeomProjLib import GeomProjLib
from OCP.ShapeFix import ShapeFix_Edge
from OCP.TColgp import TColgp_HArray1OfPnt
from OCP.TColStd import TColStd_HArray1OfReal
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_ON, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_WIRE
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Pnt2d, gp_Vec

from . import kernel_tight as tight
from .kernel_overflow import Unsupported, _boolean, _outward_normal, _solids
from .mesh_geometry import indexed_map


def _projection(point, surface):
    projection = GeomAPI_ProjectPointOnSurf(point, surface)
    if not projection.IsDone() or not projection.NbPoints():
        raise Unsupported("a contact point could not be projected onto its support surface")
    return projection.NearestPoint(), projection.LowerDistanceParameters()


def _normal(face, u, v):
    point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    BRep_Tool.Surface_s(TopoDS.Face_s(face)).D1(u, v, point, du, dv)
    normal = du.Crossed(dv).Normalized()
    if face.Orientation() == TopAbs_REVERSED:
        normal.Reverse()
    return point, normal


def _walls(shape, top):
    adjacent = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, adjacent)
    walls = []
    for edge in tight._edges(top):
        if BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(top)) or BRep_Tool.Degenerated_s(edge):
            continue
        others = [face for face in adjacent.FindFromKey(edge) if not face.IsSame(top)]
        if len(others) != 1:
            raise Unsupported("the top outline does not have exactly one wall on each edge")
        if not any(others[0].IsSame(wall) for wall in walls):
            walls.append(others[0])
    return walls


def _up(top, walls):
    if BRepAdaptor_Surface(TopoDS.Face_s(top)).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane:
        return _outward_normal(top, tight._a_point_on(top)).Normalized()
    directions = []
    for wall in walls:
        surface = BRepAdaptor_Surface(TopoDS.Face_s(wall))
        if surface.GetType() != GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion:
            raise Unsupported("a curved top combined with tapered or non-extruded walls is not supported")
        directions.append(gp_Vec(surface.Direction()))
    up = directions[0]
    if any(not gp_Dir(up).IsParallel(gp_Dir(direction), 1e-8) for direction in directions[1:]):
        raise Unsupported("the walls of a curved top must have parallel straight generators")
    if up.Dot(_outward_normal(top, tight._a_point_on(top))) < 0:
        up.Reverse()
    return up


def _cut(shape, top, walls, up, radius):
    size = tight._diagonal(shape)
    points = [point for edge in tight._edges(top) for point in tight._samples(edge, 33)]
    origin = points[0]
    level = min(gp_Vec(origin, point).Dot(up) for point in points) - 2.0 * radius
    base = origin.Translated(up.Multiplied(level))
    x = tight._any_perpendicular(gp_Dir(up)).Normalized()
    y = up.Crossed(x)
    corner = base.Translated(x.Multiplied(-size)).Translated(y.Multiplied(-size))
    box = BRepPrimAPI_MakeBox(gp_Ax2(corner, gp_Dir(up), gp_Dir(x)), 2 * size, 2 * size, 2 * size).Shape()
    above = _boolean("common", shape, box)
    below = _boolean("cut", shape, box)
    if len(_solids(below.Shape())) != 1:
        raise Unsupported("the plane below the wall contacts cuts through the bottom or splits the body")
    section = [face for face in tight._faces(below.Shape())
               if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane
               and abs(gp_Vec(base, tight._a_point_on(face)).Dot(up)) < 1e-7 * size
               and _outward_normal(face, tight._a_point_on(face)).Dot(up) > 0.99]
    if len(section) != 1:
        raise Unsupported("the cut below the wall contacts does not have one planar section")
    allowed = [top, *walls]
    permitted = [modified for face in allowed for modified in (list(above.Modified(face)) or [face])]
    for face in tight._faces(above.Shape()):
        if any(face.IsSame(other) for other in permitted):
            continue
        surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
        if (surface.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane
                or abs(gp_Vec(base, tight._a_point_on(face)).Dot(up)) > 1e-7 * size):
            raise Unsupported("above the cut there must be only the top and its wall band (no holes, protrusions or other faces)")
    if indexed_map(section[0], TopAbs_WIRE).Extent() != 1:
        raise Unsupported("the section below the wall contacts has holes")
    return section[0], base


class _Contacts(tight._Contour):
    def __init__(self, section, top, walls, up, radius):
        super().__init__(BRepTools.OuterWire_s(TopoDS.Face_s(section)), up)
        self.top = top
        self.surface = BRep_Tool.Surface_s(TopoDS.Face_s(top))
        self.walls = walls
        self.radius = radius
        self.cache = {}
        self.spans = []
        explorer = BRepTools_WireExplorer(TopoDS.Wire_s(self.wire))
        position = 0.0
        while explorer.More():
            edge = explorer.Current()
            length = GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge))
            point = tight._samples(edge, 3)[1]
            distances = [(_projection(point, BRep_Tool.Surface_s(TopoDS.Face_s(wall)))[0].Distance(point), i)
                         for i, wall in enumerate(walls)]
            distance, index = min(distances)
            if distance > 1e-6 * tight._diagonal(section):
                raise Unsupported("the planar section does not lie on the original walls")
            self.spans.append((position, position + length, index))
            position += length
            explorer.Next()

    def contact(self, s):
        s = s % self.length
        key = round(s, 13)
        if key in self.cache:
            return self.cache[key]
        point = self.at(s)[0]
        _, _, index = next(span for span in self.spans if span[0] - 1e-10 <= s <= span[1] + 1e-10)
        wall = self.walls[index]
        support = BRep_Tool.Surface_s(TopoDS.Face_s(wall))
        _, (u, v) = _projection(point, support)
        p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
        support.D1(u, v, p, du, dv)
        along_u = abs(du.Normalized().Dot(self.up)) > abs(dv.Normalized().Dot(self.up))
        if max(abs(du.Normalized().Dot(self.up)), abs(dv.Normalized().Dot(self.up))) < 1e-8:
            raise Unsupported("a wall has no parametric direction transverse to the section plane")

        def evaluate(parameter):
            foot, normal = _normal(wall, parameter, v) if along_u else _normal(wall, u, parameter)
            center = foot.Translated(normal.Multiplied(-self.radius))
            crest, uv = _projection(center, self.surface)
            _, outward = _normal(self.top, *uv)
            residual = gp_Vec(center, crest).Dot(outward) - self.radius
            return residual, (center, foot, crest, index)

        parameter = u if along_u else v
        for _ in range(24):
            residual, contacts = evaluate(parameter)
            if abs(residual) < 1e-10 * max(1.0, self.radius):
                break
            h = 1e-5 * max(1.0, abs(parameter))
            derivative = (evaluate(parameter + h)[0] - evaluate(parameter - h)[0]) / (2 * h)
            if abs(derivative) < 1e-12:
                raise Unsupported("the wall and top offset surfaces do not meet transversely")
            parameter -= residual / derivative
        else:
            raise Unsupported("the ball contacts on the wall and top did not converge")
        center, foot, crest, _ = contacts
        if (abs(center.Distance(crest) - self.radius) > 1e-7 * max(1.0, self.radius)
                or gp_Vec(foot, crest).Dot(self.up) <= 0):
            raise Unsupported("the contact solution does not give a convex rounding above the wall")
        for face, contact in ((wall, foot), (self.top, crest)):
            _, uv = _projection(contact, BRep_Tool.Surface_s(TopoDS.Face_s(face)))
            if BRepClass_FaceClassifier(TopoDS.Face_s(face), gp_Pnt2d(*uv), 1e-7).State() not in (TopAbs_IN, TopAbs_ON):
                raise Unsupported("a ball contact falls outside the trimmed top or wall face")
        self.cache[key] = contacts
        return contacts

    def center(self, s, radius):
        return self.contact(s)[0]

    def bend(self, s):
        h = 1e-5 * self.length
        tangent = gp_Vec(self.at(s - h)[0], self.at(s + h)[0])
        derivative = gp_Vec(self.center(s - h, self.radius), self.center(s + h, self.radius))
        return (1.0 - derivative.Dot(tangent) / tangent.SquareMagnitude()) / self.radius

    def clear_of_walls(self, radius, loops, count):
        for i in range(count):
            s = i * self.length / count
            if any(low < s < high or low < s + self.length < high for low, high in loops):
                continue
            center = self.center(s, self.radius)
            for wall in self.walls:
                nearest, _ = _projection(center, BRep_Tool.Surface_s(TopoDS.Face_s(wall)))
                if nearest.Distance(center) < self.radius - 1e-7 * max(1.0, self.radius):
                    raise Unsupported("the ball would cut into the opposite wall (the top is too narrow for the radius)")


def _curves(contour, low, high, count=128):
    parameters = [low + (high - low) * i / count for i in range(count + 1)]
    contacts = [contour.contact(s) for s in parameters]
    curves = []
    for column in range(3):
        points = TColgp_HArray1OfPnt(1, len(parameters))
        values = TColStd_HArray1OfReal(1, len(parameters))
        for i, (s, row) in enumerate(zip(parameters, contacts), 1):
            points.SetValue(i, row[column])
            values.SetValue(i, s)
        interpolation = GeomAPI_Interpolate(points, values, False, 1e-9)
        interpolation.Perform()
        if not interpolation.IsDone():
            raise Unsupported("the ball contact curves could not be interpolated")
        curves.append(interpolation.Curve())
    return curves


def _pcurve(edge, face, tolerance):
    edge, face = TopoDS.Edge_s(edge), TopoDS.Face_s(face)
    ShapeFix_Edge().FixAddPCurve(edge, face, False, tolerance)
    pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
    if pcurve is None:
        raise Unsupported("a contact edge has no pcurve on its support (unsafe for G1 filling)")
    adaptor = BRepAdaptor_Curve(edge)
    first, last = adaptor.FirstParameter(), adaptor.LastParameter()
    surface = BRep_Tool.Surface_s(face)
    parameters = [first + (last - first) * i / 256 for i in range(257)]

    def error(curve):
        return max(adaptor.Value(u).Distance(surface.Value(curve.Value(u).X(), curve.Value(u).Y())) for u in parameters)

    deviation = error(pcurve)
    # 実測: ShapeFix_Edge の球面上の pcurve は R=1.95 で 0.0148 離れた。GeomProjLib は 6.6e-9。
    # 押し出し面では逆に GeomProjLib が 0.00167 離れたため、3D で小さい方を選ぶ。
    if deviation > tolerance:
        alternative = GeomProjLib.Curve2d_s(BRep_Tool.Curve_s(edge, 0.0, 0.0), first, last, surface, tolerance)
        if alternative is not None and error(alternative) < deviation:
            pcurve, deviation = alternative, error(alternative)
            BRep_Builder().UpdateEdge(edge, pcurve, face, max(tolerance, deviation))
    if deviation > 1e-6 * tight._diagonal(face):
        raise Unsupported(f"a contact pcurve deviates from its 3D edge ({deviation:.6g})")


def _side(segment, contour, radius):
    path, foot, crest = _curves(contour, segment.low, segment.high,
                                max(128, int((segment.high - segment.low) / contour.length * 512)))
    pipe = GeomFill_Pipe(path, foot, crest, radius)
    pipe.Perform(1e-7 * max(1.0, radius), False, GeomAbs_Shape.GeomAbs_C1, 11, 100)
    if not pipe.IsDone():
        raise Unsupported("the constant-radius pipe between the contact curves could not be made")
    face = BRepBuilderAPI_MakeFace(pipe.Surface(), 1e-7).Face()
    begin = contour.contact(segment.low)
    finish = contour.contact(segment.high)
    edges = tight._edges(face)

    def closest(points):
        return min(edges, key=lambda edge: sum(min(p.Distance(q) for q in tight._samples(edge, 9)) for p in points))

    start = closest([begin[1], begin[2]])
    end = closest([finish[1], finish[2]])
    upper = closest([crest.Value(crest.FirstParameter() + (crest.LastParameter() - crest.FirstParameter()) * f)
                     for f in (0.0, 0.5, 1.0)])
    if len({edges.index(edge) for edge in (start, end, upper)}) != 3:
        raise Unsupported("the pipe has unexpected boundary edges")
    return tight._Side(face, upper, start, end)


def _rim_pieces(contour, low, high):
    pieces = []
    for shift in (0.0, contour.length):
        for begin, finish, index in contour.spans:
            lo, hi = max(low, begin + shift), min(high, finish + shift)
            if hi - lo <= 1e-9 * contour.length:
                continue
            curve = _curves(contour, lo, hi, max(32, int((hi - lo) / contour.length * 512)))[1]
            edge = BRepBuilderAPI_MakeEdge(curve).Edge()
            wall = contour.walls[index]
            _pcurve(edge, wall, 1e-7)
            pieces.append((edge, wall))
    return pieces


def _cap(this, following, loop, contour, radius):
    filling = BRepOffsetAPI_MakeFilling(3, 30, 3, False, 1e-5, 1e-5, 1e-2, 0.1, 8, 9)
    filling.Add(TopoDS.Edge_s(this.end), TopoDS.Face_s(this.face), GeomAbs_Shape.GeomAbs_G1, True)
    filling.Add(TopoDS.Edge_s(following.start), TopoDS.Face_s(following.face), GeomAbs_Shape.GeomAbs_G1, True)
    for edge, wall in _rim_pieces(contour, *loop):
        filling.Add(edge, TopoDS.Face_s(wall), GeomAbs_Shape.GeomAbs_G1, True)
    filling.Build()
    if (not filling.IsDone() or filling.G0Error() > 1e-4 * max(1.0, radius)
            or filling.G1Error() > tight._CAP_G1_ERROR):
        raise Unsupported("the cap at the tip could not be filled smoothly")
    return filling.Shape()


class _Trimmed:
    def __init__(self, replacements):
        self.replacements = replacements

    def Modified(self, face):
        return next((pieces for original, pieces in self.replacements if original.IsSame(face)), [])

    def IsDeleted(self, face):
        return False


def _trim_wall(wall, edges):
    split = BRepFeat_SplitShape(wall)
    wire = tight._wire(edges)
    split.Add(TopoDS.Wire_s(wire), TopoDS.Face_s(wall))
    split.Build()
    if not split.IsDone():
        raise Unsupported("the contact curve could not split an original wall")
    pieces = tight._faces(split.Shape())
    if len(pieces) != 2:
        raise Unsupported(f"the contact curve did not split a wall into two bands ({len(pieces)} faces)")
    return pieces


def round_surface_loop(shape, top, radius):
    if len(_solids(shape)) != 1:
        raise Unsupported("the body must be one solid")
    walls = _walls(shape, top)
    up = _up(top, walls)
    section, base = _cut(shape, top, walls, up, radius)
    contour = _Contacts(section, top, walls, up, radius)
    for i in range(512):
        if gp_Vec(base, contour.contact(contour.length * i / 512)[1]).Dot(up) < 1e-5:
            raise Unsupported("the cutting plane is not below all wall contacts")
    segments, loops = tight._center_path(contour, radius)
    sides = [_side(segment, contour, radius) for segment in segments]
    caps = [_cap(this, following, loop, contour, radius)
            for this, following, loop in zip(sides, sides[1:] + sides[:1], loops[1:] + loops[:1])]
    for side in sides:
        _pcurve(side.top, top, 1e-7)
    splitter = BRepFeat_SplitShape(top)
    splitter.Add(TopoDS.Wire_s(tight._wire([side.top for side in sides])), TopoDS.Face_s(top))
    splitter.Build()
    if not splitter.IsDone():
        raise Unsupported("the contact curves could not split the original top face")
    original = [edge for edge in tight._edges(top) if not BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(top))
                and not BRep_Tool.Degenerated_s(edge)]
    lenses = [face for face in tight._faces(splitter.Shape())
              if not any(indexed_map(face, TopAbs_EDGE).Contains(edge) for edge in original)]
    if len(lenses) != 1:
        raise Unsupported(f"the top contact curves do not enclose one lens ({len(lenses)} faces)")
    lens = lenses[0]
    replacements = []
    all_rims = _rim_pieces(contour, 0.0, contour.length)
    top_edges = tight._edges(top)
    for wall in walls:
        edges = [edge for edge, owner in all_rims if owner.IsSame(wall)]
        pieces = _trim_wall(wall, edges)
        retained = [piece for piece in pieces if not any(indexed_map(piece, TopAbs_EDGE).Contains(edge) for edge in top_edges)]
        if len(retained) != 1:
            raise Unsupported("the wall band below the contacts could not be identified")
        replacements.append((wall, retained))
    outer = [shell for shell in (indexed_map(shape, TopAbs_SHELL).FindKey(i)
                                for i in range(1, indexed_map(shape, TopAbs_SHELL).Extent() + 1))
             if indexed_map(shell, TopAbs_FACE).Contains(top)]
    if len(outer) != 1:
        raise Unsupported("the top is not on one outer shell")
    kept = [piece for face in tight._faces(outer[0]) if not face.IsSame(top)
            for piece in next((pieces for wall, pieces in replacements if wall.IsSame(face)), [face])]
    cavities = [indexed_map(shape, TopAbs_SHELL).FindKey(i) for i in range(1, indexed_map(shape, TopAbs_SHELL).Extent() + 1)
                if not indexed_map(shape, TopAbs_SHELL).FindKey(i).IsSame(outer[0])]
    result, sewing = tight._closed_solid([*kept, lens, *(side.face for side in sides), *caps],
                                         1e-6 * tight._diagonal(shape), "joining the contact-trimmed wall and crown", cavities)
    if not 0 < tight._precise_volume(result) < tight._precise_volume(shape):
        raise Unsupported("the rounding did not remove a positive volume")
    successor = sewing.Modified(lens) if sewing.IsModified(lens) else lens
    return tight.Tight(result, top, successor, [_Trimmed(replacements), tight._Sewn(sewing)])
