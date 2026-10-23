"""ねじ山を全周の円筒面に実体で刻む幾何(OCP を触るカーネル層)。

作り方は docs/THREAD.md「形の作り方」(集合演算でねじの面を作らず、山形の折れ線の各区間を2本のヘリックスの間の線織面として
直接組む。試作 `tools/thread_prototype.py`)。山形・隙間の上下限・推奨の範囲・既定値は docs/THREAD_SIM.md 6.10
(試し刷り TP1〜TP4 で確かめた評価。本人のふだんの印刷 0.12mm Fine)。依存は kernel_thread → kernel の一方向。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `"shape"`。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_Transform,
)
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeRevol
from OCP.BRep import BRep_Tool
from OCP.GCE2d import GCE2d_MakeSegment
from OCP.Geom import Geom_BSplineSurface
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.GeomAPI import GeomAPI_Interpolate
from OCP.GProp import GProp_GProps
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.ShapeFix import ShapeFix_Solid
from OCP.TColgp import TColgp_Array2OfPnt, TColgp_HArray1OfPnt
from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal, TColStd_HArray1OfReal
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax1, gp_Ax2, gp_Ax3, gp_Dir, gp_Pnt, gp_Pnt2d, gp_Trsf, gp_Vec

from . import kernel
from .kernel import _face_at, _face_map, _face_map_projection, _require_valid, _target_successors
from .mesh_geometry import indexed_map

# 山形の族。深さ(半円は丸みの半径)はピッチの 0.4 倍。隙間は半円が法線方向、台形が半径方向(試し刷りと同じ付け方)。
# recommended / default_female はナット側(穴)の値。軸(雄)の既定は 0(隙間はナット側に持たせると強い。THREAD_SIM.md 5.5)。
PROFILES: Dict[str, dict] = {
    "semicircle": dict(label="半円", offset="normal", recommended=(0.11, 0.14), default_female=0.12),
    "trapezoid30": dict(label="30°台形", beta=30.0, offset="radial", recommended=(0.18, 0.22), default_female=0.20),
    "trapezoid45": dict(label="45°台形", beta=45.0, offset="radial", recommended=(0.11, 0.13), default_female=0.12),
}
DEPTH_PER_PITCH = 0.4
SAMPLES_PER_TURN = 64
_FILL_MARGIN = 0.01  # 埋める・切る円柱を面より 0.01 太くする(谷の帯と既存の円筒面を重ねると三角形に分割できない面が残る。実測)
_VOLUME_TOLERANCE = 0.01  # 体積の増減の許容 = ねじ部の体積の 1%(docs/THREAD.md「合否の門と検査」)
_RELIEF = 0.05  # 雄の根元の首を谷より細くする量(谷の帯と首の円錐を同じ半径で接させない)
_AIR_EPS = 1e-6  # 軸の中の空きを「無い」とみなす体積(mm³)


def default_pitch(diameter: float) -> float:
    """径から決める既定のピッチ: 0.25D(0.05 刻み)、ただし 1.2mm 以上(半円の D3 は細かいピッチでガタ無しの範囲が無い。THREAD_SIM.md 6.10)。"""
    return max(1.2, round(0.25 * float(diameter) / 0.05) * 0.05)


def clearance_limit(profile: str, pitch: float) -> float:
    """受け付ける隙間の上限: 誤差 0 でも掛かりが 0.1mm を切る隙間(計算に当てた式。THREAD_SIM.md 6.10)。"""
    if profile == "semicircle":
        return 0.24 * pitch - 0.1
    return DEPTH_PER_PITCH * pitch - 0.125


def recommended_range(profile: str, female: bool) -> Tuple[float, float]:
    """外れたら警告する隙間の範囲。穴(雌)は評価の推奨、軸(雄)は 0 だけ。"""
    return tuple(PROFILES[profile]["recommended"]) if female else (0.0, 0.0)


def default_clearance(profile: str, female: bool) -> float:
    return float(PROFILES[profile]["default_female"]) if female else 0.0


def _require_profile(profile: str) -> dict:
    if profile not in PROFILES:
        raise ValueError(f"profile は {' / '.join(PROFILES)} のどれか(受け取ったのは {profile!r})")
    return PROFILES[profile]


# --------------------------------------------------------------------------------------
# 山形(1ピッチ分の折れ線 [(s, r)]、s は軸方向 0..P)
# --------------------------------------------------------------------------------------
def _bolt_points(profile: str, radius: float, pitch: float) -> List[Tuple[float, float]]:
    """雄の呼び寸法の山形(山頂の半径が `radius`)。"""
    depth = DEPTH_PER_PITCH * pitch
    if profile == "semicircle":
        rho, half = depth, pitch / 2
        core = radius - rho
        pts = [(0.0, core), (half - rho, core)]
        n = 96
        for i in range(1, n):
            a = math.pi - math.pi * i / n
            pts.append((half + rho * math.cos(a), core + rho * math.sin(a)))
        return pts + [(half + rho, core), (pitch, core)]
    width = depth * math.tan(math.radians(PROFILES[profile]["beta"]))
    flat = (pitch - 2 * width) / 2  # 山頂と谷の平坦を半々
    a = flat / 2
    lo, hi = radius - depth, radius
    return [(0.0, lo), (a, lo), (a + width, hi), (a + width + flat, hi), (a + 2 * width + flat, lo), (pitch, lo)]


def _offset_normal(s: np.ndarray, r: np.ndarray, pitch: float, c: float) -> np.ndarray:
    """山形の曲線を法線方向に c 動かす(c>0 で外へ)。f_c(s) = max_{|t|≤c} f(s+t) + sqrt(c²−t²)。"""
    ds = pitch / len(s)
    m = int(math.ceil(abs(c) / ds))
    t = np.arange(-m, m + 1) * ds
    t = t[np.abs(t) <= abs(c)]
    cap = np.sqrt(np.maximum(c * c - t * t, 0.0))
    idx = (np.arange(len(s))[:, None] + np.round(t / ds).astype(int)[None, :]) % len(s)
    return (r[idx] + cap[None, :]).max(axis=1) if c >= 0 else (r[idx] - cap[None, :]).min(axis=1)


def _simplify(s: np.ndarray, r: np.ndarray, pitch: float, tol: float = 0.003) -> List[Tuple[float, float]]:
    """等間隔の点列を、端と角を残して間引いた折れ線にする(偏差 `tol` 以内)。"""
    pts = np.column_stack([np.append(s, pitch), np.append(r, r[0])])
    keep = np.zeros(len(pts), bool)
    keep[[0, -1]] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = pts[b] - pts[a]
        seg = pts[a + 1:b]
        dist = np.abs(d[0] * (seg[:, 1] - pts[a, 1]) - d[1] * (seg[:, 0] - pts[a, 0])) / max(math.hypot(*d), 1e-15)
        i = int(np.argmax(dist))
        if dist[i] > tol:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return [(float(x), float(y)) for x, y in pts[keep]]


def profile_points(profile: str, radius: float, pitch: float, clearance: float, female: bool) -> List[Tuple[float, float]]:
    """刻む面の山形。雄は呼び寸法から隙間だけ内へ、雌はその外側の面として隙間だけ外へ(面の半径 = 呼び径の半分)。"""
    spec = _require_profile(profile)
    pts = _bolt_points(profile, radius, pitch)
    c = clearance if female else -clearance
    if spec["offset"] == "radial":
        return [(s, r + c) for s, r in pts]
    s = np.linspace(0.0, pitch, 2400, endpoint=False)
    r = np.interp(s, [p[0] for p in pts], [p[1] for p in pts])
    if c != 0.0:
        r = _offset_normal(s, r, pitch, c)
    return _simplify(s, r, pitch, tol=0.005)  # 0.005 で半円は 19 点(0.003 だと 25 点で、帯が増えて集合演算が 1.5 倍遅い。STL の偏差 0.01 より細かい)


# --------------------------------------------------------------------------------------
# ねじ部の立体(z ∈ [0, length]、軸は z)。試作 tools/thread_prototype.py の threaded_segment と同じ
# --------------------------------------------------------------------------------------
def _helix(r, s, k, thetas):
    pts = TColgp_HArray1OfPnt(1, len(thetas))
    par = TColStd_HArray1OfReal(1, len(thetas))
    for i, th in enumerate(thetas, 1):
        pts.SetValue(i, gp_Pnt(r * math.cos(th), r * math.sin(th), s + k * th))
        par.SetValue(i, float(th))
    it = GeomAPI_Interpolate(pts, par, False, 1e-9)
    it.Perform()
    return it.Curve()


def _ruled(c0, c1):
    """同じノット列の2曲線の間の (3,1) 次 B-spline 面。u は θ のまま残す(端の切り口を直線にするため)。"""
    nu = c0.NbPoles()
    poles = TColgp_Array2OfPnt(1, nu, 1, 2)
    for i in range(1, nu + 1):
        poles.SetValue(i, 1, c0.Pole(i))
        poles.SetValue(i, 2, c1.Pole(i))
    uk = TColStd_Array1OfReal(1, c0.NbKnots())
    um = TColStd_Array1OfInteger(1, c0.NbKnots())
    for i in range(1, c0.NbKnots() + 1):
        uk.SetValue(i, c0.Knot(i))
        um.SetValue(i, c0.Multiplicity(i))
    vk = TColStd_Array1OfReal(1, 2)
    vm = TColStd_Array1OfInteger(1, 2)
    for i, v in ((1, 0.0), (2, 1.0)):
        vk.SetValue(i, v)
        vm.SetValue(i, 2)
    return Geom_BSplineSurface(poles, uk, vk, um, vm, c0.Degree(), 1)


def _uv_edge(surf, a, b):
    return BRepBuilderAPI_MakeEdge(GCE2d_MakeSegment(gp_Pnt2d(*a), gp_Pnt2d(*b)).Value(), surf).Edge()


def threaded_segment(points: Sequence[Tuple[float, float]], pitch: float, length: float, tol: float = 1e-5):
    """右ねじの軸(中身の詰まった立体)を集合演算なしで面から組む。帯だけ先に縫い、残った2本の縁から平面の蓋を作って縫い直す
    (蓋と帯で辺を共有させて一度に縫うと、縫い合わせが端の辺を縮退扱いにして殻にならない。実測)。"""
    k = pitch / (2 * math.pi)
    th_min, th_max = -2 * math.pi, 2 * math.pi * length / pitch
    n = int(math.ceil((th_max - th_min) / (2 * math.pi) * SAMPLES_PER_TURN)) + 1
    thetas = np.linspace(th_min, th_max, n)
    sew = BRepBuilderAPI_Sewing(tol)
    for (s0, r0), (s1, r1) in zip(points, points[1:]):
        surf = _ruled(_helix(r0, s0, k, thetas), _helix(r1, s1, k, thetas))
        a, b = (-s0 / k, 0.0), (-s1 / k, 1.0)
        c, d = ((length - s1) / k, 1.0), ((length - s0) / k, 0.0)
        wire = BRepBuilderAPI_MakeWire()
        for e in (_uv_edge(surf, a, d), _uv_edge(surf, d, c), _uv_edge(surf, c, b), _uv_edge(surf, b, a)):
            wire.Add(e)
        face = BRepBuilderAPI_MakeFace(surf, wire.Wire(), True).Face()
        BRepLib.BuildCurves3d_s(face, 1e-7)
        sew.Add(face)
    sew.Perform()
    shell = sew.SewedShape()
    caps = BRepBuilderAPI_Sewing(tol)
    caps.Add(shell)
    exp = TopExp_Explorer(ShapeAnalysis_FreeBounds(shell, tol, False, False).GetClosedWires(), TopAbs_ShapeEnum.TopAbs_WIRE)
    while exp.More():
        caps.Add(BRepBuilderAPI_MakeFace(TopoDS.Wire_s(exp.Current()), True).Face())
        exp.Next()
    caps.Perform()
    fix = ShapeFix_Solid(BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(caps.SewedShape())).Solid())
    fix.Perform()
    return fix.Solid()


def _revolve(points: Sequence[Tuple[float, float]]):
    """(r, z) の多角形を z 軸まわりに回した立体。"""
    poly = BRepBuilderAPI_MakePolygon()
    for r, z in points:
        poly.Add(gp_Pnt(r, 0, z))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    return BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))).Shape()


def _volume_eps(shape) -> float:
    """Eps 付きの体積。既定の積分は何周も巻いた B-spline の面で狂う(docs/THREAD.md「体積の既定積分はねじ山で狂う」)。"""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props, 1e-7)
    return props.Mass()


# --------------------------------------------------------------------------------------
# 選んだ円筒面を読む
# --------------------------------------------------------------------------------------
def describe_target(shape: "TopoDS_Shape", face_index: int) -> dict:
    """面がねじを刻める全周の円筒面か確かめ、軸・半径・範囲・雄か雌か・両端の隣の面の種類を返す。"""
    face = _face_at(_face_map(shape), face_index)
    if kernel.classify_face(face) != "cylinder_full":
        raise ValueError(f"ねじを刻めるのは全周の円筒面だけ(面 {face_index} は {kernel.classify_face(face)})")
    adaptor = BRepAdaptor_Surface(TopoDS.Face_s(face))
    cylinder = adaptor.Cylinder()
    frame = cylinder.Position()
    origin, axis, xdir = frame.Location(), frame.Direction(), frame.XDirection()
    radius = cylinder.Radius()
    v0, v1 = sorted((adaptor.FirstVParameter(), adaptor.LastVParameter()))
    # 外向きの法線が軸から離れる向きなら軸(雄)、軸へ向かうなら穴(雌)
    u_mid, v_mid = 0.5 * (adaptor.FirstUParameter() + adaptor.LastUParameter()), 0.5 * (v0 + v1)
    point = adaptor.Value(u_mid, v_mid)
    along = gp_Vec(origin, point).Dot(gp_Vec(axis))
    foot = origin.Translated(gp_Vec(axis).Multiplied(along))
    radial = gp_Vec(foot, point)
    normal = gp_Vec(*kernel._face_normal(face))
    female = radial.Dot(normal) < 0
    ends = {"low": _end_kind(shape, face, origin, axis, radius, v0), "high": _end_kind(shape, face, origin, axis, radius, v1)}
    return dict(face=face, origin=origin, axis=axis, xdir=xdir, radius=radius, v0=v0, v1=v1, female=female, ends=ends)


def _end_kind(shape, face, origin, axis, radius: float, v: float) -> dict:
    """面の端(軸方向の位置 v)の円の辺と、その向こうの面の種類: "outward"(外へ広がる軸に直角な平面 = 部品の表面)、
    "disc"(軸に直角な円板 = 軸の先端・止まり穴の底)、"cone"(軸へ向かって細る円錐 = 面取り済みの先)、
    "shoulder"(外へ広がる円錐 = 根元の肩・皿の座ぐり)、"other"。"""
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, ancestors)
    edges, neighbours = [], []
    exp = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        exp.Next()
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() != GeomAbs_CurveType.GeomAbs_Circle:
            continue
        centre = curve.Circle().Location()
        if abs(gp_Vec(origin, centre).Dot(gp_Vec(axis)) - v) > 1e-6:
            continue
        edges.append(edge)
        for other in ancestors.FindFromKey(edge):
            if not other.IsSame(face):
                neighbours.append(other)
    kinds = {_neighbour_kind(other, origin, axis, radius) for other in neighbours}
    kind = kinds.pop() if len(kinds) == 1 else "other"
    return dict(kind=kind, edges=[kernel.edge_index_of(shape, e) for e in edges])


def _neighbour_kind(other, origin, axis, radius: float) -> str:
    adaptor = BRepAdaptor_Surface(TopoDS.Face_s(other))
    kind = adaptor.GetType()
    cone = kind == GeomAbs_SurfaceType.GeomAbs_Cone
    if not cone and (kind != GeomAbs_SurfaceType.GeomAbs_Plane or abs(adaptor.Plane().Axis().Direction().Dot(axis)) < 0.999):
        return "other"
    far = 0.0  # 隣の面の頂点の、軸からの最大の距離(面の半径を超えれば外へ広がる)
    exp = TopExp_Explorer(other, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while exp.More():
        p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(exp.Current()))
        along = gp_Vec(origin, p).Dot(gp_Vec(axis))
        far = max(far, origin.Translated(gp_Vec(axis).Multiplied(along)).Distance(p))
        exp.Next()
    if cone:
        return "shoulder" if far > radius + 1e-6 else "cone"
    return "outward" if far > radius + 1e-6 else "disc"


def resolve(target: dict, profile: str, clearance: Optional[float], pitch: Optional[float]) -> dict:
    """ピッチ・隙間の空欄を既定で埋め、上下限で断る。推奨の範囲から外れたら `warning` に理由を入れる(断らない)。"""
    _require_profile(profile)
    female = target["female"]
    diameter = 2 * target["radius"]
    p = default_pitch(diameter) if pitch is None else float(pitch)
    if not math.isfinite(p) or p <= 0:
        raise ValueError(f"pitch は正の数(受け取ったのは {pitch!r})")
    c = default_clearance(profile, female) if clearance is None else float(clearance)
    limit = clearance_limit(profile, p)
    if not math.isfinite(c) or c < 0:
        raise ValueError(f"隙間は 0 以上(受け取ったのは {clearance!r})")
    if c > limit + 1e-9:
        raise ValueError(f"隙間 {c:g} は上限 {limit:.3f} を超える(これより開けると山が掛からない。{PROFILES[profile]['label']} P{p:g})")
    depth = DEPTH_PER_PITCH * p
    if target["radius"] - depth - (0 if female else c) <= 0.2:
        raise ValueError(f"直径 {diameter:g} に対してピッチ {p:g} が粗すぎる(山の深さ {depth:g} で芯が残らない)")
    return dict(profile=profile, pitch=p, clearance=c, female=female, limit=limit,
                recommended=recommended_range(profile, female), warning=clearance_warning(profile, female, c))


def clearance_warning(profile: str, female: bool, clearance: float) -> Optional[str]:
    """推奨の範囲の外なら警告の文(範囲の中なら None)。"""
    lo, hi = recommended_range(profile, female)
    if lo - 1e-9 <= clearance <= hi + 1e-9:
        return None
    label = PROFILES[profile]["label"]
    rec = f"{label}の穴は {lo:g}〜{hi:g}" if female else f"{label}の軸は 0。隙間はナット側に付ける"
    return f"ねじの隙間 {clearance:g} は推奨の外({rec})。刷ってはまらない・ガタが出ることがある"


# --------------------------------------------------------------------------------------
# ボディへ掛ける
# --------------------------------------------------------------------------------------
def thread_face(shape: "TopoDS_Shape", face_index: int, profile: str, clearance: Optional[float] = None,
                pitch: Optional[float] = None) -> dict:
    """面 `face_index`(1始まり)にねじを刻んだ**新しい形状**(`_thread_face`。結果は `kernel.remembered`)。"""
    key = (int(face_index), str(profile), None if clearance is None else float(clearance), None if pitch is None else float(pitch))
    return kernel.remembered("thread", (shape,), key, lambda: _thread_face(shape, face_index, profile, clearance, pitch))


def _thread_face(shape, face_index, profile, clearance, pitch) -> dict:
    """戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape", "provenance", "volume_before", "volume_after", "female", "pitch", "clearance", "limit", "recommended", "warning"}

    検査(docs/THREAD.md「合否の門と検査」): 集合演算の `IsDone`・空でない・ソリッドの数が変わらない・`BRepCheck`・
    体積の増減が期待値とねじ部の体積の 1% 以内で合う(`BRepCheck` が真のまま芯が消える壊れ方を弾く)。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot thread an empty shape")
    target = describe_target(shape, int(face_index))
    plan = resolve(target, profile, clearance, pitch)
    female, p, c = plan["female"], plan["pitch"], plan["clearance"]
    radius, depth = target["radius"], DEPTH_PER_PITCH * p
    points = profile_points(profile, radius, p, c, female)
    context = f"face {face_index}, {profile}, P{p:g}, c{c:g}"
    solids_before = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID).Extent()
    if female:
        final, steps, expected_delta, thread_volume, before = _cut_female(shape, target, points, p, depth, context)
    else:
        final, steps, expected_delta, thread_volume, before = _add_male(shape, target, points, p, depth, context)
    if final is None or final.IsNull():
        raise ValueError(f"thread produced an empty shape ({context})")
    solids = indexed_map(final, TopAbs_ShapeEnum.TopAbs_SOLID).Extent()
    if solids != solids_before:
        raise ValueError(f"thread changed the number of solids {solids_before} -> {solids} ({context})")
    _require_valid(final, "thread", context)
    after = _volume_eps(final)
    if abs((after - before) - expected_delta) > _VOLUME_TOLERANCE * thread_volume:
        raise ValueError(
            f"thread changed the volume by {after - before:.6g}, expected {expected_delta:.6g} "
            f"(±{_VOLUME_TOLERANCE * thread_volume:.3g}, {context}): ねじ部の集合演算が壊れた"
        )
    return {
        "shape": final,
        "provenance": _provenance(shape, steps, final, int(face_index), plan),
        "volume_before": float(kernel.volume(shape)),
        "volume_after": float(kernel.volume(final)),
        **{k: plan[k] for k in ("female", "pitch", "clearance", "limit", "warning")},
        "recommended": list(plan["recommended"]),
    }


def _placed(solid, target: dict, z_start: float):
    """z 軸で組んだ立体を、円筒面の軸に沿って位置 `z_start` から置く。"""
    origin, axis = target["origin"], target["axis"]
    start = origin.Translated(gp_Vec(axis).Multiplied(z_start))
    trsf = gp_Trsf()
    trsf.SetDisplacement(gp_Ax3(), gp_Ax3(start, axis, target["xdir"]))
    return BRepBuilderAPI_Transform(solid, trsf, True).Shape()


def _cylinder(target: dict, radius: float, z0: float, z1: float):
    origin, axis = target["origin"], target["axis"]
    base = origin.Translated(gp_Vec(axis).Multiplied(z0))
    return BRepPrimAPI_MakeCylinder(gp_Ax2(base, axis, target["xdir"]), radius, z1 - z0).Shape()


def _boolean(builder, label: str, context: str):
    builder.Build()
    if not builder.IsDone():
        raise ValueError(f"thread: {label} failed (IsDone is false, {context})")
    return builder.Shape()


def _end_radius(kind: str, r_maj: float, r_min: float, chamfer: float) -> float:
    """雄の端の半径。先端(円板)は面取り(山頂から chamfer 細く)、それ以外(頭・板・肩)は谷より _RELIEF 細い首。
    どちらの端もねじの切り口を平面に残さない(頭の上面に 30° の切り口が載ると、その平面が三角形に分割できない。D10 では体積・個数の門で落ちる。実測 2026-10-23)。"""
    return r_maj - chamfer if kind == "disc" else r_min - _RELIEF


def _add_male(shape, target, points, pitch, depth, context):
    """雄: 円柱域を切った本体へ、両端を 45° の円錐で細くしたねじ部を足す。軸の中の空き(同軸の穴など)は、谷より内側に
    収まっていればねじ部からも抜いて残す。収まらなければ断る。先が既に面取り済み(軸へ向かって細る円錐)なら断る。
    溝だけを引く作り方(円柱 − ねじ部 を本体から引く)は、螺旋の面が2回の集合演算に入って 30°・45° で壊れた(実測 2026-10-23)。"""
    ends = target["ends"]
    if "cone" in (ends["low"]["kind"], ends["high"]["kind"]):
        raise ValueError("軸の先が既に面取りされている(先が山より太い段になってナットに入らない)。面取りの前にねじを切る")
    z0, z1 = target["v0"], target["v1"]
    length = z1 - z0
    r_maj, r_min = max(r for _, r in points), min(r for _, r in points)
    chamfer = depth + 0.1 * pitch
    r_low = _end_radius(ends["low"]["kind"], r_maj, r_min, chamfer)
    r_high = _end_radius(ends["high"]["kind"], r_maj, r_min, chamfer)
    # 両端の 45° の円錐(r = r_low + z と r = r_high + (L − z))で挟んだ回転体。円錐は軸で閉じる(延ばした線が軸の反対へ出ると回転体が作れない)
    z_cross = 0.5 * (r_high + length - r_low)
    if r_low + z_cross <= r_maj + 1:
        outline = [(0.0, -r_low), (r_low + z_cross, z_cross), (0.0, length + r_high)]
    else:
        outline = [(0.0, -r_low), (r_maj + 1, r_maj + 1 - r_low), (r_maj + 1, length - (r_maj + 1 - r_high)), (0.0, length + r_high)]
    segment = _boolean(BRepAlgoAPI_Common(threaded_segment(points, pitch, length), _revolve(outline)), "ends", context)
    segment = _placed(segment, target, z0)
    thread_volume = _volume_eps(segment)
    # 軸の範囲の中の空き(同軸の穴など)。円柱を足し引きするとき埋まるので、ねじ部からも抜く
    air = _boolean(BRepAlgoAPI_Cut(_cylinder(target, target["radius"], z0, z1), shape), "inner space", context)
    air_volume = _volume_eps(air)
    if air_volume > _AIR_EPS:
        inner = _cylinder(target, min(r_low, r_high) - _RELIEF, z0, z1)
        if abs(_volume_eps(_boolean(BRepAlgoAPI_Common(air, inner), "inner space", context)) - air_volume) > _AIR_EPS:
            raise ValueError("軸の中の穴・切り欠きがねじの谷より外へ出ている(ねじを切ると穴が埋まるか壁が抜ける)")
        segment = _boolean(BRepAlgoAPI_Cut(segment, air), "keep inner space", context)
    before = _volume_eps(shape)
    cut = BRepAlgoAPI_Cut(shape, _cylinder(target, target["radius"] + _FILL_MARGIN, z0, z1))
    body = _boolean(cut, "cut", context)
    fuse = BRepAlgoAPI_Fuse(body, segment)
    final = _boolean(fuse, "fuse", context)
    expected = thread_volume - math.pi * target["radius"] ** 2 * length  # 空きは足す側と引く側で同じだけ減る
    return final, [(cut, shape), (fuse, body)], expected, thread_volume, before


def _cut_female(shape, target, points, pitch, depth, context):
    """雌: 入口(外へ広がる平面の端)の円の辺を先に面取りし(円錐を当てるのは螺旋の面がまだ無い段だけ。docs/THREAD.md)、
    残った範囲で穴を埋めてから、雌側の山形の立体を引く。"""
    ends = target["ends"]
    entries = {name: end for name, end in ends.items() if end["kind"] == "outward"}
    steps: list = []
    body = shape
    chamfer_map = None
    if entries:
        edges = [edge for end in entries.values() for edge in end["edges"]]
        result = kernel.chamfer_edges(shape, {edge: depth for edge in edges})
        body = result["shape"]
        chamfer_map = result["provenance"]["face_map"]
    z0 = target["v0"] + (depth if "low" in entries else 0.0)
    z1 = target["v1"] - (depth if "high" in entries else 0.0)
    if z1 - z0 < pitch:
        raise ValueError(f"穴が短すぎる(入口の面取りを除いたねじの長さ {z1 - z0:.3g} がピッチ {pitch:g} に足りない)")
    before = _volume_eps(body)
    fuse = BRepAlgoAPI_Fuse(body, _cylinder(target, target["radius"] + _FILL_MARGIN, z0, z1))
    filled = _boolean(fuse, "fill", context)
    void = _placed(threaded_segment(points, pitch, z1 - z0), target, z0)
    cut = BRepAlgoAPI_Cut(filled, void)
    final = _boolean(cut, "cut", context)
    thread_volume = _volume_eps(void)
    expected = math.pi * target["radius"] ** 2 * (z1 - z0) - thread_volume
    return final, [("chamfer", chamfer_map), (fuse, body), (cut, filled)], expected, thread_volume, before


def _provenance(shape, steps, final, face_index: int, plan: dict) -> dict:
    """元の面の何番が結果の何番になったかを、段ごとの後継を合成して出す(`face_map` / `deleted` / `new_faces`)。"""
    current = {str(i): [i] for i in range(1, _face_map(shape).Extent() + 1)}
    for builder, target in steps:
        if builder == "chamfer":
            if target is None:
                continue
            step = {key: list(value) for key, value in target.items()}
        else:
            next_map = _face_map(builder.Shape())
            step = _target_successors(builder, target, None, next_map)
        current = {key: sorted({n for m in mids for n in step.get(str(m), [])}) for key, mids in current.items()}
    return {
        "verb": "thread",
        "face": face_index,
        "female": plan["female"],
        "profile": plan["profile"],
        "pitch": plan["pitch"],
        "clearance": plan["clearance"],
        **_face_map_projection(current, _face_map(final).Extent()),
    }
