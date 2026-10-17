"""ねじ verb の形の作り方の試作と実測(docs/THREAD.md「形の作り方」の数値はこれで再現する)。

    .venv/Scripts/python.exe tools/thread_prototype.py            # 採用する方式(面から直接組む)の全ケース
    .venv/Scripts/python.exe tools/thread_prototype.py --rejected # 採らなかった方式(歯と芯の和)
    .venv/Scripts/python.exe tools/thread_prototype.py --round    # 半円の山

duocad 本体からは import しない。実装は `duocad/kernel_thread.py` に書き直す。
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from OCP.BRepAdaptor import BRepAdaptor_Curve
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
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFill import BRepFill
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeRevol
from OCP.GCE2d import GCE2d_MakeLine, GCE2d_MakeSegment
from OCP.Geom import Geom_BSplineSurface, Geom_CylindricalSurface
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.GeomAPI import GeomAPI_Interpolate
from OCP.GProp import GProp_GProps
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.ShapeFix import ShapeFix_Solid
from OCP.TColgp import TColgp_Array2OfPnt, TColgp_HArray1OfPnt
from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal, TColStd_HArray1OfReal
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.gp import gp_Ax1, gp_Ax2, gp_Ax3, gp_Dir, gp_Dir2d, gp_Pnt, gp_Pnt2d, gp_Trsf, gp_Vec

from duocad import kernel

SAMPLES_PER_TURN = 64
STL_DEVIATION = 0.01  # duocad/io_step.py と同じ
CASES = [(3, 0.5, 8, 0.1), (5, 0.8, 12, 0.1), (8, 1.25, 20, 0.1), (20, 2.5, 30, 0.15), (60, 4.0, 15, 0.2)]


def iso_profile(r_maj: float, pitch: float) -> list:
    """ISO 基準山形(60度・谷の平坦 P/4・山頂の平坦 P/8)の1ピッチ分 [(s, r)]。s は軸方向 0..P。"""
    h = 5 * (math.sqrt(3) / 2 * pitch) / 8
    r_min, f = r_maj - h, 5 * pitch / 16
    return [(0.0, r_min), (pitch / 4, r_min), (pitch / 4 + f, r_maj), (pitch / 4 + f + pitch / 8, r_maj), (pitch, r_min)]


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
    """同じノット列の2曲線の間の (3,1) 次 B-spline 面。u は θ のまま残る(端の切り口を直線にするため)。"""
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


def threaded_segment(profile: list, pitch: float, length: float, tol: float = 1e-5):
    """z ∈ [0, length] の右ねじの軸(中身の詰まった立体)を、集合演算なしで面から組む。

    蓋と帯で辺を共有させて一度に縫うと、縫い合わせが端の辺を縮退扱いにして殻にならない(実測)。
    帯だけ先に縫い、残った2本の開いた縁から平面の蓋を作って縫い直す。
    """
    k = pitch / (2 * math.pi)
    th_min, th_max = -2 * math.pi, 2 * math.pi * length / pitch
    n = int(math.ceil((th_max - th_min) / (2 * math.pi) * SAMPLES_PER_TURN)) + 1
    thetas = np.linspace(th_min, th_max, n)
    sew = BRepBuilderAPI_Sewing(tol)
    for (s0, r0), (s1, r1) in zip(profile, profile[1:]):
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


def expected_volume(profile: list, pitch: float, length: float) -> float:
    """断面積 (π/P)∫f(s)²ds × 長さ。折れ線なので区間ごとに厳密。"""
    acc = sum((s1 - s0) * (r0 * r0 + r0 * r1 + r1 * r1) / 3 for (s0, r0), (s1, r1) in zip(profile, profile[1:]))
    return math.pi / pitch * acc * length


def numeric_volume(profile, pitch, length, clip, nz=1500, nth=1440) -> float:
    """∫∫ ½ clip(f(z-kθ), z)² dθ dz の中点則。clip は端の円錐で歯形を削る関数(配列を受ける)。"""
    k = pitch / (2 * math.pi)
    z = (np.arange(nz) + 0.5) * length / nz
    th = (np.arange(nth) + 0.5) * 2 * math.pi / nth
    zz, tt = np.meshgrid(z, th, indexing="ij")
    f = np.interp(np.mod(zz - k * tt, pitch), [p[0] for p in profile], [p[1] for p in profile])
    r = clip(f, zz)
    return float((0.5 * r * r).sum() * (length / nz) * (2 * math.pi / nth))


def revolve(points):
    poly = BRepBuilderAPI_MakePolygon()
    for r, z in points:
        poly.Add(gp_Pnt(r, 0, z))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    return BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))).Shape()


def cylinder(r, z0, h):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, z0), gp_Dir(0, 0, 1)), r, h).Shape()


def volume_eps(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props, 1e-7)
    return props.Mass()


def report(label, shape, seconds, expected):
    v = volume_eps(shape)
    t0 = time.perf_counter()
    mesh = kernel.tessellate(shape, STL_DEVIATION, kernel.DEFAULT_ANGULAR_DEFLECTION)
    t_mesh = time.perf_counter() - t0
    tris = sum(len(f["indices"]) // 3 for f in mesh["faces"])
    print(
        f"{label:22s} {seconds:5.2f}s valid={BRepCheck_Analyzer(shape).IsValid()!s:5s} rel={(v - expected) / expected:+.1e} "
        f"kernel.volume rel={(kernel.volume(shape) - expected) / expected:+.1e} STL {t_mesh:4.2f}s {tris} tris untri={mesh['untriangulated']}"
    )


def _circles_at(shape, z):
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, m)
    out = []
    for i in range(1, m.Extent() + 1):
        e = TopoDS.Edge_s(m.FindKey(i))
        c = BRepAdaptor_Curve(e)
        if c.GetType() == GeomAbs_CurveType.GeomAbs_Circle and abs(c.Value(c.FirstParameter()).Z() - z) < 1e-6:
            out.append(e)
    return out


def adopted():
    print("== 軸だけ / 頭(箱)の上面に立てたボルト ==")
    for d, p, L, off in CASES:
        prof = iso_profile(d / 2 - off, p)
        t0 = time.perf_counter()
        seg = threaded_segment(prof, p, L)
        report(f"軸 M{d}x{p} L{L}", seg, time.perf_counter() - t0, expected_volume(prof, p, L))
        a = d * 1.6
        head = BRepPrimAPI_MakeBox(gp_Pnt(-a / 2, -a / 2, -d * 0.6), a, a, d * 0.6).Shape()
        t0 = time.perf_counter()
        bolt = BRepAlgoAPI_Fuse(head, seg).Shape()
        report(f"ボルト M{d}x{p}", bolt, time.perf_counter() - t0, a * a * d * 0.6 + expected_volume(prof, p, L))

    print("== 板の上の円柱に雄ねじ + 先端の面取り(軸だけで共通部分 → 円柱域を切った本体へ足す) ==")
    for d, p, L, off in CASES:
        a = d * 3
        body = BRepAlgoAPI_Fuse(BRepPrimAPI_MakeBox(gp_Pnt(-a / 2, -a / 2, -3), a, a, 3).Shape(), cylinder(d / 2, 0, L)).Shape()
        prof = iso_profile(d / 2 - off, p)
        r_maj, r_min = prof[2][1], prof[0][1]
        c = (r_maj - r_min) + 0.1 * p
        t0 = time.perf_counter()
        seg = threaded_segment(prof, p, L)
        seg = BRepAlgoAPI_Common(seg, revolve([(0, -1), (r_maj + 1, -1), (r_maj + 1, L - c - 1), (r_maj - c, L), (0, L)])).Shape()
        out = BRepAlgoAPI_Fuse(BRepAlgoAPI_Cut(body, cylinder(d / 2 + 0.01, 0, L + 0.01)).Shape(), seg).Shape()
        clip = lambda f, z, L=L, c=c, r=r_maj: np.minimum(f, (r - c) + (L - z))
        report(f"板+雄+面取り M{d}x{p}", out, time.perf_counter() - t0, a * a * 3 + numeric_volume(prof, p, L, clip))

    print("== 穴の入口を面取りしてから、残った円筒面に雌ねじ(穴を埋める → 雌側の軸を引く) ==")
    for d, p, L, off in CASES:
        blk, h = d * 2.5, min(L, 15)
        body = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(gp_Pnt(-blk / 2, -blk / 2, 0), blk, blk, h).Shape(), cylinder(d / 2, 0, h)).Shape()
        prof = iso_profile(d / 2 + off, p)
        depth = prof[2][1] - prof[0][1]
        chamfer = BRepFilletAPI_MakeChamfer(body)
        for e in _circles_at(body, 0.0) + _circles_at(body, h):
            chamfer.Add(depth, e)
        body = chamfer.Shape()
        before = volume_eps(body)
        t0 = time.perf_counter()
        z0, z1 = depth, h - depth
        move = gp_Trsf()
        move.SetTranslation(gp_Vec(0, 0, z0))
        void = BRepBuilderAPI_Transform(threaded_segment(prof, p, z1 - z0), move, True).Shape()
        out = BRepAlgoAPI_Cut(BRepAlgoAPI_Fuse(body, cylinder(d / 2, z0, z1 - z0)).Shape(), void).Shape()
        expected = before + math.pi * (d / 2) ** 2 * (z1 - z0) - expected_volume(prof, p, z1 - z0)
        report(f"皿+雌 M{d}x{p}", out, time.perf_counter() - t0, expected)


def _tooth(profile_closed, pitch, z_start, turns):
    """採らなかった方式の歯: 歯形の各頂点のヘリックスを円筒面上の2D直線で作り、隣どうしを線織面で張る。"""
    def helix(r, z0):
        surf = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), r)
        slope = pitch / (2 * math.pi)
        line = GCE2d_MakeLine(gp_Pnt2d(0, z0), gp_Dir2d(1.0, slope)).Value()
        edge = BRepBuilderAPI_MakeEdge(line, surf, 0.0, turns * 2 * math.pi * math.sqrt(1 + slope * slope)).Edge()
        BRepLib.BuildCurves3d_s(edge)
        return edge

    edges = [helix(r, z_start + dz) for r, dz in profile_closed]
    sew = BRepBuilderAPI_Sewing(1e-6)
    for i in range(len(edges)):
        sew.Add(BRepFill.Face_s(edges[i], edges[(i + 1) % len(edges)]))
    for z_off, ang in ((0.0, 0.0), (turns * pitch, turns * 2 * math.pi)):
        poly = BRepBuilderAPI_MakePolygon()
        for r, dz in profile_closed:
            poly.Add(gp_Pnt(r * math.cos(ang), r * math.sin(ang), z_start + dz + z_off))
        poly.Close()
        sew.Add(BRepBuilderAPI_MakeFace(poly.Wire(), True).Face())
    sew.Perform()
    fix = ShapeFix_Solid(BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(sew.SewedShape())).Solid())
    fix.Perform()
    return fix.Solid()


def rejected():
    print("== 採らなかった方式: 螺旋の歯 ∪ 芯の円柱(谷は芯の円筒面に一致) ==")
    for d, p, L, off in CASES[:3]:
        prof = iso_profile(d / 2 - off, p)
        closed = [(r, s - p / 8) for s, r in prof[1:]]  # 谷の平坦を除いた歯の4点(谷は芯と共有)
        turns = L / p + 2
        tooth = _tooth(closed, p, -p, turns)
        core = cylinder(prof[0][1], -2 * p, L + 4 * p)
        tooth_exp = volume_eps(tooth)
        print(f"歯だけ M{d}x{p}: kernel.volume rel={(kernel.volume(tooth) - tooth_exp) / tooth_exp:+.1e}")
        t0 = time.perf_counter()
        fused = BRepAlgoAPI_Fuse(core, tooth).Shape()
        report(f"歯∪芯 M{d}x{p}", fused, time.perf_counter() - t0, volume_eps(core) + tooth_exp)


def round_profile(r_core: float, pitch: float, rho: float, n: int) -> list:
    """平らな谷と、芯の面に中心を置いた半径 rho の半円の山(Fusion のコイルの円断面・中心配置)。半円は n 本の折れ線。"""
    c = pitch / 2
    pts = [(0.0, r_core), (c - rho, r_core)]
    for i in range(1, n):
        a = math.pi - math.pi * i / n
        pts.append((c + rho * math.cos(a), r_core + rho * math.sin(a)))
    return pts + [(c + rho, r_core), (pitch, r_core)]


def round_cases():
    print("== 半円の山(折れ線近似): 軸だけ / 板の上の円柱に立てる ==")
    for d, p, rho, n, L in ((3, 1.0, 0.4, 16, 8), (3, 1.0, 0.4, 32, 8), (5, 1.5, 0.6, 16, 12), (8, 2.0, 0.8, 16, 20)):
        prof = round_profile(d / 2 - 0.15 - rho / 2, p, rho, n)
        t0 = time.perf_counter()
        seg = threaded_segment(prof, p, L)
        report(f"半円 d{d} P{p} 折れ線{n}", seg, time.perf_counter() - t0, expected_volume(prof, p, L))
        a = d * 3
        body = BRepAlgoAPI_Fuse(BRepPrimAPI_MakeBox(gp_Pnt(-a / 2, -a / 2, -3), a, a, 3).Shape(), cylinder(d / 2, 0, L)).Shape()
        t0 = time.perf_counter()
        out = BRepAlgoAPI_Fuse(BRepAlgoAPI_Cut(body, cylinder(d / 2 + 0.01, 0, L + 0.01)).Shape(), seg).Shape()
        report(f"  板の上 d{d} 折れ線{n}", out, time.perf_counter() - t0, a * a * 3 + expected_volume(prof, p, L))
        print(f"  半円の矢高 {rho * (1 - math.cos(math.pi / n / 2)):.4f} mm")


if __name__ == "__main__":
    if "--rejected" in sys.argv:
        rejected()
    elif "--round" in sys.argv:
        round_cases()
    else:
        adopted()
