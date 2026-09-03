"""4条件(保持ソリッド・除外ソリッド・拘束面・荷重面)から塊(設計空間)を導出する。

    ..\\.venv\\Scripts\\python.exe design_space.py --measure

**案1(外接直方体+余白)。** 条件の点群の軸整列外接直方体に余白を足し、そこから除外ソリッドを
引いたものを塊にする。これは最終形ではなく、後続で入れる「粗く1回回して膨らませる」案(案5)と
比べるための**ベースライン**である。案1の弱点は2つで、どちらも数字が README にある:

1. **余白が自由パラメータ** —— 利用者に見せない値なので、小さすぎると答えの居場所を黙って削り、
   誰も気づけない(engine `topopt-design-space-never-shown-to-the-user`)。
2. **姿勢依存** —— 軸整列の直方体なので、同じ部品を世界軸に対して回すと塊が膨らむ。膨らんだ塊は
   要素数・所要時間だけでなく**出てくる材料の量まで変える**(相対体積制約のため)。

## 除外ソリッドの扱い(設計判断)

除外ソリッドは**塊を作る時点で引き切り**、`voxelize.model_from_design_mask` の `obstacle` には
**渡さない**。理由は3つ:

- 塊 = 直方体 − 除外 と定義した以上、除外領域には塊の要素が1つも無い。`model_from_design_mask` は
  `keep` / `obstacle` が塊の内側に収まることを要求するので、同じ除外ソリッドを `obstacle` として
  渡すと**必ず全要素はみ出して `OutsideDesignSpaceError` になる**(しかも文面は「座標系・単位が
  違う」と誤誘導する)。
- 最適化に渡る情報は同じ。`anchor.run_pipeline` は `void = ~design | obstacle` を作るので、
  除外を塊から引いてあれば `~design` 側で同じ要素が落ちる。
- **`on_outside="clip"` へ倒す選択はしない。** clip は「塊からはみ出した保持/除外を黙って切る」
  指定で、前タスクが入れた「はみ出しは例外」という保証をこの経路だけ骨抜きにする。塊の外の除外は
  本来ただの no-op であって、切り詰めが要る状況ではない。

代わりに、**除外が保持を食っているケース**(塊から引いた結果、保持ソリッドが塊に入らなくなる)は
保持のはみ出しとして現れ、`DesignSpaceTooSmallError` になる。文面でその可能性を名指しする。

## 格子の外は誰も見ていない

`voxelize` のはみ出し判定は**格子の内側しか見ていない** —— 保持ソリッドが格子の外へ出た分は
`keep_dropped_outside` にも入らず例外にもならない(格子の外にはセル中心が存在しないため)。案1は
格子を条件の外接直方体から起こすので、余白が負のときにここを踏む。**この経路では格子の外へ出て
いないことを自分で確かめてから** `model_from_design_mask` を呼ぶ(`DesignSpaceTooSmallError`)。
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

import anchor as an
import voxelize as vx

_HERE = Path(__file__).resolve().parent

# 余白は**条件の外接直方体の最長辺に対する比率**で持つ(絶対mmでは部品の大きさに追従しない)。
# 既定値の根拠は README「余白比率をどう決めたか」。安全側=大きめに倒してある。
DEFAULT_MARGIN_RATIO = 0.25
DEFAULT_ELEMENTS_LONG_AXIS = 40


class DesignSpaceTooSmallError(vx.OutsideDesignSpaceError):
    """導出した塊が条件(保持ソリッド)を含みきれていない。

    `voxelize.OutsideDesignSpaceError` の派生なので、既存の `except ValueError` /
    `except vx.OutsideDesignSpaceError` はそのまま効く。**案1では余白を上げるのが正しい直し方**
    なので、文面でそれを名指しする(汎用の文面は「座標系・単位が違う」を疑わせて誤誘導する)。
    """


@dataclass(frozen=True)
class Conditions:
    """利用者が入れる4条件。**設計空間ソリッドは無い。**

    `fixed_face` / `load_face` は `anchor_shape`(省略時は `keep`)の三角形群での面添字。
    面添字は渡した形の面の並びで決まるので、面を選ぶのに使った形をそのまま渡すこと。
    """

    keep: Any
    fixed_face: int
    load_face: int
    obstacle: Any = None
    anchor_shape: Any = None

    @property
    def anchor(self) -> Any:
        return self.keep if self.anchor_shape is None else self.anchor_shape


@dataclass
class DesignSpace:
    """導出した塊と、その導出過程の実測。

    `mask` は `grid` と同じかたちの bool 配列(塊)。`model` は保持ソリッドを同じ格子に載せた
    `VoxelModel` で、**`model.obstacle` は常に全 False**(除外は塊から引き切っているため)。
    """

    grid: vx.Grid
    mask: np.ndarray
    model: vx.VoxelModel
    soup: vx.TriangleSoup
    conditions: Conditions
    bbox_min: np.ndarray
    bbox_max: np.ndarray
    margin: float
    margin_ratio: float
    stats: dict = field(default_factory=dict)

    @property
    def element_count(self) -> int:
        return int(np.count_nonzero(self.mask))

    @property
    def fixed_face(self) -> int:
        return int(self.conditions.fixed_face)

    @property
    def load_face(self) -> int:
        return int(self.conditions.load_face)


# ---------------------------------------------------------------------------
# 条件の点群 → 外接直方体
# ---------------------------------------------------------------------------
def condition_points(
    keep_soup: vx.TriangleSoup,
    anchor_soup: vx.TriangleSoup,
    faces: Sequence[int],
) -> np.ndarray:
    """条件の点群 = 保持ソリッドの三角形頂点 ∪ 指定した面の三角形頂点。

    **除外ソリッドは入れない。** 除外は塊から引くものであって、塊を広げる理由にはならない
    (入れると「材料を入れてはいけない場所」が塊を広げるという逆立ちが起きる)。
    """
    chunks = [np.asarray(keep_soup.positions, dtype=float).reshape(-1, 3)]
    for face in faces:
        tri = anchor_soup.triangles_of_face(int(face))
        if tri.size == 0:
            raise ValueError(
                f"面添字 {int(face)} に三角形が1枚も無い。"
                "面アンカーの対象として渡した形の面添字か確認すること"
            )
        verts = anchor_soup.triangles[tri].reshape(-1)
        chunks.append(np.asarray(anchor_soup.positions, dtype=float)[verts])
    points = np.concatenate(chunks, axis=0)
    if points.shape[0] == 0:
        raise ValueError("条件の点群が空(保持ソリッドにも指定面にも頂点が無い)")
    return points


def box_mask(grid: vx.Grid, lo: Sequence[float], hi: Sequence[float]) -> np.ndarray:
    """軸整列の箱の占有マスク。**要素中心が箱の中にあるか**だけで決める。

    OCCT を通さない(箱をソリッドに起こしてレイを飛ばすのは、答えが解析的に分かっている
    ものをわざわざ近似することになる)。
    """
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    axes = [grid.centers_1d(a) for a in range(3)]
    inside = [(axes[a] >= lo[a]) & (axes[a] <= hi[a]) for a in range(3)]
    return inside[0][:, None, None] & inside[1][None, :, None] & inside[2][None, None, :]


# ---------------------------------------------------------------------------
# 導出(本体)
# ---------------------------------------------------------------------------
def derive(
    conditions: Conditions,
    *,
    margin_ratio: float = DEFAULT_MARGIN_RATIO,
    elements_long_axis: int = DEFAULT_ELEMENTS_LONG_AXIS,
    spacing: Optional[float] = None,
    deviation: Optional[float] = None,
    deviation_ratio: float = vx.DEFAULT_DEVIATION_RATIO,
    angular_deflection: float = vx.DEFAULT_ANGULAR_DEFLECTION,
) -> DesignSpace:
    """4条件から塊(設計空間)を導出する。**設計空間ソリッドは受け取らない。**

    手順: 条件の点群 → 外接直方体 → 余白(最長辺 × `margin_ratio`)→ `grid_for_bbox` で格子 →
    直方体の占有から除外ソリッドの占有を引く → 保持ソリッドが塊に完全に含まれることを確認。

    `margin_ratio` は**負も受け付ける**(「余白を外すと壊れる」を検査で固定するための口)。
    負にすると保持ソリッドを削るので、通常は `DesignSpaceTooSmallError` になる。

    `spacing` を渡すと `elements_long_axis` の代わりに**要素サイズを固定**する(塊の大きさから
    要素数を逆算する)。姿勢や余白を振ったときに「解像度が変わったから結果が変わった」という
    交絡を外すための口 —— 実測はこちらを使う。
    """
    started = time.perf_counter()
    ratio = float(margin_ratio)
    if not math.isfinite(ratio):
        raise ValueError(f"margin_ratio must be finite, got {margin_ratio!r}")
    if int(elements_long_axis) < 1:
        raise ValueError(f"elements_long_axis must be >= 1, got {elements_long_axis!r}")
    if spacing is not None and not float(spacing) > 0:
        raise ValueError(f"spacing must be positive, got {spacing!r}")

    anchor_shape = conditions.anchor
    # 三角形分割の細かさは要素サイズから決めたいが、要素サイズは塊が決まるまで分からない。
    # そこで **B-rep の bbox** から要素サイズの当たりを付ける。`vx._shape_bbox` をそのまま使うのは
    # 「useTriangulation=False で呼ばないと同じ形を2回ボクセル化したとき値が動く」という
    # 2026-08-31の教訓がそこに閉じ込めてあるため(写すと片方だけ直る事故になる)。
    lo_b, hi_b = vx._shape_bbox(vx._unwrap(conditions.keep))
    if anchor_shape is not conditions.keep:
        a_lo, a_hi = vx._shape_bbox(vx._unwrap(anchor_shape))
        lo_b, hi_b = np.minimum(lo_b, a_lo), np.maximum(hi_b, a_hi)
    longest_guess = float(np.max(hi_b - lo_b)) * max(1.0 + 2.0 * ratio, 0.1)
    spacing_guess = (
        float(spacing) if spacing is not None else longest_guess / int(elements_long_axis)
    )
    dev_probe = float(deviation) if deviation else max(spacing_guess * deviation_ratio, 1e-6)

    soup = vx.triangles_from_shape(
        vx._unwrap(anchor_shape), deviation=dev_probe, angular_deflection=angular_deflection
    )
    keep_soup = (
        soup
        if anchor_shape is conditions.keep
        else vx.triangles_from_shape(
            vx._unwrap(conditions.keep),
            deviation=dev_probe,
            angular_deflection=angular_deflection,
        )
    )

    points = condition_points(keep_soup, soup, (conditions.fixed_face, conditions.load_face))
    cloud_lo = points.min(axis=0)
    cloud_hi = points.max(axis=0)
    longest = float(np.max(cloud_hi - cloud_lo))
    if not longest > 0:
        raise ValueError("条件の点群が1点に潰れている(外接直方体が起こせない)")
    margin = ratio * longest
    box_lo = cloud_lo - margin
    box_hi = cloud_hi + margin
    if np.any(box_hi <= box_lo):
        raise DesignSpaceTooSmallError(
            f"余白 {margin:.4f}mm(比率 {ratio}) を足すと外接直方体が潰れる"
            f"(条件の外接直方体 {np.round(cloud_lo, 3).tolist()}"
            f" .. {np.round(cloud_hi, 3).tolist()})。margin_ratio を上げること。",
            {"keep": -1},
        )

    if spacing is None:
        n_long = int(elements_long_axis)
    else:
        # `grid_for_bbox` は spacing = 最長辺 / n で決めるので、要素サイズを固定したいときは
        # 最長辺から要素数を逆算する(丸めのぶん spacing は指定値と厳密には一致しない)。
        n_long = max(1, int(round(float(np.max(box_hi - box_lo)) / float(spacing))))
    grid = vx.grid_for_bbox(box_lo, box_hi, n_long)
    design = box_mask(grid, box_lo, box_hi)
    # 格子が決まったので、**保持/除外を格子に載せるときの三角形分割は要素サイズから決め直す**。
    # `anchor.run_pipeline` は `model_from_design_mask` を deviation 無しで呼ぶ(= 要素サイズの
    # `DEFAULT_DEVIATION_RATIO` 倍)ので、ここを揃えておかないと**ここで通した保持が
    # run_pipeline では通らない**という食い違いが起こりうる。
    dev = float(deviation) if deviation else max(grid.spacing * deviation_ratio, 1e-6)

    obstacle_elements_in_box = 0
    obstacle_stats: dict = {}
    if conditions.obstacle is not None:
        obs_soup = vx.triangles_from_shape(
            vx._unwrap(conditions.obstacle),
            deviation=dev,
            angular_deflection=angular_deflection,
        )
        obs, obstacle_stats = vx.occupancy(obs_soup, grid)
        obstacle_elements_in_box = int(np.count_nonzero(obs & design))
        design = design & ~obs
    if not design.any():
        raise DesignSpaceTooSmallError(
            "除外ソリッドを引いたら塊が空になった(除外が条件の外接直方体を覆い尽くしている)。",
            {"obstacle": -1},
        )

    # **格子の外は voxelize が見ていない。** 保持が格子から出ていると、はみ出しが件数にも
    # 例外にもならないまま黙って消える。ここで先に落とす(B-rep の bbox で見る —— 三角形の
    # 弦は真の面より内側に来るので、soup の bbox だと曲面のぶんだけ甘くなる)。
    grid_lo, grid_hi = grid.world_box()
    k_lo, k_hi = vx._shape_bbox(vx._unwrap(conditions.keep))
    if np.any(k_lo < grid_lo - 1e-9) or np.any(k_hi > grid_hi + 1e-9):
        raise DesignSpaceTooSmallError(
            f"保持ソリッド {np.round(k_lo, 3).tolist()} .. {np.round(k_hi, 3).tolist()} が"
            f"格子 {np.round(grid_lo, 3).tolist()} .. {np.round(grid_hi, 3).tolist()} の外へ出ている。"
            "格子の外にはセル中心が無いので、このままだと**はみ出しが件数にも例外にも現れない**。"
            f"margin_ratio(いま {ratio})を上げること。",
            {"keep": -1},
        )

    # 保持ソリッドが塊に完全に含まれることの確認は `model_from_design_mask` に任せる
    # (`on_outside="raise"` が既定)。**除外ソリッドは渡さない** —— 理由はモジュール docstring。
    try:
        model = vx.model_from_design_mask(
            grid,
            design,
            anchor_shape=soup,
            keep=conditions.keep,
            deviation=dev,
            angular_deflection=angular_deflection,
        )
    except vx.OutsideDesignSpaceError as exc:
        hint = (
            "除外ソリッドが保持ソリッドを食っている可能性がある(塊 = 外接直方体 − 除外 なので、"
            "除外が保持に重なると保持が塊からはみ出す)。"
            if conditions.obstacle is not None
            else ""
        )
        raise DesignSpaceTooSmallError(
            f"導出した塊(余白比率 {ratio})が保持ソリッドを含みきれていない: {exc} "
            f"{hint}案1では margin_ratio を上げるのが直し方。",
            getattr(exc, "outside", {}),
        ) from exc

    stats = {
        "seconds": 0.0,
        "margin_ratio": ratio,
        "margin": float(margin),
        "deviation": dev,
        "deviation_probe": dev_probe,
        "elements_long_axis": int(n_long),
        "spacing_requested": (None if spacing is None else float(spacing)),
        "condition_points": int(points.shape[0]),
        "condition_bbox": [cloud_lo.tolist(), cloud_hi.tolist()],
        "condition_longest_edge": longest,
        "design_bbox": [box_lo.tolist(), box_hi.tolist()],
        "grid": grid.as_dict(),
        "spacing": float(grid.spacing),
        "box_elements": int(np.count_nonzero(box_mask(grid, box_lo, box_hi))),
        "design_elements": int(np.count_nonzero(design)),
        "obstacle_elements_removed": obstacle_elements_in_box,
        "keep_elements": int(model.stats["keep_elements"]),
        "obstacle_occupancy": obstacle_stats,
    }
    stats["seconds"] = time.perf_counter() - started
    return DesignSpace(
        grid=grid,
        mask=design,
        model=model,
        soup=soup,
        conditions=conditions,
        bbox_min=cloud_lo,
        bbox_max=cloud_hi,
        margin=float(margin),
        margin_ratio=ratio,
        stats=stats,
    )


def run_topopt(
    space: DesignSpace,
    *,
    volfrac: float = 0.3,
    max_iter: int = 60,
    force: Sequence[float] = (0.0, 0.0, -1.0),
    stl_path: Optional[str | Path] = None,
    progress: Any = None,
) -> dict:
    """導出した塊を端から端まで回す(塊 → 面アンカー → SIMP → marching cubes → STL)。

    `stl_path` は**必ず渡すか、既定のこの関数の出力先に任せる**。`anchor.run_pipeline` の既定は
    `output/bracket.stl` で、それは `check_anchor.py` の成果物である。
    """
    out = Path(stl_path) if stl_path is not None else _HERE / "output" / "design_space_case1.stl"
    result = an.run_pipeline(
        shape=space.soup,
        design_mask=space.mask,
        design_grid=space.grid,
        keep=space.conditions.keep,
        # 除外は塊から引き切ってある(モジュール docstring の設計判断)。
        obstacle=None,
        fixed_face=space.fixed_face,
        load_face=space.load_face,
        volfrac=volfrac,
        max_iter=max_iter,
        force=force,
        stl_path=out,
        progress=progress,
    )
    # `run_pipeline` は保持ソリッドを**自分でもう一度**格子へ載せる。三角形分割の細かさが
    # `derive` と食い違うと、ここで通した塊が向こうでは通らない(逆もある)。揃えてあることを
    # 数字で確かめる —— 揃っていなければ黙って別のものを最適化していることになる。
    if not np.array_equal(np.asarray(result["model"].keep), np.asarray(space.model.keep)):
        raise ValueError(
            "derive() と run_pipeline() で保持領域の要素が食い違った"
            f"({int(space.model.keep.sum())} 要素 → {int(result['model'].keep.sum())} 要素)。"
            "derive() に deviation を明示的に渡していないか確認すること"
            "(run_pipeline は要素サイズから決め直す)。"
        )
    return result


# ---------------------------------------------------------------------------
# 合成の4条件(受入検査と実測が共有する題材。**私物STEPに依存しない**)
# ---------------------------------------------------------------------------
def demo_conditions(angle_deg: float = 0.0) -> Conditions:
    """4条件だけの合成題材。**設計空間ソリッドは作らない。**

    - 保持ソリッド: 固定側パッド(x=-30の板)と荷重側パッド(x=+26,z=15の小箱)の2つ。
      利用者が「ここは必ず材料を残す」と指定した部分に相当する。両者は離れており、
      **その間をどう繋ぐかが最適化の答え**になる。
    - 除外ソリッド: 2つのパッドの間を y 方向に貫く円柱(配線・ボルトの通り道に相当)。
    - 拘束面: 固定側パッドの x 最小面。荷重面: 荷重側パッドの上面。
      **どちらも保持ソリッドの面**(利用者が画面でクリックできるのは自分が置いた形の面だけ)。

    `angle_deg` は世界の z 軸まわりの回転。**案1は軸整列なので、回すと塊が膨らむ**
    —— その姿勢依存を測るための口。
    """
    from build123d import Box, Cylinder, Pos, Rot

    fixed_pad = Pos(-30, 0, 0) * Box(6, 24, 24)
    load_pad = Pos(26, 0, 15) * Box(8, 8, 6)
    keep = fixed_pad + load_pad
    obstacle = Pos(0, 0, 0) * Rot(90, 0, 0) * Cylinder(radius=5, height=40)

    rot = Rot(0, 0, float(angle_deg))
    keep = rot * keep
    obstacle = rot * obstacle

    soup = vx.triangles_from_shape(vx._unwrap(keep), deviation=0.2)
    fixed = _rotated_face(soup, angle_deg, normal=(-1, 0, 0), point=(-33, 0, 0))
    load = _rotated_face(soup, angle_deg, normal=(0, 0, 1), point=(26, 0, 18))
    return Conditions(keep=keep, fixed_face=fixed, load_face=load, obstacle=obstacle)


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
    hits = an.faces_matching(
        soup, kind="plane", normal=n, near_point=p, normal_tol=1e-6, near_tol=1e-4
    )
    if len(hits) != 1:
        raise ValueError(
            f"デモの面を一意に選べなかった(法線={np.round(n, 4).tolist()}"
            f" 重心={np.round(p, 4).tolist()} → {hits})。demo_conditions を変えたら見直すこと"
        )
    return hits[0]


def wall_contact(space: DesignSpace, out: dict, threshold: float = 0.5) -> int:
    """**最適化が置いた材料のうち、塊の壁に接している要素の数。**

    余白の既定値を決める根拠になる数字。ここが0でないなら、塊の壁が答えを切っている可能性が
    ある(利用者は塊を見ないので、切られても誰も気づけない)。**アンカーで ρ=1 に固定した分は
    除く** —— 固定した要素は最適化の答えではないうえ、拘束面・荷重面は定義上いつも塊の縁にある。
    格子の外は塊の外として数える。
    """
    rho = np.asarray(out["result"].rho)
    filled = (rho > threshold) & space.mask & ~np.asarray(out["solid"], dtype=bool)
    outside = np.pad(~space.mask, 1, constant_values=True)
    neighbour_outside = (
        outside[:-2, 1:-1, 1:-1]
        | outside[2:, 1:-1, 1:-1]
        | outside[1:-1, :-2, 1:-1]
        | outside[1:-1, 2:, 1:-1]
        | outside[1:-1, 1:-1, :-2]
        | outside[1:-1, 1:-1, 2:]
    )
    return int(np.count_nonzero(filled & neighbour_outside))


# ---------------------------------------------------------------------------
# 実測(README の表を作る)
# ---------------------------------------------------------------------------
def measure_rotation(
    angles: Sequence[float],
    *,
    margin_ratio: float = DEFAULT_MARGIN_RATIO,
    spacing: float,
    max_iter: int,
    volfrac: float,
) -> list[dict]:
    """姿勢依存。同じ部品を世界軸に対して回したときの塊の要素数・所要時間・材料の総体積。

    **要素サイズは固定する。** `elements_long_axis` を固定すると回転で最長辺が変わって解像度まで
    動き、「回したから増えた」のか「細かくなったから増えた」のか分けられなくなる。
    """
    rows = []
    for angle in angles:
        space = derive(
            demo_conditions(angle), margin_ratio=margin_ratio, spacing=spacing
        )
        t0 = time.perf_counter()
        out = run_topopt(
            space,
            volfrac=volfrac,
            max_iter=max_iter,
            stl_path=_HERE / "output" / f"design_space_rot{int(round(angle))}.stl",
        )
        rows.append(_row(space, out, time.perf_counter() - t0, angle=float(angle)))
        print("  " + json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return rows


def measure_margin(
    ratios: Sequence[float],
    *,
    angle: float = 0.0,
    spacing: float,
    max_iter: int,
    volfrac: float,
    material_mm3: Optional[float] = None,
) -> list[dict]:
    """余白比率 × (要素数, 所要, 材料の総体積, 壁に接した材料)。落ちた比率は理由ごと表に残す。

    `material_mm3` を渡すと、**体積制約を絶対量に倒したとき**の比較になる(比率ごとに volfrac を
    逆算して、出てくる材料の総体積を揃える)。相対体積のままだと塊が変わると材料の量まで変わり、
    「塊の大きさで答えがどう変わるか」と「材料の量で答えがどう変わるか」が混ざる。
    """
    rows = []
    conditions = demo_conditions(angle)
    for ratio in ratios:
        try:
            space = derive(conditions, margin_ratio=ratio, spacing=spacing)
        except (vx.OutsideDesignSpaceError, ValueError) as exc:
            rows.append(
                {
                    "margin_ratio": float(ratio),
                    "error": type(exc).__name__,
                    "message": str(exc)[:160],
                }
            )
            print("  " + json.dumps(rows[-1], ensure_ascii=False), flush=True)
            continue
        table = an.build_anchors(space.soup, space.grid)
        empty = [f for f in (space.fixed_face, space.load_face) if f in table.empty_faces()]
        if empty:
            rows.append(
                {
                    "margin_ratio": float(ratio),
                    "design_elements": space.element_count,
                    "spacing": float(space.grid.spacing),
                    "error": "empty_anchor_faces",
                    "message": f"アンカーが空になった面 {empty}",
                }
            )
            print("  " + json.dumps(rows[-1], ensure_ascii=False), flush=True)
            continue
        stl = _HERE / "output" / f"design_space_margin{ratio:g}.stl"
        fraction = volfrac
        if material_mm3 is not None:
            # 固定される要素(ρ=1)と動かせる要素の数は最適化を1反復も回さずに引ける。
            # **自分で数え直さない** —— run_pipeline の作り方(アンカー近傍の固定)を写すと、
            # 向こうが変わったときに黙ってずれる。
            probe = run_topopt(space, volfrac=volfrac, max_iter=0, stl_path=stl)
            free_n = int(probe["free_elements"])
            solid_n = int(probe["solid_elements"])
            fraction = (material_mm3 / space.grid.element_volume - solid_n) / max(free_n, 1)
            if not 0.0 < fraction < 1.0:
                rows.append(
                    {
                        "margin_ratio": float(ratio),
                        "design_elements": space.element_count,
                        "error": "volfrac_out_of_range",
                        "message": f"絶対体積 {material_mm3}mm^3 を満たす volfrac が {fraction:.4f}",
                    }
                )
                print("  " + json.dumps(rows[-1], ensure_ascii=False), flush=True)
                continue
        t0 = time.perf_counter()
        out = run_topopt(
            space,
            volfrac=fraction,
            max_iter=max_iter,
            stl_path=stl,
        )
        rows.append(_row(space, out, time.perf_counter() - t0, angle=float(angle)))
        print("  " + json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return rows


def _row(space: DesignSpace, out: dict, seconds: float, *, angle: float) -> dict:
    """表の1行。**体積は mm^3 の絶対量でも出す** —— 要素数が変わると相対体積は比べられない。"""
    ev = space.grid.element_volume
    return {
        "angle_deg": angle,
        "margin_ratio": space.margin_ratio,
        "margin_mm": round(space.margin, 4),
        "spacing_mm": round(float(space.grid.spacing), 5),
        "grid": list(space.grid.shape),
        "design_elements": space.element_count,
        "free_elements": int(out["free_elements"]),
        "solid_elements": int(out["solid_elements"]),
        "seconds_optimize": round(float(out["timings"]["optimize"]), 2),
        "seconds_total": round(seconds, 2),
        "iterations": int(out["iterations"]),
        "target_volume_elements": round(float(out["target_volume"]), 2),
        "material_mm3": round(float(out["achieved_volume"]) * ev, 1),
        "design_volume_mm3": round(space.element_count * ev, 1),
        "wall_contact": wall_contact(space, out),
        "volfrac": round(float(out["target_volfrac"]), 5),
        "compliance_last": round(float(out["compliance_last"]), 4),
    }


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="4条件から塊(設計空間)を導出する(案1)")
    parser.add_argument("--measure", action="store_true", help="README の表を作る実測を回す")
    parser.add_argument("--elements-long-axis", type=int, default=28)
    parser.add_argument(
        "--spacing",
        type=float,
        default=3.635,
        help="実測で固定する要素サイズ[mm](既定は 0°/余白0.25 を長辺26要素で切ったときの値)",
    )
    parser.add_argument("--max-iter", type=int, default=60)
    parser.add_argument("--volfrac", type=float, default=0.3)
    parser.add_argument("--margin-ratio", type=float, default=DEFAULT_MARGIN_RATIO)
    parser.add_argument("--angles", type=float, nargs="*", default=[0.0, 30.0, 45.0])
    parser.add_argument(
        "--ratios",
        type=float,
        nargs="*",
        default=[-0.02, 0.0, 0.05, 0.10, 0.25, 0.40, 0.50],
    )
    parser.add_argument(
        "--material-mm3",
        type=float,
        default=0.0,
        help="余白の表を絶対体積制約でも取る(材料の総体積[mm^3]を固定する)。0で取らない",
    )
    parser.add_argument("--out", type=str, default=None, help="実測をJSONで書き出す先")
    args = parser.parse_args()

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    if not args.measure:
        space = derive(
            demo_conditions(0.0),
            margin_ratio=args.margin_ratio,
            elements_long_axis=args.elements_long_axis,
        )
        print(json.dumps(space.stats, ensure_ascii=False, default=float, indent=2))
        return 0

    payload = {
        "spacing_mm": args.spacing,
        "max_iter": args.max_iter,
        "volfrac": args.volfrac,
        "margin_ratio": args.margin_ratio,
    }
    print("[姿勢依存]", flush=True)
    payload["rotation"] = measure_rotation(
        args.angles,
        margin_ratio=args.margin_ratio,
        spacing=args.spacing,
        max_iter=args.max_iter,
        volfrac=args.volfrac,
    )
    print("[余白比率 / 相対体積制約(volfrac 固定)]", flush=True)
    payload["margin"] = measure_margin(
        args.ratios,
        spacing=args.spacing,
        max_iter=args.max_iter,
        volfrac=args.volfrac,
    )
    if args.material_mm3:
        print("[余白比率 / 絶対体積制約(材料の総体積を固定)]", flush=True)
        payload["material_mm3"] = args.material_mm3
        payload["margin_absolute"] = measure_margin(
            args.ratios,
            spacing=args.spacing,
            max_iter=args.max_iter,
            volfrac=args.volfrac,
            material_mm3=args.material_mm3,
        )
    text = json.dumps(payload, ensure_ascii=False, default=float, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
