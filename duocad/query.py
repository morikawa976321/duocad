"""問い合わせ層 — verbがカーネル/OCPを直接呼ばないための境界。

面・辺の添字と列挙は `brep_topology._face_map` / `_edge_map` に揃える(`TopExp.MapShapes_s` 相当。
**辺の契約は indexed map の件数**(`brep_topology.edge_count` / `edge_of`)。縮退辺を含む。
`TopExp_Explorer` だけの走査は辺を二重に数え、他の列挙は縮退辺を落とすことがある。
次の値は OCCT 経由で `brep_geometry` / `kernel` から取る:

- 体積は `BRepGProp.VolumeProperties_s`(Eps無し)。
- 全周円筒の判定は `BRepAdaptor_Surface` のU範囲(度)(`classify_face`)。
"""
from __future__ import annotations

from typing import Any

from . import kernel

from . import (
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)


def describe_shape(shape: Any) -> dict:
    """形状の面・辺の数と種別内訳(平面/全周円筒/部分円筒/トーラス/円錐/spline)を返す。

    `faces_by_type` の全キーの合計は常に `face_count` と一致する。`edge_count` は縮退辺を含み、`describe_edges` の件数・
    辺の選択で指せる添字の上限と同じ。
    """
    if shape is None:
        raise ValueError("shape is None (nothing to describe)")
    raw = _unwrap(shape)
    face_map = _brep_topology._face_map(raw)

    faces_by_type = {kind: 0 for kind in _brep_geometry.FACE_KINDS}
    for index in range(1, face_map.Extent() + 1):
        faces_by_type[_brep_geometry.classify_face(_brep_topology._face_at(face_map, index))] += 1

    return {
        "face_count": face_map.Extent(),
        "edge_count": _brep_topology.edge_count(raw),
        "faces_by_type": faces_by_type,
    }


def volume(shape: Any) -> float:
    """形状の体積(mm^3)。"""
    return _brep_geometry.volume(_unwrap(shape))


def describe_edges(shape: Any) -> list:
    """辺を1始まりの添字つきで列挙する(フィレット・面取りの辺はここから選ぶ)。

    **添字と種別は `kernel` 側から取る**。`_edge_map` と一致しない列挙を使うと**選んだ辺と違う辺が丸まる**。

    `start` / `end` は辺の端点(閉じた辺=円などでは同じ点になる)。
    """
    raw = _unwrap(shape)
    edge_map = _brep_topology._edge_map(raw)
    return [_describe_edge(_brep_topology._edge_at(edge_map, index), index) for index in range(1, edge_map.Extent() + 1)]


def describe_edge(shape: Any, edge_index: int) -> dict:
    """`describe_edges` の1件を**辺1本ぶんだけ**測る(選択1件の表示で全辺を測らない)。範囲外は ValueError。"""
    index = int(edge_index)
    return _describe_edge(_brep_topology.edge_of(_unwrap(shape), index), index)


def _describe_edge(edge: Any, index: int) -> dict:
    start, end = _brep_geometry.edge_endpoints(edge)
    return {
        "index": index,
        "kind": _brep_geometry.classify_edge(edge),
        "degenerate": _brep_geometry.edge_degenerate(edge),
        "length": _brep_geometry.edge_length(edge),
        "start": [float(start[0]), float(start[1]), float(start[2])],
        "end": [float(end[0]), float(end[1]), float(end[2])],
    }


def face_centroid(shape: Any, face_index: int) -> list:
    """面1枚の**面積重心**をワールド座標[mm]で返す。

    **`face_fingerprint` の `center` とは別物。** あちらは平面なら面積重心と一致するが、曲面では UV 範囲の
    中点(面パッチの上にすら乗らないことがある) —— hanger の荷重面(トーラス、面積 2783mm^2)で
    代表点は `[0, -212.862, 60]`、面積重心は `[0, -200, 60]` と 12.9mm 離れる。
    面の上に material が乗る場所を知りたい側はこちら(`optimize_run._check_anchor_centroids`)。

    添字は1始まり。範囲外は ValueError(`brep_topology.face_of` が投げる)。
    """
    index = int(face_index)
    _, centre = _brep_geometry._face_props(_brep_topology.face_of(_unwrap(shape), index))
    return [float(centre[0]), float(centre[1]), float(centre[2])]


def face_fingerprint(shape: Any, face_index: int) -> dict:
    """面1枚の**指紋** `{"index", "area", "normal", "center"}` を返す。

    **曲面の `center` は面積重心ではない**(平面なら面積重心、曲面なら UV 範囲の中点)。
    照合に使うのは面積と法線だけ(`optimize.fingerprint_mismatch`)。「面の上のどこか」には
    使えない(曲面では面パッチから離れる)ので、その用途は `face_centroid`。

    `describe_faces` の1件と同じ値を**面1枚ぶんだけ**測る(2835面のボディで全面を測らない。
    `duocad/optimize.py`)。`kind` は含めない。

    添字は1始まり。範囲外は ValueError(`brep_topology.face_of` が投げる。ここで数え直さない ——
    `_face_map` の全走査が2回になる)。
    """
    index = int(face_index)
    face = _brep_topology.face_of(_unwrap(shape), index)
    normal = _brep_geometry.face_normal_at(face)
    center = _brep_geometry.face_query_center(face)
    area, _ = _brep_geometry._face_props(face)
    return {
        "index": index,
        "area": float(area),
        "normal": [float(normal[0]), float(normal[1]), float(normal[2])],
        "center": [float(center[0]), float(center[1]), float(center[2])],
    }


def describe_faces(shape: Any) -> list:
    """面を1始まりの添字つきで列挙する。

    1. **添字は `kernel` の数え方に揃える**。ずれると選んだ面と違う面が押される。
    2. **法線は外向き**で、`kernel.tessellate` の `faces[].normal` と同じ約束 ——
       `BRepPrimAPI_MakeBox` は底面を `TopAbs_REVERSED` で持つが `BRepGProp_Face` は外向きを返すので、
       **自分で反転すると二重になる**。

    曲面の `normal` / `center` は面の中央(UV範囲の中点)での値でしかない。
    """
    raw = _unwrap(shape)
    face_map = _brep_topology._face_map(raw)
    return [_describe_face(_brep_topology._face_at(face_map, index), index) for index in range(1, face_map.Extent() + 1)]


def describe_face(shape: Any, face_index: int) -> dict:
    """`describe_faces` の1件を**面1枚ぶんだけ**測る(選択1件の表示で全面を測らない)。範囲外は ValueError。"""
    index = int(face_index)
    return _describe_face(_brep_topology.face_of(_unwrap(shape), index), index)


def _describe_face(face: Any, index: int) -> dict:
    normal = _brep_geometry.face_normal_at(face)
    center = _brep_geometry.face_query_center(face)
    area, _ = _brep_geometry._face_props(face)
    return {
        "index": index,
        "kind": _brep_geometry.classify_face(face),
        "area": float(area),
        "normal": [float(normal[0]), float(normal[1]), float(normal[2])],
        "center": [float(center[0]), float(center[1]), float(center[2])],
    }


def describe_vertices(shape: Any) -> list:
    """頂点を1始まりの添字つきで列挙する(`[{"index": int, "point": [x,y,z]}, ...]`)。

    **添字は `brep_topology.count_subshapes(shape, "vertex")` の体系**(`selection._select_shape`
    と同じ)。`kernel.vertex_positions` から取る。
    """
    return kernel.vertex_positions(_unwrap(shape))


def edges_of_face(shape: Any, face_index: int) -> list:
    """その面の**外周**を成す辺の添字を昇順で返す。

    返すのは外周(outer wire)の辺だけで、**面に空いた穴の縁は含めない**(外周を丸める操作が
    黙って穴の縁まで変えないため)。添字は `describe_edges` の `index` および
    `fillet(edges=[...])` の添字と一致する。
    """
    raw = _unwrap(shape)
    face = _brep_topology.face_of(raw, face_index)
    indexes = {
        kernel.edge_index_of(raw, edge) for edge in _brep_geometry.outer_wire_edge_shapes(face)
    }
    return sorted(indexes)


def _unwrap(shape: Any):
    if shape is None:
        raise ValueError("shape is None (nothing to measure)")
    return getattr(shape, "wrapped", shape)
