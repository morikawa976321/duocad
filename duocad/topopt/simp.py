"""3D SIMP トポロジー最適化 (片持ち梁)。"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from . import trace
import scipy.optimize as sopt

# **FEM(要素剛性・節点番号・行列組立・CG/direct/MG・残差検算)は `fem.py` の実装を使う。**
# ここでの再輸出は後方互換のため —— `topopt_checks/` の一部が `simp.Model` / `simp.MGOptions` 等を
# 直接参照する。**`_SOLVE_STATS` は再輸出しない**(可変グローバルの張り替えを想定した口
# (`profile_simp_iter_breakdown.py` 等)は `fem._SOLVE_STATS` を直接読み書きすること ——
# `from .fem import _SOLVE_STATS` は import 時点の値を simp 側へ**コピー**するだけで、
# 以後 `fem._SOLVE_STATS` を差し替えても `simp._SOLVE_STATS` は追随しない。同じ理由で
# `simp.PARALLEL_THREADS = ...` による一時的な差し替えも `fem.PARALLEL_THREADS` を直接
# 書き換えること — `check_parallel.py` 参照)。
from .fem import (
    MGOptions,
    MG_DEFAULT,
    Model,
    PARALLEL_THREADS,
    PreparedSystem,
    SOLVE_RESIDUAL_TOL,
    SolveResidualError,
    _BlockedMatvec,
    _MIN_ROWS_PER_THREAD,
    _build_blocked_levels,
    _build_edof,
    _build_load,
    _coarse_axis,
    _coo_index_arrays,
    _default_thread_count,
    _effective_threads,
    _floating_dofs,
    _mg_setup,
    _prolongation,
    _vcycle,
    build_mg_transfers,
    collect_solve_stats,
    element_stiffness,
    new_solve_stats,
    parallel_matvec,
)

# `volfrac` も絶対量も渡さなかったときの分率。**PRESET の volfrac とは別物**
# (PRESET は片持ち梁の題材、こちらは `optimize` の既定値)。値は歴史的に一致している。
DEFAULT_VOLFRAC = 0.3

# **数値側の床**[要素]。`rmin_mm` で指定した半径がこれを下回ると `FilterRadiusError`。
# 1.5要素という値の根拠(下回るとチェッカーボードが出る)は `check_filter_radius.py` [6] が
# 実測で示す(docs/TOPOPT.md「1.5要素という下限の根拠」)。
MIN_RMIN = 1.5

# `rmin` も `rmin_mm` も渡さなかったときのフィルタ半径[要素]。**PRESET の rmin とは別物**
# (PRESET は片持ち梁の題材=回帰基準、こちらは `optimize` の既定値)。
# **`MIN_RMIN` とも別物**。床は「これより下はフィルタが壊れる」、
# 既定は「普通に使ったときに出したい形」で、決める根拠が違う —— 1つの定数に兼ねさせると、
# 既定を動かした瞬間に `rmin_mm` の受理範囲と `optimize_run.resolution_limits` の
# 解像度要求まで黙って動く。
#
# 床(1.5要素)の上でも薄板・傾けた題材は1要素幅のフィラメント(チェッカーボード)を出す。
# **複数の合成条件すべてで消える最小が2.5要素**(docs/TOPOPT.md「1要素幅のフィラメントを
# 消す既定値を選び直した」の段1)。代償はコンプライアンスが 1.08〜1.40倍。
DEFAULT_RMIN = 2.5

# `update` を渡さなかったときの更新則。選べる名前は `UPDATES`(下の方で更新則の実装と一緒に
# 定義する)。**ここで先に置いてあるのは `Result.update` の既定値に使うため。**
DEFAULT_UPDATE = "oc"

# 片持ち梁の基準題材。**受入検査3本がこれを回すので、大きさがそのまま検証の1周の長さになる。**
# 40x16x16(1周およそ11分)から 20x8x8 へ落としてある。**反復数は 60 のまま** ——
# 30反復まで落とすと終盤10反復の変動が 2.47% になり、`check_simp.py` (b) の1%を割る。
# 消してはならない(`Model` の既定の境界条件そのものが片持ち梁で、`check_anchor.py` は「拘束を
# 明示した結果と既定の結果が一致する」を面アンカー経路の保証にしている。解析的に検算できる
# 唯一の系でもある)が、検査の基準として使い続ける限り寸法と反復数は変えてよい。
# **`nelz` は偶数**(奇数だと `_build_load` が荷重を2節点へ割り、z対称性の検査の意味が変わる)。
PRESET: dict = {
    "nelx": 20,
    "nely": 8,
    "nelz": 8,
    "volfrac": 0.3,
    "penal": 3.0,
    "rmin": 1.5,
    "max_iter": 60,
    "change_tol": 0.01,
    "E": 1.0,
    "nu": 0.3,
    "emin": 1e-9,
    "move": 0.2,
    "solver": "cg",
    "cg_rtol": 1e-9,
}


def _filter_kernel(rmin: float) -> tuple[np.ndarray, int]:
    """密度フィルタのカーネルと半径(整数)を返す。"""
    r = int(np.ceil(rmin)) - 1
    rng = np.arange(-r, r + 1)
    dx, dy, dz = np.meshgrid(rng, rng, rng, indexing="ij")
    dist = np.sqrt(dx**2 + dy**2 + dz**2)
    return np.maximum(0.0, rmin - dist), r


def _filter_conv(field: np.ndarray, kernel: np.ndarray, r: int) -> np.ndarray:
    """3次元シフト加算による線形重み畳み込み。"""
    out = np.zeros_like(field)
    n = 2 * r + 1
    for a in range(n):
        for b in range(n):
            for c in range(n):
                w = kernel[a, b, c]
                if w == 0.0:
                    continue
                da, db, dc = a - r, b - r, c - r
                s_src = tuple(
                    slice(max(0, -d), field.shape[i] + min(0, -d))
                    for i, d in enumerate((da, db, dc))
                )
                s_dst = tuple(
                    slice(max(0, d), field.shape[i] + min(0, d))
                    for i, d in enumerate((da, db, dc))
                )
                out[s_dst] += w * field[s_src]
    return out


def _as_mask(mask: np.ndarray | None, shape: tuple[int, int, int], label: str) -> np.ndarray:
    """パッシブ要素のマスクを検算して bool 配列にする。"""
    if mask is None:
        return np.zeros(shape, dtype=bool)
    arr = np.asarray(mask)
    if arr.shape != shape:
        raise ValueError(f"{label} の形が {arr.shape}、期待は {shape}")
    return arr.astype(bool)


@dataclass(frozen=True)
class ReactionGroup:
    """反力をまとめて取り出す拘束自由度の集まり(ふつうは CAD の拘束面1枚)。

    **層をまたぐ契約**: `dofs` は `Model.fixed` の部分集合で、グループどうしは
    重ならないこと。重なると合計が全体の釣り合い(Σ R = -F)を超え、`fraction` が
    「その取付点が受け持つ割合」を意味しなくなる。`optimize` が両方とも検算する。
    """

    name: str
    dofs: np.ndarray


@dataclass(frozen=True)
class Reaction:
    """1つの拘束面グループが受け持っている反力。

    **単位は `optimize` に渡した荷重と同じ**(`anchor.run_pipeline` 経由なら無次元で、
    荷重の総量が 1.0)。`fraction` は `magnitude / |全荷重の合力|` なので単位に依らない
    —— 「この取付点が荷重の何割を受け持っているか」を読むのはこちら。
    """

    name: str
    force: tuple[float, float, float]
    magnitude: float
    fraction: float


def load_resultant(f: np.ndarray) -> np.ndarray:
    """荷重ベクトル (ndof,) → 合力 (3,)。自由度番号は `3*節点+軸` なので `dof % 3` が軸。"""
    f = np.asarray(f, dtype=float).ravel()
    return np.bincount(np.arange(f.size) % 3, weights=f, minlength=3)


def reaction_forces(
    model: "Model",
    rho: np.ndarray,
    penal: float,
    u: np.ndarray,
    groups: Sequence[ReactionGroup],
) -> np.ndarray:
    """拘束面グループごとの反力の合力 (len(groups), 3)。

    `R = (K u - f)|_fixed` —— **いま解いている `K u = f` から直接出る**ので、新しい
    順問題は要らない。符号は「支持が構造に及ぼす力」で、全グループを足すと荷重の合力の
    **符号を反転したもの**になる(剛体並進が K の零空間にあることから従う)。
    """
    residual = model.stiffness_apply(rho, penal, u) - model.f
    out = np.zeros((len(groups), 3))
    for index, group in enumerate(groups):
        dofs = np.asarray(group.dofs, dtype=np.int64).ravel()
        out[index] = np.bincount(dofs % 3, weights=residual[dofs], minlength=3)
    return out


def _as_reactions(
    groups: Sequence[ReactionGroup], vectors: np.ndarray, reference: float
) -> tuple[Reaction, ...]:
    """合力ベクトルの並び → `Reaction` の並び。`reference` は荷重の合力の大きさ。"""
    out = []
    for group, vector in zip(groups, vectors):
        magnitude = float(np.linalg.norm(vector))
        out.append(
            Reaction(
                name=group.name,
                force=(float(vector[0]), float(vector[1]), float(vector[2])),
                magnitude=magnitude,
                fraction=magnitude / reference if reference > 0.0 else float("nan"),
            )
        )
    return tuple(out)


def reaction_sensitivity(
    model: "Model",
    system: "PreparedSystem",
    rho: np.ndarray,
    penal: float,
    u: np.ndarray,
    groups: Sequence[ReactionGroup],
    vectors: np.ndarray,
    *,
    adjoint0: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """反力の**大きさ** `|R_i|` の、物理密度に対する感度 (len(groups), nele) と随伴解を返す。

    **反力は自己随伴ではない。** コンプライアンスが追加の線形解なしで感度を作れるのは
    「目的が `f^T u` で、随伴の右辺が荷重そのもの」という特殊事情で、反力にはグループ
    ごとに随伴が1本要る:

        R_i = |v_i|,  v_i = C_i^T (K u - f)      C_i は グループ i の自由度を軸ごとに拾う行列
        c_i = C_i (v_i / |v_i|)                  この反復で凍結する向き(|.| の勾配)
        K_ff λ_i = K_fs c_i
        dR_i/dρ_e = -penal ρ_e^(p-1) (E - emin) · (λ_e^T Ke u_e)
                    ただし λ は自由DOFに λ_i、固定DOFに -c_i を入れた全長ベクトル

    **「全体の釣り合い Σ R = -F があるからグループ数-1本でよい」は、大きさには効かない。**
    一定なのは合力**ベクトル**の和であって大きさの和ではないので、最後の1本の勾配を
    他から作るには各グループの3成分ヤコビアン(3(n-1)本)が要り、n 本より多くなる。

    `adjoint0` は前の反復の随伴解(ウォームスタート)。`system` は**順問題と同じ**
    `PreparedSystem` を渡すこと —— そうすれば多重格子の setup が反復あたり1回で済む。
    """
    ndof = model.ndof
    selectors = np.zeros((len(groups), ndof))
    for index, group in enumerate(groups):
        dofs = np.asarray(group.dofs, dtype=np.int64).ravel()
        magnitude = float(np.linalg.norm(vectors[index]))
        if magnitude > 0.0:
            unit = vectors[index] / magnitude
            selectors[index, dofs] = unit[dofs % 3]
        # 大きさが厳密に 0 の点では |.| が微分できない。その本は右辺が零になり、
        # `PreparedSystem.solve` が零解を返す(= その反復では感度 0)。制約は違反側に
        # あるので、次の反復で他の変数が動けば向きが決まる。
    rhs = np.stack(
        [model.stiffness_apply(rho, penal, selectors[index]) for index in range(len(groups))]
    )
    adjoint = system.solve(rhs, u0=adjoint0)
    # 固定DOF側は -c。`adjoint` は固定DOFがきっかり 0 なので引き算で入る。
    full = adjoint - selectors
    edof = model.edof
    keu = u[edof] @ model.ke
    coefficient = -penal * np.asarray(rho).ravel() ** (penal - 1.0) * (model.E - model.emin)
    out = np.empty((len(groups), edof.shape[0]))
    for index in range(len(groups)):
        out[index] = coefficient * np.einsum("ij,ij->i", full[index][edof], keu)
    return out, adjoint


def _check_reaction_groups(model: "Model", groups: Sequence[ReactionGroup]) -> None:
    """`ReactionGroup` の契約(`fixed` の部分集合・互いに重ならない)を検算する。"""
    if not groups:
        raise ValueError("reactions が空(反力を取り出すグループが1つも無い)")
    collected = []
    for group in groups:
        dofs = np.asarray(group.dofs, dtype=np.int64).ravel()
        if dofs.size == 0:
            raise ValueError(f"反力グループ {group.name!r} に自由度が1つも無い")
        outside = np.setdiff1d(dofs, model.fixed)
        if outside.size:
            raise ValueError(
                f"反力グループ {group.name!r} に拘束されていない自由度が "
                f"{outside.size} 個ある(例 {int(outside[0])})。"
                "反力は固定自由度にしか立たないので、拘束面から作った自由度を渡すこと"
            )
        collected.append(dofs)
    allof = np.concatenate(collected)
    if np.unique(allof).size != allof.size:
        raise ValueError(
            "反力グループどうしが自由度を共有している"
            f"(のべ {allof.size} に対して相異なるのは {np.unique(allof).size})。"
            "同じ節点を2つの取付点に数えると、受け持ちの割合が全体の釣り合いと合わなくなる"
        )


@dataclass(frozen=True)
class Iteration:
    it: int
    compliance: float
    volfrac: float
    change: float
    seconds: float
    # 反力を測っていない(`optimize(reactions=None)`)ときは空。**この反復で解いた
    # `x_phys` に対する値**であって、`step` の後の設計変数に対する値ではない
    # (`compliance` と同じ足並み)。
    reactions: tuple[Reaction, ...] = ()


@dataclass
class Result:
    rho: np.ndarray
    history: list[Iteration]
    nelx: int
    nely: int
    nelz: int
    seconds: float
    stopped_by: str
    # 実際に使った体積目標。**入力をそのまま写したものではない** —— 絶対量で渡された場合は
    # ここに換算後の値が入る。呼び出し側が「何を目標に解いたか」を入力の再計算なしで引ける。
    target_volume: float = float("nan")  # 要素数
    volfrac: float = float("nan")  # 動かせる要素に対する実効分率
    element_volume_mm3: float | None = None
    # 実際に使ったフィルタ半径[要素]。**入力をそのまま写したものではない** —— mm で渡された
    # 場合はここに換算後の値が入る(volfrac/target_volume と同じ役割分担)。
    rmin: float = float("nan")
    # 実際に使った更新則(`UPDATES` の名前)。**体積目標の意味が更新則で変わる** ——
    # `"oc"` は等式(届かなければ `VolumeTargetError`)、`"mma"` は上限(届かなくても
    # 例外にならず軽い解が返る)。受け取った側が `target_volume` との差をどう読むかを
    # 決めるのに要る。
    update: str = DEFAULT_UPDATE
    # 最後に解いた密度場での、拘束面グループごとの反力。`optimize(reactions=...)` を
    # 渡さなければ空。**「効いていない取付点」はここが `fraction ≈ 0` で出る。**
    reactions: tuple[Reaction, ...] = ()
    # 課した反力の下限(荷重の合力に対する割合)。制約を掛けていなければ None。
    reaction_min: float | None = None
    # ペナルティ連続化の入口。**`penal_start` が None なら `penal` 固定**(連続化なし)。
    # `history` のコンプライアンスは、**`penal_iters` 反復目より前は違う目的関数の値**
    # (灰色が許されるぶん小さく出る)。比較するときはそこから後だけを見ること。
    penal_start: float | None = None
    penal_iters: int = 0
    # `optimize(keep_model=True)` のときだけ入る、実際に解いた `Model`。**既定は None** ——
    # `Model` は `_iK`/`_jK` に**組み立てる要素数**×576 の添字配列を2本持つので、要らない
    # 呼び出しにまで数十MBを持たせない。後処理(`stress.py`)が拘束・荷重を手で組み直さずに
    # 済むための口で、組み直しを誤ると**落ちずに違う応力**が出る。
    model: "Model | None" = None

    @property
    def target_material_mm3(self) -> float | None:
        """目標の材料総体積[mm^3]。要素体積を渡していなければ None。"""
        if self.element_volume_mm3 is None:
            return None
        return self.target_volume * self.element_volume_mm3

    @property
    def achieved_material_mm3(self) -> float | None:
        """出てきた材料の総体積[mm^3]。要素体積を渡していなければ None。"""
        if self.element_volume_mm3 is None:
            return None
        return float(self.rho.sum()) * self.element_volume_mm3

    @property
    def rmin_mm(self) -> float | None:
        """実効フィルタ半径[mm]。要素体積を渡していなければ None(格子は立方体要素なので
        `element_volume_mm3 ** (1/3)` が要素サイズ[mm]に厳密に一致する)。"""
        if self.element_volume_mm3 is None:
            return None
        return self.rmin * (self.element_volume_mm3 ** (1.0 / 3.0))


class VolumeTargetError(ValueError):
    """絶対量(mm^3)の体積目標が達成できない。

    **どちら側にどれだけ足りないかを名指しするための例外。** これが無いと OC の二分探索が
    体積と無関係な例外(`ZeroDivisionError` / 「K U = F が解けていない」)で落ちる
    (docs/TOPOPT.md「体積目標を絶対量(mm^3)で指定する」)。
    属性 `side` は "below_minimum" か "above_capacity"。

    **区間の内側でも `above_capacity` で落ちることがある。** 要素数で見れば入る量でも、
    密度フィルタが塊の縁で目減りさせるぶん、ρ を上限まで上げても目標体積に届かない配置が
    あり、そのとき OC の二分探索が退化する。`volfrac` 指定の経路でも起きるので、
    その場合 `material_mm3` / `min_mm3` / `max_mm3` は NaN(要素体積を知らされていない)。
    """

    def __init__(
        self,
        message: str,
        *,
        material_mm3: float,
        min_mm3: float,
        max_mm3: float,
        shortfall_mm3: float,
        side: str,
        element_volume_mm3: float,
    ) -> None:
        super().__init__(message)
        self.material_mm3 = material_mm3
        self.min_mm3 = min_mm3
        self.max_mm3 = max_mm3
        self.shortfall_mm3 = shortfall_mm3
        self.side = side
        self.element_volume_mm3 = element_volume_mm3


class ReactionTargetError(ValueError):
    """反力の下限制約を満たす形が出なかった。

    **満たせない支持配置がある。** 静定な配置では反力の配り方が幾何だけで決まり、
    構造をどう変えても動かせない(hanger は面ごとに多数の節点を固定するので不静定だが、
    一般には成り立たない)。そこへ「各取付点が |F| の ε 倍を受け持て」と要求すると、
    妥協した設計ではなく**失敗**が正しい返事である。

    属性: `required`(要求した割合 ε)/ `reactions`(最後に測った `Reaction` の並び)/
    `result`(そこまでで出た `Result`。**捨てずに渡す** —— 30分回した結果を見ずに
    捨てさせないため)。
    """

    def __init__(
        self,
        message: str,
        *,
        required: float,
        reactions: tuple[Reaction, ...],
        result: "Result",
    ) -> None:
        super().__init__(message)
        self.required = float(required)
        self.reactions = reactions
        self.result = result


class FilterRadiusError(ValueError):
    """密度フィルタ半径を mm で指定したが、要素サイズから決まる下限(1.5要素)を下回る。

    **`rmin` が1要素を大きく下回るとフィルタが実質効かなくなり、チェッカーボード
    (市松模様の密度)が出る**(docs/TOPOPT.md「1.5要素という下限の根拠」が隣接要素の密度差の指標で
    実測している)。1.5要素は安全側に取った下限で、`MIN_RMIN` の値
    (**既定値 `DEFAULT_RMIN` とは別物**)。

    **逃げ道を2つとも数字で持つ**(`VolumeTargetError` と同じ形):
    `max_element_size_mm`(この `rmin_mm` を通すには要素サイズをいくつ以下にすればよいか)・
    `min_elements_long_axis`(それが最長軸の分割数でいくつに当たるか)・
    `min_rmin_mm`(この解像度で指定できる `rmin_mm` の下限)。
    """

    def __init__(
        self,
        message: str,
        *,
        rmin_mm: float,
        element_size_mm: float,
        min_rmin_mm: float,
        max_element_size_mm: float,
        min_elements_long_axis: int,
        shortfall_mm: float,
    ) -> None:
        super().__init__(message)
        self.rmin_mm = rmin_mm
        self.element_size_mm = element_size_mm
        self.min_rmin_mm = min_rmin_mm
        self.max_element_size_mm = max_element_size_mm
        self.min_elements_long_axis = min_elements_long_axis
        self.shortfall_mm = shortfall_mm


#: 反力の下限を「満たした」と読む相対の緩み。**実測では1度も要らなかった** ——
#: 合成題材(遠い壁・ε=0.02〜0.70・40反復)では毎回きっかり境界に乗り、超過は
#: `fraction/ε - 1` で 4.6e-05〜2.1e-03 だった(docs/TOPOPT_REACTION.md)。**この緩みは
#: 下の `floor` のとおり割る側で効くので、絶対差 `fraction - ε` と比べてはいけない**
#: (同じ実測が 1.4e-05〜1.4e-03 になり桁が1つずれる)。0 にしないのは、`max_iter` の
#: 打ち切りで届き切らなかったときに**30分回した結果ごと捨てる**のを避けるための頭であり、
#: 実際の割合は `Result.reactions` に残るので呼び出し側が自分で厳しく見てよい。
REACTION_TOLERANCE = 0.02


def _require_reactions(
    result: "Result",
    reactions: tuple[Reaction, ...],
    required: float,
) -> None:
    """最後に測った反力が下限を満たしているかを見て、満たしていなければ落とす。

    **満たしていない解を黙って返さない。** 反力の下限は「全ての取付点が実際に荷重を
    受け持つ」という取付要求で、満たしていない形は保持ソリッドが浮き島になって
    `mesh.remove_islands` に消える —— そこまで進んでから「なぜか部品が足りない」に
    なるより、ここで名指しして落ちるほうがよい。
    """
    if not reactions:
        # **1度も測っていないものを「満たした」と読まない。** 反復が1回も回らなければ
        # (`max_iter=0`・早期break)ここは空で来る。空のまま return すると
        # `reaction_min` を持ったまま無検証の `Result` が合格として出る。
        raise ReactionTargetError(
            f"反力の下限 {required:.1%} を課したのに、反力を1度も測っていない"
            "(最適化の反復が1回も回っていない)。max_iter を1以上にすること",
            required=required,
            reactions=reactions,
            result=result,
        )
    floor = required * (1.0 - REACTION_TOLERANCE)
    short = [item for item in reactions if not item.fraction >= floor]
    if not short:
        return
    breakdown = "、".join(f"{item.name} {item.fraction:.2%}" for item in reactions)
    named = "、".join(f"{item.name}({item.fraction:.2%})" for item in short)
    raise ReactionTargetError(
        f"反力の下限 {required:.1%} を満たさない取付点が {len(short)}/{len(reactions)} ある: "
        f"{named}。内訳は {breakdown}(荷重の合力に対する割合)。"
        "**支持配置によっては満たせない** —— 静定な配置では反力の配り方が幾何だけで決まり、"
        "構造を変えても動かせない。εを下げる / 材料を増やす / 反復を増やす、"
        "のどれかを試すこと(そのどれでも動かないなら、その取付点は構造上いらない)",
        required=required,
        reactions=reactions,
        result=result,
    )


def check_rmin_target(rmin: float | None, rmin_mm: float | None) -> None:
    """密度フィルタ半径の**引数の形**だけを検算する(格子を作る前に落とすための口)。

    上位層(`anchor.run_pipeline` 等)がボクセル化の前にこれを呼ぶ。要素サイズを知っている
    層でしか見られない「下限を割っていないか」の判定は `_resolve_rmin` が持つ
    (`check_volume_target` / `_resolve_volume_target` と同じ役割分担)。
    """
    if rmin is not None and rmin_mm is not None:
        raise ValueError(
            "rmin と rmin_mm は排他(どちらか一方だけ渡すこと)。"
            f"rmin={rmin!r} / rmin_mm={rmin_mm!r}"
        )
    if rmin is not None and not float(rmin) > 0.0:
        raise ValueError(f"rmin は正の値であること(受け取った値: {rmin!r})")
    if rmin_mm is not None and not float(rmin_mm) > 0.0:
        raise ValueError(f"rmin_mm は正の値であること(受け取った値: {rmin_mm!r})")


def _resolve_rmin(
    *,
    rmin: float | None,
    rmin_mm: float | None,
    element_volume_mm3: float | None,
    nelx: int,
    nely: int,
    nelz: int,
) -> float:
    """フィルタ半径を実効値(要素単位)へ畳む。

    `rmin_mm` は要素サイズが要る。格子は立方体要素(`voxelize.Grid`)なので
    `element_volume_mm3 ** (1/3)` が要素の一辺 = 要素サイズ[mm]に厳密に一致する
    (別の値を測り直す必要はない)。要素サイズから決まる下限(`MIN_RMIN` = 1.5要素。
    **既定値 `DEFAULT_RMIN` とは別物**)を下回ると `FilterRadiusError`。
    """
    if rmin_mm is None:
        return DEFAULT_RMIN if rmin is None else float(rmin)
    if element_volume_mm3 is None:
        raise ValueError(
            "rmin_mm を渡すときは element_volume_mm3(要素1つの体積[mm^3])も渡すこと"
            "(optimize は格子の寸法を知らないので、mm から要素単位へ換算できない)"
        )
    element_size_mm = float(element_volume_mm3) ** (1.0 / 3.0)
    resolved = float(rmin_mm) / element_size_mm
    if resolved < MIN_RMIN:
        min_rmin_mm = MIN_RMIN * element_size_mm
        max_element_size_mm = float(rmin_mm) / MIN_RMIN
        elements_long_axis = max(int(nelx), int(nely), int(nelz))
        length_long_axis_mm = element_size_mm * elements_long_axis
        min_elements_long_axis = int(np.ceil(length_long_axis_mm / max_element_size_mm))
        raise FilterRadiusError(
            f"rmin_mm={float(rmin_mm):.6g} はこの解像度(要素サイズ {element_size_mm:.6g}mm、"
            f"最長軸 {elements_long_axis}分割)では表現できない: 換算すると "
            f"{resolved:.4g}要素で、下限の {MIN_RMIN}要素に "
            f"{min_rmin_mm - float(rmin_mm):.6g}mm 足りない。"
            "**逃げ道は2つ**: "
            f"(1) この rmin_mm を通すには要素サイズを {max_element_size_mm:.6g}mm 以下にする"
            f"(最長軸で {min_elements_long_axis}分割以上に相当)。"
            f"(2) この解像度で指定できる rmin_mm の下限は {min_rmin_mm:.6g}mm。",
            rmin_mm=float(rmin_mm),
            element_size_mm=element_size_mm,
            min_rmin_mm=min_rmin_mm,
            max_element_size_mm=max_element_size_mm,
            min_elements_long_axis=min_elements_long_axis,
            shortfall_mm=min_rmin_mm - float(rmin_mm),
        )
    return resolved


def check_volume_target(volfrac: float | None, material_mm3: float | None) -> None:
    """体積目標の**引数の形**だけを検算する(格子を作る前に落とすための口)。

    上位層(`anchor.run_pipeline` 等)がボクセル化の前にこれを呼ぶ。要素体積を知っている
    層でしか見られない「達成できるか」の判定は `_resolve_volume_target` が持つ。
    """
    if volfrac is not None and material_mm3 is not None:
        raise ValueError(
            "volfrac と material_mm3 は排他(どちらか一方だけ渡すこと)。"
            f"volfrac={volfrac!r} / material_mm3={material_mm3!r}"
        )
    if material_mm3 is not None and not float(material_mm3) > 0.0:
        raise ValueError(f"material_mm3 は正の値であること(受け取った値: {material_mm3!r})")


def _resolve_volume_target(
    *,
    volfrac: float | None,
    material_mm3: float | None,
    element_volume_mm3: float | None,
    n_movable: int,
    n_solid: int,
) -> tuple[float, float]:
    """体積目標を (実効 volfrac, 目標体積[要素数]) へ畳む。

    **絶対量は「材料の総体積」** —— `solid_mask` で ρ=1 に固定された要素のぶんを含む。
    したがって達成できるのは `[solid, solid + movable]` の区間だけ。区間の外は
    **指定と違う量が出る**ので、ここで不足量つきの例外にする(下流の OC も落ちはするが、
    文面に体積の話が出ない)。
    """
    if material_mm3 is None:
        fraction = DEFAULT_VOLFRAC if volfrac is None else float(volfrac)
        return fraction, fraction * n_movable + float(n_solid)

    ev = float(element_volume_mm3)  # type: ignore[arg-type]
    target = float(material_mm3) / ev
    lo, hi = float(n_solid), float(n_solid + n_movable)
    # 端ちょうどの指定が丸めで区間の外へ出るのを許す(1要素の1e-9倍の幅)。
    tol = 1e-9 * max(1.0, hi)
    if target < lo - tol:
        shortfall = lo * ev - float(material_mm3)
        raise VolumeTargetError(
            f"体積目標 {float(material_mm3):.6g}mm^3 は達成できない: "
            f"solid_mask で材料に固定された {n_solid} 要素だけで {lo * ev:.6g}mm^3 あり、"
            f"目標が {shortfall:.6g}mm^3 下回っている"
            "(solid_mask を減らすか、目標を上げること)",
            material_mm3=float(material_mm3),
            min_mm3=lo * ev,
            max_mm3=hi * ev,
            shortfall_mm3=shortfall,
            side="below_minimum",
            element_volume_mm3=ev,
        )
    if target > hi + tol:
        shortfall = float(material_mm3) - hi * ev
        raise VolumeTargetError(
            f"体積目標 {float(material_mm3):.6g}mm^3 は達成できない: "
            f"動かせる {n_movable} 要素と固定材料 {n_solid} 要素を全部埋めても "
            f"{hi * ev:.6g}mm^3 で、目標が {shortfall:.6g}mm^3 超えている"
            "(塊を広げるか、目標を下げること)",
            material_mm3=float(material_mm3),
            min_mm3=lo * ev,
            max_mm3=hi * ev,
            shortfall_mm3=shortfall,
            side="above_capacity",
            element_volume_mm3=ev,
        )
    target = min(max(target, lo), hi)
    return (target - lo) / n_movable, target


# ---------------------------------------------------------------------------
# 更新則(OC / MMA)
# ---------------------------------------------------------------------------
# **差し替えられるのは更新則だけ。** 組み立て・多重格子・CG(`Model`)、密度フィルタ、
# 感度フィルタ(`dcn`/`dvn`)、パッシブ要素の扱い(`_physical`)は共通で、どの更新則も
# 同じ感度を受け取る。


@dataclass(frozen=True)
class Constraint:
    """更新則へ渡す**追加の不等式制約** `g(x) <= 0`。体積制約は含めない(更新則が自分で持つ)。

    **層をまたぐ契約**: `value` は現在点での g、`grad` は**設計変数**に対する感度
    (密度フィルタを通した後。`dcn`/`dvn` と同じ足並み)。どちらも**1のオーダーへ
    正規化して渡すこと** —— MMA の凸化の下駄(`MMA_RAA0`)が絶対値なので、桁が題材ごとに
    変わると近似の効き方が変わる。
    """

    name: str
    value: float
    grad: np.ndarray


class _Update:
    """更新則の口。`optimize` が1反復に1回 `step()` を呼ぶ。

    **層をまたぐ契約**:

    - `step()` が返すのは**設計変数の新しい値だけ**。物理密度への写像(`_physical`)・
      収束判定・履歴・`Result` の組み立ては `optimize` の仕事で、更新則は触らない。
    - **パッシブ要素(`void`/`solid`)の設計変数を動かしてはならない。** `_physical` が
      物理密度を上書きするので、動かしても物理には出ないが、体積制約の勘定が
      「動かしても物理体積が変わらない変数」に対して立ってしまう。
    - 目的(コンプライアンス)と体積の**感度は `optimize` が作って渡す**。更新則が
      フィルタを掛け直すことはない。
    - `constraints` は**体積以外の追加の不等式制約**(`Constraint`)。受け取れない更新則は
      **黙って無視せず例外**にすること —— 制約が効かないまま「もっともらしい形」が
      返るのが最悪である。
    """

    def __init__(
        self,
        *,
        move: float,
        target_volume: float,
        void: np.ndarray,
        solid: np.ndarray,
        physical: Callable[[np.ndarray], np.ndarray],
        n_movable: int,
        n_solid: int,
        element_volume_mm3: float | None,
    ) -> None:
        self.move = move
        self.target_volume = target_volume
        self.void = void
        self.solid = solid
        self.physical = physical
        self.n_movable = n_movable
        self.n_solid = n_solid
        self.element_volume_mm3 = element_volume_mm3

    def step(
        self,
        *,
        it: int,
        x: np.ndarray,
        x_phys: np.ndarray,
        compliance: float,
        dcn: np.ndarray,
        dvn: np.ndarray,
        constraints: Sequence[Constraint] = (),
    ) -> np.ndarray:
        raise NotImplementedError


class _OCUpdate(_Update):
    """OC(Optimality Criteria)。ラグランジュ乗数1本の二分探索。

    **制約1本でしか成り立たない。** 乗数が1つのスカラーで、二分探索の判定が
    「体積が目標を超えたか」の1ビットだから、制約を2本目にすると探索する対象が
    1次元でなくなる。反力制約のように制約が増える段では `mma` を使う。
    """

    def step(
        self,
        *,
        it: int,
        x: np.ndarray,
        x_phys: np.ndarray,
        compliance: float,
        dcn: np.ndarray,
        dvn: np.ndarray,
        constraints: Sequence[Constraint] = (),
    ) -> np.ndarray:
        if constraints:
            raise ValueError(
                f"OC は制約を1本しか扱えない(体積のほかに {len(constraints)} 本渡された: "
                f"{[item.name for item in constraints]})。"
                "乗数がスカラー1つで、二分探索の判定が「体積が目標を超えたか」の1ビット"
                "だから、制約を増やすと探索する対象が1次元でなくなる。"
                'update="mma" を使うこと'
            )
        move, void, solid = self.move, self.void, self.solid
        target_volume = self.target_volume
        l1, l2 = 0.0, 1e9
        # **二分探索が退化することがある。** 目標体積がこの反復で到達できる上限
        # (移動制限 `move` と、密度フィルタが塊の縁で落とすぶん)を超えていると、`l2` が 0 まで
        # 潰れて `(l2 - l1) / (l1 + l2)` が 0除算になる —— 文面に体積も塊も出ないので、
        # 原因から一番遠い場所で落ちる。**健全な側の計算は
        # 1ビットも変えない**: `l1 + l2 > 0.0` は退化していない限りつねに真。
        while l1 + l2 > 0.0 and (l2 - l1) / (l1 + l2) > 1e-9:
            lm = 0.5 * (l1 + l2)
            # `-dcn` は理屈上つねに正だが、`ce` がきっかり0の要素(荷重の届かない孤島)や
            # 丸めで符号が反転した要素があると sqrt が NaN を返し、**以後の反復が全部
            # NaN で埋まる**。0で潰しておく(その要素は下限へ寄るだけで済む)。
            xnew = np.clip(
                x * np.sqrt(np.maximum(0.0, -dcn) / (dvn * lm)),
                x - move,
                x + move,
            )
            xnew = np.clip(xnew, 0.0, 1.0)
            xnew[void] = 0.0
            xnew[solid] = 1.0
            xp = self.physical(xnew)
            if xp.sum() > target_volume:
                l1 = lm
            else:
                l2 = lm
        if l1 + l2 <= 0.0:
            ev = (
                float(self.element_volume_mm3)
                if self.element_volume_mm3 is not None
                else float("nan")
            )
            n_movable, n_solid = self.n_movable, self.n_solid
            capacity = float(n_movable + n_solid)
            reached = float(xp.sum())
            raise VolumeTargetError(
                f"体積目標 {target_volume:.6g} 要素に対し、この反復("
                f"it={it})で到達できる最大が {reached:.6g} 要素しかない: "
                f"塊の容量は {capacity:.6g} 要素(動かせる {n_movable} + 固定材料 {n_solid})で、"
                f"目標はその {target_volume / capacity:.1%}。"
                "**塊に対して材料が多すぎる** —— 密度フィルタが塊の縁で目減りさせるぶん、"
                "要素数で入る量でも ρ を上限まで上げて届かないことがある。"
                "塊を広げる(連続値保持なら k を上げる)か、"
                "材料を減らすこと",
                material_mm3=target_volume * ev,
                min_mm3=float("nan"),
                max_mm3=capacity * ev,
                shortfall_mm3=(target_volume - reached) * ev,
                side="above_capacity",
                element_volume_mm3=ev,
            )
        return xnew


# MMA(Method of Moving Asymptotes、Svanberg 1987)の定数。**論文の記述から自前で実装した**
# —— 出回っている Python 実装(`mmapy` / `GCMMA-MMA-Python`)は原典の MATLAB が GPLv3 なので
# 見ていない(2026-09-10 本人決定。配布方針が未定のうちは GPL を取り込まない)。
#
# 値はどれも文献の標準値そのままで、この題材で選び直した数字は1つも無い。
# 設計変数の範囲は [0, 1] 固定なので「範囲に対する比」はそのまま設計変数の絶対値でもある。
MMA_ASYMPTOTE_INIT = 0.5  # 最初の2反復の漸近線の半幅[範囲比]。直前2点が無く縮めようがない
MMA_ASYMPTOTE_SHRINK = 0.7  # 直前2歩で符号が反転した変数(振動)の漸近線を縮める率
MMA_ASYMPTOTE_GROW = 1.2  # 符号が揃った変数(単調)の漸近線を広げる率
MMA_ASYMPTOTE_MIN = 0.01  # 漸近線を x に近づけてよい下限[範囲比]。近すぎると歩幅が消える
MMA_ASYMPTOTE_MAX = 10.0  # 漸近線を x から離してよい上限[範囲比]。遠いと近似がただの線形
MMA_ALBEFA = 0.1  # 移動限界を漸近線から離す割合。漸近線の極に触ると近似が発散する
# 近似を**厳密に**凸にするための下駄。感度が 0 の変数でも p/q が正になるので、部分問題の
# 解の閉じた式(下の `_x_of`)の分母が 0 にならない。目的と制約を1のオーダーへ正規化した
# 上での 1e-5 なので、感度が正常な変数には効かない。
MMA_RAA0 = 1e-5
# 双対変数(体積制約の乗数)を1次元二分探索で決めるときの上界と回数。
# 上界は OC の `l2 = 1e9` と揃えた(乗数が大きいほど解は移動限界の下端へ寄るので、
# ここまで振っても届かなければ「この反復で到達できる最小体積」に達している)。
# 60回で区間は 1e9 * 2^-60 ≈ 1e-9 まで縮む(OC の停止条件 1e-9 と同じ桁)。
MMA_DUAL_MAX = 1e9
MMA_DUAL_ITERS = 60
# 制約が2本以上あるときの双対(L-BFGS-B)。上限は Svanberg の人工変数の罰金 c_j にあたり、
# **部分問題が必ず解けることを保証する**(その反復で満たせない制約があっても双対が発散しない)。
# 文献の標準値 1000 をそのまま使っている —— 目的も制約も1のオーダーへ正規化してあるので、
# 「制約1単位の違反は目的 1000 単位ぶん悪い」という重み付けになる。
MMA_CONSTRAINT_PENALTY = 1000.0
MMA_DUAL_MAX_ITERS = 200


class _MMAUpdate(_Update):
    """MMA(Method of Moving Asymptotes)。

    **OC と違って体積は不等式制約**(`sum(rho) <= target`)で、乗数が 0 に落ちれば
    目標より軽い解をそのまま返す。OC のように「目標にきっかり乗せる」ことはしないし、
    目標へ届かないときに `VolumeTargetError` を上げることもない
    (`optimize` の docstring「更新則を選ぶ」節)。

    **反復をまたいで状態を持つ**(OC は `x` 以外の状態を持たない): 移動漸近線
    `_low`/`_upp` と直前2点 `_xold1`/`_xold2`。1つの `_MMAUpdate` は1回の
    `optimize` 呼び出しに属する。
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._low: np.ndarray | None = None
        self._upp: np.ndarray | None = None
        self._xold1: np.ndarray | None = None
        self._xold2: np.ndarray | None = None
        self._c0: float | None = None
        self._lam: np.ndarray | None = None

    def step(
        self,
        *,
        it: int,
        x: np.ndarray,
        x_phys: np.ndarray,
        compliance: float,
        dcn: np.ndarray,
        dvn: np.ndarray,
        constraints: Sequence[Constraint] = (),
    ) -> np.ndarray:
        span = 1.0  # 設計変数の範囲 [0, 1] の幅
        # **目的と制約を1のオーダーへ揃える。** 乗数の二分探索は正規化に対して不変
        # (目的を定数倍すると乗数が同じだけ動くだけ)だが、`MMA_RAA0` の下駄だけは
        # 絶対値なので、感度の桁が題材ごとに変わると効き方が変わる。基準は初回の
        # コンプライアンス(以後の反復でも同じ基準を使う —— 反復ごとに取り直すと
        # 近似の尺度が毎回変わる)。
        if self._c0 is None:
            self._c0 = abs(float(compliance)) or 1.0
        df0 = dcn / self._c0
        # 体積制約 g(x) = sum(rho_phys) / target - 1 <= 0。感度 `dvn` は OC と同じもの。
        g = float(x_phys.sum()) / self.target_volume - 1.0
        dg = dvn / self.target_volume

        if self._xold2 is None or self._low is None or self._upp is None:
            low = x - MMA_ASYMPTOTE_INIT * span
            upp = x + MMA_ASYMPTOTE_INIT * span
        else:
            # 直前2歩の符号が反転した変数は振動しているので漸近線を寄せて歩幅を殺し、
            # 揃った変数は伸ばして歩幅を出す。
            sign = (x - self._xold1) * (self._xold1 - self._xold2)  # type: ignore[operator]
            gamma = np.where(
                sign < 0.0,
                MMA_ASYMPTOTE_SHRINK,
                np.where(sign > 0.0, MMA_ASYMPTOTE_GROW, 1.0),
            )
            low = x - gamma * (self._xold1 - self._low)  # type: ignore[operator]
            upp = x + gamma * (self._upp - self._xold1)  # type: ignore[operator]
        low = np.clip(low, x - MMA_ASYMPTOTE_MAX * span, x - MMA_ASYMPTOTE_MIN * span)
        upp = np.clip(upp, x + MMA_ASYMPTOTE_MIN * span, x + MMA_ASYMPTOTE_MAX * span)

        alpha = np.maximum(
            np.maximum(0.0, low + MMA_ALBEFA * (x - low)), x - self.move * span
        )
        beta = np.minimum(
            np.minimum(1.0, upp - MMA_ALBEFA * (upp - x)), x + self.move * span
        )
        # **パッシブ要素の設計変数を部分問題の中でも凍結する。** 上書きで後から戻すだけだと、
        # 体積制約の勘定が「動かしても物理体積が変わらない変数」の移動を当てにしてしまう
        # (`_Update` の契約)。感度 `dvn` 自体はパッシブ要素でも 0 ではない —— フィルタ越しに
        # 隣の自由要素の物理密度を動かすため —— ので、感度を刈るのでは足りない。
        passive = self.void | self.solid
        alpha = np.where(passive, x, alpha)
        beta = np.where(passive, x, beta)

        ud = upp - x
        ld = x - low
        raa = MMA_RAA0 / (upp - low)
        # 分離可能な凸近似 f(x) ≈ r + Σ p/(U-x) + q/(x-L)。感度の正の側を p、負の側を q へ
        # 振り、逆側にも 0.001 を残す(片側が厳密に 0 だと近似がその方向へ無限に伸びる)。
        p0 = ud * ud * (1.001 * np.maximum(df0, 0.0) + 0.001 * np.maximum(-df0, 0.0) + raa)
        q0 = ld * ld * (0.001 * np.maximum(df0, 0.0) + 1.001 * np.maximum(-df0, 0.0) + raa)
        p1 = ud * ud * (1.001 * np.maximum(dg, 0.0) + 0.001 * np.maximum(-dg, 0.0) + raa)
        q1 = ld * ld * (0.001 * np.maximum(dg, 0.0) + 1.001 * np.maximum(-dg, 0.0) + raa)
        # 定数項は「現在点で近似が真値に一致する」ことから決まる。
        r1 = g - float((p1 / ud + q1 / ld).sum())

        if constraints:
            # **制約が2本以上あると乗数が1次元でなくなる。** 双対は m 次元の凹関数の
            # 最大化になるので、閉じた形の `x(λ)` を使ったまま L-BFGS-B へ渡す(下)。
            # 体積1本のときは**下の二分探索をそのまま通す** —— `20260910-1313` が
            # 測った MMA の数値を、制約を足したあとも再現できるようにしておくため。
            extra_p = []
            extra_q = []
            extra_r = []
            for item in constraints:
                dgj = np.asarray(item.grad)
                pj = ud * ud * (
                    1.001 * np.maximum(dgj, 0.0) + 0.001 * np.maximum(-dgj, 0.0) + raa
                )
                qj = ld * ld * (
                    0.001 * np.maximum(dgj, 0.0) + 1.001 * np.maximum(-dgj, 0.0) + raa
                )
                extra_p.append(pj)
                extra_q.append(qj)
                extra_r.append(float(item.value) - float((pj / ud + qj / ld).sum()))
            xnew = self._dual_step(
                low=low,
                upp=upp,
                alpha=alpha,
                beta=beta,
                p0=p0,
                q0=q0,
                p_stack=np.stack([p1] + extra_p),
                q_stack=np.stack([q1] + extra_q),
                r_vec=np.array([r1] + extra_r),
            )
            self._xold2 = self._xold1
            self._xold1 = x.copy()
            self._low, self._upp = low, upp
            xnew[self.void] = 0.0
            xnew[self.solid] = 1.0
            return xnew

        def _x_of(lam: float) -> np.ndarray:
            """乗数 `lam` を固定したときの部分問題の解(変数ごとに閉じた式)。

            P/(U-x) + Q/(x-L) を最小にするのは x = (L√P + U√Q)/(√P + √Q)。
            `MMA_RAA0` のおかげで P も Q も正なので分母は 0 にならない。
            """
            sp_ = np.sqrt(p0 + lam * p1)
            sq_ = np.sqrt(q0 + lam * q1)
            return np.clip((low * sp_ + upp * sq_) / (sp_ + sq_), alpha, beta)

        def _violation(xn: np.ndarray) -> float:
            """近似した体積制約の値。正なら目標超過。"""
            return float((p1 / (upp - xn) + q1 / (xn - low)).sum()) + r1

        # 乗数を上げるほど解は軽くなる(単調)ので1次元の二分探索で足りる。
        if _violation(_x_of(0.0)) <= 0.0:
            # **制約が効いていない。** 目標より軽い解をそのまま返す —— これが OC との
            # いちばん大きな違いで、体積は等式ではなく上限として扱われる。
            xnew = _x_of(0.0)
        else:
            lo, hi = 0.0, MMA_DUAL_MAX
            for _ in range(MMA_DUAL_ITERS):
                mid = 0.5 * (lo + hi)
                if _violation(_x_of(mid)) > 0.0:
                    lo = mid
                else:
                    hi = mid
            # 上界の側を採る(制約を満たす側)。`hi` が動かないまま抜けたときは、
            # この反復の移動限界の中に目標を満たす点が無い —— そのときの解は
            # 「この反復で到達できるいちばん軽い配置」で、例外にはしない。
            xnew = _x_of(hi)

        self._xold2 = self._xold1
        self._xold1 = x.copy()
        self._low, self._upp = low, upp
        # パッシブ要素は `alpha == beta == x` で凍結済みだが、設計変数の不変条件
        # (void は 0、solid は 1)はここで名指しで守る。
        xnew[self.void] = 0.0
        xnew[self.solid] = 1.0
        return xnew

    def _dual_step(
        self,
        *,
        low: np.ndarray,
        upp: np.ndarray,
        alpha: np.ndarray,
        beta: np.ndarray,
        p0: np.ndarray,
        q0: np.ndarray,
        p_stack: np.ndarray,
        q_stack: np.ndarray,
        r_vec: np.ndarray,
    ) -> np.ndarray:
        """制約が m 本(m>=2)の部分問題を、双対の最大化で解く。

        双対関数 `W(λ) = min_x L(x, λ)` は凹で、`x(λ)` は変数ごとに閉じた形で出る
        (体積1本のときと同じ式)。Danskin より `dW/dλ_j` はその点での近似した制約値
        そのものなので、勾配つきの箱制約最適化(L-BFGS-B)へそのまま渡せる。

        **λ の上限 `MMA_CONSTRAINT_PENALTY` は人工変数の重み。** Svanberg の定式化は
        制約ごとに緩和変数 `y_j >= 0` と罰金 `c_j y_j` を持ち、その双対がちょうど
        `λ_j <= c_j` という箱になる。**部分問題が常に解けることを保証するための装置**で、
        これが無いと、その反復で満たせない制約があったときに双対が発散する。
        """
        def x_of(lam: np.ndarray) -> np.ndarray:
            sp_ = np.sqrt(p0 + np.tensordot(lam, p_stack, axes=1))
            sq_ = np.sqrt(q0 + np.tensordot(lam, q_stack, axes=1))
            return np.clip((low * sp_ + upp * sq_) / (sp_ + sq_), alpha, beta)

        def negative_dual(lam: np.ndarray) -> tuple[float, np.ndarray]:
            xn = x_of(lam)
            ud_ = upp - xn
            ld_ = xn - low
            value = float(
                ((p0 + np.tensordot(lam, p_stack, axes=1)) / ud_
                 + (q0 + np.tensordot(lam, q_stack, axes=1)) / ld_).sum()
            ) + float(lam @ r_vec)
            grad = (p_stack / ud_).sum(axis=tuple(range(1, p_stack.ndim))) + (
                q_stack / ld_
            ).sum(axis=tuple(range(1, q_stack.ndim))) + r_vec
            return -value, -grad

        size = r_vec.size
        start = (
            self._lam
            if self._lam is not None and self._lam.size == size
            else np.zeros(size)
        )
        found = sopt.minimize(
            negative_dual,
            start,
            jac=True,
            method="L-BFGS-B",
            bounds=[(0.0, MMA_CONSTRAINT_PENALTY)] * size,
            options={"maxiter": MMA_DUAL_MAX_ITERS, "ftol": 1e-15, "gtol": 1e-10},
        )
        self._lam = np.clip(np.asarray(found.x, dtype=float), 0.0, MMA_CONSTRAINT_PENALTY)
        return x_of(self._lam)


#: `optimize(update=...)` で選べる更新則。**未知の名前は既定へ落とさず例外**
UPDATES: dict[str, type[_Update]] = {
    "oc": _OCUpdate,
    "mma": _MMAUpdate,
}

if DEFAULT_UPDATE not in UPDATES:  # 既定名を消したまま気づかない事故を止める
    raise RuntimeError(f"DEFAULT_UPDATE={DEFAULT_UPDATE!r} が UPDATES に無い")


def penal_at(it: int, penal: float, start: float | None, iters: int) -> float:
    """反復 `it`(1始まり)で使うペナルティ。`start` が None なら `penal` 固定。

    **低い充填率では連続化は逆効果である(2026-09-12 実測)。**
    `hanger_bar_attachment` 2.412mm・塊 87,752要素・充填率 5.8%・120反復で、
    `start=1.0` / `iters=20` はコンプライアンスを **+18.5%** 悪化させ(111.44 → 132.06)、
    **保持ソリッドを2つ殺し**(結果メッシュから消える)、秒も 26% 増えた。収束も速くならない。
    機序: 充填率が低いと penal の低い区間の最適解が「材料を薄く広げた灰色場」になり、
    形の情報を持たない —— 一様な初期場より悪い出発点から penal=3 の問題を始めることになる
    (it=20 で C=358.97 対 連続化なし 137.95)。**この口を既定にしない。**

    `start` から `penal` まで `iters` 反復かけて線形に上げ、以降は `penal` のまま。
    **`it == iters` でちょうど `penal` に届く**(`iters=1` なら初回から `penal`)。

    **これは目的関数を反復の途中で変える。** 低い penal の区間のコンプライアンスは
    `penal` のそれと比べられない(灰色が許されるぶん必ず小さく出る)ので、**収束の比較は
    両方が `penal` に達したあとの区間で行うこと。**

    **反復をまたぐ状態を持つ更新則(MMA の漸近線・直前2点)とは相性の検証が要る** ——
    それらは同じ目的関数で測った点を前提にしている。OC は状態を持たないので影響しない。
    """
    if start is None:
        return float(penal)
    if it >= int(iters):
        return float(penal)
    span = max(int(iters) - 1, 1)
    return float(start) + (float(penal) - float(start)) * (int(it) - 1) / span


@trace.stage('simp')
def optimize(
    nelx: int,
    nely: int,
    nelz: int,
    *,
    volfrac: float | None = None,
    material_mm3: float | None = None,
    element_volume_mm3: float | None = None,
    penal: float = 3.0,
    penal_start: float | None = None,
    penal_iters: int = 20,
    rmin: float | None = None,
    rmin_mm: float | None = None,
    max_iter: int = 60,
    change_tol: float = 0.01,
    E: float = 1.0,
    nu: float = 0.3,
    emin: float = 1e-9,
    move: float = 0.2,
    update: str = DEFAULT_UPDATE,
    solver: str = "mg",
    cg_rtol: float = 1e-9,
    threads: int | None = None,
    callback: Callable[[Iteration, np.ndarray], None] | None = None,
    fixed_dofs: np.ndarray | None = None,
    load: np.ndarray | None = None,
    void_mask: np.ndarray | None = None,
    solid_mask: np.ndarray | None = None,
    reactions: Sequence[ReactionGroup] | None = None,
    reaction_min: float | None = None,
    keep_model: bool = False,
    dynamic_active: bool = False,
) -> Result:
    """SIMPでコンプライアンス最小化を行う。

    `threads` は `Model.solve` へそのまま渡る(既定 `None` → `simp.PARALLEL_THREADS`)。

    `fixed_dofs` / `load` は境界条件の差し替え(`Model` へそのまま渡る)。
    `void_mask` / `solid_mask` は**設計変数を動かさない要素**(パッシブ要素)で、
    それぞれ ρ=0 / ρ=1 に固定する。stage2 が「設計空間の外」「保持領域」「荷重直下」を
    入れるための口で、**4つとも None のときの挙動は追加前と完全に同じ**。

    体積制約は動かせる要素だけに掛ける(目標体積 = volfrac × 自由要素数 + 固定ρ=1 要素数)。
    そうしないと、設計空間の外の空要素を「もう十分軽い」と数えて、
    設計空間の中がほとんど埋まったまま止まる。

    体積目標は2通りで渡せる。**`volfrac` と `material_mm3` は排他**(両方渡すと例外):

    - `volfrac` —— 動かせる要素に対する分率。塊の大きさが変われば材料の量も変わる。
    - `material_mm3` + `element_volume_mm3` —— **材料の総体積を mm^3 の絶対量で**指定する
      (`solid_mask` のぶんを含む)。`optimize` は格子の寸法を知らないので、要素1つの体積
      `element_volume_mm3` を一緒に渡す(呼び出し側は `voxelize.Grid.element_volume`)。
      **質量(グラム)は扱わない** —— 密度を掛けるのは表示の仕事で、内部表現は体積である。
    - どちらも渡さなければ `DEFAULT_VOLFRAC`。

    `element_volume_mm3` は `volfrac` の側でも渡してよい(体積目標は変わらず、`Result` が
    mm^3 でも答えられるようになるだけ)。

    `keep_model=True` にすると `Result.model` に**実際に解いた `Model`** が入る
    (既定は入らない)。応力の後処理(`stress.py`)が拘束・荷重・浮いた節点の扱いを
    手で組み直さずに済ませるための口で、**数値は1ビットも変わらない**。
    `dynamic_active` と併せて使うときは `Model.reset_active` で静的な状態へ戻して返す。

    `dynamic_active=True` にすると、`Model.prepare` が**毎反復 ρ から active set を
    作り直す** —— `emin**(1/penal)` を下回った要素を `void_mask` と同じ扱いで組み立てと
    自由度から外す(`Model.dead_rho_threshold`)。**既定は False で、そのときの数値は
    1ビットも変わらない。** 効く場・効かない場・限界は docs/TOPOPT_ACTIVE_SET.md。

    フィルタ半径も2通りで渡せる。**`rmin` と `rmin_mm` は排他**(両方渡すと例外):

    - `rmin` —— 要素単位。**格子(解像度)を変えると物理的な平滑化スケールが変わる。**
    - `rmin_mm` + `element_volume_mm3` —— **物理単位(mm)で**指定する。`optimize` は
      格子の寸法を知らないので、要素1つの体積 `element_volume_mm3` を一緒に渡す
      (格子は立方体要素なので、その立方根が要素サイズ[mm]になる)。要素サイズから決まる
      床(`MIN_RMIN` = 1.5要素。下回るとチェッカーボードが出る)を下回ると
      `FilterRadiusError`(逃げ道2つを数字で持つ)。
    - どちらも渡さなければ `DEFAULT_RMIN`。**床とは別物** —— 床は「これより下は壊れる」、
      既定は「普通に使ったときに出したい形」。

    `reactions` は**反力を取り出す拘束自由度のグループ**(ふつう CAD の拘束面1枚に1つ)。
    渡すと各反復で `R = (K u - f)|_fixed` をグループごとに足し、`Iteration.reactions` と
    `Result.reactions` に載せる。**渡さなければ1つも計算しない**(既定の費用は変わらない)。
    追加費用は反復あたり要素ループ1回ぶんで、線形解は増えない。

    `reaction_min` は**各グループが受け持つ反力の下限**(荷重の合力に対する割合 ε)。
    渡すと `|R_i| >= ε |F|` を制約に足す —— 構造最適化は定義上、荷重を運ばない材料を
    置かないので、**これが無いと拘束面が1枚あれば足り、残りの取付点は形から消える**。

    - **「全部を等しくする」は採らない。** 反力はベクトルで、面の向きがばらばらだと
      釣り合いを満たしたまま大きさを揃えられる保証がなく、実現不可能な制約は
      「妥協した設計」ではなく失敗を返す。下限だけを課す。
    - `update="mma"` が要る(体積と合わせて制約が複数本になる)。`reactions` も要る。
    - **費用はグループ数ぶんの随伴**(`reaction_sensitivity`)。多重格子の setup は
      反復あたり1回のままで、増えるのは CG の反復ぶんだけ。
    - 最後まで回しても満たせなければ `ReactionTargetError`(そこまでの `Result` を
      例外に付けて返す)。

    **更新則を選ぶ** —— `update` は `UPDATES` の名前(既定 `DEFAULT_UPDATE` = `"oc"`)。
    知らない名前は既定へ落とさず `ValueError`。差し替わるのは更新則だけで、`Model`・
    密度フィルタ・感度フィルタ・パッシブ要素の扱いは共通(`_Update` の契約)。

    - `"oc"` —— ラグランジュ乗数1本の二分探索。**体積は等式**として扱い、目標に乗せる。
      その反復で目標へ届かないと `VolumeTargetError(side="above_capacity")`。
      **制約1本でしか成り立たない。**
    - `"mma"` —— 移動漸近線法。**体積は不等式(上限)**として扱う。目標より軽い解を
      返すことがあり、届かなくても例外にならない(`Result.target_volume` と
      `rho.sum()` の差を呼び出し側が自分で見ること)。制約を増やせる。
    """
    check_volume_target(volfrac, material_mm3)
    check_rmin_target(rmin, rmin_mm)
    if material_mm3 is not None and element_volume_mm3 is None:
        raise ValueError(
            "material_mm3 を渡すときは element_volume_mm3(要素1つの体積[mm^3])も渡すこと"
            "(optimize は格子の寸法を知らないので、要素数へ換算できない)"
        )
    if element_volume_mm3 is not None and not float(element_volume_mm3) > 0.0:
        raise ValueError(
            f"element_volume_mm3 は正の値であること(受け取った値: {element_volume_mm3!r})"
        )
    if reaction_min is not None:
        # **空の並びも None と同じに落とす。** 空だと制約が1本も立たず、終端の
        # `_require_reactions` も「満たさない取付点は0件」で素通りするので、
        # **下限を頼んだのに何も掛かっていない解が「合格」として返る。**
        if reactions is None or not tuple(reactions):
            raise ValueError(
                "reaction_min を渡すときは reactions(反力を取り出す拘束面グループ)を"
                "1つ以上渡すこと(何に対する下限かが決まらない)"
            )
        if not 0.0 < float(reaction_min) < 1.0:
            raise ValueError(
                f"reaction_min は 0 < ε < 1 であること(受け取った値: {reaction_min!r})"
            )
        if update != "mma":
            raise ValueError(
                f"反力の下限制約は update={update!r} では扱えない —— 体積と合わせて制約が"
                f" {1 + len(tuple(reactions))} 本になり、OC の乗数1本の二分探索が"
                '成り立たない。update="mma" を使うこと'
            )
    if penal_start is not None:
        if not 0.0 < float(penal_start) <= float(penal):
            raise ValueError(
                f"penal_start は 0 < start <= penal であること"
                f"(受け取った値: {penal_start!r} / penal={penal!r})"
            )
        if int(penal_iters) < 1:
            raise ValueError(f"penal_iters は1以上であること(いま {penal_iters!r})")
        # **途中で終わると灰色のまま答えが出る。** penal が目標に届く前に反復を使い切ると、
        # 中間密度が残った密度場が `rho` として返り、メッシュ側の閾値 0.5 がそれを
        # 切り取る —— 例外も警告も出ないまま「別の問題の解」が製品を通る。
        if int(penal_iters) > int(max_iter):
            raise ValueError(
                f"penal_iters({penal_iters})が max_iter({max_iter})を超えている —— "
                f"penal が {penal} へ届く前に終わるので、灰色の残った密度場が返る"
            )
        # **MMA は反復をまたぐ状態が「同じ目的関数で測った点」であることを前提にしている。**
        # `_c0`(感度の正規化)は初回のコンプライアンスで固定されるので、連続化すると
        # 低い penal の値が基準になる(実測 PRESET: penal=1 の初回 39.6 対 penal=3 の 440.3
        # で11倍)。漸近線と直前2点も別の目的関数上の点になる。**確かめていない組み合わせを
        # 黙って通さない。**
        if update == "mma":
            raise ValueError(
                "ペナルティ連続化は update='mma' では確かめていない —— MMA は感度の正規化"
                "(初回のコンプライアンス)・移動漸近線・直前2点を反復をまたいで持つので、"
                "目的関数が反復ごとに変わる前提になっていない。update='oc' を使うこと"
            )
    if dynamic_active and update == "mma":
        # MMA が要るのは実質 `reaction_min` の経路で、そこは随伴も直前の解を
        # ウォームスタートに使う(`reaction_sensitivity(adjoint0=)`)。順問題の `u0` は
        # `free` が変わっても正しく縮むことを確かめてあるが、**随伴と MMA の反復をまたぐ
        # 状態は確かめていない。黙って通さない。**
        raise ValueError(
            "dynamic_active は update='mma' では確かめていない —— MMA は感度の正規化"
            "(初回のコンプライアンス)・移動漸近線・直前2点を反復をまたいで持つので、"
            "組み立てる要素が反復ごとに変わる前提になっていない。update='oc' を使うこと"
            "(反力の下限制約 reaction_min は MMA を要求するので、併用もできない)"
        )
    try:
        update_cls = UPDATES[update]
    except KeyError:
        raise ValueError(
            f"未知の更新則 {update!r}(選べるのは {sorted(UPDATES)})"
        ) from None
    t_total = time.perf_counter()
    shape = (nelx, nely, nelz)
    void = _as_mask(void_mask, shape, "void_mask")
    solid = _as_mask(solid_mask, shape, "solid_mask")
    overlap = int(np.count_nonzero(void & solid))
    if overlap:
        raise ValueError(
            f"void_mask と solid_mask が {overlap} 要素で重なっている"
            "(同じ要素を空と材料の両方に固定できない)"
        )
    # 空要素にしか接していない節点は、剛性 emin でしか繋がっていない浮いた自由度。
    # 残すと K がほぼ特異になって CG が収束しないので、拘束側へ**足して**系から外す。
    # `fixed_dofs` へ代入してはいけない —— それだと `void_mask` だけを渡した呼び出しで
    # 既定の片持ち梁の拘束が消える。
    # 空要素は剛性行列の組み立てからも外す。**`extra_fixed_dofs` と同じ `void` から作る**
    # (`Model` の `active_mask` の契約)。未知数は1つも減らない —— 外れる要素にしか接して
    # いない自由度は、上の `floating` が既に拘束へ回した集合と定義が同一である
    # (2026-09-07/08 実測・14構成で自由自由度の集合が完全一致)。
    floating = _floating_dofs(nelx, nely, nelz, void) if void.any() else None
    model = Model(
        nelx,
        nely,
        nelz,
        E=E,
        nu=nu,
        emin=emin,
        fixed_dofs=fixed_dofs,
        load=load,
        extra_fixed_dofs=floating,
        active_mask=~void if void.any() else None,
        dynamic_active=dynamic_active,
    )
    rmin_resolved = _resolve_rmin(
        rmin=rmin,
        rmin_mm=rmin_mm,
        element_volume_mm3=element_volume_mm3,
        nelx=nelx,
        nely=nely,
        nelz=nelz,
    )
    kernel, r = _filter_kernel(rmin_resolved)
    hs = _filter_conv(np.ones((nelx, nely, nelz)), kernel, r)
    movable = ~void & ~solid
    n_movable = int(movable.sum())
    if n_movable == 0:
        raise ValueError("動かせる要素が1つも無い(void と solid で埋まっている)")
    volfrac, target_volume = _resolve_volume_target(
        volfrac=volfrac,
        material_mm3=material_mm3,
        element_volume_mm3=element_volume_mm3,
        n_movable=n_movable,
        n_solid=int(np.count_nonzero(solid)),
    )
    live = movable.astype(float)
    groups: tuple[ReactionGroup, ...] = () if reactions is None else tuple(reactions)
    if groups:
        _check_reaction_groups(model, groups)
    load_reference = float(np.linalg.norm(load_resultant(model.f)))
    if reaction_min is not None and not load_reference > 0.0:
        raise ValueError(
            "荷重の合力が 0 なので反力の下限を測る基準が無い"
            "(打ち消し合う荷重だけの系では、反力の総和も 0 になる)"
        )

    def _physical(design: np.ndarray) -> np.ndarray:
        """設計変数 → 物理密度。**パッシブ要素は密度フィルタの後で上書きする。**

        設計変数だけを固定して物理密度を上書きしないと、フィルタが設計空間の外へ
        密度をにじませる(実測: 半径 rmin=1.5 で設計空間の外の要素が ρ=0.29 になった)。
        にじんだぶんは marching cubes がそのまま形にするので、**CADの設計空間から
        はみ出した形**が出てくる。上書きに合わせて感度も `live` で刈る(下の `dcn`/`dvn`)
        ので、連鎖律は上書き後の定義と厳密に一致する。
        """
        field = _filter_conv(design, kernel, r) / hs
        field[void] = 0.0
        field[solid] = 1.0
        return field

    updater = update_cls(
        move=move,
        target_volume=target_volume,
        void=void,
        solid=solid,
        physical=_physical,
        n_movable=n_movable,
        n_solid=int(np.count_nonzero(solid)),
        element_volume_mm3=element_volume_mm3,
    )

    x = np.full((nelx, nely, nelz), volfrac)
    x[void] = 0.0
    x[solid] = 1.0
    x_phys = _physical(x)
    u = np.zeros(model.ndof)
    history: list[Iteration] = []
    stopped_by = "max_iter"
    ramp_until = 0 if penal_start is None else int(penal_iters)
    reaction_now: tuple[Reaction, ...] = ()
    adjoint: np.ndarray | None = None

    trace.snapshot('simp-initial', model=model, x=x, rho=x_phys, movable=movable,
                   void=void, solid=solid, kernel=kernel, hs=hs,
                   rmin_resolved=rmin_resolved, target_volume=target_volume)

    for it in range(1, max_iter + 1):
        t0 = time.perf_counter()
        trace.snapshot('iteration-input', iteration=it, x=x, rho=x_phys)
        # **順問題と随伴で K を1回だけ組み、前処理も1回だけ作る**(`Model.prepare`)。
        # 右辺ごとに `Model.solve` を呼ぶと多重格子の setup が本数ぶん重なる。
        penal_now = penal_at(it, penal, penal_start, penal_iters)
        system = model.prepare(x_phys, penal_now, solver=solver, threads=threads)
        u = system.solve(model.f[None, :], u0=u[None, :], cg_rtol=cg_rtol)[0]
        c, ce = model.compliance(x_phys, penal_now, u)
        constraints: tuple[Constraint, ...] = ()
        if groups:
            vectors = reaction_forces(model, x_phys, penal_now, u, groups)
            reaction_now = _as_reactions(groups, vectors, load_reference)
            if reaction_min is not None:
                raw, adjoint = reaction_sensitivity(
                    model, system, x_phys, penal_now, u, groups, vectors, adjoint0=adjoint
                )
                floor = reaction_min * load_reference
                # g_i = 1 - |R_i| / (ε|F|) <= 0。**感度は目的と同じくフィルタを通す**
                # (連鎖律は `_physical` の上書き後の定義と一致する。`live` で刈るのも同じ)。
                constraints = tuple(
                    Constraint(
                        name=group.name,
                        value=1.0 - reaction.magnitude / floor,
                        grad=-_filter_conv(
                            raw[index].reshape(shape) * live / hs, kernel, r
                        )
                        / floor,
                    )
                    for index, (group, reaction) in enumerate(zip(groups, reaction_now))
                )
        # **K_ff と多重格子の各段をここで手放す。** 下のフィルタと更新則は大きい一時配列を
        # 作るので、峰メモリを反復の後半まで持ち越さない。
        system = None
        dc = -penal_now * x_phys ** (penal_now - 1.0) * (E - emin) * ce
        dv = np.ones_like(x)
        # `live` は「その要素の物理密度が設計変数で動くか」。パッシブ要素の物理密度は
        # 上書きで定数になっているので、そこへの感度は連鎖律上ちょうど 0 になる。
        dcn = _filter_conv(dc * live / hs, kernel, r)
        dvn = _filter_conv(dv * live / hs, kernel, r)
        # 近傍が全部パッシブな要素は dvn=0 になりうる。そのままだと 0 除算で NaN が
        # 全反復へ伝播する(その要素はどのみち下の上書きで固定されるので値は使わない)。
        dvn = np.maximum(dvn, 1e-300)
        trace.snapshot('iteration-analysis', iteration=it, penal=penal_now, u=u, ce=ce,
                       compliance=c, dc=dc, dcn=dcn, dvn=dvn, constraints=constraints,
                       reactions=reaction_now, adjoint=adjoint, active=model.active,
                       fixed=model.fixed, free=model.free)
        xnew = updater.step(
            it=it,
            x=x,
            x_phys=x_phys,
            compliance=c,
            dcn=dcn,
            dvn=dvn,
            constraints=constraints,
        )
        change = float(np.abs(xnew - x).max())
        x = xnew
        x_phys = _physical(x)
        seconds = time.perf_counter() - t0
        iteration = Iteration(
            it=it,
            compliance=c,
            volfrac=float(x_phys.mean()),
            change=change,
            seconds=seconds,
            reactions=reaction_now,
        )
        history.append(iteration)
        trace.snapshot('iteration-output', iteration=iteration, x=x, rho=x_phys)
        if callback is not None:
            callback(iteration, x_phys.copy())
        # **連続化の途中では収束と読まない。** penal が目標へ届く前に `change_tol` で
        # 降りると、中間密度の残った密度場が `rho` として返る(`penal_iters > max_iter` を
        # 塞いでもこちらの経路が残る)。そもそも目的関数がまだ動いているので、そこでの
        # 「変化が小さい」は解けたことを意味しない。
        if change < change_tol and it >= ramp_until:
            stopped_by = "change_tol"
            break

    if dynamic_active and keep_model:
        model.reset_active()
    result = Result(
        rho=x_phys,
        history=history,
        nelx=nelx,
        nely=nely,
        nelz=nelz,
        seconds=time.perf_counter() - t_total,
        stopped_by=stopped_by,
        target_volume=target_volume,
        volfrac=volfrac,
        element_volume_mm3=(
            None if element_volume_mm3 is None else float(element_volume_mm3)
        ),
        rmin=rmin_resolved,
        update=update,
        reactions=reaction_now,
        reaction_min=reaction_min,
        penal_start=None if penal_start is None else float(penal_start),
        penal_iters=0 if penal_start is None else int(penal_iters),
        model=model if keep_model else None,
    )
    if reaction_min is not None:
        _require_reactions(result, reaction_now, reaction_min)
    return result


def run_preset(
    callback: Callable[[Iteration, np.ndarray], None] | None = None,
    **overrides: object,
) -> Result:
    """PRESET の値で optimize を呼ぶ。overrides で個別に上書きできる
    (**格子 `nelx`/`nely`/`nelz` も上書きできる** —— 分けて扱うと `optimize` へ
    同じ引数を2度渡して TypeError になる)。"""
    opts: dict = dict(PRESET)
    opts.update(overrides)
    return optimize(
        int(opts.pop("nelx")),
        int(opts.pop("nely")),
        int(opts.pop("nelz")),
        callback=callback,
        **opts,  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    result = run_preset()
    for item in result.history:
        print(
            f"反復 {item.it:3d}  C={item.compliance:10.4f}  "
            f"体積={item.volfrac:.4f}  change={item.change:.4f}  {item.seconds:.2f}秒"
        )
    print(f"完了: {result.stopped_by}  合計 {result.seconds:.1f}秒")
