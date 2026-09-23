"""境界折れ線の交差判定(`duocad/boundary_check.py`)。OCP を使わない合成データで判定を固定する。

落ちたら、面分割へ渡す境界の交差を見逃す(穴・非多様体の三角形が黙って通る)か、
正当な接続(wire の連続する辺の共有端点)を交差と誤報して正常な形状が分割されなくなる。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from duocad import boundary_check as bc


def face(*wires):
    return [[dict(edge=e, points=p, forward=f) for (e, p, f) in w] for w in wires]


def kinds(result):
    return sorted((i['kind'], tuple(i['edges'])) for i in result['issues'])


SQUARE = face([(1, [[0, 0], [1, 0]], True), (2, [[1, 0], [1, 1]], True),
               (3, [[1, 1], [0, 1]], True), (4, [[0, 1], [0, 0]], True)])
BOWTIE = face([(1, [[0, 0], [1, 1]], True), (2, [[1, 1], [1, 0]], True),
               (3, [[1, 0], [0, 1]], True), (4, [[0, 1], [0, 0]], True)])


def test_square_and_reversed_edge_have_no_issue():
    assert bc.check(bc.pack([SQUARE]))['issues'] == []
    reversed_edge = face([(1, [[0, 0], [1, 0]], True), (2, [[1, 1], [1, 0]], False),
                          (3, [[1, 1], [0, 1]], True), (4, [[0, 1], [0, 0]], True)])
    assert bc.check(bc.pack([reversed_edge]))['issues'] == []


def test_bowtie_is_a_cross_with_its_point():
    result = bc.check(bc.pack([BOWTIE]))
    assert kinds(result) == [('cross', (1, 3))]
    assert result['issues'][0]['point'] == pytest.approx([0.5, 0.5])


def test_shallow_crossing_and_tiny_loop_are_not_ignored():
    """OCCT の FaceChecker が捨てる 5 度未満・小面積の交差も拾う(docs/TOPOPT_GAP_CAUSALITY.md)。"""
    shallow = face([(1, [[0, 0], [1, 0]], True), (2, [[1, 0], [1, 1e-6]], True),
                    (3, [[1, 1e-6], [0, -1e-9]], True), (4, [[0, -1e-9], [0, 0]], True)])
    assert kinds(bc.check(bc.pack([shallow]))) == [('cross', (1, 3))]


def test_single_edge_and_two_edge_wires_connect_at_both_ends():
    theta = np.linspace(0, 2 * np.pi, 9)
    circle = np.c_[np.cos(theta), np.sin(theta)]
    circle[-1] = circle[0]
    assert bc.check(bc.pack([face([(1, circle, True)])]))['issues'] == []
    a = np.c_[np.cos(np.linspace(0, np.pi, 5)), np.sin(np.linspace(0, np.pi, 5))]
    b = np.c_[np.cos(np.linspace(np.pi, 2 * np.pi, 5)), np.sin(np.linspace(np.pi, 2 * np.pi, 5))]
    b[0], b[-1] = a[-1], a[0]
    assert bc.check(bc.pack([face([(1, a, True), (2, b, True)])]))['issues'] == []
    assert bc.check(bc.pack([face([(1, a, True), (2, b[::-1].copy(), False)])]))['issues'] == []


def test_gap_between_consecutive_edges_is_reported_as_open_not_merged():
    gap = face([(1, [[0, 0], [1, 0]], True), (2, [[1, 0.001], [1, 1]], True),
                (3, [[1, 1], [0, 1]], True), (4, [[0, 1], [0, 0]], True)])
    issues = bc.check(bc.pack([gap]))['issues']
    assert [i['kind'] for i in issues] == ['open']
    assert issues[0]['gap'] == pytest.approx(0.001)
    assert bc.fixable_edges(issues) == set()


def test_fold_back_within_an_edge_and_between_edges():
    within = face([(1, [[0, 0], [1, 0], [0.5, 0]], True), (2, [[0.5, 0], [0.5, 1]], True), (3, [[0.5, 1], [0, 0]], True)])
    assert ('fold', (1,)) in kinds(bc.check(bc.pack([within])))
    between = face([(1, [[0, 0], [1, 0]], True), (2, [[1, 0], [0.5, 0]], True),
                    (3, [[0.5, 0], [0.5, 1]], True), (4, [[0.5, 1], [0, 0]], True)])
    assert ('fold', (1, 2)) in kinds(bc.check(bc.pack([between])))


def test_non_adjacent_touch_and_collinear_overlap():
    outer = [(1, [[0, 0], [2, 0]], True), (2, [[2, 0], [2, 2]], True), (3, [[2, 2], [0, 2]], True), (4, [[0, 2], [0, 0]], True)]
    touching = face(outer, [(5, [[1, 0], [1.5, 1]], True), (6, [[1.5, 1], [0.5, 1]], True), (7, [[0.5, 1], [1, 0]], True)])
    assert {k for k, _ in kinds(bc.check(bc.pack([touching])))} == {'touch'}
    overlapping = face(outer, [(5, [[0.5, 0], [1.5, 0]], True), (6, [[1.5, 0], [1, 1]], True), (7, [[1, 1], [0.5, 0]], True)])
    assert ('overlap', (1, 5)) in kinds(bc.check(bc.pack([overlapping])))


def test_zero_length_segments_are_skipped_without_false_touch():
    doubled = face([(1, [[0, 0], [1, 0], [1, 0]], True), (2, [[1, 0], [1, 1]], True),
                    (3, [[1, 1], [0, 1]], True), (4, [[0, 1], [0, 0]], True)])
    result = bc.check(bc.pack([doubled]))
    assert result['issues'] == [] and result['zero_length_segments'] == 1


def test_translation_and_scale_do_not_change_the_verdict():
    def moved(f, dx, s):
        return [[dict(u, points=(np.asarray(u['points'], dtype=float) + dx) * s) for u in w] for w in f]
    for dx, s in ((1e6, 1e-3), (-3.7, 250.0), (0.0, 1e-9)):
        assert [i['kind'] for i in bc.check(bc.pack([moved(BOWTIE, dx, s)]))['issues']] == ['cross']
        assert bc.check(bc.pack([moved(SQUARE, dx, s)]))['issues'] == []


def test_face_subset_and_face_ids():
    packed = bc.pack([SQUARE, BOWTIE], face_ids=[10, 20])
    assert [(i['face'], i['kind']) for i in bc.check(packed)['issues']] == [(20, 'cross')]
    assert bc.check(packed, faces=[10])['issues'] == []
    assert bc.check(packed, faces=[20])['faces_checked'] == 1


def test_orientation_falls_back_to_exact_arithmetic_only_when_uncertain():
    """浮動小数点で符号が確定しない近共線の3点を整数演算で決める(固定 ε ではない)。"""
    assert bc.orient2d_exact(0.1, 0.1, 0.7, 0.7, 0.4, 0.4) == 0
    assert bc.orient2d_exact(0, 0, 1e-300, 1e-300, 1e-300, 2e-300) == 1
    a, b = np.array([[0.0, 0.0]]), np.array([[1.0, 1.0]])
    assert bc.orient2d(a, b, np.array([[0.3, 0.3 + 1e-17]]))[0] == bc.orient2d_exact(0, 0, 1, 1, 0.3, 0.3 + 1e-17)
    assert bc.orient2d(a, b, np.array([[0.3, 0.3 + 1e-15]]))[0] == 1
    assert bc.orient2d(a, b, np.array([[0.3, 0.3 - 1e-15]]))[0] == -1


def test_non_finite_coordinates_are_rejected():
    with pytest.raises(ValueError):
        bc.pack([face([(1, [[0, 0], [math.nan, 0]], True)])])
