"""問い合わせ層 — verbがbuild123dを直接呼ばないための境界。

**build123dをimportしてよいのはこのモジュールだけ**(OCPを直接叩くのは kernel.py の責務)。

面の列挙は build123d に任せる。build123d の `.faces()` は内部で `TopExp.MapShapes_s` を使うため**重複除去済み**で、
kernel の `face_count` と一致する。**辺は一致しない**: `.edges()` は縮退辺(球の極・円錐の頂点・フィレットの角。長さ0)を除くので、
辺の数と添字は kernel の `edge_count` / `edge_of`(選択・フィレット・`describe_edges` が共有する添字の体系)から取る。
一方、次の2つは**OCCTの値そのものが契約**なので kernel へ委譲する:

- 体積は `BRepGProp.VolumeProperties_s`(Eps無し)で求める。build123d の `Compound.volume` は
  ソリッドごとの合計を取る別経路なので使わない。
- 全周円筒の判定は `BRepAdaptor_Surface` のU範囲(度)で行う(build123dの `geom_type` は
  円筒を全周/部分に分けない)。
"""
from __future__ import annotations

from typing import Any

# `Compound.cast` は TopoDS_Shape の実型を見て正しいラッパ型へ振り分ける classmethod。
# 基底の `Shape.cast` は抽象メソッドで None を返すため使えない。
from build123d import CenterOf, Compound, Edge, Face

from . import kernel


def describe_shape(shape: Any) -> dict:
    """形状の面・辺の数と種別内訳(平面/全周円筒/部分円筒/トーラス/円錐/spline)を返す。

    `faces_by_type` の全キーの合計は常に `face_count` と一致する。`edge_count` は縮退辺を含み、`describe_edges` の件数・
    辺の選択で指せる添字の上限と同じ(build123d の `.edges()` の件数ではない)。
    """
    wrapped = _wrap(shape)
    faces = wrapped.faces()

    faces_by_type = {kind: 0 for kind in kernel.FACE_KINDS}
    for face in faces:
        faces_by_type[kernel.classify_face(face.wrapped)] += 1

    return {
        "face_count": len(faces),
        "edge_count": kernel.edge_count(_unwrap(shape)),
        "faces_by_type": faces_by_type,
    }


def volume(shape: Any) -> float:
    """形状の体積(mm^3)。"""
    return kernel.volume(_unwrap(shape))


def describe_edges(shape: Any) -> list:
    """辺を1始まりの添字つきで列挙する(フィレット・面取りの辺はここから選ぶ)。

    **添字と種別は `kernel` 側から取る**。build123d の `.edges()` の順序が `kernel._edge_map`
    と一致する保証は無く、ずれると**選んだ辺と違う辺が丸まる**ので、build123d は長さと端点の
    計測にだけ使う。

    `start` / `end` は辺の端点(閉じた辺=円などでは同じ点になる)。
    """
    raw = _unwrap(shape)
    return [_describe_edge(kernel.edge_of(raw, index), index) for index in range(1, kernel.edge_count(raw) + 1)]


def describe_edge(shape: Any, edge_index: int) -> dict:
    """`describe_edges` の1件を**辺1本ぶんだけ**測る(選択1件の表示で全辺を測らない)。範囲外は ValueError。"""
    index = int(edge_index)
    return _describe_edge(kernel.edge_of(_unwrap(shape), index), index)


def _describe_edge(edge: Any, index: int) -> dict:
    wrapped = Edge(edge)
    start = wrapped.start_point()
    end = wrapped.end_point()
    return {
        "index": index,
        "kind": kernel.classify_edge(edge),
        "length": kernel.edge_length(edge),
        "start": [float(start.X), float(start.Y), float(start.Z)],
        "end": [float(end.X), float(end.Y), float(end.Z)],
    }


def face_centroid(shape: Any, face_index: int) -> list:
    """面1枚の**面積重心**(`CenterOf.MASS`)をワールド座標[mm]で返す。

    **`face_fingerprint` の `center` とは別物。** あちらは `Face.center()` の既定
    (`CenterOf.GEOMETRY`)で、曲面では面パッチの上にすら乗らない —— hanger の荷重面
    (トーラス、面積 2783mm^2)で GEOMETRY は `[0, -212.862, 60]`、MASS は `[0, -200, 60]` と
    12.9mm 離れる。面の上に material が乗る場所を知りたい側はこちら
    (`optimize_run._check_anchor_centroids`)。平面では一致する。

    添字は1始まり。範囲外は ValueError(`kernel.face_of` が投げる)。
    """
    index = int(face_index)
    point = Face(kernel.face_of(_unwrap(shape), index)).center(CenterOf.MASS)
    return [float(point.X), float(point.Y), float(point.Z)]


def face_fingerprint(shape: Any, face_index: int) -> dict:
    """面1枚の**指紋** `{"index", "area", "normal", "center"}` を返す。

    **曲面の `center` は面積重心ではない**(`Face.center()` の既定 = `CenterOf.GEOMETRY` は、build123d 0.11.1
    では平面なら面積重心、曲面なら UV 範囲の中点を返す)。
    照合に使うのは面積と法線だけ(`optimize.fingerprint_mismatch`)。「面の上のどこか」には
    使えない(曲面では面パッチから離れる)ので、その用途は `face_centroid`。

    `describe_faces` の1件と同じ値を**面1枚ぶんだけ**測る(2835面のボディで全面を測らない。
    `duocad/optimize.py`)。`kind` は含めない。

    添字は1始まり。範囲外は ValueError(`kernel.face_of` が投げる。ここで数え直さない ——
    `_face_map` の全走査が2回になる)。
    """
    index = int(face_index)
    wrapped = Face(kernel.face_of(_unwrap(shape), index))
    normal = wrapped.normal_at()
    center = wrapped.center()
    return {
        "index": index,
        "area": float(wrapped.area),
        "normal": [float(normal.X), float(normal.Y), float(normal.Z)],
        "center": [float(center.X), float(center.Y), float(center.Z)],
    }


def describe_faces(shape: Any) -> list:
    """面を1始まりの添字つきで列挙する。

    1. **添字は `kernel` の数え方に揃える**。ずれると選んだ面と違う面が押される。
    2. **法線は外向き**で、`kernel.tessellate` の `faces[].normal` と同じ約束。向きは
       build123d の `Face.normal_at()` に任せる —— `BRepPrimAPI_MakeBox` は底面を
       `TopAbs_REVERSED` で持つが `normal_at()` は外向きを返すので、**自分で反転すると二重になる**。

    曲面の `normal` / `center` は面の中央(UV範囲の中点)での値でしかない。
    """
    raw = _unwrap(shape)
    return [_describe_face(kernel.face_of(raw, index), index) for index in range(1, kernel.face_count(raw) + 1)]


def describe_face(shape: Any, face_index: int) -> dict:
    """`describe_faces` の1件を**面1枚ぶんだけ**測る(選択1件の表示で全面を測らない)。範囲外は ValueError。"""
    index = int(face_index)
    return _describe_face(kernel.face_of(_unwrap(shape), index), index)


def _describe_face(face: Any, index: int) -> dict:
    wrapped = Face(face)
    normal = wrapped.normal_at()
    center = wrapped.center()
    return {
        "index": index,
        "kind": kernel.classify_face(face),
        "area": float(wrapped.area),
        "normal": [float(normal.X), float(normal.Y), float(normal.Z)],
        "center": [float(center.X), float(center.Y), float(center.Z)],
    }


def describe_vertices(shape: Any) -> list:
    """頂点を1始まりの添字つきで列挙する(`[{"index": int, "point": [x,y,z]}, ...]`)。

    **添字は `kernel.count_subshapes(shape, "vertex")` の体系**(`selection._select_shape`
    と同じ)。build123d の `.vertices()` の順序とは一致する保証が無いので、座標も順序も
    `kernel.vertex_positions` から取る。
    """
    return kernel.vertex_positions(_unwrap(shape))


def edges_of_face(shape: Any, face_index: int) -> list:
    """その面の**外周**を成す辺の添字を昇順で返す。

    返すのは外周(outer wire)の辺だけで、**面に空いた穴の縁は含めない**(外周を丸める操作が
    黙って穴の縁まで変えないため)。添字は `describe_edges` の `index` および
    `fillet(edges=[...])` の添字と一致する。
    """
    raw = _unwrap(shape)
    face = Face(kernel.face_of(raw, face_index))
    indexes = {
        kernel.edge_index_of(raw, edge.wrapped) for edge in face.outer_wire().edges()
    }
    return sorted(indexes)


def _wrap(shape: Any):
    if shape is None:
        raise ValueError("shape is None (nothing to describe)")
    if hasattr(shape, "wrapped"):
        return shape
    return Compound.cast(shape)


def _unwrap(shape: Any):
    if shape is None:
        raise ValueError("shape is None (nothing to measure)")
    return getattr(shape, "wrapped", shape)
