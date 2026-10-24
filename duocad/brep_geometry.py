"""既存の積分・法線・境界箱の計算。計算法と許容差の異なる関数は分けて保つ。"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp, BRepGProp_Domain, BRepGProp_Face, BRepGProp_Vinert
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.Bnd import Bnd_Box, Bnd_OBB
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.GProp import GProp_GProps
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.gp import gp_Pnt, gp_Vec

from . import meshbody
from .brep_topology import edge_of, face_of, is_null

Vec = Tuple[float, float, float]


#: describe_shape の faces_by_type が持つキー(この順序が正。query.py が初期化に使う)
FACE_KINDS = (
    "plane",
    "cylinder_full",
    "cylinder_partial",
    "torus",
    "cone",
    "bspline",
    "other",
)


#: 全周円筒(=穴)と見なすU範囲のしきい値[度](docs/DESIGN_DECISIONS.md「全周円筒のしきい値」)。
#: shelf_attachment_GD.step の円筒22面のU範囲は全て正確に360.0度で、しきい値付近に一件も無い
#: (実測)ため、この値の選び方に結果は依存しない。
FULL_CYLINDER_DEGREES = 350.0


#: 体積・重心を精度指定の適応積分で測る面: 曲面がこれらの種類か、境界の辺がこれらの種類の平面(`_needs_adaptive`)。既定の積分は
#: 細い楕円を傾けた B スプラインの側面と、B スプラインの曲線で囲まれた平面のふたで 3〜152% ずれ、適応積分は押し出し面
#: (楕円柱の側面)で重心を 0.02 mm ずらす(どちらも 2026-10-19 実測、sol レビュー)。
_ADAPTIVE_SURFACES = (
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
    GeomAbs_SurfaceType.GeomAbs_BezierSurface,
)


_ADAPTIVE_CURVES = (
    GeomAbs_CurveType.GeomAbs_BSplineCurve,
    GeomAbs_CurveType.GeomAbs_BezierCurve,
    GeomAbs_CurveType.GeomAbs_OffsetCurve,
    GeomAbs_CurveType.GeomAbs_OtherCurve,
)


#: 適応積分の相対精度。
_ADAPTIVE_VOLUME_EPS = 1.0e-9


def volume(shape: "TopoDS_Shape") -> float:
    """形状の体積(mm^3)。

    `BRepGProp.VolumeProperties_s(shape, props)` の**既定の積分**を使う(`Eps` を渡さない)。`Eps` 付きの適応積分に
    すると精度が落ちる(Eps=1e-9で相対1.8e-10、Eps=1e-6で相対3.7e-8)。**ただし B スプライン・ベジエの面か辺を含む形は、
    面ごとに積分を選んで足す**(`_needs_adaptive` の面だけ `Eps=1e-9` の適応積分。`_volume_props`)。

    STEP往復を挟んだ体積は**bit-exactではない**(実測相対誤差 約8e-16)。比較には
    `math.isclose(rel_tol=1e-6)` を使う。面数は往復後も厳密一致(`==`)でよい。
    """
    if shape is None or is_null(shape):
        raise ValueError("cannot compute the volume of an empty shape")
    return _volume_props(shape).Mass()


def _volume_props(shape: "TopoDS_Shape") -> "GProp_GProps":
    """体積の積分(質量 = 体積、重心)。適応積分の要る面が無ければ形全体を既定の積分で1回、あれば**閉じた殻ごとに**
    その殻の境界箱の中心を基準点として面ごとに積分を選び、寄与を足す(`BRepGProp.VolumeProperties_s` の内側と同じ組み立て。
    体積の積分は閉じた殻ごとに成り立つ)。

    - 面を1枚ずつ `VolumeProperties_s` に渡してはいけない: 基準点が面の位置変換に合わせて動き、寄与を足しても体積に
      ならない(傾けた楕円の上下の平面のふたが 0 を返した。実測)。
    - 基準点を原点や形全体の境界箱の中心にすると、そこから遠い薄い殻で桁が落ちる(Z=100 の 12×6 の楕円を 0.001 mm 押して
      体積 1.3% ずれ、離れた2つの殻を持つボディで重心が 0.24 mm ずれた。どちらも sol レビューで再現)。
    """
    faces = _faces_of(shape)
    if not any(_needs_adaptive(face) for face in faces):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        return props
    shells = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SHELL)
    while explorer.More():
        shells.append(explorer.Current())
        explorer.Next()
    props = GProp_GProps(_box_centre(shape))
    for shell in shells or [shape]:
        centre = _box_centre(shell)
        part = GProp_GProps(centre)
        for face in _faces_of(shell):
            surface = BRepGProp_Face(face)
            domain = BRepGProp_Domain(face)
            if _needs_adaptive(face):
                part.Add(BRepGProp_Vinert(surface, domain, centre, _ADAPTIVE_VOLUME_EPS))
            else:
                part.Add(BRepGProp_Vinert(surface, domain, centre))
        props.Add(part)
    return props


def _faces_of(shape: Any) -> List[Any]:
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        out.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return out


def _box_centre(shape: Any) -> "gp_Pnt":
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return gp_Pnt((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0)


def _needs_adaptive(face: Any) -> bool:
    """B スプライン・ベジエの曲面か、自由曲線で囲まれた平面(実測で既定の積分が外れた面)。押し出し面などの
    他の曲面は既定のまま(適応積分は楕円柱の側面で重心をずらす)。"""
    kind = BRepAdaptor_Surface(face).GetType()
    if kind in _ADAPTIVE_SURFACES:
        return True
    if kind != GeomAbs_SurfaceType.GeomAbs_Plane:
        return False
    explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while explorer.More():
        if BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current())).GetType() in _ADAPTIVE_CURVES:
            return True
        explorer.Next()
    return False


def centroid(shape: "TopoDS_Shape") -> List[float]:
    """形状の**体積重心**(mm)。`volume` と同じ積分(`_volume_props`)を使う。

    `move_body` の回転の既定の中心。境界箱の中心は軸の向きで動くので使わない。

    体積が0の形状(面だけのシェル等)は ValueError(`Mass()` が0だと `CentreOfMass()` は無意味な点を返す)。
    """
    if shape is None or is_null(shape):
        raise ValueError("cannot compute the centroid of an empty shape")
    props = _volume_props(shape)
    if not props.Mass() > 0.0:
        raise ValueError("cannot compute the centroid of a zero-volume shape")
    point = props.CentreOfMass()
    return [float(point.X()), float(point.Y()), float(point.Z())]


def classify_face(face: Any) -> str:
    """1つの面を FACE_KINDS のいずれかへ分類する。

    円筒は `BRepAdaptor_Surface` のU範囲(度)が FULL_CYLINDER_DEGREES を超えるかで
    全周(=穴)と部分に分ける(docs/DESIGN_DECISIONS.md「全周円筒のしきい値」)。
    """
    adaptor = BRepAdaptor_Surface(TopoDS.Face_s(face))
    surface_type = adaptor.GetType()

    if surface_type == GeomAbs_SurfaceType.GeomAbs_Plane:
        return "plane"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        span = math.degrees(adaptor.LastUParameter() - adaptor.FirstUParameter())
        return "cylinder_full" if span > FULL_CYLINDER_DEGREES else "cylinder_partial"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Torus:
        return "torus"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Cone:
        return "cone"
    if surface_type in (
        GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
        GeomAbs_SurfaceType.GeomAbs_BezierSurface,
    ):
        return "bspline"
    return "other"


def _bounding_box_extents(
    shape: "TopoDS_Shape",
) -> Tuple[float, float, float, float, float, float]:
    """境界箱を OCCT から取る(`(x_min, y_min, z_min, x_max, y_max, z_max)`)。

    `BRepBndLib.AddOptimal_s(..., useTriangulation=False, useShapeTolerance=False)` を使う。
    **`Add_s` は面のトリムを見ず、下地のサーフェス全体で囲む** —— トリムされたトーラス面は
    ドーナツ丸ごとで囲まれるので、曲がったバーの部品では寸法が最大9倍に化ける
    (実測: `models/hanger_bar_attachment.json` の 26.6mm 角の部品が 241.3mm になる)。
    箱・円柱・平面では両者が一致するので、**曲面の種類によって壊れ方が変わる**。

    `useTriangulation=False` は、True(既定)だと**既存の三角形分割があればそれを使う**ため
    (同じ形状でも `tessellate` を呼んだ後だけ違う値になる)。`SetGap(0.0)` を入れても
    OCCT は内部許容差ぶん(実測 1e-7mm)膨らませた値を返す。
    """
    box = Bnd_Box()
    box.SetGap(0.0)
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        raise ValueError("bounding box is void (形状に頂点が無い)")
    return box.Get()


def _point_and_direction(
    point: Sequence[float], direction: Sequence[float], *, names: Tuple[str, str], zero_reason: str
) -> Tuple[List[float], List[float]]:
    """3要素・有限の点と、零ベクトルでない方向を float のリストにして返す(`rotate_shape` /
    `mirror_shape` の共通の入口。`gp_Dir` は零ベクトルで例外を投げるが、そこまで持って行かずに手前で断る)。"""
    first, second = names
    origin = [float(value) for value in point]
    axis = [float(value) for value in direction]
    if len(origin) != 3 or len(axis) != 3:
        raise ValueError(f"{first} / {second} must have 3 numbers, got {point!r} / {direction!r}")
    if not all(math.isfinite(value) for value in origin + axis):
        raise ValueError(f"{first} / {second} must be finite, got {point!r} / {direction!r}")
    if not math.sqrt(sum(value * value for value in axis)) > 0.0:
        raise ValueError(f"{second} must not be the zero vector ({zero_reason})")
    return origin, axis


def edge_axis(shape: "TopoDS_Shape", edge_index: int) -> tuple:
    """**まっすぐな辺**を回転軸 `([x, y, z], [dx, dy, dz])` にする(点は辺の上、向きは単位長)。

    添字は `edge_count` / `edge_of` と同じ1始まり(辺の選択の識別子そのもの)。
    まっすぐでない辺(円・spline)は ValueError(中心線を勝手に作ると指した辺と回る軸が食い違う)。
    """
    edge = edge_of(shape, edge_index)
    if classify_edge(edge) != "line":
        raise ValueError(
            f"edge {int(edge_index)} is not a straight line "
            f"(kind={classify_edge(edge)!r}): 回転軸に使えるのはまっすぐな辺だけ"
        )
    line = BRepAdaptor_Curve(edge).Line()
    point = line.Location()
    direction = line.Direction()
    return (
        [float(point.X()), float(point.Y()), float(point.Z())],
        [float(direction.X()), float(direction.Y()), float(direction.Z())],
    )


def edge_circle(shape: "TopoDS_Shape", edge_index: int) -> tuple:
    """**円(円弧を含む)の辺**の `([cx, cy, cz], radius)`(ワールド座標[mm])。

    添字は `edge_count` / `edge_of` と同じ1始まり。円でない辺は ValueError。
    """
    edge = edge_of(shape, edge_index)
    if classify_edge(edge) != "circle":
        raise ValueError(f"edge {int(edge_index)} is not a circle (kind={classify_edge(edge)!r})")
    circle = BRepAdaptor_Curve(edge).Circle()
    center = circle.Location()
    return ([float(center.X()), float(center.Y()), float(center.Z())], float(circle.Radius()))


def face_plane(shape: "TopoDS_Shape", face_index: int) -> tuple:
    """**平らな面**を「面積重心と外向き単位法線」`([x, y, z], [nx, ny, nz])` にする(測定用)。

    添字は `face_count` / `face_of` と同じ1始まり。平面でない面は ValueError(曲面を平面扱いすると
    指した面と測る平面が食い違う)。重心は `BRepGProp` の面積重心で、平面なら必ず面と同じ平面に乗る。
    """
    face = face_of(shape, face_index)
    kind = classify_face(face)
    if kind != "plane":
        raise ValueError(
            f"face {int(face_index)} is not planar (kind={kind!r}): 平面として測れるのは平らな面だけ"
        )
    _, centre = _face_props(face)
    normal = _face_normal(face)
    return ([float(v) for v in centre], [float(v) for v in normal])


#: 辺の種別(`classify_edge` が返す値)
EDGE_KINDS = ("line", "circle", "other")


def edge_length(edge: Any) -> float:
    """辺の長さ[mm]。辺の長さを出すところは全部ここを通す(画面の辺・`measure`・述語の `length_lt` / `length_gt`)。

    外部の罠: `GCPnts_AbscissaPoint.Length_s` の既定の許容差も build123d の `Edge.length`
    (`BRepGProp.LinearProperties_s`)も、楕円・B-spline の辺で粗い(2026-10-17 実測: 楕円の柱の辺で
    直径 20×10 は +0.13%、直径 40×2 は +1.74%。ベジエの辺は 1e-5。円・直線は厳密)。許容差 1e-9 なら数値積分と
    1e-11 まで一致し、時間は細い楕円の辺1本で 0.04ms(ベジエ 0.004ms)。
    """
    return float(GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge), 1.0e-9))


def classify_edge(edge: Any) -> str:
    """1つの辺を EDGE_KINDS のいずれかへ分類する。

    """
    adaptor = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    curve_type = adaptor.GetType()
    if curve_type == GeomAbs_CurveType.GeomAbs_Line:
        return "line"
    if curve_type == GeomAbs_CurveType.GeomAbs_Circle:
        return "circle"
    return "other"


def _face_normal(face: Any) -> tuple:
    """面の中央での**外向き**単位法線。`BRepGProp_Face` は面の向きを見るので反転面でも外を向く(20mm立方体の6面で実測)。"""
    gface = BRepGProp_Face(TopoDS.Face_s(face))
    u_min, u_max, v_min, v_max = gface.Bounds()
    point = gp_Pnt()
    normal = gp_Vec()
    gface.Normal((u_min + u_max) / 2.0, (v_min + v_max) / 2.0, point, normal)
    if normal.Magnitude() <= 0:
        raise ValueError("face normal is degenerate (magnitude 0)")
    normal.Normalize()
    return (normal.X(), normal.Y(), normal.Z())


def _face_props(face: Any) -> tuple:
    """(面積, 重心)。`BRepGProp.SurfaceProperties_s` の既定積分。"""
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(TopoDS.Face_s(face), props)
    centre = props.CentreOfMass()
    return props.Mass(), (centre.X(), centre.Y(), centre.Z())


def _bounding_box(faces: List[dict]) -> dict:
    """三角形の節点から境界箱を作る(カメラ合わせ用)。面が無ければ原点の退化した箱。"""
    lows = [float("inf")] * 3
    highs = [float("-inf")] * 3
    for face in faces:
        positions = face["positions"]
        for offset in range(0, len(positions), 3):
            for axis in range(3):
                value = positions[offset + axis]
                lows[axis] = min(lows[axis], value)
                highs[axis] = max(highs[axis], value)
    if any(low == float("inf") for low in lows):
        lows, highs = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    return meshbody.bbox_payload(lows, highs)


def _mass_and_center(solid: "TopoDS_Shape"):
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, props)
    mass = props.Mass()
    if not math.isfinite(mass) or mass < 0:
        raise RuntimeError("cell piece volume is not finite/non-negative")
    point = props.CentreOfMass()
    return mass, (point.X(), point.Y(), point.Z())


def _extrude_shape_area(shape: Any) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return props.Mass()


def _extrude_edge_length(edge: Any) -> float:
    return GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge))


def _extrude_area_centroid(face: Any) -> Vec:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return (c.X(), c.Y(), c.Z())


def _face_edit_face_area(face: Any) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


def _face_edit_face_normal(face: Any) -> tuple:
    """面の UV の中点での**外向き**法線(面の向きを反映)。"""
    adaptor = BRepAdaptor_Surface(face)
    u = 0.5 * (adaptor.FirstUParameter() + adaptor.LastUParameter())
    v = 0.5 * (adaptor.FirstVParameter() + adaptor.LastVParameter())
    normal = BRepLProp_SLProps(adaptor, u, v, 1, 1e-7).Normal()
    sign = -1.0 if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED else 1.0
    return (sign * normal.X(), sign * normal.Y(), sign * normal.Z())


def _interference_loose_bbox(shape: TopoDS_Shape) -> Tuple[Vec, Vec]:
    """緩い境界箱(`BRepBndLib.Add`)。真の箱を含むので絞り込みで組を落とさない。形状許容差の分だけ広がる。

    `kernel.bounding_box`(`AddOptimal`)は PC ケースで 1.7 秒に対しこちらは 7 ms。表示する寸法には使わない。
    """
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return (x0, y0, z0), (x1, y1, z1)


def _interference_area(shape: TopoDS_Shape) -> float:
    properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, properties)
    return properties.Mass()


def _interference_volume(shape: TopoDS_Shape) -> float:
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return properties.Mass()


def _interference_obb_sides(shape: TopoDS_Shape) -> List[float]:
    """有向境界箱の 3 辺の長さ(短い順)。"""
    box = Bnd_OBB()
    BRepBndLib.AddOBB_s(shape, box, False, True, False)
    return sorted(2 * half for half in (box.XHSize(), box.YHSize(), box.ZHSize()))


def _interference_volume_center(shape: TopoDS_Shape) -> List[float]:
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    center = properties.CentreOfMass()
    return [center.X(), center.Y(), center.Z()]


def _interference_exact_bbox(shape: TopoDS_Shape) -> Tuple[List[float], List[float]]:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return [x0, y0, z0], [x1, y1, z1]


def _overflow_diagonal(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


def _overflow_edge_length(edge: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    return props.Mass()


def _thread_volume_eps(shape) -> float:
    """Eps 付きの体積。既定の積分は何周も巻いた B-spline の面で狂う(docs/THREAD.md「体積の既定積分はねじ山で狂う」)。"""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props, 1e-7)
    return props.Mass()


def _tight_a_point_on(face: TopoDS_Shape) -> gp_Pnt:
    surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
    return surface.Value(0.5 * (surface.FirstUParameter() + surface.LastUParameter()),
                         0.5 * (surface.FirstVParameter() + surface.LastVParameter()))


def _tight_diagonal(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return max(1.0, math.dist((x0, y0, z0), (x1, y1, z1)))


def _overflow_volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def outward_normal(face: TopoDS_Shape, point: gp_Pnt) -> Optional[gp_Vec]:
    surface = BRep_Tool.Surface_s(TopoDS.Face_s(face))
    u, v = GeomAPI_ProjectPointOnSurf(point, surface).LowerDistanceParameters()
    props = BRepLProp_SLProps(BRepAdaptor_Surface(TopoDS.Face_s(face)), u, v, 1, 1.0e-7)
    if not props.IsNormalDefined():
        return None
    normal = gp_Vec(props.Normal())
    if face.Orientation() == TopAbs_REVERSED:
        normal.Reverse()
    return normal
