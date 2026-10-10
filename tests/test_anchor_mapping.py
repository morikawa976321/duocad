"""探索用の確定表(材料に接する節点だけへの対応付け。`anchor.build_anchors(allowed_nodes=...)` と
`anchor.condition_mapping`)。実CADと実数値で見る(`docs/TOPOPT.md`「回帰検査の対応」)。"""
import dataclasses
import sys
from pathlib import Path

import numpy as np
import pytest
from build123d import Box, Pos, Rotation

from duocad.topopt import acceptance, anchor, voxelize
from duocad.topopt import binary_search as bs
from duocad.topopt.acceptance import active_nodes as acceptance_active_nodes

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'topopt_checks'))
import _fixtures  # noqa: E402  合成題材(build123d を直接使うので duocad/ の外にある)


DEFAULT_PHASE = (0.37, 0.21, 0.44)


def _setup(shape, spacing=1.0, phase=DEFAULT_PHASE):
    """形状を包む格子(各軸に1要素以上の余白)と、その占有(材料を置ける範囲)を作る。"""
    soup = voxelize.triangles_from_shape(shape.wrapped, deviation=spacing * voxelize.DEFAULT_DEVIATION_RATIO)
    lo, hi = soup.bbox
    origin = np.asarray(lo, dtype=float) - spacing * (1.0 + np.asarray(phase, dtype=float))
    count = tuple(int(np.ceil((h - o) / spacing)) + 1 for o, h in zip(origin, hi))
    grid = voxelize.Grid(tuple(float(v) for v in origin), spacing, count)
    design, _ = voxelize.occupancy(soup, grid)
    return soup, grid, design


def _face_facing(soup, direction):
    """法線が `direction` に最も近い面(箱では一意)。"""
    want = np.asarray(direction, dtype=float)
    want = want / np.linalg.norm(want)
    best, score = None, -2.0
    for index, props in soup.face_props.items():
        normal = np.asarray(props['normal'], dtype=float)
        if np.linalg.norm(normal) == 0:
            continue
        dot = float(np.dot(normal / np.linalg.norm(normal), want))
        if dot > score:
            best, score = int(index), dot
    return best


def _off_plane_box():
    return Pos(0.0, 0.0, 0.3) * Box(20.0, 10.0, 10.0)


def test_the_shared_active_node_definition_is_one_function():
    assert acceptance_active_nodes is anchor.active_nodes


@pytest.mark.parametrize('seed', range(6))
def test_attached_dofs_equals_removing_the_floating_dofs(seed):
    """落ちたら、`attached_dofs` が `np.setdiff1d(dofs, floating_dofs(material))` と値・順序・dtype のどれかで違う
    (剛体拘束の判定に使う拘束自由度の集合が変わる)。並べ替え・重複・空の入力も含める。"""
    rng = np.random.default_rng(seed)
    material = rng.random((7, 5, 6)) < (0.15 + 0.1 * seed)
    ndof = 3 * int(np.prod([n + 1 for n in material.shape]))
    floating = anchor.floating_dofs(material)
    for dofs in (rng.integers(0, ndof, 40), np.arange(ndof)[::-1], np.zeros(0, dtype=np.int64),
                 np.repeat(rng.integers(0, ndof, 10), 3).astype(np.int32)):
        expected = np.setdiff1d(dofs, floating)
        actual = anchor.attached_dofs(material, dofs)
        assert actual.dtype == expected.dtype
        np.testing.assert_array_equal(actual, expected)


def test_attached_dofs_refuses_numbers_outside_the_grid():
    """落ちたら、負の番号が後ろから数えた別の節点として黙って引かれるか、格子の外・整数でない番号が通る。"""
    material = np.ones((1, 1, 1), dtype=bool)  # 節点8つ・自由度24
    for bad in (np.array([-1]), np.array([24]), np.array([0.0]), np.zeros(0, dtype=np.float64),
                np.array([0], dtype='timedelta64[ns]')):
        with pytest.raises(ValueError):
            anchor.attached_dofs(material, bad)
    np.testing.assert_array_equal(anchor.attached_dofs(material, np.array([23, 0])), [0, 23])


def test_material_mapping_uses_only_nodes_that_touch_material_and_keeps_the_load():
    """幾何の表は材料の有無を見ないので、格子面に乗らない面では材料に接しない節点へも配る。
    確定表は許可節点だけへ配り、合力は厳密に保存され、重心のずれは球の半径以内に収まる。"""
    soup, grid, design = _setup(_off_plane_box())
    top, bottom = _face_facing(soup, (0, 0, 1)), _face_facing(soup, (0, 0, -1))
    live = anchor.active_nodes(design).ravel()

    geometry = anchor.build_anchors(soup, grid)
    assert geometry.stats['mapping'] == 'geometry'
    assert int((~live[geometry[top].node_ids]).sum()) > 0  # 幾何の表には材料に接しない節点がある

    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(design))
    assert table.stats['mapping'] == 'material'
    assert table.stats['unmapped_samples'] == 0
    assert live[table[top].node_ids].all() and live[table[bottom].node_ids].all()
    assert 0 < table[top].node_count < geometry[top].node_count

    force = (0.0, 0.0, -100.0)
    report = anchor.condition_mapping(table, [bottom], [top], [force], material=design)
    assert report['status'] == 'pass', report
    load = next(row for row in report['faces'] if row['condition'] == f'load_face_{top}')
    assert load['nodes'] == table[top].node_count and load['uncovered_nodes'] == 0
    assert load['resultant_n'] == pytest.approx(list(force), abs=1e-12)
    assert load['resultant_error_n'] <= 1e-12
    assert load['centroid_shift_mm'] <= table.radius and load['within_search_radius']
    assert load['max_mapping_distance_mm'] <= table.radius * (1 + 1e-9)
    # 全ての重みが面の下の格子面(材料側)へ移るので、重心は z 方向へ下がる(面内のずれは
    # テント重みが格子位相で偏るぶんで、これも半径以内)。モーメントは節点荷重から独立に計算した
    # 値で、CAD面の重心からのずれ Δc について |Δc × F| と一致し、|Δc|·|F| を超えない。
    assert load['centroid_reference'] == 'cad' and load['sample_to_cad_shift_mm'] <= 1e-9
    shift = np.asarray(load['node_centroid_mm']) - np.asarray(load['cad_centroid_mm'])
    assert shift[2] < 0
    assert load['moment_error_nmm'] == pytest.approx(np.linalg.norm(np.cross(shift, force)), abs=1e-9)
    assert load['moment_error_nmm'] <= load['centroid_shift_mm'] * 100.0 + 1e-9
    assert report['rule']['mapping'] == 'material'


def test_samples_without_an_allowed_node_in_the_sphere_are_reported_not_moved():
    """面の一部の近傍に材料を置ける場所が無ければ、その標本は対応不能として面・座標付きで残り、
    遠い節点へは移さない。合力の保存だけを見て成功にしない。"""
    soup, grid, design = _setup(_off_plane_box())
    top, bottom = _face_facing(soup, (0, 0, 1)), _face_facing(soup, (0, 0, -1))
    centres = grid.centers_1d(0)
    cut = design.copy()
    cut[centres > 0.0, :, :] = False  # x > 0 の材料を全部無くす(面の右半分の下に材料が無い)
    allowed = anchor.active_nodes(cut)
    last_allowed_x = float(grid.origin[0] + grid.spacing * np.flatnonzero(allowed.any(axis=(1, 2))).max())
    table = anchor.build_anchors(soup, grid, allowed_nodes=allowed)
    top_anchor = table[top]
    assert top_anchor.unmapped_samples > 0
    assert 0.3 < top_anchor.unmapped_area / top_anchor.triangle_area < 0.6
    assert top_anchor.unmapped_positions.shape[1] == 3
    assert np.all(top_anchor.unmapped_positions[:, 0] > last_allowed_x)  # 最後の許可節点面より右の標本だけ
    assert allowed.ravel()[top_anchor.node_ids].all()  # 残った節点は全部許可節点

    report = anchor.condition_mapping(table, [bottom], [top], [(0.0, 0.0, -100.0)], material=cut)
    assert report['status'] == 'fail'
    load = next(row for row in report['faces'] if row['condition'] == f'load_face_{top}')
    assert 'unmapped_samples' in load['reasons']
    assert load['resultant_error_n'] <= 1e-12  # 合力は残った節点で保存される —— それでも成功扱いしない
    warning = anchor.format_mapping_warning(report)
    assert f'面{top}' in warning and '対応' not in warning[:0]
    assert '予算' not in warning


@pytest.mark.parametrize('spacing', [1.0, 0.5])
def test_a_rotated_planar_face_keeps_its_centroid_within_the_search_radius(spacing):
    """解析解を持つ面(回転した箱の平面)で、確定表の重心のずれが球の半径(格子近似の許容差)以内に
    収まり、標本の重心は面の真の重心と一致する(数値許容差)。格子を細分しても成り立つ。"""
    shape = Rotation(20.0, 35.0, 10.0) * (Pos(0.0, 0.0, 0.3) * Box(20.0, 10.0, 10.0))
    soup, grid, design = _setup(shape, spacing=spacing)
    top = _face_facing(soup, (0, 0, 1))
    bottom = _face_facing(soup, (0, 0, -1))
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(design))
    force = (0.0, 0.0, -50.0)
    report = anchor.condition_mapping(table, [bottom], [top], [force], material=design)
    assert report['status'] == 'pass', report
    load = next(row for row in report['faces'] if row['condition'] == f'load_face_{top}')
    normal = np.asarray(soup.face_props[top]['normal'], dtype=float)
    centre = np.asarray(tuple(shape.center()), dtype=float)
    analytic = centre + 5.0 * normal / np.linalg.norm(normal)  # 20x10 の面は中心から法線方向 5 の位置
    assert np.asarray(load['sample_centroid_mm']) == pytest.approx(analytic, abs=1e-6)
    assert load['centroid_shift_mm'] <= table.radius
    assert load['resultant_error_n'] <= 1e-12
    assert load['moment_error_normalized'] <= 1.0  # |Δc⊥|·|F| / (|F|·h) <= radius/h = 1
    assert load['unmapped_samples'] == 0 and load['uncovered_nodes'] == 0


@pytest.mark.parametrize('offset, phase', [
    ((0.13, 0.37, 0.71), (0.0, 0.0, 0.0)),   # 面が格子面に乗る位相
    ((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
    ((0.99, 0.01, 0.42), (0.83, 0.07, 0.61)),
])
def test_translation_and_grid_phase_do_not_change_the_contract(offset, phase):
    """平行移動・格子位相を変えても、対応・荷重保存・被覆の契約は同じ(hanger 一例で確定しない)。"""
    shape = Pos(*offset) * Box(20.0, 10.0, 10.0)
    soup, grid, design = _setup(shape, phase=phase)
    top, bottom = _face_facing(soup, (0, 0, 1)), _face_facing(soup, (0, 0, -1))
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(design))
    report = anchor.condition_mapping(table, [bottom], [top], [(10.0, -20.0, -30.0)], material=design)
    assert report['status'] == 'pass', report
    for row in report['faces']:
        assert row['unmapped_samples'] == 0 and row['uncovered_nodes'] == 0
        assert row['centroid_shift_mm'] <= table.radius
    load = next(row for row in report['faces'] if row['side'] == 'load')
    assert load['resultant_n'] == pytest.approx([10.0, -20.0, -30.0], abs=1e-12)


def test_a_face_that_vanishes_after_ownership_is_diagnosed_as_no_nodes():
    """所有権競合で空になった面は `no_nodes` として不成立。遠方の節点で穴埋めしない。"""
    model = _fixtures.voxel_model(_fixtures.demo_bracket(), 5)  # 粗い格子: 耳(8x8)の上面は隣の大きい面に取られる
    fixed, load = _fixtures.demo_faces(model.soup)
    design, _ = voxelize.occupancy(model.soup, model.grid)
    table = anchor.build_anchors(model.soup, model.grid, allowed_nodes=anchor.active_nodes(design))
    assert load in table.empty_faces(), 'フィクスチャの前提(耳の面が空になる粗さ)が崩れている'
    report = anchor.condition_mapping(table, [fixed], [load], [(0.0, 0.0, -1.0)], material=design)
    assert report['status'] == 'fail'
    row = next(r for r in report['faces'] if r['condition'] == f'load_face_{load}')
    assert row['nodes'] == 0 and 'no_nodes' in row['reasons']
    assert '節点が1つも付かない' in anchor.format_mapping_warning(report)


def test_opposing_loads_are_verified_per_face_so_a_lost_face_is_not_cancelled_out():
    """相反する2つの荷重面。片方の近傍から材料を無くしても、もう片方の合力で相殺されて隠れない。"""
    soup, grid, design = _setup(_off_plane_box())
    plus, minus = _face_facing(soup, (1, 0, 0)), _face_facing(soup, (-1, 0, 0))
    bottom = _face_facing(soup, (0, 0, -1))
    cut = design.copy()
    cut[grid.centers_1d(0) > 7.0, :, :] = False  # +X の面(x=10)の近傍 3 要素ぶんの材料を無くす
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(cut))
    report = anchor.condition_mapping(table, [bottom], [plus, minus], [(100.0, 0.0, 0.0), (-100.0, 0.0, 0.0)],
                                      material=cut)
    rows = {row['condition']: row for row in report['faces']}
    assert rows[f'load_face_{minus}']['status'] == 'pass'
    assert rows[f'load_face_{minus}']['resultant_n'] == pytest.approx([-100.0, 0.0, 0.0], abs=1e-12)
    assert rows[f'load_face_{plus}']['status'] == 'fail'
    assert {'no_nodes', 'unmapped_samples'} & set(rows[f'load_face_{plus}']['reasons'])
    assert report['status'] == 'fail'


def test_allowed_nodes_must_match_the_node_grid():
    soup, grid, design = _setup(_off_plane_box())
    with pytest.raises(ValueError, match='allowed_nodes'):
        anchor.build_anchors(soup, grid, allowed_nodes=np.ones(5, dtype=bool))


def test_the_search_entry_rejects_unmapped_samples_even_when_every_node_is_on_material(monkeypatch):
    """直接APIで検証の無い確定表を渡しても、探索の入口が表を検証し直して止まる(候補生成0回・FEM0回)。
    面の半分の標本が対応不能でも残った節点は全部材料に接するので、節点の被覆だけでは止まらない題材。"""
    soup, grid, design = _setup(_off_plane_box())
    top, bottom = _face_facing(soup, (0, 0, 1)), _face_facing(soup, (0, 0, -1))
    cut = design.copy()
    cut[grid.centers_1d(0) > 0.0, :, :] = False
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(cut))
    conditions = bs.SearchConditions(grid=grid, table=table, fixed_faces=[bottom], load_faces=[top],
                                     forces=[(0.0, 0.0, -100.0)], preserves=[], excludes=[],
                                     target_safety_factor=2.0)
    assert not any(row['uncovered_nodes'] for row in bs.uncovered_condition_nodes(cut, conditions))
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: pytest.fail('入口で止まるべき経路が評価に進んだ'))
    result = bs.find_best_feasible(cut, conditions, max_evaluations=10)
    assert result.status == 'boundary_mapping_failed' and result.evaluations == 0
    entry = result.diagnostics['entry_mapping']
    load = next(row for row in entry['faces'] if row['condition'] == f'load_face_{top}')
    assert entry['status'] == 'fail' and 'unmapped_samples' in load['reasons']


def test_the_position_gate_measures_from_the_cad_centroid_within_the_search_radius():
    """作用位置の基準はCAD面の面積重心。CAD重心が探索半径より離れていれば、標本の重心と節点が
    揃っていても重心とモーメントの両方で不成立になる。"""
    soup, grid, design = _setup(_off_plane_box())
    top, bottom = _face_facing(soup, (0, 0, 1)), _face_facing(soup, (0, 0, -1))
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(design))
    force = (0.0, 0.0, -100.0)
    assert anchor.condition_mapping(table, [bottom], [top], [force], material=design)['status'] == 'pass'

    moved = dataclasses.replace(table[top], cad_centroid=table[top].cad_centroid + np.array([2.0 * grid.spacing, 0, 0]))
    table.anchors[top] = moved
    report = anchor.condition_mapping(table, [bottom], [top], [force], material=design)
    load = next(row for row in report['faces'] if row['condition'] == f'load_face_{top}')
    assert report['status'] == 'fail' and {'centroid_shift', 'moment'} <= set(load['reasons'])
    assert load['centroid_limit_mm'] == pytest.approx(table.radius) and load['centroid_shift_mm'] > table.radius
    assert 'resultant' not in load['reasons']
    assert '探索半径' in anchor.format_mapping_warning(report)


def test_the_moment_gate_catches_a_load_vector_that_does_not_follow_the_assigned_areas(monkeypatch):
    """重心は担当面積から、モーメントは `load_vector` の自由度配列から別々に計算する。合力を保ったまま
    荷重を面の端の1節点へ寄せると、重心の門は通り、モーメントの門だけが落ちる。"""
    soup, grid, design = _setup(_off_plane_box())
    top, bottom = _face_facing(soup, (0, 0, 1)), _face_facing(soup, (0, 0, -1))
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(design))
    corner = int(table[top].node_ids[np.argmin(table.node_grid.positions_of(table[top].nodes)[:, 0])])

    def lumped(faces, total_force, **kwargs):
        vector = np.zeros(table.node_grid.ndof)
        vector[3 * corner:3 * corner + 3] = total_force
        return vector

    monkeypatch.setattr(table, 'load_vector', lumped)
    report = anchor.condition_mapping(table, [bottom], [top], [(0.0, 0.0, -100.0)], material=design)
    load = next(row for row in report['faces'] if row['condition'] == f'load_face_{top}')
    assert load['reasons'] == ['moment']
    assert load['moment_error_nmm'] > load['moment_limit_nmm'] == pytest.approx(table.radius * 100.0)
