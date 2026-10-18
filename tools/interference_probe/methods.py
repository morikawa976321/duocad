"""干渉チェックの方式候補(Issue #186 の試作)。製品コードではない。

どの関数も2つの形状(または形状の列)を受け取り、測った量と秒を辞書で返す。分類はしない
(分類の規則は `run_cases.py` が方式ごとに持つ)。

- common:     BRepAlgoAPI_Common 相当(BOPAlgo_BOP の COMMON)の体積
- dist:       BRepExtrema_DistShapeShape の最短距離と InnerSolution
- checker:    BOPAlgo_CheckerSI に2形状の複合を渡したときの干渉の数(型別)
- gf:         BOPAlgo_Builder(General Fuse)を全形状に1回かけ、2つの元形状に共有される破片から組ごとの重なりを出す
- proximity:  BRepExtrema_ShapeProximity(三角形分割の上で許容距離内の面の組を探す)
- manifold:   三角形分割して manifold3d の交差体積と min_gap
"""
from __future__ import annotations

import time
from itertools import combinations
from typing import Dict, List, Sequence

import numpy as np

from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_Builder, BOPAlgo_CheckerSI, BOPAlgo_Operation
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepExtrema import BRepExtrema_DistShapeShape, BRepExtrema_ShapeProximity
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
from OCP.TopTools import TopTools_ListOfShape

_TYPE = {TopAbs_VERTEX: "V", TopAbs_EDGE: "E", TopAbs_FACE: "F", TopAbs_SOLID: "S"}


def volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def count(shape: TopoDS_Shape, kind) -> int:
    n, exp = 0, TopExp_Explorer(shape, kind)
    while exp.More():
        n += 1
        exp.Next()
    return n


def compound(shapes: Sequence[TopoDS_Shape]) -> TopoDS_Compound:
    comp, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(comp)
    for shape in shapes:
        builder.Add(comp, shape)
    return comp


def bbox(shape: TopoDS_Shape, optimal: bool = False):
    box = Bnd_Box()
    if optimal:
        BRepBndLib.AddOptimal_s(shape, box, False, False)
    else:
        BRepBndLib.Add_s(shape, box, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def bbox_gap(a, b) -> float:
    """2つの軸整列箱の間の距離(重なっていれば0)。"""
    (alo, ahi), (blo, bhi) = a, b
    d = np.maximum(0.0, np.maximum(alo - bhi, blo - ahi))
    return float(np.linalg.norm(d))


def common(a: TopoDS_Shape, b: TopoDS_Shape, fuzzy: float = 0.0, parallel: bool = False) -> Dict:
    t0 = time.perf_counter()
    op = BOPAlgo_BOP()
    op.AddArgument(a)
    op.AddTool(b)
    op.SetOperation(BOPAlgo_Operation.BOPAlgo_COMMON)
    op.SetFuzzyValue(fuzzy)
    op.SetRunParallel(parallel)
    op.Perform()
    out = dict(errors=bool(op.HasErrors()), warnings=bool(op.HasWarnings()))
    shape = op.Shape()
    if shape is None or shape.IsNull():
        out.update(volume=0.0, solids=0, faces=0)
    else:
        out.update(volume=volume(shape), solids=count(shape, TopAbs_SOLID), faces=count(shape, TopAbs_FACE))
    out["seconds"] = time.perf_counter() - t0
    out["shape"] = shape
    return out


def dist(a: TopoDS_Shape, b: TopoDS_Shape, parallel: bool = False) -> Dict:
    t0 = time.perf_counter()
    d = BRepExtrema_DistShapeShape()
    d.SetMultiThread(parallel)
    d.LoadS1(a)
    d.LoadS2(b)
    d.Perform()
    out = dict(done=bool(d.IsDone()))
    if d.IsDone():
        out.update(distance=d.Value(), inner=bool(d.InnerSolution()), solutions=d.NbSolution())
        if d.NbSolution():
            p, q = d.PointOnShape1(1), d.PointOnShape2(1)
            out.update(p=[p.X(), p.Y(), p.Z()], q=[q.X(), q.Y(), q.Z()])
    out["seconds"] = time.perf_counter() - t0
    return out


def checker(a: TopoDS_Shape, b: TopoDS_Shape) -> Dict:
    """CheckerSI に2形状の複合を渡す。同じ形状の中の干渉も数えるので、組ごとに『A の部分と B の部分』の干渉だけを数える。"""
    t0 = time.perf_counter()
    chk = BOPAlgo_CheckerSI()
    chk.AddArgument(compound([a, b]))
    chk.SetLevelOfCheck(5)
    chk.Perform()
    ds = chk.DS()
    kinds: Dict[str, int] = {}
    for pair in ds.Interferences():
        i, j = pair.Indices()
        key = "".join(sorted(_TYPE.get(ds.Shape(i).ShapeType(), "?") + _TYPE.get(ds.Shape(j).ShapeType(), "?")))
        kinds[key] = kinds.get(key, 0) + 1
    return dict(errors=bool(chk.HasErrors()), warnings=bool(chk.HasWarnings()), kinds=kinds,
                seconds=time.perf_counter() - t0)


def gf_pairs(shapes: Sequence[TopoDS_Shape], fuzzy: float = 0.0, parallel: bool = False) -> Dict:
    """General Fuse を1回かけ、2つ以上の元形状の像に共有される破片を組ごとに集めて体積を足す。"""
    t0 = time.perf_counter()
    gf = BOPAlgo_Builder()
    args = TopTools_ListOfShape()
    for s in shapes:
        args.Append(s)
    gf.SetArguments(args)
    gf.SetFuzzyValue(fuzzy)
    gf.SetRunParallel(parallel)
    gf.Perform()
    # TopoDS_Shape の Python ラッパは __hash__ が形状の同一性を返すが、__eq__ は Python の同一性なので
    # 形状そのものを辞書のキーにすると共有された破片が別のキーになる(実測)。hash で引いて IsSame で確かめる。
    owners: Dict[int, list] = {}
    for index, s in enumerate(shapes):
        exp = TopExp_Explorer(s, TopAbs_SOLID)
        while exp.More():
            solid = exp.Current()
            pieces = list(gf.Modified(solid)) or [solid]
            for piece in pieces:
                if piece.ShapeType() != TopAbs_SOLID:
                    continue
                slot = owners.setdefault(hash(piece), [piece, set()])
                assert slot[0].IsSame(piece)
                slot[1].add(index)
            exp.Next()
    pairs: Dict[tuple, float] = {}
    for piece, who in owners.values():
        if len(who) < 2:
            continue
        v = volume(piece)
        for i, j in combinations(sorted(who), 2):
            pairs[(i, j)] = pairs.get((i, j), 0.0) + v
    return dict(errors=bool(gf.HasErrors()), warnings=bool(gf.HasWarnings()), pairs=pairs,
                seconds=time.perf_counter() - t0)


def ensure_mesh(shape: TopoDS_Shape, deflection: float) -> None:
    BRepTools.Clean_s(shape)
    BRepMesh_IncrementalMesh(shape, deflection, False, 0.35, True)


def proximity(a: TopoDS_Shape, b: TopoDS_Shape, tol: float, deflection: float = 0.01) -> Dict:
    t0 = time.perf_counter()
    ensure_mesh(a, deflection)
    ensure_mesh(b, deflection)
    t_mesh = time.perf_counter() - t0
    p = BRepExtrema_ShapeProximity(a, b, tol)
    p.Perform()
    out = dict(done=bool(p.IsDone()))
    if p.IsDone():
        try:
            out.update(face_pairs=sum(len(v) for _, v in _items(p.OverlapSubShapes1())))
        except TypeError:  # OCP 7.9.3 は戻り値の型(NCollection_DataMap<int, TColStd_PackedMapOfInteger>)を登録していない
            out.update(face_pairs="unbound")
        try:
            out["proximity"] = p.Proximity()
        except Exception as exc:  # 版によっては無い
            out["proximity_error"] = repr(exc)
    out.update(seconds=time.perf_counter() - t0, mesh_seconds=t_mesh)
    return out


def _items(data_map):
    it = data_map.cbegin() if hasattr(data_map, "cbegin") else None
    if it is None:
        try:
            return [(k, data_map.Find(k)) for k in data_map.Keys()]
        except Exception:
            pass
    out = []
    try:
        for key in data_map:
            out.append((key, data_map.Find(key)))
    except Exception:
        out = [(None, [None] * data_map.Size())]
    return out


# ------------------------------------------------------------------ manifold3d
def tessellated(shape: TopoDS_Shape, deviation: float):
    """duocad の三角形分割(画面・STL と同じ経路)で、頂点を共有したメッシュにする。"""
    from duocad.topopt.finalize import tessellate_body
    vertices, faces, _ = tessellate_body(shape, deviation=deviation)
    return vertices, faces


def to_manifold(vertices, faces):
    import manifold3d
    mesh = manifold3d.Mesh64(vert_properties=np.ascontiguousarray(vertices, dtype=np.float64),
                             tri_verts=np.ascontiguousarray(faces, dtype=np.uint64))
    solid = manifold3d.Manifold(mesh)
    if solid.status() != manifold3d.Error.NoError:
        raise ValueError(f"manifold3d が入力を受け付けない: {solid.status()}")
    return solid


def manifold_pair(ma, mb, search: float) -> Dict:
    t0 = time.perf_counter()
    inter = ma ^ mb
    v = inter.volume()
    t_bool = time.perf_counter() - t0
    t1 = time.perf_counter()
    gap = ma.min_gap(mb, search)
    t_gap = time.perf_counter() - t1
    return dict(volume=v, tris=inter.num_tri(), min_gap=gap, bool_seconds=t_bool, gap_seconds=t_gap,
                seconds=time.perf_counter() - t0)


def _hashes(shape, kind) -> Dict[int, TopoDS_Shape]:
    out, exp = {}, TopExp_Explorer(shape, kind)
    while exp.More():
        out[hash(exp.Current())] = exp.Current()
        exp.Next()
    return out


def gf_relations(shapes: Sequence[TopoDS_Shape], fuzzy: float = 0.0, parallel: bool = True,
                 pairs: Sequence[tuple] = None) -> Dict:
    """General Fuse を1回かけ、組ごとの関係を像の共有から読む。

    GF は一致する部分(重なりの立体・接する面・辺・点)を両方の像で同じ形状として共有する。
    共有された立体があれば重なり(体積と厚さ 2V/A)、無くて共有された面があれば面の接触(面積)、
    辺なら線の接触、頂点なら点の接触。どれも無ければ GF の許容差の範囲で触れていない。
    """
    from OCP.BRepGProp import BRepGProp as _G
    t0 = time.perf_counter()
    gf = BOPAlgo_Builder()
    args = TopTools_ListOfShape()
    for s in shapes:
        args.Append(s)
    gf.SetArguments(args)
    gf.SetFuzzyValue(fuzzy)
    gf.SetRunParallel(parallel)
    gf.Perform()
    t_gf = time.perf_counter() - t0
    images = []
    for s in shapes:
        pieces = []
        exp = TopExp_Explorer(s, TopAbs_SOLID)
        while exp.More():
            pieces.extend(list(gf.Modified(exp.Current())) or [exp.Current()])
            exp.Next()
        comp = compound(pieces)
        images.append({k: _hashes(comp, k) for k in (TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX)})
    if pairs is None:
        pairs = list(combinations(range(len(shapes)), 2))
    rel = {}
    for i, j in pairs:
        a, b = images[i], images[j]
        shared = {k: [a[k][h] for h in a[k].keys() & b[k].keys()] for k in a}
        if shared[TopAbs_SOLID]:
            v = sum(volume(s) for s in shared[TopAbs_SOLID])
            area = 0.0
            for s in shared[TopAbs_SOLID]:
                props = GProp_GProps()
                _G.SurfaceProperties_s(s, props)
                area += props.Mass()
            rel[(i, j)] = dict(kind="overlap", volume=v, thickness=2 * v / area if area else 0.0,
                               pieces=len(shared[TopAbs_SOLID]))
        elif shared[TopAbs_FACE]:
            area = 0.0
            for f in shared[TopAbs_FACE]:
                props = GProp_GProps()
                _G.SurfaceProperties_s(f, props)
                area += props.Mass()
            rel[(i, j)] = dict(kind="face", area=area)
        elif shared[TopAbs_EDGE]:
            rel[(i, j)] = dict(kind="edge", edges=len(shared[TopAbs_EDGE]))
        elif shared[TopAbs_VERTEX]:
            rel[(i, j)] = dict(kind="vertex", vertices=len(shared[TopAbs_VERTEX]))
        else:
            rel[(i, j)] = dict(kind="none")
    return dict(errors=bool(gf.HasErrors()), warnings=bool(gf.HasWarnings()), relations=rel,
                gf_seconds=t_gf, seconds=time.perf_counter() - t0)
