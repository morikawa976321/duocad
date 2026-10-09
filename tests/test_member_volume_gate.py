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


def test_the_finished_shape_is_refined_to_a_third_unless_the_grid_is_too_large():
    """落ちたら、完成形検査の分割数が 1/3 でないか、大きい題材でメモリの上限を超える分割を選ぶ。"""
    assert final_check.refine_factor(1000) == 3
    assert final_check.refine_factor(final_check.MAX_FINE_ELEMENTS // 8) == 2
    assert final_check.refine_factor(final_check.MAX_FINE_ELEMENTS) == 1
