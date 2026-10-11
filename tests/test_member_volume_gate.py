"""探索の安全率の代表応力(部材域の固定物理体積の上位値)と、完成形検査の表面の最大応力。
落ちたら、母数(遠くの低応力材料)で合否が動く・拘束面の1層や保持が判定に入る・表面の応力の測り方が変わった。"""
import numpy as np
import pytest

from duocad import optimize_run
from duocad.topopt import acceptance, binary_search, fem, final_check, stress, voxelize

from session_support import four_conditions


def test_the_member_volume_value_ignores_low_stress_material_added_far_away():
    """落ちたら、部材域の外や低応力の材料を足すと代表応力が動く(99.5%点の母数依存が残っている)。"""
    hot = np.array([9.0, 8.0, 7.0, 6.0, 5.0])
    base, _ = stress.member_volume_stress(hot, np.ones(5, bool), volume_mm3=2.0, cell_mm3=1.0)
    diluted, _ = stress.member_volume_stress(np.concatenate([hot, np.full(1000, 0.1)]), np.ones(1005, bool),
                                             volume_mm3=2.0, cell_mm3=1.0)
    assert base == diluted == 7.0  # 超過を許すのは上位 2 mm³(=2セル)まで、3番目の値が代表


def test_a_member_region_no_larger_than_the_allowed_volume_is_judged_by_its_maximum():
    """落ちたら、部材域の全部が許容体積に収まる小さい題材で、部材域の全部が目標を超えても合格になる。"""
    value, count = stress.member_volume_stress(np.array([3.0, 5.0, 4.0]), np.ones(3, bool), volume_mm3=8.0, cell_mm3=1.0)
    assert (value, count) == (5.0, 3)


def test_the_boundary_layer_is_the_cells_touching_a_listed_node():
    """落ちたら、拘束・荷重面の節点から外す1層が「節点を角に持つ要素」でなくなっている。"""
    shape = (4, 4, 4)
    inner = (2 * 5 + 2) * 5 + 2  # 節点 (2,2,2)
    corner = 0  # 節点 (0,0,0)
    assert stress.boundary_layer_cells(shape, [inner]).sum() == 8
    assert stress.boundary_layer_cells(shape, [corner]).sum() == 1
    assert stress.boundary_layer_cells(shape, [corner])[0, 0, 0]


def _cantilever(m: int):
    length = 16 * m
    material = np.ones((length, m, m), bool)
    nx, ny, nz = material.shape

    def node(i, j, k):
        return (i * (ny + 1) + j) * (nz + 1) + k

    fixed_nodes = [node(i, j, k) for i in range(m + 1) for j in range(ny + 1) for k in range(nz + 1)]
    load_nodes = [node(i, j, k) for i in range(nx - m, nx + 1) for j in range(ny + 1) for k in range(nz + 1)]
    load = np.zeros(3 * (nx + 1) * (ny + 1) * (nz + 1))
    load[np.asarray(load_nodes) * 3 + 1] = 1.0 / len(load_nodes)
    fixed = (np.asarray(fixed_nodes)[:, None] * 3 + np.arange(3)).ravel()
    return material, fixed, load


def test_the_exposed_face_stress_catches_most_of_the_bending_surface_stress():
    """落ちたら、面中心の表面応力が片持ち梁の表面理論値の 0.80(2セル厚。要素中心は 0.42)から外れた。"""
    m = 2
    material, fixed, load = _cantilever(m)
    model = fem.Model(*material.shape, fixed_dofs=fixed, load=load, active_mask=material)
    u = model.solve(material.astype(float), 3.0, solver='direct')
    scale = stress.UnitScale(1.0, 1.0, 1.0, solved_load=1.0)
    faces = stress.exposed_face_von_mises(model, u, material, scale)
    centre = stress.element_stress(model, u).von_mises
    x = np.arange(material.shape[0]) + 0.5
    gauge = (x >= 0.3 * 16 * m) & (x <= 0.7 * 16 * m)
    theory = ((16 * m - m / 2) - 0.3 * 16 * m) * (m / 2) / (m ** 4 / 12)
    assert np.nanmax(faces[gauge]) / theory == pytest.approx(0.804, abs=0.01)
    assert centre[gauge].max() / theory == pytest.approx(0.424, abs=0.01)
    assert np.isnan(faces[5:-5, :, :, 0]).all()  # -x の面は隣も材料なので表面ではない


def test_the_member_region_leaves_out_kept_cells_and_the_layer_at_condition_nodes():
    """落ちたら、保持セルか拘束・荷重面の節点に接する1層が部材域に入っている。"""

    class Table:
        def empty_faces(self):
            return []

        def node_ids(self, faces):
            return np.array([0]) if list(faces) == [1] else np.array([(4 * 5 + 4) * 5 + 4])

    material = np.ones((4, 4, 4), bool)
    keep = np.zeros_like(material)
    keep[2, 2, 2] = True
    region = acceptance.member_region(material, Table(), (1,), (2,), keep)
    assert not region[0, 0, 0] and not region[3, 3, 3] and not region[2, 2, 2]
    assert region.sum() == 64 - 3


def test_the_search_conditions_carry_the_member_rule_into_the_inspection_and_the_cache_key(session):
    """落ちたら、部材域の規則が受入検査に渡らないか、規則や保持セルが違う評価の結果を使い回す。"""
    four_conditions(session)
    doc = session.document
    bundle = optimize_run.build_conditions(doc)
    from duocad.topopt import design_space
    space = design_space.derive(bundle.conditions, spacing=8.0)
    conditions = optimize_run._build_search_conditions(doc, space, bundle, optimize_run.OptimizeOptions(element_size_mm=8.0),
                                                       min_width_mm=16.0)
    assert conditions.member_volume_mm3 == 16.0 ** 3
    kwargs = conditions.inspect_kwargs()
    assert kwargs['member_volume_mm3'] == 16.0 ** 3 and kwargs['keep_cells'] is conditions.keep_cells
    other = binary_search.replace(conditions, member_volume_mm3=8.0 ** 3)
    assert binary_search.evaluation_identifier(other) != binary_search.evaluation_identifier(conditions)
    legacy = binary_search.replace(conditions, member_volume_mm3=None)
    assert 'member_volume_mm3' not in legacy.inspect_kwargs()
    requested = optimize_run._build_search_conditions(
        doc, space, bundle, optimize_run.OptimizeOptions(element_size_mm=8.0, min_width_mm=24.0))
    assert requested.member_volume_mm3 == 24.0 ** 3  # 引数を省いたら利用者の要求した最小幅


def test_the_member_rule_goes_through_representative_stress_so_it_can_be_replaced(monkeypatch):
    """落ちたら、部材域の規則が `representative_stress` の外で値を上書きし、差し替えた代表応力が効かない。"""
    m = 2
    material, fixed, load = _cantilever(m)
    model = fem.Model(*material.shape, fixed_dofs=fixed, load=load, active_mask=material)
    density = material.astype(float)
    u = model.solve(density, 3.0, solver='direct')
    scale = stress.UnitScale(1.0, 1.0, 1.0, solved_load=1.0)
    real = stress.representative_stress
    monkeypatch.setattr(stress, "representative_stress",
                        lambda field, rho, **kw: stress.RepresentativeStress(**{**real(field, rho, **kw).__dict__, "value": 0.5}))
    report = stress.evaluate(model, density, scale=scale, allowable_mpa=1.0, u=u, member_region=material,
                             member_volume_mm3=1.0)
    assert report.safety_factor == 2.0


def test_the_finished_shape_is_measured_at_a_fixed_physical_spacing_unless_the_grid_is_too_large():
    """落ちたら、完成形検査の細かさが要素サイズに比例している(物理寸法で揃わない)か、要素数の上限を超える分割を選ぶ。"""
    assert final_check.refine_factor(1000, 100, 2.0, None) == 3
    assert final_check.refine_factor(1000, 100, 4.0, None) == 6
    assert final_check.refine_factor(1000, 100, 0.5, None) == 1
    over_six = final_check.MAX_FINE_ELEMENTS // 6 ** 3 + 1
    assert final_check.refine_factor(over_six, 0, 4.0, None) == 5
    assert final_check.refine_factor(final_check.MAX_FINE_ELEMENTS + 1, 0, 4.0, None) == 1
    assert not final_check.fits(final_check.MAX_FINE_ELEMENTS + 1, 0, 1, None)


def test_the_refinement_shrinks_with_free_memory_and_with_the_material_it_must_hold():
    """落ちたら、空きメモリが少ない機械で空きを超える細かい格子を確保しにいく(2026-09-26 にメモリ不足で PC が落ちた)か、
    メモリの大半を占める材料セルの数を見積もりに入れていない。"""
    budget = final_check.estimated_bytes(1000, 200, 3) / final_check.MEMORY_FRACTION
    assert final_check.refine_factor(1000, 200, 4.0, budget) == 3
    assert final_check.refine_factor(1000, 400, 4.0, budget) == 2   # 同じ格子でも材料が倍なら1段下げる
    assert final_check.fits(1000, 200, 3, budget) and not final_check.fits(1000, 200, 4, budget)
    assert final_check.estimated_bytes(1000, 200, 2) == 8 * (1000 * final_check.FINE_ELEMENT_BYTES
                                                             + 200 * final_check.MATERIAL_CELL_BYTES)


def test_material_the_search_grid_missed_is_recounted_after_occupancy_and_the_refinement_drops(monkeypatch):
    """落ちたら、探索の格子に載らない保持ボディの薄い部分などで材料が見積もりより多いのに、数え直さずに空きメモリを超える
    分割数で FEM へ進む。"""
    factors = []

    def full_occupancy(soup, fine, allow_empty):
        factors.append(round(4.0 / fine.spacing))
        return (np.ones(fine.shape, bool),)

    monkeypatch.setattr(voxelize, "occupancy", full_occupancy)
    grid = voxelize.Grid((0.0, 0.0, 0.0), 4.0, (10, 10, 2))
    lo, hi = np.zeros(3, np.int64), np.array([10, 10, 2])
    # 探索の形は 10 セルだが、占有化すると格子 200 セルが全部材料になる
    available = 2 * final_check.estimated_bytes(200, 10, 6)
    factor, fine, material, counted = final_check._occupy(None, grid, lo, hi, 10, available)
    assert factors[0] == 6 and factor < 6 and counted == 200
    assert final_check.fits(200, 200, factor, available) and not final_check.fits(200, 200, factor + 1, available)
    assert material.shape == fine.shape == (10 * factor, 10 * factor, 2 * factor)
    assert final_check._occupy(None, grid, lo, hi, 10, 2 * final_check.estimated_bytes(200, 10, 1))[0] is None  # 1 でも数え直すと収まらない


def test_the_finished_shape_is_judged_by_the_top_area_stress_times_the_coarse_penalty(monkeypatch):
    """落ちたら、合否に1面の最大を使っているか、面積の上位値の位置を取り違えているか、粗く測った値を割り増さずに比べている。"""
    material = np.ones((2, 2, 2), bool)
    fine = voxelize.Grid((0.0, 0.0, 0.0), 1.0, (2, 2, 2))  # 1面 1 mm² なので面積 4 mm² は大きい方から4面目
    faces = np.full((2, 2, 2, 6), np.nan)
    faces[0, 0, 0, :] = [10.0, 9.0, 8.0, 7.0, 6.0, 1.0]
    monkeypatch.setattr(stress, "exposed_face_von_mises", lambda *a, **k: faces)
    monkeypatch.setattr(acceptance, "rigid_body_constrained", lambda *a: True)
    scale = stress.UnitScale(1.0, 1.0, 1.0, solved_load=1.0)
    monkeypatch.setattr(acceptance, "solve_material", lambda *a, **k: (None, None, None, scale, 0.0))

    class Conditions:
        youngs_modulus_mpa, solver, allowable_mpa = 1.0, "direct", 40.0

    report = final_check._strength(material, material, ~material, ~material, fine, None, None, Conditions(), 2.0)
    penalty = (1.0 / final_check.MEASURE_SPACING_MM) ** final_check.COARSE_PENALTY_EXPONENT
    assert report["peak_mpa"] == 10.0 and report["top_area_mpa"] == 7.0
    assert report["coarse_penalty"] == pytest.approx(penalty) and penalty > 1.0
    assert report["safety_factor"] == pytest.approx(40.0 / (7.0 * penalty))
    assert final_check.coarse_penalty(final_check.MEASURE_SPACING_MM / 2) == 1.0


def test_the_reported_peak_position_is_the_face_centre_not_the_element_centre(monkeypatch):
    """落ちたら、完成形検査の最大応力の位置が測った面の中心から半要素ずれている。"""
    material = np.ones((2, 2, 2), bool)
    fine = voxelize.Grid((10.0, 20.0, 30.0), 1.0, (2, 2, 2))
    faces = np.full((2, 2, 2, 6), np.nan)
    faces[0, 0, 0, 1] = 5.0  # 要素 (0,0,0) の +x 面
    monkeypatch.setattr(stress, "exposed_face_von_mises", lambda *a, **k: faces)
    monkeypatch.setattr(acceptance, "rigid_body_constrained", lambda *a: True)
    scale = stress.UnitScale(1.0, 1.0, 1.0, solved_load=1.0)
    monkeypatch.setattr(acceptance, "solve_material", lambda *a, **k: (None, None, None, scale, 0.0))

    class Conditions:
        youngs_modulus_mpa, solver, allowable_mpa = 1.0, "direct", 40.0

    report = final_check._strength(material, material, ~material, ~material, fine, None, None, Conditions(), 2.0)
    assert report["peak_position_mm"] == [11.0, 20.5, 30.5] and report["peak_mpa"] == 5.0


def test_a_finished_shape_that_cannot_be_held_still_is_unmeasured_not_unsafe(monkeypatch):
    """落ちたら、細かい格子で剛体運動を止められない完成形を「目標を割った」失敗として返す(安全率が無いまま失敗文を作る)。"""
    material = np.ones((2, 2, 2), bool)
    monkeypatch.setattr(acceptance, "rigid_body_constrained", lambda *a: False)

    class Conditions:
        youngs_modulus_mpa, solver, allowable_mpa = 1.0, "direct", 40.0

    fine = voxelize.Grid((0.0, 0.0, 0.0), 1.0, (2, 2, 2))
    report = final_check._strength(material, material, ~material, ~material, fine, None, None, Conditions(), 2.0)
    assert report["status"] == "unknown" and report["safety_factor"] is None
