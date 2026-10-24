"""6条件の安全率の報告に載る最大応力・最大変位の意味。落ちたら、報告の数字が材料の外の偽の値や1成分の値になっている。"""
import numpy as np
import pytest

from duocad.topopt import acceptance, fem, stress, voxelize

SHAPE = (8, 8, 8)
SPACING = 2.0
LOAD_N = 100.0


def _bar_in_an_empty_box(direction):
    """空の箱の中の 4×4 セル(8 mm 角)の棒。x=0 の端面を拘束し、x 最大の端面に `direction` 向き 100 N を
    端面の節点へ整合的に配る(角 1/4・辺 1/2・内側 1)。"""
    material = np.zeros(SHAPE, bool)
    material[:, 2:6, 2:6] = True
    nodes = tuple(n + 1 for n in SHAPE)

    def node(ix, iy, iz):
        return (ix * nodes[1] + iy) * nodes[2] + iz

    weights = {(iy, iz): (0.5 if iy in (2, 6) else 1.0) * (0.5 if iz in (2, 6) else 1.0)
               for iy in range(2, 7) for iz in range(2, 7)}
    total = sum(weights.values())
    unit = np.asarray(direction, float) / np.linalg.norm(direction)
    fixed = []
    load = np.zeros(3 * int(np.prod(nodes)))
    for (iy, iz), weight in weights.items():
        fixed += [3 * node(0, iy, iz) + axis for axis in range(3)]
        load[3 * node(SHAPE[0], iy, iz):3 * node(SHAPE[0], iy, iz) + 3] = LOAD_N * weight / total * unit
    return material, np.array(sorted(fixed)), load


def test_the_reported_maximum_stress_comes_from_material_cells_only():
    """落ちたら、報告の最大応力に空セル(一部の節点だけ動く)の「変位 ÷ 要素サイズ」が入っている(issue #38)。"""
    material, fixed, load = _bar_in_an_empty_box((1.0, 0.0, 0.0))
    grid = voxelize.Grid((0.0, 0.0, 0.0), SPACING, SHAPE)
    report = acceptance.safety_check(material, grid, fixed, load, target=1.0, allowable_mpa=100.0,
                                     youngs_modulus_mpa=2000.0, percentile=99.5, solver='direct')
    nominal = LOAD_N / (4 * SPACING) ** 2
    assert 'raw_max_mpa' not in report
    assert nominal <= report['material_max_mpa'] < 1.1 * nominal


def test_the_maximum_displacement_is_the_largest_nodal_displacement_magnitude():
    """落ちたら、最大変位が節点変位の大きさではなく x/y/z の1成分の最大(斜めの荷重で最大 √3 倍小さい)になっている(issue #39)。"""
    material, fixed, load = _bar_in_an_empty_box((0.0, 1.0, 1.0))
    live = np.repeat(acceptance.active_nodes(material).ravel(), 3)
    model = fem.Model(*SHAPE, fixed_dofs=fixed, load=load, extra_fixed_dofs=np.flatnonzero(~live),
                      active_mask=material)
    density = material.astype(float)
    u = stress.solve_displacement(model, density, solver='direct')
    scale = stress.UnitScale(SPACING, 1.0, 2000.0, solved_load=1.0)
    report = stress.evaluate(model, density, scale=scale, allowable_mpa=100.0, u=u)
    magnitude = np.linalg.norm(u.reshape(-1, 3), axis=1).max() * scale.displacement_factor
    assert report.max_displacement_mm == pytest.approx(magnitude)
    assert report.max_displacement_mm > 1.3 * np.abs(u).max() * scale.displacement_factor
