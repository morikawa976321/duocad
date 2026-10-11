import numpy as np
import pytest

from duocad.topopt import binary_search as bs
from duocad.topopt import min_width


def _end_face_anchors(shape):
    """x 最小と x 最大の面の節点(島が出るように両端を条件節点にする)。"""
    ny1, nz1 = shape[1] + 1, shape[2] + 1
    last = shape[0] * ny1 * nz1
    return np.concatenate([np.arange(ny1 * nz1), np.arange(last, last + ny1 * nz1)]).astype(np.int64)


def _same(got, want):
    (mask_a, stats_a), (mask_b, stats_b) = got, want
    assert np.array_equal(mask_a, mask_b)
    assert np.array_equal(stats_a.picked_cells, stats_b.picked_cells)
    assert {k: v for k, v in stats_a.as_dict().items() if k != 'seconds'} == \
        {k: v for k, v in stats_b.as_dict().items() if k != 'seconds'}


def _parents(rng, m, exempt):
    """全材料から数回削った、不変条件を満たす親マスク。"""
    shape = (9, 7, 7)
    mask = np.ones(shape, bool)
    anchors = _end_face_anchors(shape)
    out = []
    for _ in range(3):
        mask, _ = bs.peel(mask, rng.permutation(mask.size), int(mask.sum()) // 6, anchors,
                          min_width_cells=m, exempt=exempt)
        out.append(mask)
    return out, anchors


@pytest.mark.parametrize('m', [1, 2, 3])
@pytest.mark.parametrize('with_keep', [False, True])
def test_reused_walk_returns_what_a_fresh_peel_returns(m, with_keep):
    """落ちたら、同じ親で歩きを使い回したときの候補・選んだセル・数えが、毎回 `peel` を呼んだときとずれている(探索の軌道が変わる)。
    段階4の使い方(要求数の半減・抜いた1手を順位から外す・順位の途中を外す・要求数を戻す)を混ぜて比べる。"""
    rng = np.random.default_rng(20261012 + m + 10 * with_keep)
    exempt = protected = None
    if with_keep:
        protected = np.zeros((9, 7, 7), bool)
        protected[0, :, :] = True
        exempt = protected.copy() if m > 1 else None
    parents, anchors = _parents(rng, m, exempt)
    steps = 0
    for parent in parents:
        kwargs = dict(min_width_cells=m, exempt=exempt, protected=protected if (m == 1 or exempt is not None) else None)
        walk = bs.PeelWalk(parent, anchors, **kwargs)
        order = rng.permutation(parent.size)
        count = max(1, int(parent.sum()) // 4)
        for _ in range(40):
            got = walk.take(order, count)
            _same(got, bs.peel(parent, order, count, anchors, **kwargs))
            steps += 1
            picked = got[1].picked_cells
            move = rng.random()
            if picked.size > 1 and move < .6:
                count = max(1, count // 2)
            elif picked.size and move < .85:
                order = order[~np.isin(order, picked[:1])]
            elif move < .95:
                order = np.delete(order, int(rng.integers(0, len(order))))
            else:
                count = max(1, int(parent.sum()) // 3)
    assert steps == 120


def test_dropping_the_first_pick_resumes_instead_of_walking_from_the_start(monkeypatch):
    """落ちたら、1手を順位から外したあとの歩きが、外した手より前のセルをもう一度判定している(段階4の終盤の歩き直しが戻る)。"""
    rng = np.random.default_rng(7)
    parents, anchors = _parents(rng, 2, None)
    parent = parents[-1]
    order = rng.permutation(parent.size)
    calls = {'n': 0}
    real = min_width.casualties

    def counted(*args, **kwargs):
        calls['n'] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(min_width, 'casualties', counted)
    walk = bs.PeelWalk(parent, anchors, min_width_cells=2)
    fresh = 0
    for _ in range(6):
        before = calls['n']
        _, stats = bs.peel(parent, order, 1, anchors, min_width_cells=2)
        fresh += calls['n'] - before
        walk.take(order, 1)
        order = order[~np.isin(order, stats.picked_cells)]
    reused = calls['n'] - fresh
    assert reused < fresh / 2


def test_rewriting_the_order_in_place_and_asking_for_nothing_still_match_a_fresh_peel():
    """落ちたら、呼び出し側が同じ順位の配列を書き換えたときか、要求数が 0 以下のときに、覚えた歩きを誤って返している。"""
    full = np.ones((4, 4, 4), bool)
    anchors = np.zeros(0, dtype=np.int64)
    walk = bs.PeelWalk(full, anchors)
    order = np.arange(full.size)
    _same(walk.take(order, 3), bs.peel(full, order, 3, anchors))
    order[:] = order[::-1].copy()
    _same(walk.take(order, 3), bs.peel(full, order, 3, anchors))
    for count in (0, -2):
        got = walk.take(order, count)
        _same(got, bs.peel(full, order, count, anchors))
        assert np.array_equal(got[0], full) and got[1].picked == 0
