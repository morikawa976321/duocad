"""完成形のつなぎ目の診断(`neck_persistence`)。落ちたら、真の首の直径が報告区間から外れた(ε = √3·s の誤差境界が崩れた)か、
輪のくびれ・木のくびれ・太い形の振り分けが仕様書 §3 と食い違う。形は陰関数で作り、真の直径が分かるものだけを使う。"""
import numpy as np
import pytest

from duocad.topopt import neck_persistence, voxelize


def _segment_distance(points, a, b):
    ab = b - a
    t = np.clip(((points - a) @ ab) / (ab @ ab), 0.0, 1.0)
    return np.linalg.norm(points - (a + t[:, None] * ab), axis=1)


def _sample(shape_fn, size, s):
    n = int(np.ceil(size / s)) + 4
    grid = voxelize.Grid((-2 * s - size / 2,) * 3, s, (n, n, n))
    axis = grid.origin[0] + (np.arange(n) + 0.5) * s
    x, y, z = np.meshgrid(axis, axis, axis, indexing='ij')
    pts = np.stack([x.ravel(), y.ravel(), z.ravel()], axis=1)
    inside = shape_fn(pts).reshape(n, n, n)
    inside[[0, -1], :, :] = inside[:, [0, -1], :] = inside[:, :, [0, -1]] = False
    return inside, grid


def _rotation(rng):
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    return q


def _dumbbell(R, rho, rotation, loop=False):
    """半径 R の2球を、半径 rho の棒でつなぐ(木のくびれ)。`loop` なら半径 R の太い弧でもう一度つなぐ(輪のくびれ)。"""
    a, b = rotation @ np.array([-7.0, 0, 0]), rotation @ np.array([7.0, 0, 0])
    c1, c2 = rotation @ np.array([-7.0, 9.0, 0]), rotation @ np.array([7.0, 9.0, 0])

    def inside(p):
        body = (np.linalg.norm(p - a, axis=1) < R) | (np.linalg.norm(p - b, axis=1) < R) | (_segment_distance(p, a, b) < rho)
        if loop:
            body |= (_segment_distance(p, a, c1) < R) | (_segment_distance(p, c1, c2) < R) | (_segment_distance(p, c2, b) < R)
        return body
    return inside


@pytest.mark.parametrize('seed', range(4))
def test_tree_neck_interval_contains_the_true_diameter(seed):
    rng = np.random.default_rng(seed)
    rho = 1.0 + rng.random()
    inside, grid = _sample(_dumbbell(4.0, rho, _rotation(rng)), 24.0, 0.5)
    result = neck_persistence.inspect_samples(inside, grid, required_mm=6.0, min_width_cells=3)
    necks = [e for e in result['events'] if e['kind'] == 'tree_neck']
    assert result['status'] == 'suspect'
    assert any(e['diameter_mm'][0] <= 2 * rho <= e['diameter_mm'][1] and e['verdict'] == 'suspect' for e in necks)


@pytest.mark.parametrize('seed', range(4))
def test_loop_neck_is_an_essential_class_with_no_lifetime_threshold(seed):
    rng = np.random.default_rng(seed)
    rho = 1.0 + rng.random()
    inside, grid = _sample(_dumbbell(3.5, rho, _rotation(rng), loop=True), 28.0, 0.5)
    result = neck_persistence.inspect_samples(inside, grid, required_mm=6.0, min_width_cells=3)
    loops = [e for e in result['events'] if e['kind'] == 'loop_neck']
    assert len(loops) == 1 and loops[0]['lifetime_mm'] is None
    low, high = loops[0]['diameter_mm']
    assert low <= 2 * rho <= high and loops[0]['verdict'] == 'suspect'


def test_thick_rod_is_not_observed():
    inside, grid = _sample(lambda p: _segment_distance(p, np.array([-8.0, 0, 0]), np.array([8.0, 0, 0])) < 6.0, 30.0, 0.5)
    result = neck_persistence.inspect_samples(inside, grid, required_mm=4.0, min_width_cells=2)
    assert result['status'] == 'not_observed' and result['events'] == []


def test_keep_zone_is_never_reported():
    """保持ソリッドとその周りは最初から入れる(保持経由は太い)。保持の細い棒は首として出ない。"""
    inside, grid = _sample(_dumbbell(4.0, 1.0, np.eye(3)), 24.0, 0.5)
    coarse = voxelize.Grid(grid.origin, 1.0, tuple(int(np.ceil(n / 2)) for n in inside.shape))
    keep = np.zeros(coarse.shape, bool)
    centre = np.array(coarse.shape) // 2
    keep[centre[0] - 4:centre[0] + 4, centre[1] - 1:centre[1] + 1, centre[2] - 1:centre[2] + 1] = True  # 棒の全長(球の縁まで)
    result = neck_persistence.inspect_samples(inside, grid, required_mm=6.0, min_width_cells=3, keep=(keep, coarse))
    assert not [e for e in result['events'] if e['verdict'] == 'suspect']


def test_too_many_samples_is_undetermined_not_coarsened():
    vertices = np.array([[0, 0, 0], [1000, 0, 0], [0, 1000, 0], [0, 0, 1000]], float)
    faces = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    result = neck_persistence.inspect_mesh(vertices, faces, spacing=1.0, required_mm=4.0, min_width_cells=4)
    assert result['status'] == 'undetermined' and result['reason']
