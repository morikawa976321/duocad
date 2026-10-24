"""三角形メッシュの自己交差判定(`duocad/mesh_intersections.py`)。

落ちたら、辺を正しく共有したまま幾何的に貫通・接触する三角形を成果物として通す(検出漏れ)か、
正当な共有辺・共有頂点の接触を交差と誤報して正常なメッシュを不合格にする。
"""
from __future__ import annotations

import numpy as np
import pytest

from duocad.mesh_intersections import orient3d, orient3d_exact, triangle_intersections


def pairs(vertices, faces):
    return [(i, j, kind) for i, j, kind in triangle_intersections(np.asarray(vertices, float), np.asarray(faces))['pairs']]


T1 = [[0, 0, 0], [2, 0, 0], [0, 2, 0]]


@pytest.mark.parametrize('other, expected', [
    ([[0, 0, 1], [1, 0, 1], [0, 1, 1]], []),                                   # 離れている
    ([[0.5, 0.5, -1], [0.5, 0.5, 1], [1.5, 1.5, 0.2]], ['cross']),             # 貫通
    ([[0.5, 0.5, 0], [0.5, 0.5, 1], [1.5, 1.5, 1]], ['touch']),                # 頂点が相手の面に触れる
    ([[0.5, 0.5, 0], [3, 0.5, 0], [0.5, 3, 0]], ['coplanar']),                 # 共面で重なる
    ([[3, 3, 0], [5, 3, 0], [3, 5, 0]], []),                                   # 共面で離れている
])
def test_pairs_without_shared_vertices(other, expected):
    assert [k for _, _, k in pairs(T1 + other, [[0, 1, 2], [3, 4, 5]])] == expected


def test_shared_edge_is_legit_unless_folded():
    assert pairs([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], [[0, 1, 2], [0, 1, 3]]) == []
    assert pairs([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, -1, 0]], [[0, 1, 2], [0, 1, 3]]) == []
    assert pairs([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0.2, 0.5, 0]], [[0, 1, 2], [0, 1, 3]]) == [(0, 1, 'fold')]


def test_shared_vertex_is_legit_unless_the_opposite_edge_or_wedge_enters():
    assert pairs([[0, 0, 0], [1, 0, 0], [0, 1, 0], [-1, 0, 1], [0, -1, 1]], [[0, 1, 2], [0, 3, 4]]) == []
    assert pairs([[0, 0, 0], [2, 0, 0], [0, 2, 0], [-2, 0, 0], [0, -2, 0]], [[0, 1, 2], [0, 3, 4]]) == []
    assert [k for *_, k in pairs([[0, 0, 0], [2, 0, 0], [0, 2, 0], [1, 1, -1], [1, 1, 1]], [[0, 1, 2], [0, 3, 4]])] == ['touch']
    assert [k for *_, k in pairs([[0, 0, 0], [2, 0, 0], [0, 2, 0], [1, 0.5, 0], [0.5, 1, 0]], [[0, 1, 2], [0, 3, 4]])] == ['touch']
    assert [k for *_, k in pairs([[0, 0, 0], [2, 0, 0], [0, 2, 0], [0.5, 0.5, -1], [0.5, 0.5, 1]], [[0, 1, 2], [0, 3, 4]])] == ['cross']


def test_duplicate_triangles_are_reported():
    assert pairs([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2], [0, 2, 1]]) == [(0, 1, 'duplicate')]


def cube():
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f


def test_closed_cube_passes_and_a_piercing_triangle_is_caught():
    v, f = cube()
    assert pairs(v, f) == []
    v2 = np.vstack([v, [[0.5, 0.5, -1], [0.5, 0.5, 2], [2, 2, 0.5]]])
    f2 = np.vstack([f, [[8, 9, 10]]])
    found = pairs(v2, f2)
    assert found and all(j == 12 for _, j, _ in found)


def test_cube_far_from_origin_and_tiny_scale_still_pass():
    v, f = cube()
    assert pairs(v * 1e-4 + 1e5, f) == []
    assert pairs(v * 1e3 - 7e6, f) == []


def test_orient3d_exact_fallback():
    assert orient3d_exact([0, 0, 0], [1, 0, 0], [0, 1, 0], [0.3, 0.7, 1e-300]) == -1
    a, b, c = np.array([[0., 0, 0]]), np.array([[1., 0, 0]]), np.array([[0., 1, 0]])
    assert orient3d(a, b, c, np.array([[0.3, 0.7, 0]]))[0] == 0
    assert orient3d(a, b, c, np.array([[0.3, 0.7, 1e-18]]))[0] == orient3d_exact([0, 0, 0], [1, 0, 0], [0, 1, 0], [0.3, 0.7, 1e-18])
    tilted = (np.array([[0.1, 0.2, 0.3]]), np.array([[1.3, 0.5, 0.7]]), np.array([[0.4, 1.1, 0.9]]))
    inside = tilted[0] * 0.2 + tilted[1] * 0.3 + tilted[2] * 0.5
    assert orient3d(*tilted, inside)[0] == orient3d_exact(tilted[0][0], tilted[1][0], tilted[2][0], inside[0])
