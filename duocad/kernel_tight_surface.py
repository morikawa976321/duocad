"""曲面の上面・テーパー壁の接点を実面から求める、曲率半径を超える丸め。

球の中心は壁の法線上に置き、上面への射影距離が R となる壁の母線位置を求根する。
中心の道を母線方向に射影して燕尾を切り、交点は空間でも一致することを検査する。
残した道と両接点の線から半径一定の管を作り、燕尾の壁接点を境界に G1 の帽子を張る。
上面と壁は接点の線で切り、元の面を支えに使う。切断面より上に別の面がある形は断る。
"""
from __future__ import annotations

import math

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeVertex
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeFilling
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.GeomAPI import GeomAPI_Interpolate, GeomAPI_ProjectPointOnCurve, GeomAPI_ProjectPointOnSurf
from OCP.GeomAbs import GeomAbs_Shape, GeomAbs_SurfaceType
from OCP.GeomFill import GeomFill_Pipe
from OCP.GeomProjLib import GeomProjLib
from OCP.ShapeFix import ShapeFix_Edge
from OCP.ShapeAnalysis import ShapeAnalysis_Surface
from OCP.TColgp import TColgp_HArray1OfPnt
from OCP.TColStd import TColStd_HArray1OfReal
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_ON, TopAbs_OUT, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_WIRE
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_ListOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Pnt2d, gp_Vec

from . import kernel_tight as tight
from .kernel_overflow import Unsupported, _boolean, _outward_normal, _solids
from .mesh_geometry import indexed_map


def _projection(point, surface):
    """支持面上の最近点と、その UV 座標を返す。"""
    projection = GeomAPI_ProjectPointOnSurf(point, surface)
    if not projection.IsDone() or not projection.NbPoints():
        raise Unsupported("a contact point could not be projected onto its support surface")
    return projection.NearestPoint(), projection.LowerDistanceParameters()


def _normal(face, u, v):
    """面上の点と、面の向きを反映した外向き単位法線を返す。"""
    point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    BRep_Tool.Surface_s(TopoDS.Face_s(face)).D1(u, v, point, du, dv)
    normal = du.Crossed(dv).Normalized()
    if face.Orientation() == TopAbs_REVERSED:
        normal.Reverse()
    return point, normal


def _contact_state(face, uv):
    """UV 範囲内の全周期の代表点で、トリム内の接点かを返す。"""
    face = TopoDS.Face_s(face)
    surface = BRep_Tool.Surface_s(face)
    u, v = uv
    first_u, last_u, first_v, last_v = BRepTools.UVBounds_s(face)

    def representatives(value, first, last, periodic, period):
        if not periodic:
            return [value]
        return [value + k * period for k in range(math.ceil((first - value - 1e-7) / period),
                                                 math.floor((last - value + 1e-7) / period) + 1)]

    us = representatives(u, first_u, last_u, surface.IsUPeriodic(), surface.UPeriod() if surface.IsUPeriodic() else 0)
    vs = representatives(v, first_v, last_v, surface.IsVPeriodic(), surface.VPeriod() if surface.IsVPeriodic() else 0)
    state = TopAbs_OUT
    for u in us:
        for v in vs:
            state = BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State()
            if state in (TopAbs_IN, TopAbs_ON):
                return state
    return state


def _walls(shape, top):
    """上面の外周に隣接する壁を、重複なしで返す。"""
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
    """平面の上向き法線、または全壁に共通する直線母線の向きを返す。"""
    if BRepAdaptor_Surface(TopoDS.Face_s(top)).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane:
        return _outward_normal(top, tight._a_point_on(top)).Normalized()
    directions, normals = [], []
    for wall in walls:
        surface = BRepAdaptor_Surface(TopoDS.Face_s(wall))
        kind = surface.GetType()
        if kind == GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion:
            directions.append(gp_Vec(surface.Direction()))
        elif kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            directions.append(gp_Vec(surface.Cylinder().Axis().Direction()))
        elif kind == GeomAbs_SurfaceType.GeomAbs_Plane:
            normals.append(gp_Vec(surface.Plane().Axis().Direction()))
        else:
            raise Unsupported("a curved top combined with tapered or non-extruded walls is not supported")
    if not directions:
        directions = [a.Crossed(b) for a in normals for b in normals if a.Crossed(b).Magnitude() > 1e-8]
    if not directions:
        raise Unsupported("the walls do not determine a common straight generator")
    up = directions[0].Normalized()
    if (any(not gp_Dir(up).IsParallel(gp_Dir(direction), 1e-8) for direction in directions[1:])
            or any(abs(normal.Dot(up)) > 1e-8 for normal in normals)):
        raise Unsupported("the walls of a curved top must have parallel straight generators")
    if up.Dot(_outward_normal(top, tight._a_point_on(top))) < 0:
        up.Reverse()
    return up


def _cut(shape, top, walls, up, radius):
    """壁接点の最低点の少し下で切り、単一の断面・基点・上側の体積を返す。"""
    size = tight._diagonal(shape)
    trial = _Contacts(top, top, walls, up, radius)
    origin = trial.origin
    count = max(512, 64 * trial.edges)
    step = trial.length / count

    def height(s):
        """仮の輪郭パラメータで求めた壁接点の高さを返す。"""
        return gp_Vec(origin, trial.contact(s)[1]).Dot(up)

    heights = [height(i * step) for i in range(count)]
    margin = max(1e-5 * size, 0.01 * radius)
    minima = [tight._golden_minimum(height, (i - 1) * step, (i + 1) * step, 24)
              for i in range(count) if max(heights) - min(heights) > margin
              and heights[i] < heights[i - 1] and heights[i] < heights[(i + 1) % count]]
    level = min([*heights, *(height(s) for s in minima)]) - margin
    base = origin.Translated(up.Multiplied(level))
    x = tight._any_perpendicular(gp_Dir(up)).Normalized()
    y = up.Crossed(x)
    corner = base.Translated(x.Multiplied(-size)).Translated(y.Multiplied(-size))
    box = BRepPrimAPI_MakeBox(gp_Ax2(corner, gp_Dir(up), gp_Dir(x)), 2 * size, 2 * size, 2 * size).Shape()
    above = _boolean("common", shape, box)
    below = _boolean("cut", shape, box)
    if len(_solids(below.Shape())) != 1:
        raise Unsupported("the wall contacts are too close to the bottom for a cutting plane, or the plane splits the body")
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
    return section[0], base, tight._precise_volume(above.Shape(), curved=True)


class _Contacts(tight._Contour):
    """輪郭パラメータから、球の中心・壁接点・上面接点・壁番号を求める。"""

    def __init__(self, section, top, walls, up, radius):
        """輪郭の各辺を元の壁に対応付け、接点のキャッシュを用意する。"""
        boundary = [edge for edge in tight._edges(BRepTools.OuterWire_s(TopoDS.Face_s(section)))
                    if not BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(section)) and not BRep_Tool.Degenerated_s(edge)]
        super().__init__(tight._wire(boundary), up)
        self.top = top
        self.surface = BRep_Tool.Surface_s(TopoDS.Face_s(top))
        self.walls = walls
        self.radius = radius
        self.cache = {}
        self.offsets = {}
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
        """上面に半径 R で接する中心・壁接点・上面接点・壁番号を返す。"""
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

        def ball(parameter):
            """母線上の位置に対する壁接点と球の中心を返す。"""
            foot, normal = _normal(wall, parameter, v) if along_u else _normal(wall, u, parameter)
            return foot, foot.Translated(normal.Multiplied(-self.radius))

        def evaluate(parameter):
            """上面との距離の残差・接点・射影先の法線と UV 座標を返す。"""
            foot, center = ball(parameter)
            crest, uv = _projection(center, self.surface)
            _, outward = _normal(self.top, *uv)
            residual = gp_Vec(center, crest).Dot(outward) - self.radius
            return residual, (center, foot, crest, index), outward, uv

        seed = u if along_u else v
        parameter = seed + self.offsets.get(index, 0.0)
        for _ in range(24):
            residual, contacts, outward, crest_uv = evaluate(parameter)
            if abs(residual) < 1e-10 * max(1.0, self.radius):
                break
            h = 1e-5 * max(1.0, abs(parameter))
            derivative = -gp_Vec(ball(parameter - h)[1], ball(parameter + h)[1]).Dot(outward) / (2 * h)
            if abs(derivative) < 1e-12:
                raise Unsupported("the wall and top offset surfaces do not meet transversely")
            parameter -= residual / derivative
        else:
            raise Unsupported("the ball contacts on the wall and top did not converge")
        center, foot, crest, _ = contacts
        if (abs(center.Distance(crest) - self.radius) > 1e-7 * max(1.0, self.radius)
                or gp_Vec(foot, crest).Dot(self.up) <= 0):
            raise Unsupported("the contact solution does not give a convex rounding above the wall")
        wall_uv = (parameter, v) if along_u else (u, parameter)
        for face, uv in ((wall, wall_uv), (self.top, crest_uv)):
            if _contact_state(face, uv) not in (TopAbs_IN, TopAbs_ON):
                raise Unsupported("a ball contact falls outside the trimmed top or wall face (the radius may reach the bottom)")
        self.offsets[index] = parameter - seed
        self.cache[key] = contacts
        return contacts

    def center(self, s, radius):
        """上面と壁に半径 R で接する球の中心を返す。"""
        return self.contact(s)[0]

    def bend(self, s):
        """中心の道の進行が反転するかを判定する符号付きの曲率を返す。"""
        h = 1e-5 * self.length
        tangent = gp_Vec(self.at(s - h)[0], self.at(s + h)[0])
        derivative = gp_Vec(self.center(s - h, self.radius), self.center(s + h, self.radius))
        return (1.0 - derivative.Dot(tangent) / tangent.SquareMagnitude()) / self.radius

    def clear_of_walls(self, radius, loops, count):
        """残す道の球が、どの壁にも半径より近づかないことを検査する。"""
        for i in range(count):
            s = i * self.length / count
            if any(low < s < high or low < s + self.length < high for low, high in loops):
                continue
            center = self.center(s, self.radius)
            for wall in self.walls:
                nearest, uv = _projection(center, BRep_Tool.Surface_s(TopoDS.Face_s(wall)))
                limit = self.radius - 1e-7 * max(1.0, self.radius)
                if nearest.Distance(center) < limit:
                    if _contact_state(wall, uv) not in (TopAbs_IN, TopAbs_ON):
                        distance = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(center).Vertex(), wall)
                        if not distance.IsDone():
                            raise Unsupported("the distance to a trimmed wall could not be measured")
                        if distance.Value() >= limit:
                            continue
                    raise Unsupported("the ball would cut into the opposite wall (the top is too narrow for the radius)")


def _curves(contour, low, high, count=128):
    """同じパラメータを持つ中心・壁接点・上面接点の補間曲線を返す。"""
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


def _support_pieces(edge, face, role="support"):
    """pcurve を作る前に、支持面の周期の切れ目で3D辺を分ける。"""
    edge, face = TopoDS.Edge_s(edge), TopoDS.Face_s(face)
    surface = BRep_Tool.Surface_s(face)
    axes = [(axis, period, origin) for axis, periodic, period, origin in (
        (0, surface.IsUPeriodic(), surface.UPeriod() if surface.IsUPeriodic() else 0, surface.Bounds()[0]),
        (1, surface.IsVPeriodic(), surface.VPeriod() if surface.IsVPeriodic() else 0, surface.Bounds()[2])) if periodic]
    curve = BRep_Tool.Curve_s(edge, 0.0, 0.0)
    adaptor = BRepAdaptor_Curve(edge)
    first, last = adaptor.FirstParameter(), adaptor.LastParameter()
    analysis = ShapeAnalysis_Surface(surface)
    for i in range(1, analysis.NbSingularities(1e-7) + 1):
        pole = gp_Pnt()
        if analysis.Singularity(i, 0.0, pole, gp_Pnt2d(), gp_Pnt2d(), 0.0, 0.0, False):
            nearest = GeomAPI_ProjectPointOnCurve(pole, curve, first, last)
            # 設計上の除外幅。極の内外ではなく、接点辺までの3D距離を使う。
            distance = min(pole.Distance(adaptor.Value(first)), pole.Distance(adaptor.Value(last)),
                           nearest.LowerDistance() if nearest.NbPoints() else math.inf)
            if distance < 1e-4 * tight._diagonal(face):
                raise Unsupported(f"the {role} surface parameter pole is near a contact curve")
    if not axes:
        return [edge]
    parameters = [first + (last - first) * i / 128 for i in range(129)]
    uvs = [_projection(adaptor.Value(t), surface)[1] for t in parameters]
    cuts = []
    for axis, period, origin in axes:
        # 面に保持されたシームが支持面の標準の周期とずれている場合も拾う。
        seams = [origin]
        for seam in tight._edges(face):
            if BRep_Tool.IsClosed_s(seam, face):
                pc = BRep_Tool.CurveOnSurface_s(seam, face, 0.0, 0.0)
                if pc is not None:
                    p = pc.Value(pc.FirstParameter())
                    value = p.X() if axis == 0 else p.Y()
                    if all(abs((value - known + period / 2) % period - period / 2) > 1e-9 for known in seams):
                        seams.append(value)
        for seam in seams:
            values = [(uv[axis] - seam) % period for uv in uvs]
            for i, (a, b) in enumerate(zip(values, values[1:])):
                if abs(b - a) <= period / 2:
                    continue
                lo, hi = parameters[i:i + 2]
                target = period if a > b else 0.0
                for _ in range(44):
                    middle = (lo + hi) / 2
                    value = (_projection(adaptor.Value(middle), surface)[1][axis] - seam) % period
                    value += period * round((a - value) / period)
                    if (value < target) == (a < target):
                        lo = middle
                    else:
                        hi = middle
                cut = (lo + hi) / 2
                if first + 1e-10 * (last - first) < cut < last - 1e-10 * (last - first):
                    cuts.append(cut)
    cuts = sorted(cuts)
    unique = [first]
    for cut in cuts:
        if cut - unique[-1] > 1e-9 * (last - first):
            unique.append(cut)
    unique.append(last)
    if len(unique) == 2:
        return [edge]
    # 隣り合う片は同じ頂点を使う。管・帽子側の同じ3D曲線とは縫合で分割を共有する。
    vertices = [TopExp.FirstVertex_s(edge),
                *(BRepBuilderAPI_MakeVertex(adaptor.Value(t)).Vertex() for t in unique[1:-1]),
                TopExp.LastVertex_s(edge)]
    return [BRepBuilderAPI_MakeEdge(curve, v1, v2, a, b).Edge()
            for a, b, v1, v2 in zip(unique, unique[1:], vertices, vertices[1:])]


def _pcurve(edge, face, tolerance):
    """支持面上の pcurve を付け、3D の辺からのずれを検査する。"""
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
        """pcurve と 3D の辺の標本点での最大距離を返す。"""
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
    """接点の2本の曲線の間に、半径一定の管面とその境界辺を返す。"""
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
        """指定された点列に最も近い管面の境界辺を返す。"""
        return min(edges, key=lambda edge: sum(min(p.Distance(q) for q in tight._samples(edge, 9)) for p in points))

    start = closest([begin[1], begin[2]])
    end = closest([finish[1], finish[2]])
    upper = closest([crest.Value(crest.FirstParameter() + (crest.LastParameter() - crest.FirstParameter()) * f)
                     for f in (0.0, 0.5, 1.0)])
    if len({edges.index(edge) for edge in (start, end, upper)}) != 3:
        raise Unsupported("the pipe has unexpected boundary edges")
    return tight._Side(face, upper, start, end)


def _rim_pieces(contour, low, high):
    """壁の境目で分けた壁接点の辺と、その支持面の組を返す。"""
    pieces = []
    for shift in (0.0, contour.length):
        for begin, finish, index in contour.spans:
            lo, hi = max(low, begin + shift), min(high, finish + shift)
            if hi - lo <= 1e-9 * contour.length:
                continue
            curve = _curves(contour, lo, hi, max(32, int((hi - lo) / contour.length * 512)))[1]
            edge = BRepBuilderAPI_MakeEdge(curve).Edge()
            wall = contour.walls[index]
            for piece in _support_pieces(edge, wall, "wall"):
                _pcurve(piece, wall, 1e-7)
                pieces.append((piece, wall))
    return pieces


def _cap(this, following, loop, contour, radius):
    """隣の管面と実壁に G1 で接する、燕尾を覆う帽子を返す。"""
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
    """壁の分割の由来を kernel のビルダと同じ問いで返す。"""

    def __init__(self, replacements):
        """元の壁と残す面の対応を保持する。"""
        self.replacements = replacements

    def Modified(self, face):
        """元の壁に対応する分割後の面の列を返す。"""
        return next((pieces for original, pieces in self.replacements if original.IsSame(face)), [])

    def IsDeleted(self, face):
        """壁は下側を残すため、削除扱いにしない。"""
        return False


def _split_face(face, edges):
    """周期内で連続な pcurve を持つ接点辺で支持面を分割する。"""
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(face)
    tools.Append(tight._wire(edges))
    split = BRepAlgoAPI_Splitter()
    split.SetArguments(arguments)
    split.SetTools(tools)
    split.SetNonDestructive(True)
    split.Build()
    if not split.IsDone():
        raise Unsupported("the contact curves could not split an original support face")
    pieces = tight._faces(split.Shape())
    if not pieces or not all(BRepCheck_Analyzer(piece).IsValid() for piece in pieces):
        raise Unsupported("the contact curves produced an invalid support face")
    return pieces


def _trim_wall(wall, edges):
    """壁接点の輪で元の壁を2つの帯に分け、その面を返す。"""
    pieces = _split_face(wall, edges)
    if len(pieces) != 2:
        raise Unsupported(f"the contact curve did not split a wall into two bands ({len(pieces)} faces)")
    return pieces


def round_surface_loop(shape, top, radius):
    """実面の接点で上面の外周を丸め、有効な立体と面の由来を返す。"""
    if len(_solids(shape)) != 1:
        raise Unsupported("the body must be one solid")
    walls = _walls(shape, top)
    up = _up(top, walls)
    section, base, expected = _cut(shape, top, walls, up, radius)
    contour = _Contacts(section, top, walls, up, radius)
    for i in range(512):
        if gp_Vec(base, contour.contact(contour.length * i / 512)[1]).Dot(up) < 1e-5:
            raise Unsupported("the cutting plane is not below all wall contacts")
    segments, loops = tight._center_path(contour, radius)
    sides = [_side(segment, contour, radius) for segment in segments]
    caps = [_cap(this, following, loop, contour, radius)
            for this, following, loop in zip(sides, sides[1:] + sides[:1], loops[1:] + loops[:1])]
    top_edges = [side.top for side in sides]
    if len(top_edges) == 1:
        edge = top_edges[0]
        curve = BRepAdaptor_Curve(edge)
        first, last = curve.FirstParameter(), curve.LastParameter()
        if curve.Value(first).Distance(curve.Value(last)) > 1e-7 * tight._diagonal(shape):
            raise Unsupported("the single top contact edge is not closed")
        vertex = BRepBuilderAPI_MakeVertex(curve.Value(first)).Vertex()
        top_edges = [BRepBuilderAPI_MakeEdge(BRep_Tool.Curve_s(edge, 0.0, 0.0), vertex, vertex, first, last).Edge()]
    top_edges = [piece for edge in top_edges for piece in _support_pieces(edge, top, "top")]
    for edge in top_edges:
        _pcurve(edge, top, 1e-7)
    pieces = _split_face(top, top_edges)
    original = [edge for edge in tight._edges(top) if not BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(top))
                and not BRep_Tool.Degenerated_s(edge)]
    lenses = [face for face in pieces
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
    removed = tight._precise_volume(shape, curved=True) - tight._precise_volume(result, curved=True)
    if not 0 < removed < expected:
        raise Unsupported(f"the rounding removed an implausible volume ({removed:.6g} of at most {expected:.6g})")
    successor = sewing.Modified(lens) if sewing.IsModified(lens) else lens
    return tight.Tight(result, top, successor, [_Trimmed(replacements), tight._Sewn(sewing)])
