"""タスク7(hanger高荷重・幅の一般性・旧64条件の照合)が共有する条件定義と診断。

結果と解釈は `docs/topopt_idle_validation/07_HANGER_LOADS.md`。製品コード(`duocad.topopt.*`)は変更しない。
`search_methods.py`・`synthetic_matrix_support.py`・`hanger_low_support.py` を再利用する。

最小幅は**実寸mm固定**(06と同じ): 要求mmを `optimize_run._resolve_min_width` が `ceil(mm/格子幅)` セルへ丸めるので、
実現幅は格子ごとに違う。比較表には要求mmと実現mmの両方を載せ、旧64条件(セル数固定)と混ぜない。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from duocad.topopt import anchor, binary_search, fem, stress  # noqa: E402

import _common  # noqa: E402
import _fem_probe  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import search_methods as sm  # noqa: E402

HANGER_MODEL = hls.HANGER_MODEL
OUT = hls.OUT
MASK_DIR = OUT / "07_masks"
LOCK = hls.LOCK  # 04〜06と単一FEM実行ロックを共有する(1実行ずつ)

LOADS_N = (500.0, 1000.0, 2000.0)
GRIDS_MM = (1.8, 2.0, 2.2, 2.4)
#: 細い幅の要求値。最も粗い格子(2.4mm→実際は2.3875mm)の下限 2×格子幅=4.775mm 以上で、全格子で3セルに丸まる(5.4〜7.2mm)。
#: 太い幅は06のH1と同じ10mm。いずれも要求mmで、実現mmは格子ごとに変わる。
WIDTHS_MM = {"thin": 5.0, "thick": 10.0}

#: 低寄与材料の判定しきい値(解析者が置いた値で、物理的根拠はない。未校正)。目標許容応力(許容/目標SF)に対する比。
LOW_STRESS_RATIOS = (0.05, 0.10, 0.25)
#: 要素エネルギーの小さい側から累積して全エネルギーのこの割合に達するまでの体積割合(解析者の値。未校正)。
LOW_ENERGY_SHARES = (0.01, 0.05)
#: 目標許容応力を超えるセルの塊(26近傍)を「高応力域」として数える。
HOTSPOT_TOP = 8


def acquire_fem_lock(tag: str, *, max_wait_hours: float = 24.0):
    """単一FEM実行ロックが空くまで待ってから取る(他セッションの計算が握っている間は待つ)。
    `with` で使える `ExecutionLock` を返す(ロックは既に取得済みで、`__exit__` で解放される)。"""
    lock = _common.ExecutionLock(LOCK, tag=tag)
    deadline = time.time() + max_wait_hours * 3600.0
    while not lock.acquire():
        if time.time() > deadline:
            raise RuntimeError(f"{LOCK} が{max_wait_hours}時間空かなかった")
        time.sleep(60)
    lock.acquire = lambda: True  # `with` の __enter__ が再取得しない(取得済み)
    return lock


def case_tag(load_n: float, width_key: str, size_mm: float) -> str:
    return f"F{int(load_n):04d}_{width_key}{WIDTHS_MM[width_key]:g}mm_h{size_mm:g}"


def build_ctx(load_n: float, width_key_or_mm, size_mm: float, *, shift_cells=None) -> sm.SearchContext:
    import synthetic_matrix_support as sup

    width_mm = WIDTHS_MM[width_key_or_mm] if isinstance(width_key_or_mm, str) else float(width_key_or_mm)
    if shift_cells and any(shift_cells):
        shift = tuple(float(c) * size_mm for c in shift_cells)
        return sup.build_context_with_origin_shift(HANGER_MODEL, load_n=load_n, element_size_mm=size_mm,
                                                   min_width_mm=width_mm, shift_mm=shift)
    return sm.build_context(HANGER_MODEL, load_n=load_n, element_size_mm=size_mm, min_width_mm=width_mm)


def _face_boxes_by_id(table, faces) -> list[tuple[int, tuple]]:
    out = []
    for face in faces:
        if anchor.face_node_ids(table, face).size == 0:
            continue
        pos = np.asarray(table.positions(face), dtype=float)
        out.append((int(face), (tuple(pos.min(axis=0)), tuple(pos.max(axis=0)))))
    return out


def _centers(grid) -> np.ndarray:
    xx, yy, zz = np.meshgrid(grid.centers_1d(0), grid.centers_1d(1), grid.centers_1d(2), indexing="ij")
    return np.stack([xx, yy, zz], axis=-1)


def support_reactions(probe: dict, ctx: sm.SearchContext) -> dict:
    """支持面ごとの反力ベクトルと、その面の節点の重心まわりのモーメント(`R = K u - f` の拘束自由度の値)。
    浮遊節点(材料に接しない節点)は除く(`_fem_probe.per_face_reactions` と同じ規則)。
    荷重の合力との釣り合い(全面の反力の和 + 荷重合力)も返す。"""
    model, u, rho = probe["_model"], probe["_displacement"], probe["_density"]
    floating = anchor.floating_dofs(probe["density"])
    residual = model.stiffness_apply(rho, stress.PENAL, u) - model.f
    per_face = {}
    total = np.zeros(3)
    for face in ctx.cond.fixed_faces:
        ids = np.asarray(anchor.face_node_ids(ctx.table, face))
        if ids.size == 0:
            continue
        pos = np.asarray(ctx.table.positions(face), dtype=float)
        dofs = ids[:, None] * 3 + np.arange(3)[None, :]
        keep = ~np.isin(dofs, floating).all(axis=1)
        if not keep.any():
            per_face[str(face)] = dict(node_count=0, force_n=[0.0, 0.0, 0.0], force_magnitude_n=0.0,
                                       moment_about_face_centroid_n_mm=[0.0, 0.0, 0.0], centroid_mm=None)
            continue
        r = np.where(np.isin(dofs[keep], floating), 0.0, residual[dofs[keep]])
        p = pos[keep]
        centroid = p.mean(axis=0)
        force = r.sum(axis=0)
        moment = np.cross(p - centroid, r).sum(axis=0)
        total += force
        per_face[str(face)] = dict(node_count=int(keep.sum()), force_n=force.tolist(),
                                   force_magnitude_n=float(np.linalg.norm(force)),
                                   moment_about_face_centroid_n_mm=moment.tolist(),
                                   moment_magnitude_n_mm=float(np.linalg.norm(moment)),
                                   centroid_mm=centroid.tolist())
    applied = np.asarray(fem.load_resultant(model.f), dtype=float)
    return dict(per_face=per_face, reaction_sum_n=total.tolist(), applied_load_resultant_n=applied.tolist(),
                equilibrium_residual_n=float(np.linalg.norm(total + applied)),
                applied_load_magnitude_n=float(np.linalg.norm(applied)))


def _nearest_face(point: np.ndarray, boxes: list[tuple[int, tuple]]) -> tuple[int | None, float | None]:
    best = (None, None)
    for face, box in boxes:
        d = float(_fem_probe._point_aabb_distance(point[None, :], box)[0])
        if best[1] is None or d < best[1]:
            best = (face, d)
    return best


def diagnose_mask(mask: np.ndarray, ctx: sm.SearchContext, *, label: str = "") -> dict:
    """マスク1つを1回FEMで解き、採点(M5b/M3/M7b)・4ゾーン別の超過・高応力域の塊と支持への帰属・
    支持別反力/モーメント・低寄与材料・実現最小幅を返す。ゾーン距離は3セル分(06と同じ)。
    低寄与材料のしきい値(`LOW_STRESS_RATIOS`・`LOW_ENERGY_SHARES`)は解析者の値で未校正。"""
    t0 = time.time()
    mask = np.asarray(mask, dtype=bool)
    cond, grid = ctx.cond, ctx.grid
    probe = _fem_probe.evaluate_full(
        mask, grid, ctx.table, cond.fixed_faces, cond.load_faces, cond.forces,
        allowable_mpa=cond.allowable_mpa, youngs_modulus_mpa=cond.youngs_modulus_mpa,
        percentile=cond.percentile or stress.DEFAULT_PERCENTILE, solver=cond.solver,
    )
    if probe["status"] != "ok":
        return dict(label=label, status="fem_failed", fem_status=probe["status"], error=probe.get("error"))
    score = sm.score_mask(mask, ctx, probe=probe)
    vm = np.asarray(probe["von_mises_mpa"], dtype=float)
    vol = float(grid.element_volume)
    allowable = float(cond.allowable_mpa)
    target_allowable = allowable / float(cond.target_safety_factor)
    n = int(mask.sum())

    fixed_by_id = _face_boxes_by_id(ctx.table, cond.fixed_faces)
    load_by_id = _face_boxes_by_id(ctx.table, cond.load_faces)
    dist_mm = 3.0 * float(grid.spacing)
    zones = _fem_probe.zone_masks_with_load(mask, grid, fixed_world_boxes=[b for _, b in fixed_by_id],
                                            load_world_boxes=[b for _, b in load_by_id], distance_mm=dist_mm)
    zone_stats = {}
    for name in ("holding", "load", "joint", "general"):
        zm = zones[name]
        cnt = int(zm.sum())
        if cnt == 0:
            zone_stats[name] = dict(cells=0)
            continue
        v = vm[zm]
        zone_stats[name] = dict(
            cells=cnt, volume_mm3=cnt * vol, p99_5_mpa=float(np.percentile(v, 99.5)), max_mpa=float(v.max()),
            cells_over_target=int((v > target_allowable).sum()), cells_over_allowable=int((v > allowable).sum()),
            exceedance_target_mpa_mm3=float(np.clip(v - target_allowable, 0, None).sum() * vol),
            exceedance_raw_mpa_mm3=float(np.clip(v - allowable, 0, None).sum() * vol))
    total_exceed = sum(z.get("exceedance_target_mpa_mm3", 0.0) for z in zone_stats.values())
    for z in zone_stats.values():
        if z.get("cells"):
            z["share_of_target_exceedance"] = (z["exceedance_target_mpa_mm3"] / total_exceed) if total_exceed > 0 else None

    # ピーク
    peak_idx = np.unravel_index(int(np.argmax(np.where(mask, vm, -np.inf))), vm.shape)
    centers_origin = np.asarray(grid.origin, dtype=float)
    peak_world = centers_origin + (np.array(peak_idx, dtype=float) + 0.5) * grid.spacing
    pf, pfd = _nearest_face(peak_world, fixed_by_id)
    pl, pld = _nearest_face(peak_world, load_by_id)
    peak_zone = next((nm for nm in ("holding", "load", "joint", "general") if zones[nm][peak_idx]), None)
    peak = dict(index=[int(i) for i in peak_idx], world_mm=peak_world.tolist(), von_mises_mpa=float(vm[peak_idx]),
                zone=peak_zone, nearest_fixed_face=pf, distance_to_fixed_mm=pfd,
                nearest_load_face=pl, distance_to_load_mm=pld)

    # 高応力域の塊(目標許容応力を超えるセルの26近傍連結成分)
    hot = mask & (vm > target_allowable)
    labeled, n_comp = ndimage.label(hot, structure=np.ones((3, 3, 3), dtype=bool))
    hotspots = []
    per_support = {}
    if n_comp:
        idx = np.arange(1, n_comp + 1)
        excess = np.where(hot, vm - target_allowable, 0.0)
        integral = ndimage.sum(excess, labeled, idx) * vol
        sizes = ndimage.sum(hot, labeled, idx)
        maxima = ndimage.maximum(np.where(hot, vm, 0.0), labeled, idx)
        positions = ndimage.maximum_position(np.where(hot, vm, 0.0), labeled, idx)
        order = np.argsort(-integral)
        for k in order:
            pos = positions[k]
            world = centers_origin + (np.array(pos, dtype=float) + 0.5) * grid.spacing
            face, fd = _nearest_face(world, fixed_by_id)
            _, ld = _nearest_face(world, load_by_id)
            zone = next((nm for nm in ("holding", "load", "joint", "general") if zones[nm][tuple(pos)]), None)
            key = str(face)
            slot = per_support.setdefault(key, dict(components=0, exceedance_target_mpa_mm3=0.0, max_mpa=0.0))
            slot["components"] += 1
            slot["exceedance_target_mpa_mm3"] += float(integral[k])
            slot["max_mpa"] = max(slot["max_mpa"], float(maxima[k]))
            if len(hotspots) < HOTSPOT_TOP:
                hotspots.append(dict(cells=int(sizes[k]), max_mpa=float(maxima[k]), exceedance_target_mpa_mm3=float(integral[k]),
                                     peak_world_mm=world.tolist(), zone=zone, nearest_fixed_face=face,
                                     distance_to_fixed_mm=fd, distance_to_load_mm=ld))
    hot_cells_over_allowable = int((mask & (vm > allowable)).sum())

    # 低寄与材料
    low_stress = {f"vm_lt_{r:g}x_target_allowable": float((vm[mask] < r * target_allowable).mean()) for r in LOW_STRESS_RATIOS}
    energy = np.asarray(score.element_energy, dtype=float)[mask]
    low_energy = {}
    if energy.size and energy.sum() > 0:
        order = np.argsort(energy)
        cum = np.cumsum(energy[order]) / energy.sum()
        for s in LOW_ENERGY_SHARES:
            low_energy[f"volume_fraction_carrying_lowest_{s:g}_energy"] = float(np.searchsorted(cum, s, side="right") / energy.size)

    exempt = binary_search._min_width_exempt(ctx.cond, mask.shape, ctx.min_width_cells)
    min_width_ok = bool(binary_search._mask_satisfies_min_width(mask, ctx.min_width_cells, exempt))

    return dict(
        label=label, status="ok", material_cells=n, material_mm3=n * vol, spacing_mm=float(grid.spacing),
        safety_factor_product=float(probe["safety_factor"]), representative_mpa=float(probe["representative_mpa"]),
        raw_max_mpa=float(probe["raw_max_mpa"]), max_displacement_mm=float(probe["max_displacement_mm"]),
        compliance=score.compliance, allowable_mpa=allowable, target_allowable_mpa=target_allowable,
        m5b_raw=score.m5b_raw_exceedance_mpa_mm3, m5b_raw_passed=score.m5b_raw_passed,
        m5b_target=score.m5b_target_exceedance_mpa_mm3, m5b_target_passed=score.m5b_target_passed,
        m3_worst_zone=score.m3_worst_zone, m3_worst_value=score.m3_worst_value, m3_passed=score.m3_passed,
        m7b_ks_mpa=score.m7b_ks_mpa, mask_hash=score.mask_hash,
        zone_distance_mm=dist_mm, zones=zone_stats, peak=peak,
        hotspot_count=int(n_comp), hotspot_cells=int(hot.sum()), cells_over_allowable=hot_cells_over_allowable,
        hotspots_top=hotspots, hotspot_by_support=per_support,
        low_stress_volume_fraction=low_stress, low_energy=low_energy,
        min_width=dict(cells=int(ctx.min_width_cells), realized_mm=float(ctx.min_width_cells * grid.spacing), ok=min_width_ok),
        supports=support_reactions(probe, ctx),
        diagnose_seconds=time.time() - t0,
    )
