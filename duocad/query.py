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
from build123d import Compound

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
