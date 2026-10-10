"""格子FEMの全量評価・ゾーン集計・断面力近似・Richardson外挿の共有ヘルパー。

`refine_shape.py` と `analytic_checks.py` が import する。製品の `acceptance` / `fem` / `stress` を
そのまま呼び、再実装しない。
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from duocad.topopt import acceptance, anchor, fem, stress


def _nan_result(status: str, checks, error: str | None, *, solver: str | None = None) -> dict:
    return dict(
        status=status,
        checks=checks,
        safety_factor=float("nan"),
        representative_mpa=float("nan"),
        raw_max_mpa=float("nan"),
        max_displacement_mm=float("nan"),
        relative_residual=float("nan"),
        von_mises_mpa=np.array([]),
        tensor_mpa=np.array([]),
        principal_mpa=np.array([]),
        density=np.array([]),
        reactions_n=np.zeros(3),
        solver_used=solver or "",
        error=error,
    )


def evaluate_full(
    material,
    grid,
    table,
    fixed_faces,
    load_faces,
    forces,
    *,
    allowable_mpa,
    youngs_modulus_mpa,
    percentile=stress.DEFAULT_PERCENTILE,
    solver="auto",
) -> dict:
    """境界条件から解き、要素応力・反力・変位まで返す(`acceptance.safety_check` より詳しい出口)。"""
    mat = np.asarray(material, dtype=bool)
    if mat.ndim != 3:
        raise ValueError(f"material は3次元 bool 配列であること(受け取った形: {mat.shape!r})")

    solve_info: dict = {}
    checks = None
    try:
        checks, fixed, load = acceptance.condition_check(
            mat, table, fixed_faces, load_faces, forces
        )
        if checks.get("status") != "pass":
            return _nan_result(
                "condition_check_failed",
                checks,
                "condition_check が pass しなかった",
                solver=solver,
            )

        physical_fixed = anchor.attached_dofs(mat, fixed)
        node_indices = np.array(
            np.unravel_index(physical_fixed // 3, tuple(n + 1 for n in mat.shape))
        ).T
        positions = node_indices - node_indices.mean(axis=0)
        rigid = np.zeros((len(physical_fixed), 6))
        for i, (position, dof) in enumerate(zip(positions, physical_fixed)):
            axis = dof % 3
            rigid[i, axis] = 1
            for rotation in range(3):
                rigid[i, 3 + rotation] = np.cross(np.eye(3)[rotation], position)[axis]
        if np.linalg.matrix_rank(rigid) < 6:
            return _nan_result(
                "solve_failed",
                checks,
                "剛体運動を拘束できない",
                solver=solve_info.get("solver", solver),
            )

        model = fem.Model(
            *mat.shape,
            fixed_dofs=fixed,
            load=load,
            extra_fixed_dofs=anchor.floating_dofs(mat),
            active_mask=mat,
        )
        density = mat.astype(float)
        displacement = stress.solve_displacement(model, density, solver=solver, info=solve_info)
        residual = model.stiffness_apply(density, stress.PENAL, displacement) - model.f
        relative = float(
            np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free])
        )
        if (
            not np.isfinite(displacement).all()
            or not math.isfinite(relative)
            or relative > fem.SOLVE_RESIDUAL_TOL
        ):
            return _nan_result(
                "solve_failed",
                checks,
                f"残差検算に失敗: {relative}",
                solver=solve_info.get("solver", solver),
            )

        scale = stress.UnitScale(grid.spacing, 1.0, youngs_modulus_mpa, solved_load=1.0)
        report = stress.evaluate(
            model,
            density,
            scale=scale,
            allowable_mpa=allowable_mpa,
            u=displacement,
            percentile=percentile,
        )
        field = report.field
        principal = principal_stresses(field.tensor)
        reactions = fem.reaction_forces(model, density, stress.PENAL, displacement)

        return dict(
            status="ok",
            checks=checks,
            safety_factor=float(report.safety_factor),
            representative_mpa=float(report.representative_mpa),
            raw_max_mpa=float(report.raw_max_mpa),
            max_displacement_mm=float(report.max_displacement_mm),
            relative_residual=relative,
            von_mises_mpa=np.asarray(field.von_mises, dtype=float),
            tensor_mpa=np.asarray(field.tensor, dtype=float),
            principal_mpa=principal,
            density=mat,
            reactions_n=np.asarray(reactions, dtype=float),
            solver_used=str(solve_info.get("solver", solver)),
            error=None,
            _model=model,
            _displacement=displacement,
            _density=density,
        )
    except Exception as exc:
        return _nan_result(
            "solve_failed",
            checks,
            f"{type(exc).__name__}: {exc}",
            solver=solve_info.get("solver", solver),
        )


def principal_stresses(tensor_mpa: np.ndarray) -> np.ndarray:
    """対称応力テンソル [xx,yy,zz,xy,yz,zx] → 主応力3成分(降順)。"""
    t = np.asarray(tensor_mpa, dtype=float)
    flat = t.reshape(-1, 6)
    mat = np.zeros((flat.shape[0], 3, 3), dtype=float)
    mat[:, 0, 0] = flat[:, 0]
    mat[:, 1, 1] = flat[:, 1]
    mat[:, 2, 2] = flat[:, 2]
    mat[:, 0, 1] = mat[:, 1, 0] = flat[:, 3]
    mat[:, 1, 2] = mat[:, 2, 1] = flat[:, 4]
    mat[:, 0, 2] = mat[:, 2, 0] = flat[:, 5]
    vals = np.linalg.eigvalsh(mat)[:, ::-1]
    return vals.reshape(t.shape[:-1] + (3,))


def _box_centroid(box: tuple) -> np.ndarray:
    lo, hi = box
    return np.array(
        [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2],
        dtype=float,
    )


def _point_aabb_distance(points: np.ndarray, box: tuple) -> np.ndarray:
    lo = np.asarray(box[0], dtype=float)
    hi = np.asarray(box[1], dtype=float)
    closest = np.clip(points, lo, hi)
    return np.linalg.norm(points - closest, axis=-1)


def _dist_points_to_segment(points: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """points (N,3), segment a→b."""
    ab = b - a
    denom = float(np.dot(ab, ab))
    if denom <= 0.0:
        return np.linalg.norm(points - a, axis=-1)
    t = np.clip(((points - a) @ ab) / denom, 0.0, 1.0)
    proj = a + t[:, None] * ab
    return np.linalg.norm(points - proj, axis=-1)


def zone_masks(
    material: np.ndarray,
    grid,
    *,
    fixed_world_boxes: list[tuple],
    load_world_boxes: list[tuple],
    distance_mm: float,
) -> dict:
    mat = np.asarray(material, dtype=bool)
    xx, yy, zz = np.meshgrid(
        grid.centers_1d(0), grid.centers_1d(1), grid.centers_1d(2), indexing="ij"
    )
    centers = np.stack([xx, yy, zz], axis=-1)
    flat_centers = centers.reshape(-1, 3)

    hold_dist = np.full(flat_centers.shape[0], np.inf)
    for box in fixed_world_boxes:
        hold_dist = np.minimum(hold_dist, _point_aabb_distance(flat_centers, box))
    holding = (hold_dist <= distance_mm).reshape(mat.shape) & mat

    segments: list[tuple[np.ndarray, np.ndarray]] = []
    fixed_c = [_box_centroid(b) for b in fixed_world_boxes]
    load_c = [_box_centroid(b) for b in load_world_boxes]
    for fc in fixed_c:
        if not load_c:
            continue
        dists = [np.linalg.norm(fc - lc) for lc in load_c]
        segments.append((fc, load_c[int(np.argmin(dists))]))
    for i in range(len(fixed_c)):
        for j in range(i + 1, len(fixed_c)):
            segments.append((fixed_c[i], fixed_c[j]))

    joint_dist = np.full(flat_centers.shape[0], np.inf)
    for a, b in segments:
        joint_dist = np.minimum(joint_dist, _dist_points_to_segment(flat_centers, a, b))
    joint = (joint_dist <= distance_mm).reshape(mat.shape) & mat & ~holding
    general = mat & ~holding & ~joint

    counts = dict(
        holding=int(holding.sum()),
        joint=int(joint.sum()),
        general=int(general.sum()),
        total=int(mat.sum()),
    )
    return dict(
        holding=holding,
        joint=joint,
        general=general,
        distance_mm=float(distance_mm),
        counts=counts,
    )


def zone_masks_with_load(
    material: np.ndarray,
    grid,
    *,
    fixed_world_boxes: list[tuple],
    load_world_boxes: list[tuple],
    distance_mm: float,
) -> dict:
    """`zone_masks`に荷重面近傍ゾーン`load`を足した4ゾーン版。`zone_masks`は3ゾーンのまま(02〜05が使う)。
    荷重パッド接触端のピークは3ゾーンでは`general`へ希釈される(10_SYNTHESIS 付録A(旧 03A_REFERENCE_REPAIR 7-1節))。
    優先順位はholding→load→joint→general(holdingと重なる小さい形状ではholdingが先)。
    """
    mat = np.asarray(material, dtype=bool)
    xx, yy, zz = np.meshgrid(
        grid.centers_1d(0), grid.centers_1d(1), grid.centers_1d(2), indexing="ij"
    )
    centers = np.stack([xx, yy, zz], axis=-1)
    flat_centers = centers.reshape(-1, 3)

    hold_dist = np.full(flat_centers.shape[0], np.inf)
    for box in fixed_world_boxes:
        hold_dist = np.minimum(hold_dist, _point_aabb_distance(flat_centers, box))
    holding = (hold_dist <= distance_mm).reshape(mat.shape) & mat

    load_dist = np.full(flat_centers.shape[0], np.inf)
    for box in load_world_boxes:
        load_dist = np.minimum(load_dist, _point_aabb_distance(flat_centers, box))
    load_zone = (load_dist <= distance_mm).reshape(mat.shape) & mat & ~holding

    segments: list[tuple[np.ndarray, np.ndarray]] = []
    fixed_c = [_box_centroid(b) for b in fixed_world_boxes]
    load_c = [_box_centroid(b) for b in load_world_boxes]
    for fc in fixed_c:
        if not load_c:
            continue
        dists = [np.linalg.norm(fc - lc) for lc in load_c]
        segments.append((fc, load_c[int(np.argmin(dists))]))
    for i in range(len(fixed_c)):
        for j in range(i + 1, len(fixed_c)):
            segments.append((fixed_c[i], fixed_c[j]))

    joint_dist = np.full(flat_centers.shape[0], np.inf)
    for a, b in segments:
        joint_dist = np.minimum(joint_dist, _dist_points_to_segment(flat_centers, a, b))
    joint = (joint_dist <= distance_mm).reshape(mat.shape) & mat & ~holding & ~load_zone
    general = mat & ~holding & ~load_zone & ~joint

    counts = dict(
        holding=int(holding.sum()),
        load=int(load_zone.sum()),
        joint=int(joint.sum()),
        general=int(general.sum()),
        total=int(mat.sum()),
    )
    return dict(
        holding=holding,
        load=load_zone,
        joint=joint,
        general=general,
        distance_mm=float(distance_mm),
        counts=counts,
    )


def zone_stats(
    von_mises_mpa: np.ndarray,
    principal_mpa: np.ndarray,
    zone_mask: np.ndarray,
    *,
    percentile=stress.DEFAULT_PERCENTILE,
) -> dict:
    mask = np.asarray(zone_mask, dtype=bool)
    cell_count = int(mask.sum())
    if cell_count == 0:
        return dict(
            cell_count=0,
            max_mpa=None,
            percentile_mpa=None,
            mean_mpa=None,
            peak_index=None,
            peak_principal_mpa=None,
        )
    vals = np.asarray(von_mises_mpa, dtype=float)[mask]
    peak_flat = int(np.argmax(vals))
    indices = np.argwhere(mask)
    peak_index = tuple(int(x) for x in indices[peak_flat])
    peak_principal = np.asarray(principal_mpa[peak_index], dtype=float).tolist()
    return dict(
        cell_count=cell_count,
        max_mpa=float(vals.max()),
        percentile_mpa=float(np.percentile(vals, percentile)),
        mean_mpa=float(vals.mean()),
        peak_index=peak_index,
        peak_principal_mpa=peak_principal,
    )


def section_force_moment(
    tensor_mpa: np.ndarray,
    material: np.ndarray,
    grid,
    *,
    axis: int,
    index_mm: float,
) -> dict | None:
    """要素中心応力のみの断面力・曲げモーメント近似(せん断応力場の再構成なし)。"""
    mat = np.asarray(material, dtype=bool)
    spacing = float(grid.spacing)
    i0 = int(math.floor((index_mm - grid.origin[axis]) / spacing))
    if i0 < 0 or i0 >= mat.shape[axis]:
        return None

    if axis == 0:
        slab_m = mat[i0, :, :]
        slab_t = tensor_mpa[i0, :, :]
        normal_comp = 0
        shear_idx = (3, 5)
        plane_axes = (1, 2)
    elif axis == 1:
        slab_m = mat[:, i0, :]
        slab_t = tensor_mpa[:, i0, :]
        normal_comp = 1
        shear_idx = (3, 4)
        plane_axes = (0, 2)
    else:
        slab_m = mat[:, :, i0]
        slab_t = tensor_mpa[:, :, i0]
        normal_comp = 2
        shear_idx = (4, 5)
        plane_axes = (0, 1)

    if not np.any(slab_m):
        return None

    area = spacing ** 2
    sigma_n = slab_t[..., normal_comp]
    normal_force = float(sigma_n[slab_m].sum() * area)

    tau_a = slab_t[..., shear_idx[0]][slab_m].sum() * area
    tau_b = slab_t[..., shear_idx[1]][slab_m].sum() * area

    c0 = grid.centers_1d(plane_axes[0])
    c1 = grid.centers_1d(plane_axes[1])
    if slab_m.ndim != 2:
        raise ValueError("internal: slab mask must be 2d")
    p0, p1 = np.meshgrid(c0, c1, indexing="ij")
    m0 = p0[slab_m]
    m1 = p1[slab_m]
    r0_bar = float(m0.mean())
    r1_bar = float(m1.mean())
    m0_m = float((sigma_n[slab_m] * (m0 - r0_bar)).sum() * area)
    m1_m = float((sigma_n[slab_m] * (m1 - r1_bar)).sum() * area)

    slice_center_mm = float(grid.origin[axis] + spacing * (i0 + 0.5))
    return dict(
        index_mm=slice_center_mm,
        cell_count=int(slab_m.sum()),
        normal_force_n=normal_force,
        shear_force_n=[float(tau_a), float(tau_b)],
        bending_moment_n_mm=[m0_m, m1_m],
        centroid_mm=[r0_bar, r1_bar],
    )


def richardson_extrapolate(values: list[float], ratios: list[float]) -> dict | None:
    if len(values) < 2:
        return None
    order_estimates: list[float | None] = []
    extrapolated: list[float | None] = []
    n = len(values)
    for i in range(n - 2):
        # **ratios[i] は values[i]→values[i+1] 間の細分比(呼び出し側の規約: run_bending の
        # `ratios.append(prev_el / element_mm)` と同じ並び)。d0 も同じ2点の差なので、
        # d0とペアにする比は ratios[i](1つ後ろのratios[i+1]ではない)——QAで指摘された
        # 「d1側の比をd0/d1の対数比に使っていた」ずれを修正。比が全区間で一定でない場合
        # (このモジュールの呼び出し元は等比細分を保証しない)、この3点式の次数推定は近似に
        # なる(docstringのとおり「収束していない/発散気味の列で無意味な外挿値を出さない」
        # ための閾値チェックはそのまま機能する)。
        ratio = ratios[i] if i < len(ratios) else ratios[-1]
        d0 = abs(values[i + 1] - values[i])
        d1 = abs(values[i + 2] - values[i + 1])
        if d0 <= 0.0 or d1 <= 0.0 or ratio <= 0.0:
            order_estimates.append(None)
            extrapolated.append(None)
            continue
        p = math.log(d0 / d1) / math.log(ratio)
        if not math.isfinite(p) or p <= 0.0 or p >= 6.0:
            order_estimates.append(None)
            extrapolated.append(None)
            continue
        order_estimates.append(p)
        denom = ratio ** p - 1.0
        if abs(denom) < 1e-30:
            extrapolated.append(None)
            continue
        v_inf = values[i + 2] + (values[i + 2] - values[i + 1]) / denom
        extrapolated.append(float(v_inf) if math.isfinite(v_inf) else None)
    while len(order_estimates) < max(0, n - 2):
        order_estimates.append(None)
    while len(extrapolated) < max(0, n - 2):
        extrapolated.append(None)
    return dict(order_estimates=order_estimates, extrapolated=extrapolated)


def _richardson_rhs(p: float, ratio_a: float, ratio_b: float) -> float:
    rb = ratio_b ** p
    ra = ratio_a ** p
    denom = rb - 1.0
    if abs(denom) < 1e-30:
        return float("nan")
    return rb * (ra - 1.0) / denom


def _richardson_solve_order(
    d0: float, d1: float, ratio_a: float, ratio_b: float
) -> float | None:
    if d0 * d1 <= 0.0:
        return None
    target = d0 / d1
    if target <= 0.0:
        return None

    p_lo, p_hi = 1e-3, 10.0
    rhs_lo = _richardson_rhs(p_lo, ratio_a, ratio_b)
    rhs_hi = _richardson_rhs(p_hi, ratio_a, ratio_b)
    if not math.isfinite(rhs_lo) or not math.isfinite(rhs_hi):
        return None
    f_lo = target - rhs_lo
    f_hi = target - rhs_hi
    if f_lo * f_hi > 0.0:
        return None

    for _ in range(200):
        p_mid = (p_lo + p_hi) / 2.0
        rhs_mid = _richardson_rhs(p_mid, ratio_a, ratio_b)
        if not math.isfinite(rhs_mid):
            return None
        f_mid = target - rhs_mid
        if abs(f_mid) < 1e-10:
            return p_mid
        if f_lo * f_mid <= 0.0:
            p_hi = p_mid
            f_hi = f_mid
        else:
            p_lo = p_mid
            f_lo = f_mid
    return (p_lo + p_hi) / 2.0


def richardson_extrapolate_general(values: list[float], spacings: list[float]) -> dict | None:
    if len(values) < 2:
        return None
    if len(spacings) != len(values):
        raise ValueError("values と spacings の長さが一致すること")
    order_estimates: list[float | None] = []
    extrapolated: list[float | None] = []
    window_diagnostics: list[dict] = []
    n = len(values)
    for i in range(n - 2):
        h1, h2, h3 = spacings[i], spacings[i + 1], spacings[i + 2]
        if h2 <= 0.0 or h3 <= 0.0 or h1 <= 0.0:
            raise ValueError("spacing は正であること")
        ratio_a = h1 / h2
        ratio_b = h2 / h3
        if ratio_a <= 1.0 or ratio_b <= 1.0:
            raise ValueError("spacings は粗い順(各隣接比>1)であること")
        d0 = values[i] - values[i + 1]
        d1 = values[i + 1] - values[i + 2]
        diag: dict = dict(
            ratio_a=float(ratio_a),
            ratio_b=float(ratio_b),
            converged_no_extrapolation_needed=False,
            no_solution=False,
        )
        if abs(d0) < 1e-12 or abs(d1) < 1e-12:
            diag["converged_no_extrapolation_needed"] = True
            order_estimates.append(None)
            extrapolated.append(None)
            window_diagnostics.append(diag)
            continue
        target = d0 / d1
        rhs_lo = _richardson_rhs(1e-3, ratio_a, ratio_b)
        if not math.isfinite(rhs_lo) or (target > 0.0) != (rhs_lo > 0.0):
            diag["no_solution"] = True
            order_estimates.append(None)
            extrapolated.append(None)
            window_diagnostics.append(diag)
            continue
        p = _richardson_solve_order(d0, d1, ratio_a, ratio_b)
        if p is None or not math.isfinite(p) or p <= 0.0:
            diag["no_solution"] = True
            order_estimates.append(None)
            extrapolated.append(None)
            window_diagnostics.append(diag)
            continue
        order_estimates.append(float(p))
        denom = ratio_b ** p - 1.0
        if abs(denom) < 1e-30:
            extrapolated.append(None)
        else:
            v_inf = values[i + 2] + (values[i + 2] - values[i + 1]) / denom
            extrapolated.append(float(v_inf) if math.isfinite(v_inf) else None)
        window_diagnostics.append(diag)
    while len(order_estimates) < max(0, n - 2):
        order_estimates.append(None)
    while len(extrapolated) < max(0, n - 2):
        extrapolated.append(None)
    return dict(
        order_estimates=order_estimates,
        extrapolated=extrapolated,
        window_diagnostics=window_diagnostics,
    )


def face_world_boxes(table, faces: Sequence[int]) -> list[tuple]:
    """拘束/荷重面の節点座標からワールドAABBを作る。"""
    boxes = []
    for face in faces:
        pos = np.asarray(table.positions(face), dtype=float)
        if pos.size == 0:
            continue
        lo = pos.min(axis=0)
        hi = pos.max(axis=0)
        boxes.append((tuple(lo), tuple(hi)))
    return boxes


def per_face_reactions(probe_result: dict, table, fixed_faces: Sequence[int]) -> dict:
    """`evaluate_full` の内部モデルから支持面ごとの反力合力 (3,) を出す。"""
    if probe_result.get("status") != "ok":
        return {}
    model = probe_result["_model"]
    u = probe_result["_displacement"]
    rho = probe_result["_density"]
    residual = model.stiffness_apply(rho, stress.PENAL, u) - model.f
    out: dict = {}
    for face in fixed_faces:
        dofs = table.fixed_dofs([face])
        dofs = anchor.attached_dofs(probe_result["density"], dofs)
        if dofs.size == 0:
            out[str(face)] = [0.0, 0.0, 0.0]
            continue
        rf = np.bincount(dofs % 3, weights=residual[dofs], minlength=3)
        out[str(face)] = rf.tolist()
    return out
