"""密度場と変位から要素ごとの von Mises 応力・代表応力・最小安全率を出す(後処理)。

**最適化そのものには手を入れない。** 応力を制約に入れて最軽量を解く型(応力制約トポロジー
最適化)は採らないと決まっている。ここは「出てきた形の応力を後から測る」側だけを持つ。

変位の取り方(設計判断): **最終 ρ で `Model.solve()` をもう一度呼ぶ。**
呼び出し側の結果オブジェクトに `u` を持たせる案を採らなかったのは、`rho` を後から
差し替えた瞬間に `u` が黙って古くなる欄になることと、この後処理が**探索を通っていない
ρ 場**(受入検査の一様な棒・片持ち梁)でも同じ口で動く必要があるため。

無次元と実単位: `fem.Model` は `E=1.0` / 総荷重 1.0 / 要素サイズ 1 の無次元で解く。
換算は後処理側の責任で、`UnitScale` が持つ(docs/TOPOPT.md「応力の実単位への換算則」)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from . import trace

from . import fem

# 代表応力の既定の規則。**素の max ではない。** 階段状境界の角にある1要素は離散化の産物で、
# そこで安全率が決まると解像度を上げるほど安全率が下がり続ける(docs/TOPOPT.md の実測表)。
DEFAULT_RHO_MIN = 0.5
DEFAULT_PERCENTILE = 99.5

# 局所節点 l = 4a+2b+c ↔ 単位立方体の頂点 (a,b,c)。**`fem.element_stiffness` の
# `lam_nodes` と `fem._build_edof` の l のまわし方に一致していること** ——
# ここがずれると応力は「もっともらしい別の値」になって落ちない。
_NODE_SIGNS = np.array(
    [[2 * a - 1, 2 * b - 1, 2 * c - 1] for a in (0, 1) for b in (0, 1) for c in (0, 1)],
    dtype=float,
)


def elasticity_matrix(E: float = 1.0, nu: float = 0.3) -> np.ndarray:
    """弾性行列 D (6,6)。順は [xx, yy, zz, xy, yz, zx] で、せん断は工学ひずみ。"""
    d = np.zeros((6, 6))
    c1 = E / ((1.0 + nu) * (1.0 - 2.0 * nu))
    d[:3, :3] = c1 * (nu * np.ones((3, 3)) + (1.0 - 2.0 * nu) * np.eye(3))
    d[3:, 3:] = c1 * (1.0 - 2.0 * nu) / 2.0 * np.eye(3)
    return d


def strain_displacement_center(element_size: float = 1.0) -> np.ndarray:
    """要素**中心**(xi=eta=zeta=0)での B 行列 (6,24)。

    中心1点だけを見るのは意図した平滑化である。8つの角で評価すると、階段状境界の
    出隅・入隅が要素内でも尖り、拾いたくない離散化のスパイクを1段増やすことになる。
    trilinear hex では中心が最も精度の良い点でもある(検査2で梁理論と 0.2〜0.9% で一致)。
    """
    b = np.zeros((6, 24))
    grad = 0.25 * _NODE_SIGNS / float(element_size)
    for l in range(8):
        bx, by, bz = grad[l]
        b[0, 3 * l + 0] = bx
        b[1, 3 * l + 1] = by
        b[2, 3 * l + 2] = bz
        b[3, 3 * l + 0] = by
        b[3, 3 * l + 1] = bx
        b[4, 3 * l + 1] = bz
        b[4, 3 * l + 2] = by
        b[5, 3 * l + 0] = bz
        b[5, 3 * l + 2] = bx
    return b


def von_mises(tensor: np.ndarray) -> np.ndarray:
    """応力テンソル (..., 6) → von Mises 相当応力 (...)。"""
    sxx, syy, szz, sxy, syz, szx = (tensor[..., i] for i in range(6))
    return np.sqrt(
        0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
        + 3.0 * (sxy**2 + syz**2 + szx**2)
    )


def load_resultant(model: fem.Model) -> float:
    """荷重ベクトルの**合力**(ベクトル和)の大きさ(無次元)。

    **換算則の分母には使わない**(`load_magnitude_total` を使う)。自己釣り合いの荷重系で
    0になるので、実荷重[N]との対応が決まらないからである。診断・検査のために残してある。
    """
    f = model.f.reshape(-1, 3)
    return float(np.linalg.norm(f.sum(axis=0)))


def load_magnitude_total(model: fem.Model) -> float:
    """荷重ベクトルの**総量**(節点力の大きさの総和・無次元)。**換算則の分母はこれ**。

    合力(`load_resultant`)ではなく総和を取るのは、向きの違う荷重が同じ系に載りうるため。
    打ち消し合う組(面Aへ下向き50N・面Bへ上向き50N)では合力が0になり、それで割ると
    応力の実単位換算が壊れる —— 総和なら0にならない。

    **荷重が1つ(または全部同じ向き)のときは合力と厳密に一致する。** `anchor.load_vector` は
    `重み × 力ベクトル` を配るので全節点の力が平行になり、`Σ|f_i| = |Σf_i|` が成り立つ。
    したがってこの定義への切り替えで既存の1荷重の数値は1ビットも動かない
    (`check_stress.py` がこの一致を数字で押さえている)。

    上流の `anchor._assemble_load` が**同じ量で割って**無次元化しているので、通常経路の
    戻り値は 1.0 である。**2つの定義が食い違うと応力が黙って定数倍ずれる。**
    """
    f = model.f.reshape(-1, 3)
    return float(np.linalg.norm(f, axis=1).sum())


@dataclass(frozen=True)
class UnitScale:
    """無次元解 → 実単位の換算則。

    **応力は E に依存しない。** 剛性は E に比例し変位は 1/E に比例するので、
    σ = E ε で E が落ちる。`youngs_modulus_mpa` が効くのは変位の側だけで、
    ここに実材料の値を入れても安全率は動かない(検査3がこれを数字で押さえる)。
    """

    element_mm: float
    load_n: float
    youngs_modulus_mpa: float
    solved_load: float = 1.0
    solved_youngs_modulus: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "element_mm",
            "load_n",
            "youngs_modulus_mpa",
            "solved_load",
            "solved_youngs_modulus",
        ):
            value = float(getattr(self, name))
            if not value > 0.0 or not math.isfinite(value):
                raise ValueError(f"{name} は有限の正の値であること(受け取った値: {value!r})")

    @classmethod
    def for_model(
        cls,
        model: fem.Model,
        *,
        element_mm: float,
        load_n: float,
        youngs_modulus_mpa: float,
    ) -> "UnitScale":
        """解いた `Model` から無次元側(総荷重・E)を読んで換算則を作る。

        **こちらを既定の作り方にすること。** `solved_load` を手で書くと、荷重ベクトルの
        総量が 1.0 でない系(`anchor.load_vector` に `force` を渡した場合など)で応力が
        黙って定数倍ずれる。

        読むのは `load_magnitude_total`(節点力の大きさの総和)であって合力ではない ——
        **自己釣り合いの荷重系(向きの違う荷重が打ち消し合う)もここを通る**。
        荷重が1本のときは両者が一致するので、既存の数値は動かない。
        """
        resultant = load_magnitude_total(model)
        if not resultant > 0.0:
            raise ValueError(
                "荷重ベクトルが全て0で、実荷重[N]との対応が決まらない"
                "(荷重が1つも入っていない系。`UnitScale` を直接作って solved_load を明示すること)"
            )
        return cls(
            element_mm=float(element_mm),
            load_n=float(load_n),
            youngs_modulus_mpa=float(youngs_modulus_mpa),
            solved_load=resultant,
            solved_youngs_modulus=float(model.E),
        )

    @property
    def stress_factor(self) -> float:
        """無次元応力 → [MPa]。 F/F_nd / L^2。"""
        return (self.load_n / self.solved_load) / self.element_mm**2

    @property
    def displacement_factor(self) -> float:
        """無次元変位 → [mm]。 (F/F_nd)(E_nd/E)/L。"""
        return (
            (self.load_n / self.solved_load)
            * (self.solved_youngs_modulus / self.youngs_modulus_mpa)
            / self.element_mm
        )


@dataclass(frozen=True, eq=False)
class StressField:
    """要素ごとの応力。`unit` は `"1"`(無次元)か `"MPa"`。"""

    tensor: np.ndarray  # (nelx, nely, nelz, 6)
    von_mises: np.ndarray  # (nelx, nely, nelz)
    unit: str

    def scaled(self, scale: UnitScale) -> "StressField":
        """換算則を掛けて [MPa] にする。"""
        if self.unit != "1":
            raise ValueError(f"無次元の場にだけ掛けられる(いまの単位: {self.unit})")
        factor = scale.stress_factor
        return StressField(
            tensor=self.tensor * factor, von_mises=self.von_mises * factor, unit="MPa"
        )


def element_stress(model: fem.Model, u: np.ndarray) -> StressField:
    """変位 u から要素中心の応力を出す(無次元)。

    **materialの応力**(D は `model.E` そのまま)であって、SIMP の見かけの剛性
    `rho**penal * E` を掛けた等価応力ではない。中間密度の要素はひずみが大きく出るので
    この定義は**安全率を小さい側へ倒す**(安全側)。代表応力の規則が ρ<0.5 を捨てるので、
    採用値のところでは ρ≈1 であり、どちらの定義でも実質同じ値になる。
    """
    vec = np.asarray(u, dtype=float).ravel()
    if vec.size != model.ndof:
        raise ValueError(f"変位の長さが {vec.size}、期待は ndof={model.ndof}")
    db = elasticity_matrix(model.E, model.nu) @ strain_displacement_center()
    tensor = (vec[model.edof] @ db.T).reshape(model.nelx, model.nely, model.nelz, 6)
    return StressField(tensor=tensor, von_mises=von_mises(tensor), unit="1")


def solve_displacement(
    model: fem.Model,
    rho: np.ndarray,
    penal: float = 3.0,
    *,
    solver: str = "cg",
    cg_rtol: float = 1e-9,
    u0: np.ndarray | None = None,
) -> np.ndarray:
    """最終 ρ でもう一度 K U = F を解く。"""
    return model.solve(
        np.asarray(rho, dtype=float), penal, solver=solver, cg_rtol=cg_rtol, u0=u0
    )


@dataclass(frozen=True)
class RepresentativeStress:
    """代表応力と、それを選んだ規則。`raw_max` / `dense_max` は比較のために必ず持ち回る。"""

    value: float
    raw_max: float  # 全要素の素の max(ρ の低い要素を含む)
    dense_max: float  # ρ >= rho_min の中の max
    percentile: float
    rho_min: float
    element_count: int  # 代表値を取った母集団(ρ >= rho_min)の要素数
    unit: str


def representative_stress(
    field: StressField,
    rho: np.ndarray,
    *,
    rho_min: float = DEFAULT_RHO_MIN,
    percentile: float = DEFAULT_PERCENTILE,
) -> RepresentativeStress:
    """代表応力 = 「ρ >= rho_min の要素」の von Mises の上位パーセンタイル。

    **素の max を使わない。** 規則は2段で、どちらも実測に裏がある(docs/TOPOPT.md の表):

    1. ρ が低い要素を落とす —— そこの応力は物理的に意味が薄いうえ、実測では
       全要素の max が代表値の 13〜40倍まで跳ね、解像度を上げるほど大きくなる。
    2. 残った母集団の上位パーセンタイルを取る —— ρ>=0.5 に絞ってもなお max は
       階段状境界の角で発散する(1.54 → 1.86 → 2.09 倍、3段の細分で単調増加)。
       99.5% 点は同じ細分で 1.32 → 1.29 → 1.30 と動かない。

    母集団が空(どの要素も ρ < rho_min)なら落とす。**0 を返して安全率を無限大にしない。**
    """
    if not 0.0 <= rho_min < 1.0:
        raise ValueError(f"rho_min は [0, 1) であること(受け取った値: {rho_min!r})")
    if not 0.0 < percentile <= 100.0:
        raise ValueError(f"percentile は (0, 100] であること(受け取った値: {percentile!r})")
    values = np.asarray(field.von_mises, dtype=float)
    density = np.asarray(rho, dtype=float)
    if density.shape != values.shape:
        raise ValueError(f"ρ の形が {density.shape}、期待は {values.shape}")
    dense = density >= rho_min
    count = int(np.count_nonzero(dense))
    if count == 0:
        raise ValueError(
            f"ρ >= {rho_min} の要素が1つも無い(代表応力を取る母集団が空)。"
            f"最大の ρ は {float(density.max()) if density.size else float('nan'):.4g}"
        )
    picked = values[dense]
    # 非有限が混ざると percentile が黙って nan を返し、安全率が nan のまま下流へ流れる
    # (nan は比較が常に False なので「危険側」の判定にも引っかからない)
    bad = int(np.count_nonzero(~np.isfinite(picked)))
    if bad:
        raise ValueError(
            f"母集団の von Mises 応力に非有限値が {bad} 個ある(全 {count} 要素)。"
            "解が発散しているか、拘束・荷重の与え方が壊れている"
        )
    return RepresentativeStress(
        value=float(np.percentile(picked, percentile)),
        raw_max=float(values.max()),
        dense_max=float(picked.max()),
        percentile=float(percentile),
        rho_min=float(rho_min),
        element_count=count,
        unit=field.unit,
    )


@dataclass(frozen=True, eq=False)
class SafetyReport:
    """最小安全率と、その根拠になった数字ひとそろい。"""

    safety_factor: float
    allowable_mpa: float
    representative_mpa: float
    raw_max_mpa: float
    max_displacement_mm: float
    scale: UnitScale
    representative: RepresentativeStress
    field: StressField

    def summary(self) -> str:
        return (
            f"最小安全率 {self.safety_factor:.3f} "
            f"(許容 {self.allowable_mpa:.4g} MPa / 代表 {self.representative_mpa:.4g} MPa、"
            f"ρ>={self.representative.rho_min} の {self.representative.percentile}% 点。"
            f"素の max {self.raw_max_mpa:.4g} MPa は採らない)"
        )


def safety_factor(representative_mpa: float, allowable_mpa: float) -> float:
    """最小安全率 = 許容応力 / 代表応力。"""
    if not allowable_mpa > 0.0 or not math.isfinite(allowable_mpa):
        raise ValueError(f"許容応力は有限の正の値であること(受け取った値: {allowable_mpa!r})")
    if not representative_mpa > 0.0 or not math.isfinite(representative_mpa):
        raise ValueError(
            f"代表応力が {representative_mpa!r} で安全率を出せない"
            "(荷重が入っていないか、材料が荷重経路に無い)"
        )
    return float(allowable_mpa) / float(representative_mpa)


@trace.stage('stress')
def evaluate(
    model: fem.Model,
    rho: np.ndarray,
    *,
    scale: UnitScale,
    allowable_mpa: float,
    penal: float = 3.0,
    u: np.ndarray | None = None,
    solver: str = "cg",
    cg_rtol: float = 1e-9,
    rho_min: float = DEFAULT_RHO_MIN,
    percentile: float = DEFAULT_PERCENTILE,
) -> SafetyReport:
    """形(密度場)を渡すと最小安全率が返る、この段の入口。

    `u` を渡さなければ最終 ρ で解き直す。外側のループ(安全率 → 材料量の二分探索)は
    このタスクの範囲外で、ここが返す `safety_factor` を読む側にある。
    """
    density = np.asarray(rho, dtype=float)
    if density.shape != (model.nelx, model.nely, model.nelz):
        raise ValueError(
            f"ρ の形が {density.shape}、期待は {(model.nelx, model.nely, model.nelz)}"
        )
    displacement = (
        solve_displacement(model, density, penal, solver=solver, cg_rtol=cg_rtol)
        if u is None
        else np.asarray(u, dtype=float).ravel()
    )
    field = element_stress(model, displacement).scaled(scale)
    trace.snapshot('stress-displacement', displacement=displacement, scale=scale)
    rep = representative_stress(field, density, rho_min=rho_min, percentile=percentile)
    return SafetyReport(
        safety_factor=safety_factor(rep.value, allowable_mpa),
        allowable_mpa=float(allowable_mpa),
        representative_mpa=rep.value,
        raw_max_mpa=rep.raw_max,
        max_displacement_mm=float(np.abs(displacement).max()) * scale.displacement_factor,
        scale=scale,
        representative=rep,
        field=field,
    )
