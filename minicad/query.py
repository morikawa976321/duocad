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
"""
from __future__ import annotations

from typing import Any


def describe_shape(shape: Any) -> dict:
    """形状の面・辺の数と種別内訳(平面/全周円筒/部分円筒/トーラス/円錐/spline)を返す。"""
    raise NotImplementedError


def volume(shape: Any) -> float:
    """形状の体積(mm^3)を返す。"""
    raise NotImplementedError
