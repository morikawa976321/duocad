"""密度場と変位から要素ごとの von Mises 応力・代表応力・最小安全率を出す(後処理)。

最適化そのものには手を入れない(応力制約トポロジー最適化は採らない)。出てきた形の応力を後から測る側だけを持つ。

変位は最終 ρ で `Model.solve()` をもう一度呼んで得る。結果オブジェクトに `u` を持たせる案は、`rho` を差し替えると
`u` が黙って古くなること、探索を通っていない ρ 場(受入検査の一様な棒・片持ち梁)でも動く必要があることから採らなかった。

無次元と実単位: `fem.Model` は `E=1.0`・要素サイズ 1 で解く。換算は後処理側の責任で、`UnitScale` が持つ
(docs/TOPOPT.md「応力の実単位への換算則」)。製品経路(`acceptance.safety_check`)は荷重を実単位[N]のまま
載せるので、`load_n=1.0`・`solved_load=1.0` として要素サイズだけを換算する。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from . import trace

from . import fem

# 代表応力の既定の規則。**素の max ではない。** 階段状境界の角にある1要素は離散化の産物で、
# そこで安全率が決まると解像度を上げるほど安全率が下がり続ける(docs/TOPOPT.md「代表応力の規則」)。
DEFAULT_RHO_MIN = 0.5
DEFAULT_PERCENTILE = 99.5
#: 部材域の規則で許す超過体積 V の一辺 = 最小幅 × これ(V = (最小幅 × これ)³)。最小幅に結びつけるのは「最小幅より細かい形は
#: 探索が作れない」という意味づけで、強度上の根拠ではない。0.5 は小型でピークを少し抑えるが hanger 1000N を入口で断った
#: (docs/TOPOPT_STRESS_GATE_DILUTION.md 6・10章)。
MEMBER_VOLUME_WIDTH_RATIO = 1.0

def strain_displacement_center(element_size: float = 1.0) -> np.ndarray:
    """要素**中心**(xi=eta=zeta=0)での B 行列 (6,24)。

    中心1点だけを見るのは意図した平滑化(8つの角で評価すると階段状境界の離散化のスパイクが増える)。
    trilinear hex では中心が最も精度の良い点でもある(梁理論と 0.2〜0.9% で一致)。
    """
    return fem.strain_displacement(0.25 * fem.NODE_SIGNS / float(element_size))


def von_mises(tensor: np.ndarray) -> np.ndarray:
    """応力テンソル (..., 6) → von Mises 相当応力 (...)。"""
    sxx, syy, szz, sxy, syz, szx = (tensor[..., i] for i in range(6))
    return np.sqrt(
        0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
        + 3.0 * (sxy**2 + syz**2 + szx**2)
    )


#: SIMP 補間の指数 `penal`。製品は二値の場しか解かない(値は効かない)が、**解くとき(`solve_displacement`)と
#: 検算するとき(`acceptance.safety_check` の残差・要素エネルギー)は同じ値でなければならない**。
PENAL = 3.0


def load_magnitude_total(model: fem.Model) -> float:
    """荷重ベクトルの**総量**(節点力の大きさの総和・無次元)。`UnitScale.for_model` の分母(検査専用)。

    合力ではなく総和を取るのは、打ち消し合う荷重の組(面Aへ下向き50N・面Bへ上向き50N)では合力が0になり、
    それで割ると実単位換算が壊れるため。荷重が1つ(または全部同じ向き)のときは合力と一致する。
    """
    f = model.f.reshape(-1, 3)
    return float(np.linalg.norm(f, axis=1).sum())


@dataclass(frozen=True)
class UnitScale:
    """無次元解 → 実単位の換算則。

    **応力は E に依存しない**(剛性は E に比例し変位は 1/E に比例するので σ = E ε で E が落ちる)。
    `youngs_modulus_mpa` が効くのは変位の側だけで、安全率は動かない。
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
        """解いた `Model` から無次元側(総荷重・E)を読んで換算則を作る(**検査専用**。`anchor.faces_matching` と同じ扱い)。

        製品(`acceptance.safety_check`)は荷重を実単位[N]のまま解くので、`UnitScale(..., solved_load=1.0)` を
        直接作る。`check_stress` / `check_fem` が無次元の荷重で解いた系に実荷重 `load_n` を割り当てる口で、
        `solved_load` は `load_magnitude_total`。
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

    **materialの応力**(D は `model.E` そのまま)であって、SIMP の見かけの剛性 `rho**penal * E` を掛けた
    等価応力ではない(中間密度の要素で安全率を小さい側へ倒す)。代表応力の規則が ρ<0.5 を捨てるので採用値では同じ。
    """
    vec = np.asarray(u, dtype=float).ravel()
    if vec.size != model.ndof:
        raise ValueError(f"変位の長さが {vec.size}、期待は ndof={model.ndof}")
    db = fem.elasticity_matrix(model.E, model.nu) @ strain_displacement_center()
    tensor = (vec[model.edof] @ db.T).reshape(model.nelx, model.nely, model.nelz, 6)
    return StressField(tensor=tensor, von_mises=von_mises(tensor), unit="1")


def solve_displacement(
    model: fem.Model,
    rho: np.ndarray,
    penal: float = PENAL,
    *,
    solver: str = "cg",
    cg_rtol: float = 1e-9,
    info: dict | None = None,
) -> np.ndarray:
    """最終 ρ でもう一度 K U = F を解く。`info` に dict を渡すと、実際に使った解法(`"auto"` の解決後)を
    `info['solver']` に入れる —— 解く前に入れるので、解けずに落ちたときも呼び出し側に残る。"""
    prepared = model.prepare(np.asarray(rho, dtype=float), penal, solver=solver)
    if info is not None:
        info["solver"] = prepared.solver
    return prepared.solve(model.f[None, :], cg_rtol=cg_rtol)[0]


@dataclass(frozen=True)
class RepresentativeStress:
    """代表応力と、それを選んだ規則。`raw_max` / `dense_max` は比較のために必ず持ち回る。"""

    value: float
    raw_max: float  # 全要素の素の max(ρ の低い要素を含む)
    dense_max: float  # ρ >= rho_min の中の max
    percentile: float
    rho_min: float
    element_count: int  # 代表値を取った母集団の要素数(パーセンタイルは ρ >= rho_min、部材域の規則は部材域)
    unit: str
    #: `"percentile"`(ρ >= rho_min の上位パーセンタイル)か `"member_volume"`(部材域の固定物理体積の上位値)。
    rule: str = "percentile"
    #: 部材域の規則で許した超過体積 V [mm³]。パーセンタイルの規則では `None`。
    volume_mm3: float | None = None


def representative_stress(
    field: StressField,
    rho: np.ndarray,
    *,
    rho_min: float = DEFAULT_RHO_MIN,
    percentile: float = DEFAULT_PERCENTILE,
    member_region: np.ndarray | None = None,
    member_volume_mm3: float | None = None,
    cell_mm3: float | None = None,
) -> RepresentativeStress:
    """代表応力 = 「ρ >= rho_min の要素」の von Mises の上位パーセンタイル。

    `member_volume_mm3` を渡すと、代わりに部材域 `member_region`(∧ ρ >= rho_min)の固定物理体積の上位値
    (`member_volume_stress`。`cell_mm3` は要素の体積)。部材域が空なら ρ >= rho_min の全体で取る。

    **素の max を使わない。** (1) ρ が低い要素を落とし、(2) 残りの上位パーセンタイルを取る。ρ>=0.5 に絞っても max は
    階段状境界の角で発散する(細分3段で 1.54 → 2.09 倍)が、99.5% 点は 1.32 → 1.30 で動かない
    (実測の表は docs/TOPOPT.md「代表応力の規則」)。

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
    if member_volume_mm3 is not None:
        if member_region is None or np.asarray(member_region).shape != values.shape or cell_mm3 is None:
            raise ValueError("部材域の規則には格子と同じ形の member_region と cell_mm3 が要る")
        region = np.asarray(member_region, dtype=bool) & dense
        value, population = member_volume_stress(values, region if region.any() else dense,
                                                 volume_mm3=float(member_volume_mm3), cell_mm3=float(cell_mm3))
        return RepresentativeStress(value=value, raw_max=float(values.max()), dense_max=float(picked.max()),
                                    percentile=float(percentile), rho_min=float(rho_min), element_count=population,
                                    unit=field.unit, rule="member_volume", volume_mm3=float(member_volume_mm3))
    return RepresentativeStress(
        value=float(np.percentile(picked, percentile)),
        raw_max=float(values.max()),
        dense_max=float(picked.max()),
        percentile=float(percentile),
        rho_min=float(rho_min),
        element_count=count,
        unit=field.unit,
    )


def member_volume_stress(values: np.ndarray, region: np.ndarray, *, volume_mm3: float, cell_mm3: float) -> tuple[float, int]:
    """部材域の固定物理体積の上位値: 「これを超える応力を持つ部材域の体積が `volume_mm3` 以下」になる最小の応力と、母集団の要素数。

    `Q_V ≤ σ` は「σ を超える部材域の体積 ≤ V」と同値で、部材域の外の材料をいくら足し引きしても値が変わらない
    (99.5% 点は材料全体の割合なので、遠くの低応力材料を残すほど高応力部が上位に隠れる。docs/TOPOPT_STRESS_GATE_DILUTION.md)。

    **意図的な変則**: 部材域の体積が V 以下なら、許容量を使わず部材域の最大を返す。そのまま数えると
    「部材域の全部が目標を超えても合格」になるため(小さい題材・粗い格子で起きる)。
    部材域が空なら母集団を材料全体(`values` の非 nan)に広げる —— 全部が保持か拘束・荷重面の1層という退化した入力。
    """
    if not (math.isfinite(volume_mm3) and volume_mm3 > 0.0 and math.isfinite(cell_mm3) and cell_mm3 > 0.0):
        raise ValueError(f"volume_mm3・cell_mm3 は有限の正の値であること(受け取った値: {volume_mm3!r}, {cell_mm3!r})")
    picked = np.asarray(values, dtype=float)[np.asarray(region, dtype=bool)]
    if picked.size == 0:
        raise ValueError("部材域が空")
    ordered = np.sort(picked)[::-1]
    allowed = int(math.floor(volume_mm3 / cell_mm3 + 1e-9))
    if allowed >= ordered.size:
        return float(ordered[0]), int(ordered.size)
    return float(ordered[allowed]), int(ordered.size)


def boundary_layer_cells(shape: tuple, node_ids: np.ndarray) -> np.ndarray:
    """`node_ids`(節点の通し番号、`fem` の規約)のどれかを角に持つ要素のマスク。

    要素中心から自分の角までは 0.87h、隣の要素だけの角までは 1.66h なので、「節点から h 以内の要素」と同じ。
    拘束面・荷重面の節点に接する1層を部材域から外すのに使う(そこの最大は細分で発散する。docs/TOPOPT_STRESS_GATE_DILUTION.md 3章)。
    """
    nx, ny, nz = (int(n) for n in shape)
    out = np.zeros((nx, ny, nz), dtype=bool)
    ids = np.asarray(node_ids, dtype=np.int64).ravel()
    if ids.size == 0:
        return out
    i, rest = np.divmod(ids, (ny + 1) * (nz + 1))
    j, k = np.divmod(rest, nz + 1)
    for di in (0, 1):
        for dj in (0, 1):
            for dk in (0, 1):
                ci, cj, ck = i - di, j - dj, k - dk
                ok = (ci >= 0) & (ci < nx) & (cj >= 0) & (cj < ny) & (ck >= 0) & (ck < nz)
                out[ci[ok], cj[ok], ck[ok]] = True
    return out


#: 露出面の面中心の自然座標。面の順は (-x, +x, -y, +y, -z, +z)。
_FACE_POINTS = ((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0), (0.0, 0.0, 1.0))


def _strain_displacement_at(xi: float, eta: float, zeta: float) -> np.ndarray:
    """一辺1の要素の自然座標 (xi, eta, zeta) での B 行列 (6,24)。中心(0,0,0)で `strain_displacement_center()` と一致する。"""
    s = fem.NODE_SIGNS
    grad = np.empty((8, 3))
    grad[:, 0] = 0.25 * s[:, 0] * (1.0 + s[:, 1] * eta) * (1.0 + s[:, 2] * zeta)
    grad[:, 1] = 0.25 * (1.0 + s[:, 0] * xi) * s[:, 1] * (1.0 + s[:, 2] * zeta)
    grad[:, 2] = 0.25 * (1.0 + s[:, 0] * xi) * (1.0 + s[:, 1] * eta) * s[:, 2]
    return fem.strain_displacement(grad)


def exposed_face_von_mises(model: fem.Model, u: np.ndarray, material: np.ndarray, scale: UnitScale,
                           cells: np.ndarray | None = None, *, all_faces: bool = False) -> np.ndarray:
    """材料の表面に出ている面(隣が空隙か格子の外)の面中心の von Mises [MPa]。形 (nx,ny,nz,6)、表面でない面と
    `cells` の外は nan。`all_faces` なら隣が材料の面も含めて6面すべてを出す(ピーク判定の基準線用)。

    要素中心の応力は曲げの表面応力を取りこぼす(片持ち梁の表面理論値に対し 2セル厚で 0.42。面中心なら 0.80。
    docs/TOPOPT_STRESS_GATE_DILUTION.md 10-1)。完成形検査の最大応力はこれで測る。
    """
    material = np.asarray(material, dtype=bool)
    if material.shape != (model.nelx, model.nely, model.nelz):
        raise ValueError(f"material の形が {material.shape}、期待は {(model.nelx, model.nely, model.nelz)}")
    target = material if cells is None else material & np.asarray(cells, dtype=bool)
    out = np.full(material.shape + (6,), np.nan)
    flat = np.flatnonzero(target.ravel())
    if flat.size == 0:
        return out
    vec = np.asarray(u, dtype=float).ravel()[model.edof[flat]]
    d = fem.elasticity_matrix(model.E, model.nu)
    padded = np.pad(material, 1, constant_values=False)
    idx = np.unravel_index(flat, material.shape)
    for f, point in enumerate(_FACE_POINTS):
        axis, step = f // 2, (-1 if f % 2 == 0 else 1)
        neighbour = [idx[a] + 1 for a in range(3)]
        neighbour[axis] = neighbour[axis] + step
        exposed = np.ones(flat.size, dtype=bool) if all_faces else ~padded[tuple(neighbour)]
        if not exposed.any():
            continue
        tensor = vec[exposed] @ (d @ _strain_displacement_at(*point)).T
        vm = von_mises(tensor) * scale.stress_factor
        column = out[..., f].reshape(-1)
        column[flat[exposed]] = vm
        out[..., f] = column.reshape(material.shape)
    return out


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
        rep = self.representative
        rule = (f"部材域の上位 {rep.volume_mm3:.4g} mm³ を除いた値" if rep.rule == "member_volume"
                else f"ρ>={rep.rho_min} の {rep.percentile}% 点")
        return (
            f"最小安全率 {self.safety_factor:.3f} "
            f"(許容 {self.allowable_mpa:.4g} MPa / 代表 {self.representative_mpa:.4g} MPa、{rule}。"
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
    u: np.ndarray,
    rho_min: float = DEFAULT_RHO_MIN,
    percentile: float = DEFAULT_PERCENTILE,
    member_region: np.ndarray | None = None,
    member_volume_mm3: float | None = None,
) -> SafetyReport:
    """形(密度場)とその変位 `u`(`solve_displacement` の解)を渡すと最小安全率が返る、この段の入口。

    `member_volume_mm3` を渡すと代表応力は部材域 `member_region` の固定物理体積の上位値(`member_volume_stress`)、
    渡さなければ ρ >= rho_min の上位パーセンタイル。
    """
    density = np.asarray(rho, dtype=float)
    if density.shape != (model.nelx, model.nely, model.nelz):
        raise ValueError(
            f"ρ の形が {density.shape}、期待は {(model.nelx, model.nely, model.nelz)}"
        )
    displacement = np.asarray(u, dtype=float).ravel()
    field = element_stress(model, displacement).scaled(scale)
    trace.snapshot('stress-displacement', displacement=displacement, scale=scale)
    member = {} if member_volume_mm3 is None else dict(member_region=member_region, member_volume_mm3=member_volume_mm3,
                                                       cell_mm3=scale.element_mm ** 3)
    rep = representative_stress(field, density, rho_min=rho_min, percentile=percentile, **member)
    return SafetyReport(
        safety_factor=safety_factor(rep.value, allowable_mpa),
        allowable_mpa=float(allowable_mpa),
        representative_mpa=rep.value,
        raw_max_mpa=rep.raw_max,
        # 節点変位の大きさ |(ux, uy, uz)| の最大(成分の最大は斜めの荷重で最大 √3 倍小さく出る)。
        max_displacement_mm=float(np.linalg.norm(displacement.reshape(-1, 3), axis=1).max())
        * scale.displacement_factor,
        scale=scale,
        representative=rep,
        field=field,
    )
