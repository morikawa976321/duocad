"""解析解(一様引張・片持ち曲げ・Kirsch)とボクセルFEMの独立比較CLI。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from _documents import open_session  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import design_space as ds, fem, material, stress  # noqa: E402
from build_compact_models import (  # noqa: E402
    DILUTION_BOSS_D, DILUTION_BOSS_H, DILUTION_BOSS_W, DILUTION_HOLE_R,
)

import _common  # noqa: E402
import _fem_probe  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"

TENSION_LOAD_N = 1000.0
BENDING_LOAD_N = 200.0
PHYS_TENSION_MM = 30.0
BEND_LENGTH_MM = 100.0
BEND_WIDTH_MM = 10.0
BEND_HEIGHT_MM = 20.0
BEND_PROBE_X_MM = 20.0  # 固定端からの応力測定位置(物理mm固定、②のコメント参照)

# Peterson: finite-width plate, central hole, uniaxial tension (2r/W 補正)。
KT_FINITE_WIDTH_COEF = (3.0, -3.14, 3.667, -1.527)


def _node_index(i: int, j: int, k: int, nely: int, nelz: int) -> int:
    ny1 = nely + 1
    nz1 = nelz + 1
    return i * ny1 * nz1 + j * nz1 + k


def _face_node_weight(j: int, k: int, nely: int, nelz: int) -> float:
    on_j_edge = j == 0 or j == nely
    on_k_edge = k == 0 or k == nelz
    if on_j_edge and on_k_edge:
        return 0.25
    if on_j_edge or on_k_edge:
        return 0.5
    return 1.0


def _tension_fixed_load(nelx: int, nely: int, nelz: int, load_n: float) -> tuple[np.ndarray, np.ndarray]:
    ny1 = nely + 1
    nz1 = nelz + 1
    ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
    fixed = []
    for j in range(nely + 1):
        for k in range(nelz + 1):
            n = _node_index(0, j, k, nely, nelz)
            fixed.extend([3 * n, 3 * n + 1, 3 * n + 2])
    weights = []
    load_nodes = []
    for j in range(nely + 1):
        for k in range(nelz + 1):
            w = _face_node_weight(j, k, nely, nelz)
            weights.append(w)
            load_nodes.append(_node_index(nelx, j, k, nely, nelz))
    wsum = sum(weights)
    f = np.zeros(ndof)
    for n, w in zip(load_nodes, weights):
        f[3 * n] += load_n * w / wsum
    return np.unique(fixed).astype(np.int64), f


def run_tension(levels: list[int]) -> list[dict]:
    E_mpa = material.DEFAULT_YOUNGS_MODULUS_MPA
    records = []
    for level in levels:
        # **同じ物理形状(30mm立方)を保ったまま3軸とも同じ比率で細分する**——`fem.Model`は等方格子
        # (1つのelement_mmをx/y/z共通で使う)なので、1軸だけ要素数を増やすと他の2軸の物理寸法が
        # 動いてしまう(level間で理論値まで変わり、収束比較にならない実装ミスをここで避けている)。
        nelx = nely = nelz = 6 * level
        element_mm = PHYS_TENSION_MM / nelx
        fixed, load = _tension_fixed_load(nelx, nely, nelz, TENSION_LOAD_N)
        model = fem.Model(
            nelx, nely, nelz, fixed_dofs=fixed, load=load, active_mask=np.ones((nelx, nely, nelz), bool)
        )
        rho = np.ones((nelx, nely, nelz))
        info: dict = {}
        u = stress.solve_displacement(model, rho, solver="auto", info=info)
        residual = model.stiffness_apply(rho, stress.PENAL, u) - model.f
        rel = float(np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free]))
        field = stress.element_stress(model, u).scaled(
            stress.UnitScale(element_mm, 1.0, E_mpa, solved_load=1.0)
        )
        i_mid = nelx // 2
        slice_vm = field.von_mises[i_mid, :, :]
        slice_xx = field.tensor[i_mid, :, :, 0]
        sigma_theory = TENSION_LOAD_N / (nely * element_mm * nelz * element_mm)
        rel_err = abs(float(slice_xx.mean()) - sigma_theory) / sigma_theory
        reactions = fem.reaction_forces(model, rho, stress.PENAL, u)
        records.append(
            dict(
                level=level,
                element_mm=element_mm,
                sigma_xx_mean_mpa=float(slice_xx.mean()),
                sigma_xx_center_slice_mpa=float(slice_xx.mean()),
                von_mises_p99_5_mpa=float(np.percentile(slice_vm, 99.5)),
                sigma_theory_mpa=sigma_theory,
                relative_error=rel_err,
                residual=rel,
                reaction_sum_n=reactions.tolist(),
            )
        )
    return records


def _bending_fixed_load(
    nelx: int, nely: int, nelz: int, load_n: float
) -> tuple[np.ndarray, np.ndarray]:
    ny1 = nely + 1
    nz1 = nelz + 1
    ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
    fixed = []
    for j in range(nely + 1):
        for k in range(nelz + 1):
            n = _node_index(0, j, k, nely, nelz)
            fixed.extend([3 * n, 3 * n + 1, 3 * n + 2])
    weights = []
    nodes = []
    for j in range(nely + 1):
        n = _node_index(nelx, j, nelz, nely, nelz)
        w = 0.5 if j in (0, nely) else 1.0
        weights.append(w)
        nodes.append(n)
    wsum = sum(weights)
    f = np.zeros(ndof)
    for n, w in zip(nodes, weights):
        f[3 * n + 2] -= load_n * w / wsum
    return np.unique(fixed).astype(np.int64), f


def run_bending(levels: list[int]) -> list[dict]:
    E_mpa = material.DEFAULT_YOUNGS_MODULUS_MPA
    I = BEND_WIDTH_MM * BEND_HEIGHT_MM ** 3 / 12.0
    # **曲げモーメントの腕は測定位置までの距離**(固定端までのBEND_LENGTH_MMではない)。
    # 測定点をBEND_PROBE_X_MMだけ固定端から離しているので、その位置でのモーメントは
    # 荷重×(全長−BEND_PROBE_X_MM)(=自由端から測定点までの距離)。固定端の値(全長を腕にした値)を
    # 使うと、測定点をわざと固定端から離した意味が無くなり理論値が実際より過大になる。
    moment_arm_mm = BEND_LENGTH_MM - BEND_PROBE_X_MM
    sigma_theory = BENDING_LOAD_N * moment_arm_mm * (BEND_HEIGHT_MM / 2.0) / I
    tip_theory = BENDING_LOAD_N * BEND_LENGTH_MM ** 3 / (3.0 * E_mpa * I)
    records = []
    stress_levels = []
    defl_levels = []
    for level in levels:
        # 基準比 length:width:height = 100:10:20 = 10:1:2 に要素数を揃える(等方格子なので
        # tension側と同じ理由で3軸とも同じ比率で細分しないと物理寸法がlevelごとに動く。
        # 旧版はnely=2固定・nelz=4*levelで、level=1で既に幅20mm・高さ40mm相当になり
        # BEND_WIDTH_MM/BEND_HEIGHT_MMの理論値前提〈10mm/20mm〉と食い違っていた)。
        nelx = 10 * level
        nely = 1 * level
        nelz = 2 * level
        element_mm = BEND_LENGTH_MM / nelx
        fixed, load = _bending_fixed_load(nelx, nely, nelz, BENDING_LOAD_N)
        model = fem.Model(
            nelx, nely, nelz, fixed_dofs=fixed, load=load, active_mask=np.ones((nelx, nely, nelz), bool)
        )
        rho = np.ones((nelx, nely, nelz))
        info: dict = {}
        u = stress.solve_displacement(model, rho, solver="auto", info=info)
        residual = model.stiffness_apply(rho, stress.PENAL, u) - model.f
        rel = float(np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free]))
        scale = stress.UnitScale(element_mm, 1.0, E_mpa, solved_load=1.0)
        field = stress.element_stress(model, u).scaled(scale)
        # **固定端からの物理距離(mm)を揃えて測る**——index=2固定だと、細分するほどelement_mmが
        # 縮んで測定点が固定端(局所3次元効果の領域)へ寄っていき、Euler-Bernoulli理論(遠方の純曲げ
        # を仮定)との比較が細分するほど不利になる(level間で比較の物理位置がずれる実装ミスを
        # ここで避けている)。BEND_PROBE_X_MMはlevel=1のindex=2(=20mm)と同じ物理位置に揃えてある。
        i_probe = min(max(1, round(BEND_PROBE_X_MM / element_mm)), nelx - 1)
        probe_x_actual_mm = (i_probe + 0.5) * element_mm
        probe_z_half_actual_mm = BEND_HEIGHT_MM / 2.0 - element_mm / 2.0
        moment_arm_actual_mm = BEND_LENGTH_MM - probe_x_actual_mm
        position_correction_invalid = probe_z_half_actual_mm <= 0.0
        if position_correction_invalid:
            sigma_theory_position_corrected = None
            relative_error_stress_position_corrected = None
        else:
            sigma_theory_position_corrected = (
                BENDING_LOAD_N * moment_arm_actual_mm * probe_z_half_actual_mm / I
            )
            relative_error_stress_position_corrected = None
        top = field.tensor[i_probe, :, -1, 0]
        bot = field.tensor[i_probe, :, 0, 0]
        sigma_fem = float(max(abs(top.max()), abs(bot.min())))
        if sigma_theory_position_corrected is not None:
            relative_error_stress_position_corrected = (
                abs(sigma_fem - sigma_theory_position_corrected)
                / sigma_theory_position_corrected
            )
        stress_levels.append(sigma_fem)
        ny1 = nely + 1
        nz1 = nelz + 1
        tip_n = _node_index(nelx, nely // 2, nelz, nely, nelz)
        tip_defl = float(u[3 * tip_n + 2] * scale.displacement_factor)
        defl_levels.append(abs(tip_defl))
        records.append(
            dict(
                level=level,
                element_mm=element_mm,
                sigma_max_fem_mpa=sigma_fem,
                sigma_max_theory_mpa=sigma_theory,
                relative_error_stress=abs(sigma_fem - sigma_theory) / sigma_theory,
                sigma_theory_position_corrected_mpa=sigma_theory_position_corrected,
                relative_error_stress_position_corrected=relative_error_stress_position_corrected,
                probe_x_actual_mm=float(probe_x_actual_mm),
                probe_z_half_actual_mm=float(probe_z_half_actual_mm),
                position_correction_invalid=position_correction_invalid,
                tip_deflection_fem_mm=tip_defl,
                tip_deflection_theory_mm=tip_theory,
                relative_error_deflection=abs(abs(tip_defl) - tip_theory) / tip_theory,
                residual=rel,
            )
        )
    element_mm_list = [rec["element_mm"] for rec in records]
    rich_stress = (
        _fem_probe.richardson_extrapolate_general(stress_levels, element_mm_list)
        if len(stress_levels) >= 2
        else None
    )
    rich_defl = (
        _fem_probe.richardson_extrapolate_general(defl_levels, element_mm_list)
        if len(defl_levels) >= 2
        else None
    )
    for i, rec in enumerate(records):
        rec["richardson_stress"] = rich_stress
        rec["richardson_deflection"] = rich_defl
        rec["note"] = (
            "hex1次の曲げは粗格子で理論から外れうる(せん断ロッキング等)。収束しないだけでは実装バグとは限らない。"
        )
    return records


def _kt_finite_width(two_r: float, width: float) -> float:
    x = two_r / width
    return KT_FINITE_WIDTH_COEF[0] + sum(c * x ** (i + 1) for i, c in enumerate(KT_FINITE_WIDTH_COEF[1:]))


def run_kirsch(shape: str, element_mm: float) -> dict | None:
    if shape == "path":
        return dict(
            shape=shape,
            status="not_applicable",
            note="path 小型模型に円孔が無いため Kirsch 比較は該当なし",
        )
    model_path = REPO / "models" / "topopt_idle_dilution_small.json"
    doc = open_session(model_path).document
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=element_mm)
    mask = np.asarray(space.mask, dtype=bool)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(
        doc,
        space,
        bundle,
        optimize_run.OptimizeOptions(element_size_mm=element_mm),
        table=table,
        mapping=mapping,
    )
    probe = _fem_probe.evaluate_full(
        mask,
        space.grid,
        table,
        cond.fixed_faces,
        cond.load_faces,
        cond.forces,
        allowable_mpa=cond.allowable_mpa,
        youngs_modulus_mpa=cond.youngs_modulus_mpa,
        percentile=cond.percentile or stress.DEFAULT_PERCENTILE,
    )
    if probe["status"] != "ok":
        return dict(shape=shape, status="failed", error=probe.get("error"), probe_status=probe["status"])

    vm = probe["von_mises_mpa"]
    grid = space.grid
    hole_r = DILUTION_HOLE_R
    boss_w = DILUTION_BOSS_W
    # **穴は Z 軸に沿って貫通する縦穴**(`build_compact_models._drill_through`がXYスケッチを
    # Z方向へ押し出す/切り取る)。穴の中心は台座の平面座標 (boss_w/2, boss_d/2) に固定(層をまたぐ
    # 契約: 座標はbuild_compact_models.pyのジオメトリ定数そのもの、格子から逆算しない——`keep`セルを
    # Y-Z平面へ集約して中心を推定する旧実装は、穴の実際の軸(X-Y平面内)と違う平面を見ており、
    # 台座から遠く離れた位置(荷重パッド付近)を「穴の近く」と誤認してkt_femが3桁の異常値になった)。
    hole_x = DILUTION_BOSS_W / 2.0
    hole_y = DILUTION_BOSS_D / 2.0
    xx = grid.centers_1d(0)
    yy_axis = grid.centers_1d(1)
    zz_axis = grid.centers_1d(2)
    far_i = int(np.argmin(np.abs(xx - (boss_w - 2.0))))
    far_slice = vm[far_i, :, :]
    von_avg = float(far_slice[mask[far_i, :, :]].mean()) if np.any(mask[far_i, :, :]) else float("nan")
    load_n = float(np.linalg.norm(cond.forces[0])) if len(cond.forces) else 300.0
    sigma_load = load_n / (boss_w * boss_w)
    if von_avg > 0 and sigma_load > 0:
        if von_avg <= sigma_load:
            sigma_nominal, method = von_avg, "far_field_von_mises_mean"
        else:
            sigma_nominal, method = sigma_load, "load_over_area"
    else:
        sigma_nominal, method = von_avg, "far_field_von_mises_mean"

    # 穴表面のすぐ外側の薄い環(半径 hole_r〜1.5*hole_r)を X-Y 平面で取り、台座の厚み
    # (0<=z<=DILUTION_BOSS_H)に制限する(荷重パッド側の応力を「穴の近く」に混ぜない)。
    xx_grid, yy_grid = np.meshgrid(xx, yy_axis, indexing="ij")
    radial = np.sqrt((xx_grid - hole_x) ** 2 + (yy_grid - hole_y) ** 2)
    near_xy = (radial >= hole_r * 0.95) & (radial <= hole_r * 1.5)
    z_in_boss = (zz_axis >= 0.0) & (zz_axis <= DILUTION_BOSS_H)
    near = near_xy[:, :, None] & z_in_boss[None, None, :] & mask
    if not np.any(near):
        kt_fem = float("nan")
        peak_loc = None
    else:
        vm_masked = np.where(near, vm, -1.0)
        peak_idx = np.unravel_index(int(np.argmax(vm_masked)), vm.shape)
        kt_fem = float(vm[peak_idx] / sigma_nominal) if sigma_nominal > 0 else float("nan")
        peak_loc = [float(xx[peak_idx[0]]), float(yy_axis[peak_idx[1]]), float(zz_axis[peak_idx[2]])]

    kt_inf = 3.0
    kt_fin = _kt_finite_width(2.0 * hole_r, boss_w)
    note = (
        "同軸引張のKirsch/Peterson近似とFEMの比較。実際の拘束は円筒面固定・荷重は-Zで、"
        "曲げ成分が混じるため過大な確信を持たないこと。"
    )
    return dict(
        element_mm=element_mm,
        sigma_nominal_mpa=sigma_nominal,
        sigma_nominal_method=method,
        kt_theory_finite_width=kt_fin,
        kt_theory_infinite=kt_inf,
        kt_fem=kt_fem,
        relative_error_vs_finite_width=abs(kt_fem - kt_fin) / kt_fin if kt_fin > 0 else None,
        peak_location_mm=peak_loc,
        note=note,
        status="ok",
    )


def _print_table(title: str, rows: list[dict], keys: list[str]) -> None:
    print(title)
    hdr = " ".join(f"{k:>18}" for k in keys)
    print(hdr)
    for row in rows:
        print(" ".join(f"{str(row.get(k, '')):>18}"[:18] for k in keys))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_t = sub.add_parser("tension")
    p_t.add_argument("--levels", nargs="+", type=int, default=[1, 2, 3, 4])
    p_b = sub.add_parser("bending")
    p_b.add_argument("--levels", nargs="+", type=int, default=[1, 2, 3, 4])
    p_k = sub.add_parser("kirsch")
    p_k.add_argument("--shape", choices=["dilution", "path"], required=True)
    p_k.add_argument("--element-mm", type=float, default=2.0)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if args.cmd == "tension":
        records = run_tension(args.levels)
        payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(), records=records)
        out = OUT / "analytic_tension.json"
        _common.write_atomic(out, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
        _print_table(
            "tension",
            records,
            ["level", "element_mm", "sigma_xx_mean_mpa", "sigma_theory_mpa", "relative_error", "residual"],
        )
    elif args.cmd == "bending":
        records = run_bending(args.levels)
        payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(), records=records)
        out = OUT / "analytic_bending.json"
        _common.write_atomic(out, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
        _print_table(
            "bending",
            records,
            ["level", "sigma_max_fem_mpa", "sigma_max_theory_mpa", "tip_deflection_fem_mm", "relative_error_stress"],
        )
    else:
        record = run_kirsch(args.shape, args.element_mm)
        payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(), record=record)
        # element_mmをファイル名に含める(細分レベルを変えて複数回呼ぶことを想定——同名上書きだと
        # kt_femの格子依存(細分するほど発散する階段角アーチファクト)の記録が直前の1回だけになる)。
        name = f"analytic_kirsch_{args.shape}_{args.element_mm:g}mm.json"
        out = OUT / name
        _common.write_atomic(out, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
        print(json.dumps(record, ensure_ascii=False, indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
