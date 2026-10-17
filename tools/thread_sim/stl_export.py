"""試し刷り用のボルトとナットの STL を作る。

形は tools/thread_prototype.py の「面から直接組む」方式(docs/THREAD.md「形の作り方」で実測して選んだもの)。
山形の密な折れ線は、帯の数を減らすため Douglas–Peucker で間引く(許容 0.003mm。STL の偏差 0.01mm より小さい)。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import thread_prototype as tp  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace, BRepBuilderAPI_Transform  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism  # noqa: E402
from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec  # noqa: E402

from duocad import kernel  # noqa: E402

from .profiles import Profile  # noqa: E402


def simplify_profile(p: Profile, tol: float = 0.003):
    """1ピッチの折れ線 [(s, r)] を間引く(端点と角は残る)。"""
    s, r = p.dense(1200)
    s = np.append(s, p.pitch)
    r = np.append(r, r[0])
    pts = np.column_stack([s, r])
    keep = np.zeros(len(pts), bool)
    keep[[0, -1]] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = pts[b] - pts[a]
        seg = pts[a + 1:b]
        dist = np.abs(d[0] * (seg[:, 1] - pts[a, 1]) - d[1] * (seg[:, 0] - pts[a, 0])) / max(np.hypot(*d), 1e-15)
        i = int(np.argmax(dist))
        if dist[i] > tol:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    out = [(float(x), float(y)) for x, y in pts[keep]]
    return out


def _hex_prism(across_flats: float, z0: float, h: float, sides: int = 6):
    """正多角柱(既定は六角)。across_flats は対辺の距離。"""
    R = across_flats / 2 / math.cos(math.pi / sides)
    poly = BRepBuilderAPI_MakePolygon()
    for i in range(sides):
        a = math.pi / sides + i * 2 * math.pi / sides
        poly.Add(gp_Pnt(R * math.cos(a), R * math.sin(a), z0))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, h)).Shape()


def bolt(profile: Profile, length: float, head_af: float, head_h: float, chamfer: float | None = None, sides: int = 6):
    """六角頭のボルト。頭の上面 z=0 から軸が z=length まで。先端に 45° の面取り(深さ = 山の高さ + 0.1P)。"""
    prof = simplify_profile(profile)
    P = profile.pitch
    seg = tp.threaded_segment(prof, P, length)
    r_maj = max(r for _, r in prof)
    r_min = min(r for _, r in prof)
    c = (r_maj - r_min) + 0.1 * P if chamfer is None else chamfer
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    seg = BRepAlgoAPI_Common(seg, tp.revolve([(0, -1), (r_maj + 1, -1), (r_maj + 1, length - c - 1), (r_maj - c, length), (0, length)])).Shape()
    head = _hex_prism(head_af, -head_h, head_h, sides)
    return BRepAlgoAPI_Fuse(head, seg).Shape()


def nut(profile: Profile, height: float, af: float, sides: int = 6):
    """六角ナット。穴の両端を山の高さだけ 45° で面取りしてから、ねじを切る(THREAD.md の雌の手順)。
    profile は雌の穴の面の山形(nut_of で作ったもの)。"""
    prof = simplify_profile(profile)
    P = profile.pitch
    r_root = max(r for _, r in prof)  # 谷(穴の最大半径)
    depth = r_root - min(r for _, r in prof)
    # 下穴と埋める円柱を谷と同じ半径にすると、谷の帯(r 一定の線織面)が円筒面と重なり、山形によっては
    # 三角形に分割できない面が残る(実測: 45° 台形 d6 P1.5)。谷より 0.01mm 大きくして重ねない
    r_bore_max = r_root + 0.01
    body = BRepAlgoAPI_Cut(_hex_prism(af, 0.0, height, sides), tp.cylinder(r_bore_max, 0.0, height)).Shape()
    ch = BRepFilletAPI_MakeChamfer(body)
    for e in tp._circles_at(body, 0.0) + tp._circles_at(body, height):
        ch.Add(depth, e)
    body = ch.Shape()
    z0, z1 = depth, height - depth
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(0, 0, z0))
    void = BRepBuilderAPI_Transform(tp.threaded_segment(prof, P, z1 - z0), move, True).Shape()
    return BRepAlgoAPI_Cut(BRepAlgoAPI_Fuse(body, tp.cylinder(r_bore_max, z0, z1 - z0)).Shape(), void).Shape()


def write_stl(shape, path: Path, deviation: float = 0.01):
    """kernel.tessellate の三角形を二進 STL へ書く。"""
    if not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError(f"{path.name}: BRepCheck が偽")
    mesh = kernel.tessellate(shape, deviation, kernel.DEFAULT_ANGULAR_DEFLECTION)
    if mesh["untriangulated"]:
        raise ValueError(f"{path.name}: 三角形の取れない面がある")
    tris = []
    for f in mesh["faces"]:
        v = np.asarray(f["positions"], float).reshape(-1, 3)
        idx = np.asarray(f["indices"], int).reshape(-1, 3)
        tris.append(v[idx])
    T = np.concatenate(tris)
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)
    rec = np.zeros(len(T), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, T
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(b"duocad thread_sim".ljust(80, b" "))
        fh.write(np.uint32(len(T)).tobytes())
        fh.write(rec.tobytes())
    return len(T)
