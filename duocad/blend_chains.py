"""辺の連なり: フィレット・面取りで1本として振る舞う辺の組(画面の辺の選択単位)。

`kernel.tessellate_edges` が各辺の `chain` に使う。添字は `kernel._edge_map` と同じ数え方(`indexed_map` の1始まり)。
"""
from __future__ import annotations

import math
from typing import Dict, List

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCP.gp import gp_Pnt, gp_Vec

from .mesh_geometry import indexed_map

#: 端点で接線が平行とみなす角度(ラジアン)。`MakeFillet.Add` を呼ぶ辺を絞るだけの篩で、連なりそのものは
#: OCCT が決める。OCCT の接線判定より緩くしておく(取りこぼすと連なりが割れ、余分は `Add` 1回ぶんの時間だけ)。
SIEVE_ANGLE = math.radians(5.0)


def blend_chains(shape: TopoDS_Shape) -> Dict[int, int]:
    """辺の添字 → その辺が属する連なりの代表(連なりの最小の添字)。

    連なりは OCCT のフィレットが1本の辺から広げる範囲そのもの(`MakeFillet.Add` の輪郭)。
    実測: 1本を渡すと、一直線上で割れた辺も、直線→円弧→直線の接線連続も、フィレット・面取りとも
    全部にかかる。画面の選択をこの単位にしないと「光った1本より広く丸まる」になる。

    `Add` 1回は形状全体に比例して重い(実測: 1,212辺で 0.9ms、4,812辺で 3.8ms)ので、全辺に呼ぶと辺数の2乗で
    伸びる(円柱の穴を400個あけた板の4,812辺で 8.4 秒)。端点で接線が平行な相手を持つ辺だけに呼ぶ(`_sieve`。同じ板で
    `Add` は0回、篩だけの 1.1 秒)。
    """
    edge_map = indexed_map(shape, TopAbs_EDGE)
    chains: Dict[int, int] = {index: index for index in range(1, edge_map.Extent() + 1)}
    candidates = _sieve(shape, edge_map)
    if not candidates:
        return chains
    maker = BRepFilletAPI_MakeFillet(shape)
    done: set = set()
    for index in candidates:
        if index in done:
            continue
        contours = maker.NbContours()
        try:
            maker.Add(1.0, TopoDS.Edge_s(edge_map.FindKey(index)))
        except Exception:  # noqa: BLE001  OCCTはかけられない辺で例外を投げることがある
            continue
        if maker.NbContours() == contours:
            continue
        contour = contours + 1
        members = {index} | {edge_map.FindIndex(maker.Edge(contour, j)) for j in range(1, maker.NbEdges(contour) + 1)}
        members.discard(0)
        for member in members:
            chains[member] = min(members)
        done |= members
    return chains


def _sieve(shape: TopoDS_Shape, edge_map) -> List[int]:
    """端点を共有する別の辺と、その端点での接線が(向きを問わず)平行な辺の添字。"""
    vertex_edges = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_VERTEX, TopAbs_EDGE, vertex_edges)
    sine_limit = math.sin(SIEVE_ANGLE)
    found: set = set()
    for vertex_number in range(1, vertex_edges.Extent() + 1):
        vertex = TopoDS.Vertex_s(vertex_edges.FindKey(vertex_number))
        point = BRep_Tool.Pnt_s(vertex)
        tangents = []
        seen: set = set()
        for ancestor in vertex_edges.FindFromIndex(vertex_number):
            index = edge_map.FindIndex(ancestor)
            if index == 0 or index in seen:
                continue
            seen.add(index)
            tangent = _tangent_at(TopoDS.Edge_s(ancestor), point)
            if tangent is not None:
                tangents.append((index, tangent))
        for a in range(len(tangents)):
            for b in range(a + 1, len(tangents)):
                if tangents[a][1].Crossed(tangents[b][1]).Magnitude() <= sine_limit:
                    found.add(tangents[a][0])
                    found.add(tangents[b][0])
    return sorted(found)


def _tangent_at(edge, point: gp_Pnt):
    """辺の、`point` に近い方の端での単位接線(退化していれば None)。"""
    if BRep_Tool.Degenerated_s(edge):
        return None
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    parameter = first if curve.Value(first).Distance(point) <= curve.Value(last).Distance(point) else last
    origin, derivative = gp_Pnt(), gp_Vec()
    curve.D1(parameter, origin, derivative)
    if derivative.Magnitude() <= 1e-12:
        return None
    return derivative.Normalized()
