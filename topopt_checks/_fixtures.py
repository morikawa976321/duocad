"""受入検査・測定・pytest が共有する合成題材と、製品と同じ部品で組む格子モデル。**私物STEPに依存しない。**

`duocad/` の外に置く理由は `_bootstrap.py` と同じ(build123d を直接 import する)。
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

import numpy as np

from duocad.topopt import anchor as an
from duocad.topopt import design_space as ds
from duocad.topopt import voxelize as vx


def voxel_model(
    shape: Any,
    elements_long_axis: int = 40,
    *,
    keep: Any = None,
    obstacle: Any = None,
    grid: Optional[vx.Grid] = None,
) -> vx.VoxelModel:
    """1つのソリッドを「削る前の塊」兼「面アンカーの対象」として格子に載せる。

    格子は **B-rep の bbox** から起こす(三角形分割の bbox からではない。弦は真の面より内側に来るので、
    仕様の「B-repの周囲に1要素分の余白」が満たせなくなる)。占有は `stats["occupancy"]` に入れる。
    """
    solid = vx.unwrap(shape)
    if grid is None:
        lo, hi = vx._shape_bbox(solid)
        grid = vx.grid_for_bbox(lo, hi, elements_long_axis)
    dev = max(grid.spacing * vx.DEFAULT_DEVIATION_RATIO, 1e-6)
    soup = vx.triangles_from_shape(
        solid, deviation=dev, angular_deflection=vx.DEFAULT_ANGULAR_DEFLECTION
    )
    design, occupancy_stats = vx.occupancy(soup, grid)
    model = vx.model_from_design_mask(
        grid, design, anchor_shape=soup, keep=keep, obstacle=obstacle, deviation=dev
    )
    model.stats["occupancy"] = occupancy_stats
    return model


# ---------------------------------------------------------------------------
# 合成ブラケット
# ---------------------------------------------------------------------------
def demo_bracket():
    """x方向に伸びた箱の、先端の上に小さな耳(lug)を生やした形。

    - 固定面: 箱の x が最小の面(面積 24x24)
    - 荷重面: 耳の上面(面積 8x8)。箱の面とは別の面添字になるので、
      「小さい面を名指しして荷重を載せる」がそのまま試せる。
    """
    from build123d import Box, Pos

    body = Box(60, 24, 24)
    lug = Pos(26, 0, 15) * Box(8, 8, 6)
    return body + lug


def demo_faces(soup: vx.TriangleSoup) -> tuple[int, int]:
    """`demo_bracket()` の (固定面, 荷重面) の面添字を、位置から決定的に選ぶ。"""
    hint = "。demo_bracket を変えたら見直すこと"
    fixed = an.the_face_matching(soup, kind="plane", normal=(-1, 0, 0), near_point=(-30, 0, 0), near_tol=1e-6, hint=hint)
    load = an.the_face_matching(soup, kind="plane", normal=(0, 0, 1), near_point=(26, 0, 18), near_tol=1e-6, hint=hint)
    return fixed, load


# ---------------------------------------------------------------------------
# 合成の4条件
# ---------------------------------------------------------------------------
def demo_conditions(angle_deg: float = 0.0) -> ds.Conditions:
    """4条件だけの合成題材。**設計空間ソリッドは作らない。**

    - 保持ソリッド: 固定側パッド(x=-30の板)と荷重側パッド(x=+26,z=15の小箱)の2つ。
      利用者が「ここは必ず材料を残す」と指定した部分に相当する。両者は離れており、
      **その間をどう繋ぐかが最適化の答え**になる。
    - 除外ソリッド: 2つのパッドの間を y 方向に貫く円柱(配線・ボルトの通り道に相当)。
    - 拘束面: 固定側パッドの x 最小面。荷重面: 荷重側パッドの上面。
      **どちらも保持ソリッドの面**(利用者が画面でクリックできるのは自分が置いた形の面だけ)。

    `angle_deg` は世界の z 軸まわりの回転。**外接箱は軸整列なので、回すと塊が膨らむ**
    —— その姿勢依存を測るための口。
    """
    from build123d import Box, Cylinder, Pos, Rot

    rot = Rot(0, 0, float(angle_deg))
    fixed_pad = rot * (Pos(-30, 0, 0) * Box(6, 24, 24))
    load_pad = rot * (Pos(26, 0, 15) * Box(8, 8, 6))
    keep = rot * ((Pos(-30, 0, 0) * Box(6, 24, 24)) + (Pos(26, 0, 15) * Box(8, 8, 6)))
    obstacle = rot * (Pos(0, 0, 0) * Rot(90, 0, 0) * Cylinder(radius=5, height=40))

    soup = vx.triangles_from_shape(vx.unwrap(keep), deviation=0.2)
    fixed = _rotated_face(soup, angle_deg, normal=(-1, 0, 0), point=(-33, 0, 0))
    load = _rotated_face(soup, angle_deg, normal=(0, 0, 1), point=(26, 0, 18))
    bodies = (
        ds.condition_body("fixed_pad", fixed_pad, roles=("keep", "fixed")),
        ds.condition_body("load_pad", load_pad, roles=("keep", "load")),
    )
    return ds.Conditions(
        keep=keep, fixed_faces=fixed, load_faces=load, obstacle=obstacle, bodies=bodies
    )


def severed_conditions(angle_deg: float = 0.0, *, thickness: float = 10.0) -> ds.Conditions:
    """`demo_conditions` の除外ソリッドを、**拘束と荷重の間を丸ごと塞ぐ壁**に差し替えた配置。

    壁は2つのパッドの間(x=0)に立て、y/z 方向は塊よりずっと大きく取る —— 塊の断面を端まで
    塞がないと、**縁を回り込む道が残って「切れている」配置にならない**(既定の余白比率 0.20 で
    塊は y -25..25 / z -25..31 程度なので、±100 あれば余白比率を振っても足りる)。

    `thickness` は壁の厚み[mm]。**要素サイズより十分厚くすること** —— 要素中心が壁に入らないと
    塊が1要素も削れず、塞いだつもりの配置がつながったまま通る。
    """
    from build123d import Box, Pos, Rot

    base = demo_conditions(angle_deg)
    wall = Rot(0, 0, float(angle_deg)) * (Pos(0, 0, 0) * Box(float(thickness), 200, 200))
    return ds.Conditions(
        keep=base.keep,
        fixed_faces=base.fixed_faces,
        load_faces=base.load_faces,
        obstacle=wall,
        anchor_shape=base.anchor_shape,
        bodies=base.bodies,
    )


def _rotated_face(
    soup: vx.TriangleSoup, angle_deg: float, *, normal: Sequence[float], point: Sequence[float]
) -> int:
    """回す前の法線・重心で指定した面を、回したあとの三角形群から一意に選ぶ。

    面添字が回転で変わらないことに賭けない(OCCT の面の並びは形の作り方に依存するので、
    「回しても同じ添字のはず」は検査が守っていない仮定になる)。
    """
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    n = rot @ np.asarray(normal, dtype=float)
    p = rot @ np.asarray(point, dtype=float)
    return an.the_face_matching(
        soup, kind="plane", normal=np.round(n, 4).tolist(), near_point=np.round(p, 4).tolist(),
        normal_tol=1e-6, near_tol=1e-4, hint="。demo_conditions を変えたら見直すこと",
    )
