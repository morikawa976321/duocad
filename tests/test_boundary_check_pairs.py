"""境界検査の候補の組(`boundary_check._candidate_pairs`)を、y の帯に分けない x の掃引だけの組み方と突き合わせる。

落ちたら、帯に分けた探し方が組を取りこぼす・増やす・並びを変えている(検査の問題の一覧と再分割する辺が変わる)。
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


def segments(kind, rng):
    count = int(rng.integers(1, 300))
    faces = rng.integers(0, int(rng.integers(1, 6)), count)
    scale = {'huge': 1e5, 'tiny': 1e-6}.get(kind, 1.0)
    start = rng.normal(size=(count, 2)) * scale
    end = start + rng.normal(size=(count, 2)) * scale * (2.0 if kind == 'vertical' else 0.05)
    if kind == 'vertical':
        end[:, 0] = start[:, 0]
    if kind == 'ties':
        start, end = np.round(start, 1), np.round(end, 1)
    if kind == 'flat':
        start[:, 1] = end[:, 1] = 0.25
    return faces, np.minimum(start, end), np.maximum(start, end)


@pytest.mark.parametrize('kind', ['plain', 'huge', 'tiny', 'vertical', 'ties', 'flat'])
def test_pairs_and_their_order_match_the_x_sweep(kind):
    """座標の大きさ・同じ座標・長い縦の線分(帯を減らす側)・高さ 0 で、組とその並びが x の掃引だけのときと同じ。"""
    rng = np.random.default_rng(len(kind))
    for _ in range(300):
        faces, lo, hi = segments(kind, rng)
        expected = reference_pairs(faces, lo, hi)
        got = bc._candidate_pairs(faces, lo, hi)
        assert np.array_equal(got[0], expected[0]) and np.array_equal(got[1], expected[1])


def test_holes_in_a_grid_match_the_x_sweep():
    """穴を格子に並べた平面の境界(y で落とす組が多い形)で同じ組・同じ並び。"""
    angles = np.linspace(0.0, 2.0 * np.pi, 49)
    chunks = []
    for cx in range(12):
        for cy in range(8):
            ring = np.column_stack((cx * 4.0 + np.cos(angles), cy * 4.0 + np.sin(angles)))
            chunks.append(np.stack((ring[:-1], ring[1:]), axis=1))
    pairs = np.concatenate(chunks)
    lo, hi = pairs.min(axis=1), pairs.max(axis=1)
    faces = np.zeros(len(lo), dtype=np.int64)
    expected = reference_pairs(faces, lo, hi)
    got = bc._candidate_pairs(faces, lo, hi)
    assert len(expected[0]) > 0
    assert np.array_equal(got[0], expected[0]) and np.array_equal(got[1], expected[1])
