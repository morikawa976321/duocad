r"""干渉チェックの試作の形と判定結果を、撮影用の三角形(色・透明度つき)に書き出す。撮るのは shoot.mjs。

    .\.venv\Scripts\python.exe -m tools.interference_probe.shoot_export [--step <PC ケースの STEP>] [--hanger <文書.json>]
    node tools\interference_probe\shoot.mjs

色の約束: 部品は半透明の灰(組の2部品は青みと緑みの半透明)、overlap は赤、tiny_overlap は橙、接触面は青、最短距離は黄の線。
出力は output/interference_probe/shots/_data/(git 管理外)。
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
from pathlib import Path

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.BOPAlgo import BOPAlgo_Builder
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from . import methods as M

OUT = Path("output/interference_probe/shots/_data")
PC_STEP = r"C:\Users\tm976\mf\3d-models\mATX_PC_case_20251122\PC_case_20251122.step"

GREY, PART_A, PART_B = "#9aa3b5", "#7fa7e0", "#8fd19e"
RED, ORANGE, BLUE, YELLOW = "#ff3b30", "#ff9f0a", "#2f7bff", "#ffd60a"


def triangles(shape, deviation=0.2, angular=0.5):
    """三角形分割(写しの上で。分割できない面は飛ばす。厚さ数 nm の薄片の側面も黙って落ちる)。"""
    shape = BRepBuilderAPI_Copy(shape, True, False).Shape()
    BRepMesh_IncrementalMesh(shape, deviation, False, angular, True)
    verts, faces, base = [], [], 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            for k in range(1, tri.NbNodes() + 1):
                p = tri.Node(k).Transformed(trsf)
                verts.append((p.X(), p.Y(), p.Z()))
            flip = face.Orientation() == TopAbs_REVERSED
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                faces.append((base + a - 1, base + c - 1, base + b - 1) if flip else (base + a - 1, base + b - 1, base + c - 1))
            base += tri.NbNodes()
        exp.Next()
    return np.asarray(verts, dtype=np.float32).reshape(-1, 3), np.asarray(faces, dtype=np.uint32).reshape(-1, 3)


def obj(label, shape_or_mesh, color, opacity=1.0, deviation=0.2, edges=False, front=False):
    if isinstance(shape_or_mesh, tuple):
        v, f = shape_or_mesh
        v, f = np.asarray(v, dtype=np.float32), np.asarray(f, dtype=np.uint32)
    else:
        v, f = triangles(shape_or_mesh, deviation)
    return dict(label=label, color=color, opacity=opacity, edges=edges, front=front,
                verts_b64=base64.b64encode(v.tobytes()).decode(), faces_b64=base64.b64encode(f.tobytes()).decode(),
                triangles=int(len(f)), lo=v.min(axis=0).tolist() if len(v) else None,
                hi=v.max(axis=0).tolist() if len(v) else None)


def segment(p, q, radius):
    """2点を結ぶ細い円柱と両端の球(最短距離の線)。"""
    p, q = np.asarray(p, float), np.asarray(q, float)
    d = q - p
    n = np.linalg.norm(d)
    shapes = [BRepPrimAPI_MakeSphere(gp_Pnt(*p), radius * 2).Shape(), BRepPrimAPI_MakeSphere(gp_Pnt(*q), radius * 2).Shape()]
    if n > 1e-9:
        shapes.append(BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(*(d / n))), radius, n).Shape())
    return M.compound(shapes)


def pair_parts(a, b):
    """2体 GF(非破壊)で、共有される立体片と面を返す。"""
    gf = BOPAlgo_Builder()
    args = TopTools_ListOfShape()
    args.Append(a)
    args.Append(b)
    gf.SetArguments(args)
    gf.SetNonDestructive(True)
    gf.SetRunParallel(True)
    gf.Perform()
    images = []
    for s in (a, b):
        pieces, exp = [], TopExp_Explorer(s, TopAbs_SOLID)
        while exp.More():
            sol = exp.Current()
            mod = list(gf.Modified(sol))
            pieces += mod if mod else ([] if gf.IsDeleted(sol) else [sol])
            exp.Next()
        comp = M.compound(pieces)
        images.append({k: M._hashes(comp, k) for k in (TopAbs_SOLID, TopAbs_FACE)})
    solids = M._shared(images[0][TopAbs_SOLID], images[1][TopAbs_SOLID])
    faces = M._shared(images[0][TopAbs_FACE], images[1][TopAbs_FACE]) if not solids else []
    return solids, faces


def scene(name, title, caption, objects, view, fit="all", zoom=1.0, target=None, fit_box=None):
    return dict(name=name, title=title, caption=caption, objects=objects, view=view, fit=fit, zoom=zoom, target=target,
                fit_box=fit_box)


def pc_scenes(step):
    from .pipeline import classify
    from .real_docs import load_step
    solids, _ = load_step(Path(step))
    shapes = [s.shape for s in solids]
    names = [s.name for s in solids]
    res = classify(shapes, 0.0, names)
    idx = {n: k for k, n in enumerate(names)}
    rel = {(r["a"], r["b"]): r for r in res["pairs"]}
    parts = [obj(n, s, GREY, 0.18, edges=True) for n, s in zip(names, shapes)]
    reds, oranges, blues = [], [], []
    for (a, b), r in rel.items():
        if r["relation"] in ("overlap", "tiny_overlap", "contact"):
            solids_ab, faces_ab = pair_parts(shapes[idx[a]], shapes[idx[b]])
            if r["relation"] == "overlap":
                reds += [obj(f"{a}×{b}", M.compound(solids_ab), RED, 1.0, deviation=0.1, front=True)]
            elif r["relation"] == "tiny_overlap":
                oranges += [obj(f"{a}×{b}", M.compound(solids_ab), ORANGE, 1.0, deviation=0.1, front=True)]
            elif faces_ab:
                blues += [obj(f"{a}×{b}", M.compound(faces_ab), BLUE, 0.9, deviation=0.2, front=True)]
    out = [
        scene("pc_overview_overlaps", "PC ケース全体: 干渉(赤) 2 組と微小な重なり(橙) 20 組",
              "部品 27 個は半透明の灰。橙は厚さ 0.3〜8 nm の膜で、板の合わせ面に沿って並ぶ(clearance 0)",
              parts + reds + oranges, [0.62, -0.66, 0.42]),
        scene("pc_overview_contacts", "PC ケース全体: 面の接触(青) 30 組",
              "共有された面を青で塗った。接触面積 14〜8,700 mm²(中央 613 mm²)",
              parts + blues, [0.62, -0.66, 0.42]),
    ]
    for a, b, vol in [("solid2", "solid10", "1270"), ("solid1", "solid14", "25.4")]:
        solids_ab, _ = pair_parts(shapes[idx[a]], shapes[idx[b]])
        r = rel[(a, b)]
        objs = [obj(a, shapes[idx[a]], PART_A, 0.28, edges=True), obj(b, shapes[idx[b]], PART_B, 0.28, edges=True),
                obj("overlap", M.compound(solids_ab), RED, 1.0, deviation=0.05, front=True)]
        out.append(scene(f"pc_overlap_{a}_{b}", f"PC ケースの干渉: {a} × {b}(体積 {vol} mm³)",
                         f"赤が重なりの立体。厚さの目安 2V/A = {r['thickness']:.2f} mm、有向境界箱の最小辺 {r['obb_min']:.2f} mm",
                         objs, [0.62, -0.66, 0.42], fit="highlight", zoom=2.2))
    a, b = "solid17", "solid23"
    solids_ab, _ = pair_parts(shapes[idx[a]], shapes[idx[b]])
    objs = [obj(a, shapes[idx[a]], PART_A, 0.28, edges=True), obj(b, shapes[idx[b]], PART_B, 0.28, edges=True),
            obj("tiny_overlap", M.compound(solids_ab), ORANGE, 1.0, deviation=0.05, front=True)]
    out.append(scene("pc_tiny_solid17_solid23", f"PC ケースの微小な重なり: {a} × {b}(体積 0.0138 mm³)",
                     "橙は厚さ 0.0000071 mm(7 nm)× 幅 10 mm × 長さ 200 mm の膜。形としては面の接触と同じ",
                     objs, [0.62, -0.66, 0.42], fit="highlight", zoom=1.3))
    return out


def nested_box_scenes():
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from .cases import box, cut, filleted_box
    r_out, c = 3.0, 0.2
    box_ = cut(filleted_box(0, 0, 0, 40, 30, 20, r_out + 2.0), filleted_box(2, 2, 2, 36, 26, 30, r_out))
    core = filleted_box(2 + c, 2 + c, 2 + c, 36 - 2 * c, 26 - 2 * c, 25, r_out - c)
    d, p, q = facewise_with_points(box_, core)
    whole = scene("nested_rounded_box", f"角を丸めた箱に丸めた中子(隙間 0.2): 最短距離 {d:.8f} mm → at",
                  "形全体の最短距離は例外で落ちる。面の対ごとに測り、落ちた対だけ縁の点から測った(全体図では 0.2 mm の隙間は見えない。次の断面図)",
                  [obj("box", box_, PART_A, 0.3, deviation=0.05, edges=True),
                   obj("core", core, PART_B, 0.45, deviation=0.05, edges=True)],
                  [0.55, -0.75, 0.9])
    # z = 10 の薄い板で切った断面を真上から、角の丸めの中心 (5, 5) のまわりを拡大する。丸めは同心(箱の内側 r3、中子 r2.8)なので、
    # 45° の向きの2点の距離は丸めの半径の差 0.2 で、平らな壁どうしの 0.2 と同じ。
    slab = box(-1, -1, 10.0, 42, 32, 0.05)   # 薄くしないと、遠近法で板の厚みの内壁が帯に写って隙間と紛れる
    box_cut = BRepAlgoAPI_Common(box_, slab).Shape()
    core_cut = BRepAlgoAPI_Common(core, slab).Shape()
    k = 1 / math.sqrt(2)
    p45, q45 = (5 - 3.0 * k, 5 - 3.0 * k, 10.06), (5 - 2.8 * k, 5 - 2.8 * k, 10.06)
    section = scene("nested_rounded_box_section", "同じ箱を z=10 で切った断面(角を拡大): 箱の内側 r3 と中子の外側 r2.8 が同心で隙間 0.2",
                    f"青が箱、緑が中子。黄の線は角の丸めの上で向かい合う2点(長さ 0.2)。測った最短距離 {d:.8f} は 0.2 との差 3e-8 で比較帯 ε=1e-6 の中",
                    [obj("box", box_cut, PART_A, 1.0, deviation=0.005, edges=True),
                     obj("core", core_cut, PART_B, 1.0, deviation=0.005, edges=True),
                     obj("gap", segment(p45, q45, 0.012), YELLOW, 1.0, deviation=0.002, front=True)],
                    [0.0001, -0.0001, 1.0], fit_box=[[1.2, 1.2, 10.0], [6.2, 6.2, 10.05]], zoom=1.0)
    return [whole, section]


def facewise_with_points(a, b):
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_EDGE

    def faces(s):
        out, exp = [], TopExp_Explorer(s, TopAbs_FACE)
        while exp.More():
            out.append(exp.Current())
            exp.Next()
        return out

    def one(x, y):
        try:
            d = BRepExtrema_DistShapeShape(x, y)
            if not d.IsDone():
                return None
            p, q = d.PointOnShape1(1), d.PointOnShape2(1)
            return d.Value(), (p.X(), p.Y(), p.Z()), (q.X(), q.Y(), q.Z())
        except Exception:
            return None

    def points(face, n=16):
        pts, exp = [], TopExp_Explorer(face, TopAbs_EDGE)
        while exp.More():
            e = TopoDS.Edge_s(exp.Current())
            if not BRep_Tool.Degenerated_s(e):
                cu = BRepAdaptor_Curve(e)
                f0, l0 = cu.FirstParameter(), cu.LastParameter()
                pts += [BRepBuilderAPI_MakeVertex(cu.Value(f0 + (l0 - f0) * k / n)).Vertex() for k in range(n + 1)]
            exp.Next()
        return pts

    fa, fb = faces(a), faces(b)
    ba, bb = [M.bbox(f) for f in fa], [M.bbox(f) for f in fb]
    order = sorted((M.bbox_gap(ba[i], bb[j]), i, j) for i in range(len(fa)) for j in range(len(fb)))
    best = (math.inf, None, None)
    for gap, i, j in order:
        if gap > best[0]:
            break
        r = one(fa[i], fb[j])
        if r is None:
            cands = [one(p, fb[j]) for p in points(fa[i])]
            cands += [(x[0], x[2], x[1]) for x in (one(p, fa[i]) for p in points(fb[j])) if x is not None]
            cands = [x for x in cands if x is not None]
            if not cands:
                continue
            r = min(cands, key=lambda x: x[0])
        if r[0] < best[0]:
            best = r
    return best


def thread_scene():
    from .threads import build
    male, nut = build(8.0, 1.25, -0.05)
    solids_ab, _ = pair_parts(male, nut)
    objs = [obj("bolt", male, PART_A, 0.25, deviation=0.05), obj("nut", nut, PART_B, 0.22, deviation=0.05, edges=True),
            obj("overlap", M.compound(solids_ab), RED, 1.0, deviation=0.02, front=True)]
    return scene("thread_m8_press", "ねじ M8×1.25、雄の山形が半径方向に 0.05 太い: 重なり 18.19 mm³(厚さ 0.061 mm)",
                 "赤がらせんに沿った重なりの立体(2体 GF で 4.6 秒)。同じ形で山形が一致する組は GF が 240 秒で終わらない",
                 objs, [0.85, -0.45, 0.35], fit="highlight", zoom=1.15)


def hanger_scene(doc_path):
    import manifold3d  # noqa: F401
    from .mixed import mesh_arrays
    from .real_docs import load
    doc, _ = load(Path(doc_path))
    bodies = list(doc.bodies)
    mesh_body = next(b for b in bodies if b.kind == "mesh")
    mv, mf = mesh_arrays(mesh_body)
    mm = M.to_manifold(mv, mf)
    preserve = set(doc.optimize.preserve_bodies)
    exclude = set(doc.optimize.exclude_bodies)
    objs = [obj(mesh_body.name, (mv, mf), GREY, 0.22, edges=False)]
    reds, oranges = [], []
    for b in bodies:
        if b.kind != "solid" or b.name == "solid":
            continue
        v, f = M.tessellated(b.shape, 0.01)
        inter = M.to_manifold(v, f) ^ mm
        if inter.volume() <= 0:
            continue
        mesh = inter.to_mesh()
        iv = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
        it = np.asarray(mesh.tri_verts, dtype=np.uint32)
        if b.id in preserve:
            reds.append(obj(f"{b.name} ∩ mesh", (iv, it), RED, 1.0))
        elif b.id in exclude:
            oranges.append(obj(f"{b.name} ∩ mesh", (iv, it), ORANGE, 1.0, front=True))
            objs.append(obj(b.name, b.shape, PART_B, 0.25, edges=True))
    return scene("hanger_mesh", "形状最適化のメッシュ × B-rep: 保持との重なり(赤)・除外との薄い重なり(橙)",
                 "赤は保持の4部品(メッシュが内側に含む。意図どおり)。橙は除外部品との厚さ 0.003〜0.02 mm の重なり(0.01 mm 分割の近似)",
                 objs + reds + oranges, [0.62, -0.66, 0.42])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", default=PC_STEP)
    ap.add_argument("--hanger", default=r"C:\Users\tm976\mf\duocad\models\hanger_bar_attachment.json")
    ap.add_argument("--only")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    makers = [("pc", lambda: pc_scenes(args.step)), ("nested", nested_box_scenes),
              ("thread", lambda: [thread_scene()]), ("hanger", lambda: [hanger_scene(args.hanger)])]
    manifest = []
    for key, make in makers:
        if args.only and key not in args.only.split(","):
            continue
        for sc in make():
            path = OUT / f"{sc['name']}.json"
            path.write_text(json.dumps(sc, ensure_ascii=False), encoding="utf-8")
            manifest.append(sc["name"])
            print(f"{sc['name']}: {sum(o['triangles'] for o in sc['objects'])} 三角形, {len(sc['objects'])} 物体", flush=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
