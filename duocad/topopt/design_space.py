"""4条件(保持ソリッド・除外ソリッド・拘束面・荷重面)から塊(設計空間)を導出する。

    .\\.venv\\Scripts\\python.exe -m duocad.topopt.design_space

導出は2つ入っている。**既定は案5**(`derive_from_coarse_run`)、案1(`derive`)は比較の
ベースラインと退避経路として残してある。**導出にこだわりのない呼び出し側は `derive_default`
を通ること** —— 既定を次に倒すときに書き換える場所を1箇所に閉じ込めるための入口である。
両方式の弱点・比較の数字は docs/TOPOPT.md「4条件から塊(設計空間)を導出する」以降の各節。

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

## 案5(粗く1回回して膨らませる。`derive_from_coarse_run`)

案1〜案4は**条件の位置だけ**から塊を決める(どこに材料が要るかを見ていない)。案5だけが
**一度解いてから決める** —— 案1で粗い塊を作って SIMP を短く回し、出てきた材料を二値化して
本番の格子へ再サンプルし、mm で膨らませ、4条件の要素を無条件に足す。**姿勢依存がほぼ消える**
のが利点(壁に接した材料の割合だけは代理指標として負けるが、本命のコンプライアンスは同等以上)。
既定は案5(入口は `derive_default`)。数字の根拠は docs/TOPOPT.md「4条件から塊を導出する —
案5」・「既定をどちらにすべきか」。

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


class CoarseRunConditionFacesEmptyError(ValueError):
    """案5(`derive_from_coarse_run`)の**粗い段**で拘束面・荷重面のアンカーが空になった。

    `run_topopt(coarse, ...)` は `anchor.run_pipeline` の契約どおり例外にせず
    `empty_condition_faces` を積んだ辞書を返す(`"result"` は `None`)。しかし
    `derive_from_coarse_run` は `DesignSpace` を返す契約の関数で、`run_topopt` のような
    「早期returnで辞書を返す」形は取れない —— **入口層(`run_topopt`/`anchor.run_pipeline`)の
    戻り値の形はこの関数のために変えない。** そこでここは同じ診断を例外に載せて落とす。
    `empty_condition_faces` に `anchor.AnchorTable.condition_face_diagnoses` と同じ形の
    リストが入る。**このガードが無いと** `coarse_out["result"]` が `None` のまま `.rho` に
    触れて素の `AttributeError` になる(空の面だとすら分からない)。
    """

    def __init__(self, empty_condition_faces: Sequence[dict]) -> None:
        diagnoses = list(empty_condition_faces)
        detail = an.format_condition_face_warning(diagnoses)
        message = (
            "案5(derive_from_coarse_run)の粗い段で拘束面・荷重面のアンカーが空になった: "
            f"{detail} coarse_elements_long_axis を上げるか、条件面を選び直すこと"
            "(本番解像度 elements_long_axis では空でなくても、粗い段だけ空になりうる)。"
        )
        super().__init__(message)
        self.empty_condition_faces = diagnoses


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
    # —— 案5は同じ格子でもう一度アンカー要素が要るので、無いと表を2回作ることになる
    # (本番解像度では導出時間の大半がこれ)。`None` を許すのは、塊を直接組んだ
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
# 案5(粗く1回回して膨らませる)
# ---------------------------------------------------------------------------
# 既定値の根拠はすべて実測(docs/TOPOPT.md「案5の既定値をどう決めたか」)。**「なんとなく」の既定を
# 置いていない** —— 案1の `margin_ratio` と同じ扱いで、検査ではなく実測が根拠である。
COARSE_ELEMENTS_LONG_AXIS = 14
COARSE_MAX_ITER = 20
COARSE_THRESHOLD = 0.3
COARSE_DILATE_MM = 12.0


def grow_by_mm(mask: np.ndarray, grid: vx.Grid, distance_mm: float) -> np.ndarray:
    """マスクを距離 `distance_mm`[mm] だけ膨らませる(ユークリッド距離)。

    **膨張は層数ではなく mm で持つ。** 格子は箱に合わせて切り直されるので要素サイズは姿勢と
    余白ごとに変わり(実測で要素体積 62.99 → 66.43 mm^3)、層数で持つと
    同じ設定が姿勢ごとに別の物理距離になる。
    """
    m = np.asarray(mask, dtype=bool)
    d = float(distance_mm)
    if not math.isfinite(d) or d < 0:
        raise ValueError(f"distance_mm must be a finite number >= 0, got {distance_mm!r}")
    if d == 0.0 or not m.any():
        return m.copy()
    # `distance_transform_edt(~m)` は「m でないセルから最も近い m のセルまでの距離」。
    dist = ndimage.distance_transform_edt(~m, sampling=float(grid.spacing))
    return m | (dist <= d + 1e-9)


def resample_nearest(mask: np.ndarray, source: vx.Grid, target: vx.Grid) -> np.ndarray:
    """`source` 格子のマスクを `target` 格子へ最近傍で移す。**対応は物理座標で取る。**

    粗い格子と本番の格子は**原点も要素サイズも違う**(どちらも箱に合わせて切り直されるので、
    要素数の比にも要素サイズの比にもならない)。軸ごとに本番のセル中心へ最も近い粗いセル中心を
    探し、**距離が粗い要素の半分を超えたら粗い格子の外**として False にする(端で1列ぶん値を
    引き伸ばさないため)。
    """
    src = np.asarray(mask, dtype=bool)
    if tuple(src.shape) != tuple(source.shape):
        raise ValueError(f"mask のかたち {src.shape} が source 格子 {source.shape} と違う")
    idx: list[np.ndarray] = []
    valid: list[np.ndarray] = []
    for axis in range(3):
        tc = target.centers_1d(axis)
        sc = source.centers_1d(axis)
        j = np.clip(np.rint((tc - sc[0]) / float(source.spacing)).astype(int), 0, sc.size - 1)
        idx.append(j)
        valid.append(np.abs(tc - sc[j]) <= 0.5 * float(source.spacing) + 1e-9)
    out = src[np.ix_(idx[0], idx[1], idx[2])]
    return out & (valid[0][:, None, None] & valid[1][None, :, None] & valid[2][None, None, :])


def derive_from_coarse_run(
    conditions: Conditions,
    *,
    margin_ratio: float = DEFAULT_MARGIN_RATIO,
    elements_long_axis: int = DEFAULT_ELEMENTS_LONG_AXIS,
    spacing: Optional[float] = None,
    coarse_elements_long_axis: int = COARSE_ELEMENTS_LONG_AXIS,
    coarse_max_iter: int = COARSE_MAX_ITER,
    coarse_volfrac: Optional[float] = None,
    coarse_material_mm3: Optional[float] = None,
    threshold: float = COARSE_THRESHOLD,
    dilate_mm: float = COARSE_DILATE_MM,
    coarse_stl_path: Optional[str | Path] = None,
    deviation: Optional[float] = None,
    deviation_ratio: float = vx.DEFAULT_DEVIATION_RATIO,
    angular_deflection: float = vx.DEFAULT_ANGULAR_DEFLECTION,
) -> DesignSpace:
    """**案5: 粗い格子で1回だけ SIMP を回し、出てきた材料を膨らませたものを塊にする。**

    案1(`derive`)と**同じ型・同じ不変条件**の `DesignSpace` を返すので、`run_topopt(space, ...)`
    も表示側もそのまま受けられる。案1は条件の位置だけから塊を決める(どこに材料が要るかを見て
    いない)のに対し、案5は**一度解いてから決める**。

    手順(この順序で組んである):

    1. **案1で粗い塊を作る**(`derive` を粗い解像度で呼ぶ。新しい箱の作り方は発明しない)。
    2. 粗い塊で SIMP を短く回す(`run_topopt`)。
    3. ρ を `threshold` で二値化し、本番の格子へ最近傍で再サンプルする
       (**対応は物理座標で取る** —— 粗い格子と本番の格子は原点も要素サイズも違う)。
    4. `dilate_mm`[mm] だけ膨らませる。**本番の格子でかける** —— 理由は下の実測コメント。
    5. **4条件の要素(保持・拘束アンカー・荷重アンカー)を無条件で OR し、除外を引く。**
       粗い段で削れても膨張で届かなくても、条件の要素は必ず塊に入る。
    6. 連結性ガード(`assert_faces_connected`。6近傍)を通す。

    `coarse_volfrac` / `coarse_material_mm3` は粗い段の体積目標(排他。どちらも省略すると
    `simp.DEFAULT_VOLFRAC`)。**本番で使う材料の量が分かっているなら
    `coarse_material_mm3` に同じ量を渡すこと** —— 塊は「その量の材料がどこへ行きたいか」で
    決まるべきで、粗い段だけ別の量で解くと別の形の周りを膨らませることになる。
    **目標を達成可能区間の端に置かないこと**(`volfrac=1.0`/`0.0` 相当は OC の二分探索が
    退化する。**その退化は `simp.VolumeTargetError`(`side="above_capacity"`)で落ちる**
    —— 塊の容量と、この反復で到達できる最大が文面に出る)。
    **区間の内側でも落ちることがある**: 密度フィルタが塊の縁で目減りさせるので、
    要素数で入る量でも届かない配置がある。**膨張を絞りすぎると踏む**。
    """
    started = time.perf_counter()
    thr = float(threshold)
    if not 0.0 < thr < 1.0:
        raise ValueError(f"threshold は 0 < t < 1 であること(いま {threshold!r})")
    if float(dilate_mm) < 0 or not math.isfinite(float(dilate_mm)):
        raise ValueError(f"dilate_mm must be a finite number >= 0, got {dilate_mm!r}")
    if int(coarse_elements_long_axis) < 1:
        raise ValueError(
            f"coarse_elements_long_axis must be >= 1, got {coarse_elements_long_axis!r}"
        )
    simp.check_volume_target(coarse_volfrac, coarse_material_mm3)

    common = {
        "margin_ratio": margin_ratio,
        "deviation": deviation,
        "deviation_ratio": deviation_ratio,
        "angular_deflection": angular_deflection,
    }
    # 本番の塊(案1の箱)。**案5の塊はこの中にしか作らない** —— 箱は上限であって答えではない。
    fine = derive(
        conditions, elements_long_axis=elements_long_axis, spacing=spacing, **common
    )
    t_coarse = time.perf_counter()
    coarse = derive(conditions, elements_long_axis=int(coarse_elements_long_axis), **common)
    coarse_stl = (
        Path(coarse_stl_path)
        if coarse_stl_path is not None
        else _HERE / "output" / "design_space_coarse.stl"
    )
    coarse_out = run_topopt(
        coarse,
        volfrac=coarse_volfrac,
        material_mm3=coarse_material_mm3,
        max_iter=int(coarse_max_iter),
        stl_path=coarse_stl,
    )
    # **`run_topopt` は拘束面・荷重面のアンカーが空だと例外にせず早期returnする**
    # (`"result"` が `None` のまま返る)。ここで見ずに `coarse_out["result"].rho` へ触ると
    # 素の `AttributeError` になる。**入口層(`run_topopt`)の戻り値の形はここのために
    # 変えない**ので、ここで検知して構造化された例外に変換する(粗い段だけ本番解像度より
    # 要素サイズが大きく、条件面が粗い段でだけ空になりうる)。
    if coarse_out.get("empty_condition_faces"):
        raise CoarseRunConditionFacesEmptyError(coarse_out["empty_condition_faces"])
    coarse_seconds = time.perf_counter() - t_coarse

    rho = np.asarray(coarse_out["result"].rho, dtype=float)
    # 二値化に固定要素(ρ=1)を明示的に足す。閾値がどれだけ高くても条件は落とさない。
    binary = ((rho >= thr) | np.asarray(coarse_out["solid"], dtype=bool)) & coarse.mask
    # **膨張は本番の格子でかける(再サンプルのあと)。** 粗い格子でかけると膨張の量が粗い
    # 要素サイズに量子化され、mm の指定が実質2〜3段階しか取れなくなる(実測は docs/TOPOPT.md
    # 「4条件から塊を導出する — 案5」)。本番の格子でかければ量子は本番の要素サイズまで下がる。
    resampled = resample_nearest(binary, coarse.grid, fine.grid)
    grown = grow_by_mm(resampled, fine.grid, dilate_mm)

    table = fine.table if fine.table is not None else an.build_anchors(fine.soup, fine.grid)
    # **4条件の要素は無条件で入れる。** 粗い段で削れた・膨張が届かなかった、のどちらでも
    # 条件の要素が塊から落ちてはならない(落ちると拘束も荷重も伝わらない)。
    conditions_mask = np.asarray(fine.model.keep, dtype=bool).copy()
    condition_faces = tuple(fine.fixed_faces) + tuple(fine.load_faces)
    empty = [f for f in condition_faces if f in table.empty_faces()]
    if not empty:
        conditions_mask |= table.element_mask(list(condition_faces), layers=1)
    # 除外は `fine.mask`(= 箱 − 除外)を掛けることで引く。条件の要素であっても除外の中には
    # 置かない —— 除外を跨ぐ塊を許すと、案1で `DesignSpaceTooSmallError` になる配置が
    # 案5だけ黙って通ることになる。
    mask = (grown | conditions_mask) & fine.mask
    if not mask.any():
        raise DesignSpaceTooSmallError(
            f"案5の塊が空になった(閾値 {thr} / 膨張 {float(dilate_mm)}mm)。"
            "粗い段の反復数か体積目標を見直すこと。",
            {"coarse": -1},
        )

    dev = float(fine.stats["deviation"])
    try:
        model = vx.model_from_design_mask(
            fine.grid,
            mask,
            anchor_shape=fine.soup,
            keep=conditions.keep,
            deviation=dev,
            angular_deflection=angular_deflection,
        )
    except vx.OutsideDesignSpaceError as exc:
        raise DesignSpaceTooSmallError(
            f"案5の塊(閾値 {thr} / 膨張 {float(dilate_mm)}mm)が保持ソリッドを含みきれていない: "
            f"{exc} 案5では保持は無条件に塊へ入れているので、これは除外ソリッドが保持を"
            "食っている配置(案1でも同じ理由で落ちる)。",
            getattr(exc, "outside", {}),
        ) from exc

    connectivity = assert_faces_connected(mask, table, fine.fixed_faces, fine.load_faces)

    stats = dict(fine.stats)
    stats.update(
        {
            "derivation": "coarse_run",
            "connectivity": connectivity,
            "case1_elements": fine.element_count,
            "design_elements": int(np.count_nonzero(mask)),
            "shrink_vs_case1": (
                float(np.count_nonzero(mask)) / fine.element_count if fine.element_count else 0.0
            ),
            "keep_elements": int(model.stats["keep_elements"]),
            "coarse": {
                "seconds": coarse_seconds,
                "elements_long_axis": int(coarse_elements_long_axis),
                "grid": list(coarse.grid.shape),
                "spacing": float(coarse.grid.spacing),
                "design_elements": coarse.element_count,
                "max_iter": int(coarse_max_iter),
                "iterations": int(coarse_out["iterations"]),
                "volfrac": float(coarse_out["target_volfrac"]),
                "material_mm3": float(coarse_out["achieved_material_mm3"]),
                "compliance_last": float(coarse_out["compliance_last"]),
                "grey_fraction": float(
                    np.count_nonzero((rho > 0.1) & (rho < 0.9) & ~np.asarray(coarse_out["void"]))
                )
                / max(1, int(np.count_nonzero(~np.asarray(coarse_out["void"])))),
                "binary_elements": int(np.count_nonzero(binary)),
                "binary_resampled_elements": int(np.count_nonzero(resampled)),
            },
            "threshold": thr,
            "dilate_mm": float(dilate_mm),
            "dilate_layers_fine": float(dilate_mm) / float(fine.grid.spacing),
            "resampled_elements": int(np.count_nonzero(resampled)),
            "grown_elements_fine": int(np.count_nonzero(grown)),
            "condition_elements": int(np.count_nonzero(conditions_mask & fine.mask)),
            "condition_only_elements": int(
                np.count_nonzero(conditions_mask & fine.mask & ~grown)
            ),
            "fine_seconds": float(fine.stats["seconds"]),
        }
    )
    stats["seconds"] = time.perf_counter() - started
    return DesignSpace(
        grid=fine.grid,
        mask=mask,
        model=model,
        soup=fine.soup,
        conditions=conditions,
        bbox_min=fine.bbox_min,
        bbox_max=fine.bbox_max,
        margin=fine.margin,
        margin_ratio=fine.margin_ratio,
        stats=stats,
        table=table,
    )


# ---------------------------------------------------------------------------
# 既定の導出(入口を1つにする)
# ---------------------------------------------------------------------------

# **既定の導出は案5(`"coarse_run"`)。** 根拠は docs/TOPOPT.md「既定をどちらにすべきか」
# ——「速い・姿勢依存が消える・コンプライアンスは同等以上」で、負けているのは
# 代理指標(壁に接した材料の割合)の 0° の1項目だけ。
DEFAULT_DERIVATION = "coarse_run"

# 名前 → 導出。名前を受け取る側(`perf.py --derivation` / 受入検査)がここを引く。
# **塊を作りたいだけの呼び出し側はこの表を引かない** —— 引くと既定を倒す場所が増える。
DERIVATIONS = {"bbox": derive, "coarse_run": derive_from_coarse_run}


def derive_default(
    conditions: Conditions, *, derivation: Optional[str] = None, **kwargs: Any
) -> DesignSpace:
    """**導出にこだわりのない呼び出し側の入口。** 既定(`DEFAULT_DERIVATION`)へ委ねる。

    `duocad/optimize_run.py`(製品の経路)はここを通る。案1・案5を名指しで
    呼びたい側(比較の実測・受入検査)は `derive` / `derive_from_coarse_run` を直接呼ぶ ——
    **名指しの呼び出しを既定の変更で動かしてはいけない**(比較のベースラインが黙って
    入れ替わる)。

    `kwargs` は**そのまま既定の導出へ渡る**(素通し)。したがって受け付ける引数は
    `DEFAULT_DERIVATION` が指す関数のもので、いまは案5:
    **`coarse_material_mm3` を塞がないこと** —— 本番で使う材料の量が分かっているなら
    粗い段にも同じ量を渡す必要があり(`derive_from_coarse_run` の docstring)、
    ここで受けないと外側のループ(安全率)がその量を渡す手段を失う。

    `derivation="bbox"` は**退避経路**。既定を倒したあとに案1の挙動へ戻して比べるための口で、
    ここで名指しした側は `DEFAULT_DERIVATION` が変わっても動かない。
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

    parser = argparse.ArgumentParser(description="4条件から塊(設計空間)を導出する(案1/案5)")
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
