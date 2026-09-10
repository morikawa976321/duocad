"""4条件(保持ソリッド・除外ソリッド・拘束面・荷重面)から塊(設計空間)を導出する。

    .\\.venv\\Scripts\\python.exe -m duocad.topopt.design_space

導出は2つ入っている。**既定は連続値保持**(`derive_from_reference`)で、案1(`derive`)は
その土台(参照の箱を起こす)であり、比較のベースライン兼退避経路でもある。
**導出にこだわりのない呼び出し側は `derive_default` を通ること** —— 既定を次に倒すときに
書き換える場所を1箇所に閉じ込めるための入口である。各方式の弱点・比較の数字は
docs/TOPOPT.md「4条件から塊(設計空間)を導出する」以降の各節。

## 除外ソリッドの扱い(設計判断)

除外ソリッドは**塊を作る時点で引き切り**、`voxelize.model_from_design_mask` の `obstacle` には
**渡さない** —— 塊 = 直方体 − 除外 と定義した以上、同じ除外ソリッドを `obstacle` にも渡すと
**必ず全要素はみ出して `OutsideDesignSpaceError` になる**。**`on_outside="clip"` へ倒す選択も
しない** —— clip は「塊からはみ出した保持/除外を黙って切る」指定で、「はみ出しは例外」という
保証をこの経路だけ骨抜きにする。**除外が保持を食っているケース**(塊から引いた結果、保持が塊に
入らなくなる)は保持のはみ出しとして現れ、`DesignSpaceTooSmallError` になる。理由の全文は
docs/TOPOPT.md「除外ソリッドを `obstacle` に渡さない(設計判断)」。

## 拘束と荷重がひと続きかを見る(連結性ガード)

塊から除外ソリッドを引いた結果、**拘束面と荷重面が別々の連結成分に落ちる**ことがある。そのまま
最適化に通しても**多くの場合エラーは出ない**(空要素も剛性の下限 `emin` を持つので残差検算も
通ってしまう)。利用者は塊を見ないので気づく手段が無い。だから `derive` が**つながりを確認して
から返す**(`assert_faces_connected` → `DesignSpaceDisconnectedError`)。連結性の定義は
**6近傍**(`NEIGHBOURHOOD`。理由はその定数のところ)。実測は docs/TOPOPT.md
「塊の中で拘束と荷重がひと続きかを検査する」節。

## 連続値保持(`derive_from_reference`。いまの既定)

案1は**条件の位置だけ**から塊を決める(どこに材料が要るかを見ていない)。連続値保持は
**探索を通して1回だけ本番の格子で SIMP を回し、ρ を二値化せず順位として使い回す**。材料量ごとの
塊は「順位の上から `k × 材料量` ぶん、ただし回廊(`Reference.corridor`)を下回らない」。
**回廊は参照ごとに1回だけ求める固定値**で、これが連結性の保証であり、塊が材料量について
単調であることの根拠でもある(材料量ごとに繋がるまで広げる作りにすると単調性が壊れ、
外側の二分探索の前提が崩れる)。hanger の実測で、被覆 100%・壁に接した材料 5.37%(案1と同値)。

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
from scipy import ndimage

from . import anchor as an
from . import simp
from . import voxelize as vx

_HERE = Path(__file__).resolve().parent

# 余白は**条件の外接直方体の最長辺に対する比率**で持つ(絶対mmでは部品の大きさに追従しない)。
# 既定値の根拠は docs/TOPOPT.md「余白比率をどう決めたか」。安全側=大きめに倒してある。
DEFAULT_MARGIN_RATIO = 0.25
DEFAULT_ELEMENTS_LONG_AXIS = 40


class DesignSpaceTooSmallError(vx.OutsideDesignSpaceError):
    """導出した塊が条件(保持ソリッド)を含みきれていない。

    `voxelize.OutsideDesignSpaceError` の派生なので、既存の `except ValueError` /
    `except vx.OutsideDesignSpaceError` はそのまま効く。**案1では余白を上げるのが正しい直し方**
    なので、文面でそれを名指しする(汎用の文面は「座標系・単位が違う」を疑わせて誤誘導する)。
    """


class DesignSpaceDisconnectedError(ValueError):
    """塊の中で拘束面と荷重面がひと続きになっていない(壁と荷重をつなぐ道が無い)。

    **`DesignSpaceTooSmallError` とは別の型にしてある。** あちらは「余白を上げろ」と言う文面で、
    分断には効かない直し方を指す —— 分断の直し方は除外ソリッドを削るか条件を置き直すことで、
    余白を上げても塞がれた道は開かない。`ValueError` の派生なので既存の `except ValueError` は
    そのまま効く。

    `components`(塊の連結成分の数)/ `fixed_labels` / `load_labels` を属性で持つので、
    呼び出し側は文面を読まずに機械で分岐できる。
    """

    def __init__(
        self,
        message: str,
        *,
        components: int = 0,
        fixed_labels: Sequence[int] = (),
        load_labels: Sequence[int] = (),
    ) -> None:
        super().__init__(message)
        self.components = int(components)
        self.fixed_labels = [int(v) for v in fixed_labels]
        self.load_labels = [int(v) for v in load_labels]


# ---------------------------------------------------------------------------
# 連結性(拘束↔荷重の道)
# ---------------------------------------------------------------------------
# **連結性は 6近傍(面で接する)で定義する。** 26近傍にすると、角1点・稜1本だけで触れている
# 2つの島を「つながっている」と数える。構造格子の六面体要素では、角で触れた2要素が共有するのは
# 節点1つだけで、それは回転を止められないピン接合(機構)にしかならない —— 力の道としては
# 成立しないので、26近傍で見るとこの検査は「切れていない」と言い張りながら宙に浮いた形を通す。
# 検査[10]がこの定義と実装が一致していることを、角1点で接する2島で見張る。
NEIGHBOURHOOD = 6
_RANK_OF_NEIGHBOURHOOD = {6: 1, 18: 2, 26: 3}


def element_components(
    design: np.ndarray, *, neighbourhood: int = NEIGHBOURHOOD
) -> tuple[np.ndarray, int]:
    """塊の連結成分。`(labels, n)` を返す(`labels == 0` は塊の外)。

    `neighbourhood` は連結性の定義で、**塊のガードは常に 6近傍**(`NEIGHBOURHOOD`)。
    18/26 を受けるのは「定義を変えると答えが変わる」ことを検査で見せるためだけの口で、
    導出の経路からは 6 以外を渡さない。
    """
    mask = np.asarray(design, dtype=bool)
    if mask.ndim != 3:
        raise ValueError(f"design は3次元の配列であること(いま shape={mask.shape})")
    try:
        rank = _RANK_OF_NEIGHBOURHOOD[int(neighbourhood)]
    except KeyError:
        raise ValueError(
            f"neighbourhood は {sorted(_RANK_OF_NEIGHBOURHOOD)} のいずれか(いま {neighbourhood!r})"
        ) from None
    labels, count = ndimage.label(mask, structure=ndimage.generate_binary_structure(3, rank))
    return labels, int(count)


def assert_load_path(
    design: np.ndarray,
    fixed_elements: np.ndarray,
    load_elements: np.ndarray,
) -> dict:
    """**塊の中で拘束側と荷重側が同じ連結成分にいることを確かめる。** 違えば例外。

    **切れていても最適化は落ちてくれない。** 空要素も剛性の下限 `emin` を持つので行列は特異に
    ならず、`simp` の残差検算も通る。切れた系は「拘束側と荷重側が別々に浮いた形」で黙って
    STL まで出るか、原因の分からない場所(OC更新)で落ちるかのどちらかになる(利用者は塊を
    見ないのでどちらも気づく手段が無い)。**だから塊を組み立てる側で落とす。** 実測は
    docs/TOPOPT.md「切れている塊をそのまま最適化に通すと何が起きるか」。

    `fixed_elements` / `load_elements` は要素マスク(`AnchorTable.element_mask`)。
    **塊の外にはみ出した分は落として数える** —— 除外ソリッドが拘束面のすぐ外側を削っていると、
    アンカー要素の一部は塊の外に立つ。道になるのは塊の中の要素だけ。
    """
    mask = np.asarray(design, dtype=bool)
    fixed = np.asarray(fixed_elements, dtype=bool)
    load = np.asarray(load_elements, dtype=bool)
    for name, arr in (("fixed_elements", fixed), ("load_elements", load)):
        if arr.shape != mask.shape:
            raise ValueError(f"{name} のかたち {arr.shape} が塊 {mask.shape} と違う")

    labels, count = element_components(mask)
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    fixed_labels = sorted(int(v) for v in np.unique(labels[fixed & mask]) if v != 0)
    load_labels = sorted(int(v) for v in np.unique(labels[load & mask]) if v != 0)
    shared = sorted(set(fixed_labels) & set(load_labels))
    report = {
        "neighbourhood": NEIGHBOURHOOD,
        "components": count,
        "component_sizes": [int(sizes[i]) for i in range(1, count + 1)],
        "design_elements": int(np.count_nonzero(mask)),
        "fixed_elements": int(np.count_nonzero(fixed & mask)),
        "load_elements": int(np.count_nonzero(load & mask)),
        "fixed_labels": fixed_labels,
        "load_labels": load_labels,
        "shared_labels": shared,
    }
    where = (
        f"塊の連結成分は {count} 個(要素数 {report['component_sizes']}、{NEIGHBOURHOOD}近傍で判定)。"
        f"拘束側は成分 {fixed_labels or '無し'} / 荷重側は成分 {load_labels or '無し'} にいる。"
    )
    if not fixed_labels or not load_labels:
        missing = "拘束側" if not fixed_labels else "荷重側"
        raise DesignSpaceDisconnectedError(
            f"{missing}のアンカー要素が塊の中に1つも無い。{where}"
            "除外ソリッドが条件面を削っていないか確認すること"
            "(削られた面には材料が付けられないので、荷重も拘束も伝わらない)。",
            components=count,
            fixed_labels=fixed_labels,
            load_labels=load_labels,
        )
    if not shared:
        raise DesignSpaceDisconnectedError(
            f"拘束面と荷重面が塊の中でつながっていない。{where}"
            "共通の成分が1つも無いので、壁と荷重をつなぐ道が塊に存在しない。"
            "除外ソリッドが両者の間を塞いでいないか、条件面の位置を確認すること"
            "(このまま最適化に通すと、多くの場合エラーも警告も出ないまま"
            "拘束側と荷重側が別々に浮いた形が出る)。",
            components=count,
            fixed_labels=fixed_labels,
            load_labels=load_labels,
        )
    return report


def assert_faces_connected(
    design: np.ndarray,
    table: an.AnchorTable,
    fixed_faces: Sequence[int] | int,
    load_faces: Sequence[int] | int,
    *,
    layers: int = 1,
) -> dict:
    """拘束面・荷重面のアンカー要素で `assert_load_path` を呼ぶ。

    `layers=1` は `anchor.run_pipeline` が ρ=1 に固定する範囲(`keep_layers` の既定)と同じ。
    **節点が1つも付かなかった条件面があるときは連結性を見ない** —— それは
    「要素サイズより小さい面にアンカーが付かない」という別の穴(engine
    `face-to-node-anchoring-loses-faces-smaller-than-the-cell`)で、ここで落とすと
    `empty_faces()` を見る既存の口より手前で別の例外に化けて原因が分かりにくくなる。

    **拘束面・荷重面をそれぞれ和集合として見る**(拘束のどれか1枚と荷重のどれか1枚が
    塊の中でつながっていれば通る)。**全部つながっていることは要求しない** ——
    要求すると、意図して片持ちの一部だけを別成分に置いた配置が落ちる。ただし
    「つながっていない拘束面は効かない」ので、これは検出漏れの側に倒した判断である。
    """
    faces = [int(f) for f in _as_faces(fixed_faces)] + [
        int(f) for f in _as_faces(load_faces)
    ]
    empty = [f for f in faces if f in table.empty_faces()]
    if empty:
        return {"skipped": "empty_anchor_faces", "empty_faces": empty}
    fixed = table.element_mask([int(f) for f in _as_faces(fixed_faces)], layers=layers)
    load = table.element_mask([int(f) for f in _as_faces(load_faces)], layers=layers)
    return assert_load_path(design, fixed, load)


def _as_faces(value: Sequence[int] | int) -> tuple:
    """`int` 1つでも並びでも受けて、面添字のタプルにする。

    **既存の呼び出し(デモ・検査)が `int` を渡し続けられるようにするため**の入口で、
    ここで均しておかないと「面が1枚の場合」の分岐が呼び出し側に散らばる。
    """
    if isinstance(value, (int, np.integer)):
        return (int(value),)
    return tuple(int(item) for item in value)


@dataclass(frozen=True)
class Conditions:
    """利用者が入れる4条件。**設計空間ソリッドは無い。**

    `fixed_faces` / `load_faces` は `anchor_shape`(省略時は `keep`)の三角形群での面添字で、
    **どちらも何枚でもよい**(`int` 1つも受ける)。面添字は渡した形の面の並びで決まるので、
    面を選ぶのに使った形をそのまま渡すこと。

    `load_n` は荷重の**実際の大きさ**[N]。荷重が複数のときは**その大きさの総和**であって
    合力(ベクトル和)ではない —— 向きの違う荷重が打ち消し合うと合力は0になりうるが、
    総和なら0にならない(`optimize_run.build_conditions` / `stress.load_magnitude_total`)。
    最適化そのもの(`simp.optimize` / `run_topopt` の `force` / `forces`)は無次元
    (荷重の総量が1.0)で回り続ける —— `load_n` はここでは使われず、
    `stress.UnitScale.for_model(..., load_n=conditions.load_n)` が応力を実単位[MPa]へ
    換算するときにだけ参照する(`safety_loop.py`)。
    """

    keep: Any
    fixed_faces: Any
    load_faces: Any
    obstacle: Any = None
    anchor_shape: Any = None
    load_n: float = 1.0

    def __post_init__(self) -> None:
        # `int` 1つも面の並びも受けてタプルへ均す(`frozen=True` なので `object.__setattr__`)。
        object.__setattr__(self, "fixed_faces", _as_faces(self.fixed_faces))
        object.__setattr__(self, "load_faces", _as_faces(self.load_faces))
        if not self.fixed_faces:
            raise ValueError("拘束面が1枚も無い")
        if not self.load_faces:
            raise ValueError("荷重面が1枚も無い")

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
    # 導出の中で作った面アンカーの表(連結性の確認に使ったもの)。**再利用のために持たせてある**
    # —— 連続値保持は同じ格子でもう一度アンカー要素が要るので、無いと表を2回作る
    # ことになる(本番解像度では導出時間の大半がこれ)。`None` を許すのは、塊を直接組んだ
    # `DesignSpace` を検査が作れるようにするため。
    table: Any = None

    @property
    def element_count(self) -> int:
        return int(np.count_nonzero(self.mask))

    @property
    def fixed_faces(self) -> tuple:
        return self.conditions.fixed_faces

    @property
    def load_faces(self) -> tuple:
        return self.conditions.load_faces


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
    # そこで **B-rep の bbox** から要素サイズの当たりを付ける。**`vx._shape_bbox` をそのまま使い、
    # ロジックを写さないこと** —— 「useTriangulation=False で呼ばないと同じ形を2回
    # ボクセル化したとき値が動く」という教訓がそこに閉じ込めてある(写すと片方だけ直る事故になる)。
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

    points = condition_points(
        keep_soup, soup, tuple(conditions.fixed_faces) + tuple(conditions.load_faces)
    )
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

    # **つながりを確認してから返す。** 拘束面と荷重面が塊の中でひと続きでなければ、壁と荷重を
    # つなぐ道が塊に存在しない。切れていても SIMP は落ちない(実測は docs/TOPOPT.md「切れている塊を
    # そのまま最適化に通すと何が起きるか」)ので、ここで落とさないと誰も気づけない。
    #
    # **順序(実測で確かめた。契約(前タスク)の「格子外ガードより前」からは意図的に外している)**:
    #   - **格子外ガードより前へ動かすと `check_design_space.py` の検査[9]が落ちる。** 余白比率
    #     -0.05 / -0.10 では保持が格子の外へ出るが、その前に条件面のアンカー要素が塊から消えて
    #     連結性が先に発火し、梯子の verdict が「格子外」から「拘束側のアンカー要素が塊の中に
    #     1つも無い」へ化ける(実測: 検査[9]の最後の1件だけが FAIL)。**格子外ガードより後は必須。**
    #   - `model_from_design_mask` の前でも後でも、いまの検査はどちらも全緑(実測)。後に置いて
    #     あるのは判断であって検査に固定されてはいない —— 除外が保持を食っている配置では
    #     「保持がはみ出している」ほうが原因に近い診断だから。なお検査[6]が使う除外
    #     (Box 10x30x30 @ (-30,0,0))は塊の断面を塞がないので、前に置いても連結性は通る
    #     (実測: 1成分・拘束側の要素63個が除外の外に生き残る)。検査[6]は動かない。
    #
    # アンカーの表は `run_pipeline` と同じ作り方(全面・既定の半径)で作る。`faces=` で2面に
    # 絞ると稜線の規則(節点は最も面積を配った面が持つ)の競争相手が減り、**向こうとは別の
    # 節点集合**になる —— 同じものを見ているつもりで違うものを見ることになる。
    table = an.build_anchors(soup, grid)
    connectivity = assert_faces_connected(
        design, table, conditions.fixed_faces, conditions.load_faces
    )

    stats = {
        "seconds": 0.0,
        "connectivity": connectivity,
        "anchor_seconds": float(table.stats["seconds"]),
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
        table=table,
    )


# ---------------------------------------------------------------------------
# 連続値保持(参照 ρ を1回作り、材料量ごとに順位で切り出す)
# ---------------------------------------------------------------------------
# 既定値の根拠はすべて実測(docs/TOPOPT.md「連続値保持の既定値をどう決めたか」)。案1の
# `margin_ratio` と同じ扱いで、**「なんとなく」の既定は置いていない。**

# 参照 run の材料量 = 塊の容量 × これ。**探索する材料量より下に置くこと** —— 参照の材料量が
# 対象より多いと ρ が飽和して(hanger の上端で ρ の中央値 0.74)順位が同点だらけになり、
# 被覆が 3.95% まで落ちる。対象以下なら 100%。安全帯の実測は volfrac 0.066〜0.105 で、
# その下寄りを採った。
REFERENCE_MATERIAL_RATIO = 0.08

# 参照 run の SIMP 反復数。**40反復と被覆が変わらない**(hanger の実測で材料量5点の被覆が
# 100/100/100/99.98/99.97% 対 100/100/100/100/99.87%)ので、3.5倍速い側を採った。
REFERENCE_MAX_ITER = 10

# **`REFERENCE_MATERIAL_RATIO` は部品によっては痩せすぎる。** 8% は hanger の答えが容量の
# 約8%にあったから当たった値で、答えが容量の半分にある部品では参照の構造が出来上がらない
# (実測: bracket 9,386要素で 8% は solid 578要素が2成分にちぎれ、回廊が案1の 71.2% になった。
# 12% なら 1成分で回廊 7.9%)。**痩せすぎは回廊の大きさで検出できる**ので、超えたら材料を
# 増やして参照を取り直す。**下げる側には動かさない** —— 参照は薄いほうが安全側で、
# 濃い側は崖(材料量が対象より多いと ρ が飽和して被覆が 3.95% まで落ちる)。
REFERENCE_MAX_CORRIDOR = 0.40
REFERENCE_RETRY_GROWTH = 1.6
REFERENCE_MAX_ATTEMPTS = 4

# 設計空間の容量 ÷ 材料量。**被覆だけで決めると 1.5 になるが、それでは密度フィルタが
# 体積目標に届かない。** hanger の実測では 1.5 で全材料量域の被覆が 99.91〜100% だが、
# 設計空間が絶対要素数で小さいと `simp.VolumeTargetError` を踏む
# (bracket 3,336要素の実測: 塊 751要素・充填率 67% で届かず、1,001要素・67% なら届く)。
# **2.0(充填率 50%)は同じ題材の全材料量域で届き、壁に接した材料の割合も案1と完全に一致する。**
# 1.75(57%)も届いたが、落ちた 67% との隔たりが1段しかないので採らなかった。
#
# **これを下回るのは「回廊」に足りないときだけである。** 回廊(`Reference.corridor`)は拘束と
# 荷重を繋ぐのに要る順位の先頭からの最小本数で、参照ごとに1回だけ求めて全材料量で共有する。
# 材料量ごとに「繋がるまで k を上げる」設計は**採らなかった** —— 繋がった時点で止めると
# 塊が材料量に対して単調でなくなり(bracket 20要素の実測で 45%→2,927要素 / 50%→2,502要素)、
# 二分探索が前提にしている単調性が崩れて安全率が 5522 → 3893 と逆行した。
K = 2.0

# 参照 run も STL を書く(`run_topopt` の契約)。**受入検査が成果物として当てにしている既定の
# 出力先(`output/bracket.stl` / `output/design_space_case1.stl`)へ向けない。**
_REFERENCE_STL_PATH = _HERE / "output" / "design_space_reference.stl"


class ReferenceRunConditionFacesEmptyError(ValueError):
    """連続値保持の**参照 run** で拘束面・荷重面のアンカーが空になった。

    `run_topopt` は `anchor.run_pipeline` の契約どおり例外にせず `empty_condition_faces` を
    積んだ辞書を返す(`"result"` は `None` のまま)。ここで見ずに `.rho` へ触ると素の
    `AttributeError` になる。

    **文面が名指しする直し方は「要素サイズを小さくする」だけである。** 参照 run は本番と
    同じ格子で回るので、これは本番解像度そのものの問題であって、参照側だけ細かくして
    逃げる道は無い。
    """

    def __init__(self, empty_condition_faces: Sequence[dict]) -> None:
        diagnoses = list(empty_condition_faces)
        detail = an.format_condition_face_warning(diagnoses)
        super().__init__(
            "連続値保持の参照 run で拘束面・荷重面のアンカーが空になった: "
            f"{detail} 要素サイズを小さくするか、条件面を選び直すこと"
            "(参照は本番と同じ格子で回るので、これは本番解像度そのものの問題である)。"
        )
        self.empty_condition_faces = diagnoses


@dataclass(frozen=True)
class Reference:
    """本番の格子で1回だけ回した ρ と、そこから作った要素の並び。

    `base` は参照 run を回した案1の塊。`order` は**塊に入れたい順**に並べた要素の平坦添字で、
    `base.mask` の中の要素だけが入っている(だから `order[:n]` は必ず塊の中に収まる)。

    `corridor` は**拘束と荷重を繋ぐのに要る `order` の先頭からの最小本数**。材料量がいくら
    小さくてもここまでは必ず取る —— これが連結性の保証であり、同時に「塊は材料量について
    単調」を成り立たせる仕掛けでもある(材料量ごとに繋がるまで広げる作りにすると、繋がった
    時点で止まるぶん塊が上下して単調性が壊れる)。

    `derive_kwargs` は `base` を起こしたときの導出引数(`angular_deflection` を含む)、
    `conditions` はその元になった4条件。**格子が違えば ρ も順位も別の場所を指す**ので、
    切り出す側が両方を突き合わせる(`derive_from_reference`)。**条件は同一性(`is`)で見る**
    —— 形状は OCCT のオブジェクトなので値比較が高くつくうえ、作り直した条件は形が変わって
    いるかもしれない(参照も作り直すのが正しい)。
    """

    base: DesignSpace
    rho: np.ndarray
    order: np.ndarray
    material_mm3: float
    corridor: int = 0
    conditions: Any = None
    derive_kwargs: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def reference_order(
    rho: np.ndarray, mask: np.ndarray, grid: vx.Grid, *, threshold: float = 0.5
) -> np.ndarray:
    """塊に入れたい順に並べた要素の平坦添字。**距離が主キー、ρ が副キー。**

    収束した ρ はほぼ二値なので、ρ だけで並べると `ρ >= threshold` の集合を取り切った先が
    同点だらけになり、そこから先の順序が任意になる。`ρ >= threshold` からのユークリッド距離を
    主キーにすると「構造をまず取り、そこから同心に膨らませる」順になる —— hanger の実測で、
    参照より多い材料量での被覆が 94.14% から 99.88% に上がった(k=1.5)。

    **膨らませる量は材料量と k から決まる**(定数の絶対距離では部品の大きさに追随しない)。
    """
    values = np.asarray(rho, dtype=float)
    inside = np.asarray(mask, dtype=bool)
    if values.shape != inside.shape:
        raise ValueError(f"rho のかたち {values.shape} が塊 {inside.shape} と違う")
    solid = (values >= float(threshold)) & inside
    if solid.any():
        distance = ndimage.distance_transform_edt(~solid, sampling=float(grid.spacing))
    else:
        # 参照 run が二値化できるほど固まらなかったとき。距離が定義できないので ρ だけで
        # 並べる(同点は多いが、全域が等距離になって順序が壊れるよりはよい)。
        distance = np.zeros_like(values)
    index = np.flatnonzero(inside.ravel())
    return index[np.lexsort((-values.ravel()[index], distance.ravel()[index]))]


def _condition_elements(base: DesignSpace, table: an.AnchorTable) -> np.ndarray:
    """塊へ無条件で入れる要素(保持ソリッド ∪ 拘束・荷重のアンカー)。

    **順位から落ちても条件の要素は塊から落ちてはならない**(落ちると拘束も荷重も伝わらない)。
    節点が1つも付かなかった条件面があるときはアンカーを足さない —— それは別の穴で、ここで
    足すと `AnchorTable.empty_faces()` を見る既存の口より手前で挙動が変わる。
    """
    mask = np.asarray(base.model.keep, dtype=bool).copy()
    faces = tuple(base.fixed_faces) + tuple(base.load_faces)
    if not [f for f in faces if f in table.empty_faces()]:
        mask |= table.element_mask(list(faces), layers=1)
    return mask


def corridor_length(
    order: np.ndarray,
    base: DesignSpace,
    table: an.AnchorTable,
    conditions_mask: np.ndarray,
) -> int:
    """拘束と荷重を繋ぐのに要る、`order` の先頭からの**最小本数**を二分探索で求める。

    **繋がるかどうかは本数について単調である**(要素を足して繋がりが切れることはない)ので
    二分探索が使える。全部使い切っても繋がらなければ、それは案1の塊そのものが切れている配置
    なので、`assert_faces_connected` にそのまま診断付きで落としてもらう。

    計算量は連結成分ラベリング `log2(要素数)` 回。本番解像度(100万要素級)でも数秒で、
    **参照ごとに1回だけ**払えばよい(材料量ごとには払わない)。
    """
    inside = np.asarray(base.mask, dtype=bool)
    total = int(order.size)

    def mask_for(count: int) -> np.ndarray:
        picked = np.zeros(inside.size, dtype=bool)
        picked[order[:count]] = True
        return (picked.reshape(inside.shape) | conditions_mask) & inside

    def connects(count: int) -> bool:
        try:
            assert_faces_connected(mask_for(count), table, base.fixed_faces, base.load_faces)
            return True
        except DesignSpaceDisconnectedError:
            return False

    if not connects(total):
        # 診断(成分の数・拘束/荷重がどの成分にいるか)を持った例外をそのまま上げる。
        assert_faces_connected(mask_for(total), table, base.fixed_faces, base.load_faces)
    if connects(0):
        return 0
    lo, hi = 0, total
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if connects(mid):
            hi = mid
        else:
            lo = mid
    return hi


def build_reference(
    conditions: Conditions,
    *,
    material_mm3: Optional[float] = None,
    material_ratio: float = REFERENCE_MATERIAL_RATIO,
    max_iter: int = REFERENCE_MAX_ITER,
    threshold: float = 0.5,
    max_corridor: float = REFERENCE_MAX_CORRIDOR,
    retry_growth: float = REFERENCE_RETRY_GROWTH,
    max_attempts: int = REFERENCE_MAX_ATTEMPTS,
    angular_deflection: float = vx.DEFAULT_ANGULAR_DEFLECTION,
    force: Sequence[float] = (0.0, 0.0, -1.0),
    forces: Optional[Sequence[Sequence[float]]] = None,
    stl_path: Optional[str | Path] = None,
    progress: Any = None,
    solver: Optional[str] = None,
    threads: Optional[int] = None,
    **derive_kwargs: Any,
) -> Reference:
    """**案1の塊で1回だけ SIMP を回して、連続値の ρ を持ち帰る。**

    材料量は `material_mm3`(絶対量)か `material_ratio`(塊の容量に対する分率)。
    **既定は分率である** —— 参照は部品ごとに違う大きさの箱で回るので、絶対量の既定は部品の
    大きさに追随しない(hanger の実測: 絶対距離で持った塊の膨張量は部品が変わると外れる)。
    それでも分率は部品によって痩せすぎるので、**回廊が `max_corridor` を超えたら
    `retry_growth` 倍して取り直す**(`REFERENCE_MAX_CORRIDOR` のところに実測)。
    `material_mm3` を明示で渡したときは呼び出し側が量を決めているので取り直さない。

    `derive_kwargs` は `derive`(案1)へ素通し(`margin_ratio` / `elements_long_axis` /
    `spacing` など)。**`derive_from_reference` へ渡すものと揃えること** —— 揃っていないと
    格子が変わり、順位が別の場所を指す(切り出す側が突き合わせて落とす)。
    """
    started = time.perf_counter()
    ratio = float(material_ratio)
    if material_mm3 is None and not 0.0 < ratio < 1.0:
        raise ValueError(f"material_ratio は 0 < r < 1 であること(いま {material_ratio!r})")
    if int(max_iter) < 1:
        raise ValueError(f"max_iter は1以上であること(いま {max_iter!r})")
    if not 0.0 < float(max_corridor) <= 1.0:
        raise ValueError(f"max_corridor は 0 < r <= 1 であること(いま {max_corridor!r})")
    if not float(retry_growth) > 1.0 or not math.isfinite(float(retry_growth)):
        raise ValueError(f"retry_growth は 1 より大きい有限の値であること(いま {retry_growth!r})")
    if int(max_attempts) < 1:
        raise ValueError(f"max_attempts は1以上であること(いま {max_attempts!r})")

    # **`angular_deflection` は名前付きで受けて明示的に渡す。** `derive_from_reference` も
    # 同じように名前付きで受けるので、`derive_kwargs` の中身が両者で揃う(揃っていないと
    # 切り出し側の突き合わせが、格子は同じなのに違うと言い張る)。
    base = derive(conditions, angular_deflection=angular_deflection, **derive_kwargs)
    capacity = float(base.element_count) * float(base.grid.element_volume)
    material = capacity * ratio if material_mm3 is None else float(material_mm3)
    table = base.table if base.table is not None else an.build_anchors(base.soup, base.grid)
    conditions_mask = _condition_elements(base, table)
    limit = float(max_corridor) * base.element_count
    attempts = 0
    failed: Optional[BaseException] = None

    # **痩せすぎたら材料を増やして取り直す。** 判定は回廊の大きさ(`REFERENCE_MAX_CORRIDOR`)。
    # `material_mm3` を明示で渡された場合は呼び出し側が量を決めているので取り直さない。
    while True:
        attempts += 1
        last = attempts >= int(max_attempts) or material_mm3 is not None
        try:
            out = run_topopt(
                base,
                material_mm3=material,
                max_iter=int(max_iter),
                force=force,
                forces=forces,
                solver=solver,
                threads=threads,
                progress=progress,
                stl_path=_REFERENCE_STL_PATH if stl_path is None else stl_path,
            )
        except (simp.VolumeTargetError, simp.SolveResidualError) as exc:
            # **取り直すのは「材料が少なすぎる」現れだけ。** 体積目標が固定材料に届かない
            # (`below_minimum`)/ 低密度で系が悪条件になって残差検算が通らない、はどちらも
            # 増やせば直る。**`above_capacity` は増やすと悪化する**ので即座に上げる
            # (他の `ValueError` —— 引数不正・拘束不足など —— も同じ理由で握らない)。
            if last or getattr(exc, "side", None) == "above_capacity":
                raise
            failed = exc
            material *= float(retry_growth)
            continue
        # **`run_topopt` は空条件面のとき例外にせず `"result"` を `None` にして返す**
        # (`anchor.run_pipeline` の契約)。ここで見ないと `.rho` で素の `AttributeError` になる。
        if out.get("empty_condition_faces"):
            raise ReferenceRunConditionFacesEmptyError(out["empty_condition_faces"])

        rho = np.asarray(out["result"].rho, dtype=float)
        order = reference_order(rho, base.mask, base.grid, threshold=threshold)
        corridor = corridor_length(order, base, table, conditions_mask)
        if corridor <= limit or last:
            break
        failed = None
        material *= float(retry_growth)

    stats = {
        "seconds": time.perf_counter() - started,
        "attempts": attempts,
        "retried_after": None if failed is None else f"{type(failed).__name__}: {failed}"[:200],
        "material_mm3": material,
        "material_ratio": material / capacity if capacity else 0.0,
        "capacity_mm3": capacity,
        "base_elements": base.element_count,
        "max_iter": int(max_iter),
        "iterations": int(out["iterations"]),
        "achieved_material_mm3": float(out["achieved_material_mm3"]),
        "compliance_last": float(out["compliance_last"]),
        "threshold": float(threshold),
        "solid_elements": int(
            np.count_nonzero((rho >= float(threshold)) & np.asarray(base.mask, dtype=bool))
        ),
        "corridor": int(corridor),
        "corridor_ratio": float(corridor) / base.element_count if base.element_count else 0.0,
        "base_seconds": float(base.stats["seconds"]),
    }
    return Reference(
        base=base,
        rho=rho,
        order=order,
        material_mm3=material,
        corridor=int(corridor),
        conditions=conditions,
        # **`angular_deflection` も入れる。** 名前付きで受けているので `derive_kwargs` には
        # 入っておらず、入れないと「格子が変わりうる引数なのに突き合わせない」穴が残る。
        derive_kwargs=dict(derive_kwargs, angular_deflection=float(angular_deflection)),
        stats=stats,
    )


def derive_from_reference(
    conditions: Conditions,
    *,
    material_mm3: float,
    reference: Optional[Reference] = None,
    k: float = K,
    reference_material_ratio: float = REFERENCE_MATERIAL_RATIO,
    reference_max_iter: int = REFERENCE_MAX_ITER,
    angular_deflection: float = vx.DEFAULT_ANGULAR_DEFLECTION,
    force: Sequence[float] = (0.0, 0.0, -1.0),
    forces: Optional[Sequence[Sequence[float]]] = None,
    progress: Any = None,
    solver: Optional[str] = None,
    threads: Optional[int] = None,
    **derive_kwargs: Any,
) -> DesignSpace:
    """**連続値保持: 保存した ρ の順位で、この材料量ぶんの塊を切り出す。**

    案1(`derive`)と**同じ型・同じ不変条件**の `DesignSpace` を返す。SIMP を回すのは
    **材料量ごとではなく探索を通して1回**だけで、参照は本番と同じ格子で回る。

    手順:

    1. 参照(`reference`)が無ければ作る。**探索から呼ぶときは必ず渡すこと** —— 渡さないと
       材料量ごとに参照を作り直すことになり、探索の段数ぶん SIMP が増える。
    2. `max(k * material_mm3 / 要素体積, reference.corridor)` 本を順位の先頭から取る。
    3. 4条件の要素(保持・拘束アンカー・荷重アンカー)を無条件で OR し、案1の塊で AND する。

    **連結性は回廊(`Reference.corridor`)が構造的に保証する。** 回廊は参照ごとに1回だけ
    二分探索で求めた「拘束と荷重を繋ぐのに要る最小本数」なので、どの材料量でも必ず繋がり、
    かつ**塊は材料量について単調**になる(単調でないと外側の二分探索の前提が崩れる)。
    案1の塊全体でも繋がらない配置は `build_reference` の時点で
    `DesignSpaceDisconnectedError` になる。
    """
    started = time.perf_counter()
    material = float(material_mm3)
    if not material > 0.0 or not math.isfinite(material):
        raise ValueError(f"material_mm3 は有限の正の値であること(いま {material_mm3!r})")
    if not float(k) > 0.0 or not math.isfinite(float(k)):
        raise ValueError(f"k は有限の正の値であること(いま {k!r})")

    if reference is None:
        reference = build_reference(
            conditions,
            material_ratio=reference_material_ratio,
            max_iter=reference_max_iter,
            force=force,
            forces=forces,
            progress=progress,
            solver=solver,
            threads=threads,
            angular_deflection=angular_deflection,
            **derive_kwargs,
        )
    else:
        # **別の格子・別の条件で作った参照は使えない。** ρ も順位も `base.grid` の上でしか
        # 意味を持たないので、取り違えると例外も出さずに別の場所を選ぶ。**引数と条件で
        # 突き合わせる**のは、格子を起こし直すと本番解像度ではボクセル化が導出時間の大半を
        # 占めるため(毎段払うわけにいかない)。
        wanted = dict(derive_kwargs, angular_deflection=float(angular_deflection))
        if wanted != dict(reference.derive_kwargs):
            raise ValueError(
                f"参照を作ったときの導出引数 {sorted(reference.derive_kwargs.items())} と、"
                f"いま渡された {sorted(wanted.items())} が違う。"
                "格子が変わると保存した ρ の順位が別の場所を指すので、参照を作り直すこと"
            )
        if reference.conditions is not None and reference.conditions is not conditions:
            raise ValueError(
                "参照を作ったときの条件と、いま渡された条件が別のオブジェクトである。"
                "保存した ρ はその条件の格子・形状の上でしか意味を持たないので、"
                "参照を作り直すこと(条件を組み直したなら形も変わっている可能性がある)"
            )

    base = reference.base
    table = base.table if base.table is not None else an.build_anchors(base.soup, base.grid)
    conditions_mask = _condition_elements(base, table)
    inside = np.asarray(base.mask, dtype=bool)
    element_volume = float(base.grid.element_volume)
    # **回廊を下回らない。** ここが連結性の保証であり、塊が材料量について単調であることの
    # 根拠でもある(`Reference.corridor` の docstring)。
    wanted = int(round(float(k) * material / element_volume))
    taken = min(max(wanted, int(reference.corridor)), int(reference.order.size))
    picked = np.zeros(inside.size, dtype=bool)
    picked[reference.order[:taken]] = True
    picked = picked.reshape(inside.shape)
    mask = (picked | conditions_mask) & inside

    if not mask.any():
        raise DesignSpaceTooSmallError(
            f"連続値保持の塊が空になった(材料 {material:.6g}mm^3 / k={float(k):.4g})。"
            "参照 run の材料量か k を見直すこと。",
            {"reference": -1},
        )
    # 回廊が保証しているので通るはずだが、**案1と同じく導出が自分で確かめてから返す**。
    connectivity = assert_faces_connected(mask, table, base.fixed_faces, base.load_faces)

    try:
        model = vx.model_from_design_mask(
            base.grid,
            mask,
            anchor_shape=base.soup,
            keep=conditions.keep,
            deviation=float(base.stats["deviation"]),
            angular_deflection=angular_deflection,
        )
    except vx.OutsideDesignSpaceError as exc:
        raise DesignSpaceTooSmallError(
            f"連続値保持の塊(材料 {material:.6g}mm^3 / k={k:.4g})が保持ソリッドを"
            f"含みきれていない: {exc} 保持は無条件に塊へ入れているので、これは除外ソリッドが"
            "保持を食っている配置(案1でも同じ理由で落ちる)。",
            getattr(exc, "outside", {}),
        ) from exc

    stats = dict(base.stats)
    stats.update(
        {
            "derivation": "reference",
            "connectivity": connectivity,
            "case1_elements": base.element_count,
            "design_elements": int(np.count_nonzero(mask)),
            "shrink_vs_case1": (
                float(np.count_nonzero(mask)) / base.element_count if base.element_count else 0.0
            ),
            "keep_elements": int(model.stats["keep_elements"]),
            "material_mm3": material,
            "k_requested": float(k),
            "k": float(np.count_nonzero(mask)) * element_volume / material,
            "ranked_elements": taken,
            "corridor": int(reference.corridor),
            # **回廊が k より効いた**(材料量が小さすぎて、繋ぐのに要る量のほうが多い)。
            "corridor_bound": bool(int(reference.corridor) > wanted),
            "condition_only_elements": int(np.count_nonzero(conditions_mask & inside & ~picked)),
            "case1_seconds": float(base.stats["seconds"]),
            "reference": dict(reference.stats),
        }
    )
    stats["seconds"] = time.perf_counter() - started
    return DesignSpace(
        grid=base.grid,
        mask=mask,
        model=model,
        soup=base.soup,
        conditions=conditions,
        bbox_min=base.bbox_min,
        bbox_max=base.bbox_max,
        margin=base.margin,
        margin_ratio=base.margin_ratio,
        stats=stats,
        table=table,
    )

# ---------------------------------------------------------------------------
# 既定の導出(入口を1つにする)
# ---------------------------------------------------------------------------

# **既定の導出は連続値保持(`"reference"`)。** hanger の実測で、答えの被覆 100%・壁に
# 接した材料 5.38%(案1と同値)で、塊は案1より 24% 小さい。塊の広げ方を絶対距離ではなく
# k(= 塊の容量 ÷ 材料量)で持つので、部品の大きさに追随する。
DEFAULT_DERIVATION = "reference"

# 名前 → 導出。名前を受け取る側(受入検査の比較実測)がここを引く。
# **塊を作りたいだけの呼び出し側はこの表を引かない** —— 引くと既定を倒す場所が増える。
DERIVATIONS = {
    "bbox": derive,
    "reference": derive_from_reference,
}


def derive_default(
    conditions: Conditions, *, derivation: Optional[str] = None, **kwargs: Any
) -> DesignSpace:
    """**導出にこだわりのない呼び出し側の入口。** 既定(`DEFAULT_DERIVATION`)へ委ねる。

    `duocad/optimize_run.py`(製品の経路)はここを通る。特定の導出を名指しで呼びたい側
    (比較の実測・受入検査)は `derive` / `derive_from_reference` を直接呼ぶ —— **名指しの呼び出しを既定の変更で動かしてはいけない**(比較のベースラインが
    黙って入れ替わる)。

    `kwargs` は**そのまま既定の導出へ渡る**(素通し)。したがって受け付ける引数は
    `DEFAULT_DERIVATION` が指す関数のもので、いまは連続値保持:
    **`material_mm3` と `reference` を塞がないこと** —— 塊はその材料量ぶんを順位から切り出す
    ので量が要り、参照は探索を通して1つを使い回すので外から渡す必要がある。ここで受けないと
    外側のループ(安全率)がどちらも渡す手段を失う。

    `derivation="bbox"` は**案1を名指しする口**。既定を倒したあとに以前の挙動へ戻して
    比べるための口で、ここで名指しした側は `DEFAULT_DERIVATION` が変わっても動かない。
    **製品の経路(`optimize_run.run`)もここを通って塊の容量と保持体積を測る** ——
    既定の導出は材料量を要求するので、材料量の探索範囲を決める道具には使えない。
    """
    name = DEFAULT_DERIVATION if derivation is None else str(derivation)
    try:
        fn = DERIVATIONS[name]
    except KeyError:
        raise ValueError(f"未知の導出 {name!r}(選べるのは {sorted(DERIVATIONS)})") from None
    return fn(conditions, **kwargs)


def run_topopt(
    space: DesignSpace,
    *,
    volfrac: Optional[float] = None,
    material_mm3: Optional[float] = None,
    rmin: Optional[float] = None,
    rmin_mm: Optional[float] = None,
    max_iter: int = 60,
    force: Sequence[float] = (0.0, 0.0, -1.0),
    forces: Optional[Sequence[Sequence[float]]] = None,
    stl_path: Optional[str | Path] = None,
    progress: Any = None,
    solver: Optional[str] = None,
    threads: Optional[int] = None,
    keep_model: bool = False,
) -> dict:
    """導出した塊を端から端まで回す(塊 → 面アンカー → SIMP → marching cubes → STL)。

    `stl_path` は**必ず渡すか、既定のこの関数の出力先に任せる**。`anchor.run_pipeline` の既定は
    `output/bracket.stl` で、それは `check_anchor.py` の成果物である。

    体積目標は `volfrac`(分率)か `material_mm3`(材料の総体積[mm^3])のどちらか一方
    (両方渡すと例外)。**塊を自動で導出する経路では絶対量を使うこと** —— 余白比率や姿勢で
    塊の大きさが変わると、同じ分率でも材料の量が変わってしまう。どちらも渡さなければ
    `simp.DEFAULT_VOLFRAC`。

    密度フィルタ半径は `rmin`(要素単位)か `rmin_mm`(mm)のどちらか一方(排他)。
    どちらも渡さなければ `simp.DEFAULT_RMIN`(**値は動く。床の `simp.MIN_RMIN` とは別物**)。
    `anchor.run_pipeline` へそのまま渡す(要素サイズはそちらが格子から決める)。

    `keep_model=True` は `anchor.run_pipeline` へそのまま渡る(既定 `False`。数値は
    1ビットも変わらない)。

    荷重の向きは `force`(全ての荷重面で共有する1本)か `forces`(荷重面と**同じ並び**の
    面ごとの力ベクトル[N])のどちらか一方。どちらも `anchor.run_pipeline` の同名の引数へ
    そのまま渡る(意味の正はそちら)。

    戻り値の `"reactions"` は拘束面1枚ごとの反力(`anchor.run_pipeline` の契約そのまま)。

    拘束面・荷重面が要素サイズより小さくて格子節点が1つも付かないと
    (`anchor.AnchorTable.empty_faces()`)、**例外にはせず**戻り値の
    `"empty_condition_faces"` に診断が積まれて早期returnする(`anchor.run_pipeline` の契約
    そのまま)。同じ診断を `space.stats["empty_condition_faces_warning"]` にも書く
    (`DesignSpace` は呼び出し元がそのまま持っているので、戻り値を経由しなくても後から
    見える。当面の措置 — docs/TOPOPT.md「面が小さすぎて空になる」参照)。
    """
    out = Path(stl_path) if stl_path is not None else _HERE / "output" / "design_space_case1.stl"
    result = an.run_pipeline(
        shape=space.soup,
        design_mask=space.mask,
        design_grid=space.grid,
        keep=space.conditions.keep,
        # 除外は塊から引き切ってある(モジュール docstring の設計判断)。
        obstacle=None,
        fixed_faces=space.fixed_faces,
        load_faces=space.load_faces,
        volfrac=volfrac,
        material_mm3=material_mm3,
        rmin=rmin,
        rmin_mm=rmin_mm,
        max_iter=max_iter,
        force=force,
        forces=forces,
        stl_path=out,
        progress=progress,
        solver=solver,
        threads=threads,
        keep_model=keep_model,
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
    if result.get("empty_condition_faces"):
        space.stats["empty_condition_faces_warning"] = result["empty_condition_faces"]
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
    return Conditions(keep=keep, fixed_faces=fixed, load_faces=load, obstacle=obstacle)


def severed_conditions(angle_deg: float = 0.0, *, thickness: float = 10.0) -> Conditions:
    """`demo_conditions` の除外ソリッドを、**拘束と荷重の間を丸ごと塞ぐ壁**に差し替えた配置。

    受入検査[10]と docs/TOPOPT.md の実測(切れている系をそのまま最適化に通す)が共有する題材。
    壁は2つのパッドの間(x=0)に立て、y/z 方向は塊よりずっと大きく取る —— 塊の断面を端まで
    塞がないと、**縁を回り込む道が残って「切れている」配置にならない**(既定の余白比率 0.25 で
    塊は y -28..28 / z -28..34 程度なので、±100 あれば余白比率を振っても足りる)。

    `thickness` は壁の厚み[mm]。**要素サイズより十分厚くすること** —— 要素中心が壁に入らないと
    塊が1要素も削れず、塞いだつもりの配置がつながったまま通る。
    """
    from build123d import Box, Pos, Rot

    base = demo_conditions(angle_deg)
    wall = Rot(0, 0, float(angle_deg)) * (Pos(0, 0, 0) * Box(float(thickness), 200, 200))
    return Conditions(
        keep=base.keep,
        fixed_faces=base.fixed_faces,
        load_faces=base.load_faces,
        obstacle=wall,
        anchor_shape=base.anchor_shape,
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
    hits = an.faces_matching(
        soup, kind="plane", normal=n, near_point=p, normal_tol=1e-6, near_tol=1e-4
    )
    if len(hits) != 1:
        raise ValueError(
            f"デモの面を一意に選べなかった(法線={np.round(n, 4).tolist()}"
            f" 重心={np.round(p, 4).tolist()} → {hits})。demo_conditions を変えたら見直すこと"
        )
    return hits[0]


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="4条件から塊(設計空間)を導出する")
    parser.add_argument("--margin-ratio", type=float, default=DEFAULT_MARGIN_RATIO)
    parser.add_argument("--elements-long-axis", type=int, default=28)
    args = parser.parse_args()

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    space = derive(
        demo_conditions(0.0),
        margin_ratio=args.margin_ratio,
        elements_long_axis=args.elements_long_axis,
    )
    print(json.dumps(space.stats, ensure_ascii=False, default=float, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
