"""問い合わせ層のインタフェース(実装計画書 §2 アーキテクチャ、§4 技術スタック、§6予防4)。

verbがbuild123dを直接呼ばないための境界。**build123dをimportしてよいのはこのモジュールだけ**
(tests/test_boundaries.py が機械的に検査する)。差し替え可能に保つのが目的で、
OCPを直接叩くのはkernel.pyの責務。

M0の契約:
    describe_shape(shape) -> dict
        {
            "face_count": int,
            "edge_count": int,
            "faces_by_type": {
                "plane": int,
                "cylinder_full": int,     # 全周円筒(穴)。UVBoundsのU範囲が350度超で判定
                "cylinder_partial": int,  # 全周でない円筒
                "torus": int,
                "cone": int,
                "bspline": int,
                "other": int,
            },
        }
    volume(shape) -> float
        形状の体積(mm^3)。

**役割分担**: トポロジの走査(面・辺の列挙と数え上げ)は build123d をラッパとして使う
(実装計画書§4「問い合わせは build123d をラッパとして」。`ShapeList` のまま
`filter_by(Axis.Z)` 等へ繋げられる形を保つ)。一方、面の種別判定と体積は
**OCCTの値そのものが契約になっている**ため kernel.py へ委譲する:

- 体積は `BRepGProp.VolumeProperties_s`(Eps無し)で求めることがtests/README.mdの契約。
  build123d の `Compound.volume` はソリッドごとの合計を取る別経路なので使わない。
- 全周円筒の判定は `BRepAdaptor_Surface` のU範囲(度)で行う(build123dの `geom_type` は
  円筒を全周/部分に分けない)。

なお build123d の `.faces()` / `.edges()` は内部で `TopExp.MapShapes_s` を使うため
**重複除去済み**である(shelf_attachment_GD.step 実測: 面56・辺66。kernelの
`count_subshapes` と一致することを2026-08-10に確認した)。
"""
from __future__ import annotations

from typing import Any

# build123d の import はこのモジュールだけに許される(tests/test_boundaries.py)。
# `Compound.cast` は TopoDS_Shape の実型を見て正しいラッパ型へ振り分ける classmethod で、
# ソリッドを渡せば Solid、コンパウンドを渡せば Compound が返る(2026-08-10実測)。
# 基底の `Shape.cast` は抽象メソッドで None を返すため使えない。
from build123d import Compound, Edge

from . import kernel


def describe_shape(shape: Any) -> dict:
    """形状の面・辺の数と種別内訳(平面/全周円筒/部分円筒/トーラス/円錐/spline)を返す。

    `faces_by_type` の全キーの合計は常に `face_count` と一致する(数え漏れ・二重計上・
    未知キーの混入を検出できるようにするため、内訳は面を1枚ずつ分類して積み上げる)。
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
    """形状の体積(mm^3)を返す(Documentのunitsが常に "mm" であることに対応)。"""
    return kernel.volume(_unwrap(shape))


def describe_edges(shape: Any) -> list:
    """辺を1始まりの添字つきで列挙する。

    戻り値: `[{"index": int, "kind": "line"|"circle"|"other", "length": float,
              "start": [x,y,z], "end": [x,y,z]}, ...]`

    `start` / `end` は辺の端点(閉じた辺＝円などでは同じ点になる)。**向きが分からないと
    辺を選べない**ので端点まで返す — 「縦の4辺だけ丸める」のような、人間にもAIにも自然な
    選び方が、端点なしでは表現できない(添字の決め打ちになり、形が変わると別の辺を指す)。

    **フィレット・面取りの辺はここから選ぶ**(verbが build123d を直接呼ばないための境界。
    実装計画書§6予防4)。

    **添字と種別は `kernel` 側から取る**(`edge_count` / `edge_of` / `classify_edge`)。
    build123d の `.edges()` も内部では同じ `TopExp.MapShapes_s` を使うが、その順序が
    `kernel._edge_map` と一致することは保証されていない。ここがずれると
    **「選んだ辺と違う辺が丸まる」**という、例外も出ない静かな事故になるので、
    **選択の識別子は必ず kernel の数え方に揃え**、build123d は長さと端点の計測にだけ使う。
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


def _wrap(shape: Any):
    """生の TopoDS_Shape を build123d のラッパへ(既にラッパならそのまま)。"""
    if shape is None:
        raise ValueError("shape is None (nothing to describe)")
    if hasattr(shape, "wrapped"):  # 既に build123d の Shape
        return shape
    return Compound.cast(shape)


def _unwrap(shape: Any):
    """build123d のラッパなら中の TopoDS_Shape を取り出す(kernelへ渡すため)。"""
    if shape is None:
        raise ValueError("shape is None (nothing to measure)")
    return getattr(shape, "wrapped", shape)
