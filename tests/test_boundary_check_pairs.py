"""境界検査の候補の組(`boundary_check._candidate_pairs`)を、y の帯に分けない x の掃引だけの組み方と突き合わせる。

落ちたら、帯に分けた探し方が組を取りこぼす・増やす・並びを変えている(検査の問題の一覧と再分割する辺が変わる)か、
帯に分けた探し方が帯に分けない掃引より多くの組を作る(時間とメモリが悪くなる)。
"""
from __future__ import annotations

import numpy as np
import pytest

from duocad import boundary_check as bc


def reference_pairs(face_of_seg, lo, hi):
    span = float(hi[:, 0].max() - lo[:, 0].min()) + 1.0
    shift = face_of_seg.astype(np.float64) * span * 2.0
    lo_x = lo[:, 0] + shift
    hi_x = hi[:, 0] + shift
    order = np.argsort(lo_x, kind='stable')
    upper = np.searchsorted(lo_x[order], hi_x[order], side='right')
    base = np.arange(len(order))
    counts = np.maximum(upper - base - 1, 0)
    total = int(counts.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    second = np.repeat(base + 1, counts) + (np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts))
    i = order[np.repeat(base, counts)]
    j = order[second]
    keep = (face_of_seg[i] == face_of_seg[j]) & (lo[j, 1] <= hi[i, 1]) & (lo[i, 1] <= hi[j, 1])
    i, j = i[keep], j[keep]
    swap = i > j
    return np.where(swap, j, i), np.where(swap, i, j)


def same_face_x_overlaps(face_of_seg, lo, hi) -> int:
    count = 0
    for face in np.unique(face_of_seg):
        lo_x, hi_x = lo[face_of_seg == face, 0], hi[face_of_seg == face, 0]
        order = np.argsort(lo_x, kind='stable')
        upper = np.searchsorted(lo_x[order], hi_x[order], side='right')
        count += int(np.maximum(upper - np.arange(len(order)) - 1, 0).sum())
    return count


def assert_same(faces, lo, hi):
    expected = reference_pairs(faces, lo, hi)
    got = bc._candidate_pairs(faces, lo, hi)
    assert np.array_equal(got[0], expected[0]) and np.array_equal(got[1], expected[1])


def segments(kind, rng):
    count = int(rng.integers(1, 300))
    faces = rng.integers(0, int(rng.integers(1, 6)), count)
    scale = {'huge': 1e5, 'tiny': 1e-6}.get(kind, 1.0)
    start = rng.normal(size=(count, 2)) * scale
    end = start + rng.normal(size=(count, 2)) * scale * (2.0 if kind in ('vertical', 'long') else 0.05)
    if kind == 'vertical':
        end[:, 0] = start[:, 0]
    if kind == 'long':
        tall = rng.random(count) < 0.1
        start[tall, 1], end[tall, 1] = -3.0, 3.0
    if kind == 'ties':
        start, end = np.round(start, 1), np.round(end, 1)
    if kind == 'flat':
        start[:, 1] = end[:, 1] = 0.25
    if kind == 'faces':
        faces = rng.integers(0, 10 ** 6, count) * 7919
    return faces, np.minimum(start, end), np.maximum(start, end)


def force_banded(monkeypatch):
    monkeypatch.setattr(bc, '_BANDED_FROM', 0)
    monkeypatch.setattr(bc, '_BANDED_COPIES', float('inf'))
    monkeypatch.setattr(bc, '_BANDED_MADE', float('inf'))
    monkeypatch.setattr(bc, '_BANDED_MADE_SORTED', float('inf'))
    monkeypatch.setattr(bc, '_BANDED_KEEP', float('inf'))


@pytest.mark.parametrize('banded', [True, False])
@pytest.mark.parametrize('kind', ['plain', 'huge', 'tiny', 'vertical', 'long', 'ties', 'flat', 'faces'])
def test_pairs_and_their_order_match_the_x_sweep(kind, banded, monkeypatch):
    """座標の大きさ・同じ座標・長い縦の線分・一部だけ高い線分・高さ 0・飛び飛びの面番号で、組とその並びが x の掃引だけの
    ときと同じ(帯に分ける道と分けない道の両方)。"""
    if banded:
        force_banded(monkeypatch)
    else:
        monkeypatch.setattr(bc, '_BANDED_FROM', 10 ** 9)
    rng = np.random.default_rng(len(kind))
    for _ in range(300):
        assert_same(*segments(kind, rng))


def hole_grid(nx, ny, points):
    angles = np.linspace(0.0, 2.0 * np.pi, points + 1)
    chunks = []
    for cx in range(nx):
        for cy in range(ny):
            ring = np.column_stack((cx * 4.0 + np.cos(angles), cy * 4.0 + np.sin(angles)))
            chunks.append(np.stack((ring[:-1], ring[1:]), axis=1))
    pairs = np.concatenate(chunks)
    return np.zeros(len(pairs), dtype=np.int64), pairs.min(axis=1), pairs.max(axis=1)


def test_holes_in_a_grid_match_the_x_sweep():
    """穴を格子に並べた平面の境界(y で落とす組が多い形。既定の閾値で帯に分ける)で同じ組・同じ並び。"""
    faces, lo, hi = hole_grid(16, 12, 48)
    assert len(lo) >= bc._BANDED_FROM
    assert len(reference_pairs(faces, lo, hi)[0]) > 0
    assert_same(faces, lo, hi)


def tall_beside_short(count=16384, tall=380):
    """同じ面に、x も y も重なり合う高い線分の束 `tall` 本と、互いに離れた短い線分。"""
    k = np.arange(tall)
    start = [np.column_stack((np.zeros(tall), k * 1e-4))]
    end = [np.column_stack((np.ones(tall), 1 + k * 1e-4))]
    k = np.arange(count - tall)
    start.append(np.column_stack((2.0 * k + 2, np.full(len(k), 0.25))))
    end.append(np.column_stack((2.0 * k + 3, np.full(len(k), 0.251))))
    start, end = np.concatenate(start), np.concatenate(end)
    return np.zeros(count, dtype=np.int64), np.minimum(start, end), np.maximum(start, end)


@pytest.mark.parametrize('shape', ['tall_beside_short', 'hole_grid', 'random_long'])
def test_banded_search_makes_each_pair_once_and_no_more_than_the_x_sweep(shape, monkeypatch):
    """帯をまたぐ線分が多い入力で、帯に分けた探し方が同じ組を何度も作らず、作る組が帯に分けない掃引の(同じ面の)組より
    多くならない。"""
    if shape == 'tall_beside_short':
        faces, lo, hi = tall_beside_short()
    elif shape == 'hole_grid':
        faces, lo, hi = hole_grid(12, 8, 48)
    else:
        rng = np.random.default_rng(3)
        start = rng.random((5000, 2)) * 10
        end = start + rng.normal(size=(5000, 2)) * 0.1
        tall = rng.random(5000) < 0.05
        start[tall, 1], end[tall, 1] = 0.0, 10.0
        faces, lo, hi = rng.integers(0, 3, 5000), np.minimum(start, end), np.maximum(start, end)
    force_banded(monkeypatch)
    made = []
    original = bc._banded_pairs
    monkeypatch.setattr(bc, '_banded_pairs', lambda *args: made.append(original(*args)) or made[-1])
    assert_same(faces, lo, hi)
    assert made and made[0] is not None
    keys = np.minimum(*made[0]) * len(lo) + np.maximum(*made[0])
    assert len(np.unique(keys)) == len(keys)
    assert len(keys) <= same_face_x_overlaps(faces, lo, hi)


@pytest.mark.parametrize('values', [(-1e308, 1e308), (-np.inf, np.inf), (0.0, np.nan), (5e-324, 1e-323)])
def test_extreme_coordinates_fall_back_to_the_x_sweep(values, monkeypatch):
    """有限でも差があふれる高さ・無限大・NaN・非正規化数の y でも例外にならず、x の掃引だけのときと同じ組・並び。"""
    force_banded(monkeypatch)
    k = np.arange(1024)
    start = np.column_stack((2.0 * k, np.full(1024, values[0])))
    end = np.column_stack((2.0 * k + 1 + (k % 3 == 0) * 4, np.full(1024, values[1])))
    with np.errstate(all='ignore'):
        assert_same(np.zeros(1024, dtype=np.int64), np.minimum(start, end), np.maximum(start, end))


def test_huge_x_shifts_fall_back_to_the_x_sweep(monkeypatch):
    """面ごとの x のずらしがあふれる大きな x でも、x の掃引だけのときと同じ組・並び。"""
    force_banded(monkeypatch)
    rng = np.random.default_rng(5)
    start = rng.random((2000, 2)) * 1e307
    end = start + rng.random((2000, 2)) * 1e306
    faces = rng.integers(0, 4, 2000)
    with np.errstate(all='ignore'):
        assert_same(faces, np.minimum(start, end), np.maximum(start, end))


def tall_permuted(width, height, count=262144):
    """y の順位がばらばらで、多くの帯にまたがる縦に長い線分(帯の写し・帯で作る組が多い)。"""
    k = np.arange(count, dtype=float)
    lo = np.column_stack((k, np.random.default_rng(47).permutation(count).astype(float)))
    return np.zeros(count, dtype=np.int64), lo, lo + [width, height]


@pytest.mark.parametrize('shape', ['bundle 380', 'bundle 513', 'bundle 2000', 'tall 9.25 1536', 'tall 17.25 6000'])
def test_inputs_that_bands_do_not_pay_for_stay_on_the_x_sweep(shape, monkeypatch):
    """x の掃引の候補が多くても、大半が y でも重なる入力・帯の写しが多くなる入力は、帯に分けない(分けると最後の並べ直しや
    写しの整列の分だけ遅くなる)。組と並びは x の掃引だけのときと同じ。"""
    kind, *size = shape.split()
    faces, lo, hi = tall_permuted(*map(float, size)) if kind == 'tall' else tall_beside_short(tall=int(size[0]))
    original = bc._banded_pairs

    def declined(*args):
        made = original(*args)
        assert made is None, '帯に分けた'
        return made
    monkeypatch.setattr(bc, '_banded_pairs', declined)
    assert_same(faces, lo, hi)


def test_holes_in_a_grid_are_searched_by_bands(monkeypatch):
    """穴を格子に並べた境界(候補の 2% だけが y でも重なる)は既定の閾値で帯に分ける。"""
    faces, lo, hi = hole_grid(16, 12, 48)
    called = []
    original = bc._banded_pairs
    monkeypatch.setattr(bc, '_banded_pairs', lambda *args: called.append(1) or original(*args))
    assert_same(faces, lo, hi)
    assert called
