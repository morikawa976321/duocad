"""問い合わせ層 — verbがbuild123dを直接呼ばないための境界。

**build123dをimportしてよいのはこのモジュールだけ**(OCPを直接叩くのは kernel.py の責務)。

トポロジの走査(面・辺の列挙と数え上げ)は build123d に任せる。build123d の `.faces()` /
`.edges()` は内部で `TopExp.MapShapes_s` を使うため**重複除去済み**で、kernel の
`count_subshapes` と一致する。一方、次の2つは**OCCTの値そのものが契約**なので kernel へ委譲する:

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

    `faces_by_type` の全キーの合計は常に `face_count` と一致する。
    """
    wrapped = _wrap(shape)
    faces = wrapped.faces()
    edges = wrapped.edges()

    faces_by_type = {kind: 0 for kind in kernel.FACE_KINDS}
    for face in faces:
        faces_by_type[kernel.classify_face(face.wrapped)] += 1

    return {
        "face_count": len(faces),
        "edge_count": len(edges),
        "faces_by_type": faces_by_type,
    }


def volume(shape: Any) -> float:
    """形状の体積(mm^3)。"""
    return kernel.volume(_unwrap(shape))


def describe_edges(shape: Any) -> list:
    """辺を1始まりの添字つきで列挙する(フィレット・面取りの辺はここから選ぶ)。

    **添字と種別は `kernel` 側から取る**(`edge_count` / `edge_of` / `classify_edge`)。
    build123d の `.edges()` も内部では同じ `TopExp.MapShapes_s` を使うが、その順序が
    `kernel._edge_map` と一致することは保証されていない。ずれると**選んだ辺と違う辺が丸まる**
    という、例外も出ない静かな事故になるので、build123d は長さと端点の計測にだけ使う。

    `start` / `end` は辺の端点(閉じた辺=円などでは同じ点になる)。
    """
    raw = _unwrap(shape)
    described = []
    for index in range(1, kernel.edge_count(raw) + 1):
        edge = kernel.edge_of(raw, index)
        wrapped = Edge(edge)
        start = wrapped.start_point()
        end = wrapped.end_point()
        described.append(
            {
                "index": index,
                "kind": kernel.classify_edge(edge),
                "length": float(wrapped.length),
                "start": [float(start.X), float(start.Y), float(start.Z)],
                "end": [float(end.X), float(end.Y), float(end.Z)],
            }
        )
    return described


def face_centroid(shape: Any, face_index: int) -> list:
    """面1枚の**面積重心**(`CenterOf.MASS`)をワールド座標[mm]で返す。

    **`face_fingerprint` の `center` とは別物である。** あちらは `Face.center()` の既定
    (`CenterOf.GEOMETRY`)で、曲面では面パッチの上にすら乗らない —— hanger の荷重面
    (トーラス、面積 2783mm^2)で GEOMETRY は `[0, -212.862, 60]`、MASS は `[0, -200, 60]` と
    12.9mm 離れる。**面の上に material が乗る場所を知りたい側はこちらを使うこと**
    (`optimize_run._check_anchor_centroids`)。平面では両者は一致する。

    添字は1始まり(`face_fingerprint` と同じ体系)。範囲外は ValueError(`kernel.face_of` が投げる)。
    """
    index = int(face_index)
    point = Face(kernel.face_of(_unwrap(shape), index)).center(CenterOf.MASS)
    return [float(point.X), float(point.Y), float(point.Z)]


def face_fingerprint(shape: Any, face_index: int) -> dict:
    """面1枚の**指紋**(面積・法線・`Face.center()`)を返す。
    `{"index", "area", "normal", "center"}`。

    **`center` は面積重心ではない**(`Face.center()` の既定 = `CenterOf.GEOMETRY`)。
    照合に使うのは面積と法線だけ(`optimize.fingerprint_mismatch`)なので値そのものは
    問題にならないが、**「面の上のどこか」として使ってはいけない** —— 曲面では面パッチから
    離れる。その用途は `face_centroid`。

    値の作り方は `describe_faces` の1件と同じで、**面1枚ぶんしか測らない**のが違い ——
    形状最適化の条件は数枚の面を指すだけなので、2835面のボディで全面を測る理由が無い
    (`duocad/optimize.py`)。`kind` を含めないのは指紋に使わないためで、種別が要る用途は
    `describe_faces` を使うこと。

    添字は1始まり(`describe_faces` / `selection.select_face` と同じ体系)。範囲外は ValueError
    (`kernel.face_of` が投げる。ここで数え直さない —— `_face_map` の全走査が2回になる)。
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

    1. **添字は `kernel` の数え方に揃える**(`face_count` / `face_of`)。ずれると
       「選んだ面と違う面が押される」という、例外も出ない静かな事故になる。
    2. **法線は外向き**で、`kernel.tessellate` の `faces[].normal` と同じ約束
       (立方体なら `normal·Z > 0.9` を満たす面は上面のちょうど1枚)。向きは build123d の
       `Face.normal_at()` に任せる —— `BRepPrimAPI_MakeBox` は底面を `TopAbs_REVERSED` で
       持つが `normal_at()` はそれを織り込んだ外向きを返すので、**自分でもう一度反転すると
       二重になる**。tessellate とは別のコードパスなので、どちらかだけ壊れると述語だけが
       静かにずれる。

    曲面の `normal` / `center` は面の中央(UV範囲の中点)での値でしかなく、述語で曲面を
    厳密に選ぶ用途には使わない。
    """
    raw = _unwrap(shape)
    described = []
    for index in range(1, kernel.face_count(raw) + 1):
        face = kernel.face_of(raw, index)
        wrapped = Face(face)
        normal = wrapped.normal_at()
        center = wrapped.center()
        described.append(
            {
                "index": index,
                "kind": kernel.classify_face(face),
                "area": float(wrapped.area),
                "normal": [float(normal.X), float(normal.Y), float(normal.Z)],
                "center": [float(center.X), float(center.Y), float(center.Z)],
            }
        )
    return described


def describe_vertices(shape: Any) -> list:
    """頂点を1始まりの添字つきで列挙する(`[{"index": int, "point": [x,y,z]}, ...]`)。

    **添字は `kernel.count_subshapes(shape, "vertex")` の体系**で、`selection.select_vertex`
    が範囲を検査する数え方と同じ。build123d の `.vertices()` の順序がこれと一致する保証は
    無いので、座標も順序も `kernel.vertex_positions` から取る(`describe_edges` が
    `.edges()` を使わないのと同じ理由)。
    """
    return kernel.vertex_positions(_unwrap(shape))


def edges_of_face(shape: Any, face_index: int) -> list:
    """その面の**外周**を成す辺の添字を昇順で返す。

    返すのは外周(outer wire)の辺だけで、**面に空いた穴の縁は含めない** —— 外周を
    丸めたつもりの操作が黙って穴の縁まで変えないため(穴の縁を丸めるとトーラスになる)。
    穴を対象にする述語が要るようになったら別の `op` として足すこと。
    添字は `describe_edges` の `index` および `fillet(edges=[...])` の添字と一致する。
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
