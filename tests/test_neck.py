"""探索の1手のくびれ判定(`neck.NeckJudge`)と形の静的診断(`neck.static_necks`)。落ちたら、輪の中の1セル幅のくびれ(#120)を
探索が作れるようになったか、折れ目・切り切る手・T字などの正常な手を誤って断るか、窓の判定が仕様書 §2 の定義(参照実装)と食い違う。"""
import itertools
from collections import deque

import numpy as np
import pytest
from scipy import ndimage

from duocad.topopt import min_width, neck


def _blocks(shape, *boxes):
    mask = np.zeros(shape, bool)
    for lo, hi in boxes:
        mask[tuple(slice(a, b) for a, b in zip(lo, hi))] = True
    return mask


def _judge(before, removed, m, keep=None):
    after = before.copy()
    for c in removed:
        after[tuple(c)] = False
    return neck.NeckJudge(min_width.CoreConnectivity(m, keep)).creates_neck(after, np.array(removed))


_SIX = [d for d in itertools.product((-1, 0, 1), repeat=3) if sum(map(abs, d)) == 1]
_TWENTY_SIX = [d for d in itertools.product((-1, 0, 1), repeat=3) if any(d)]


def _core_steps(m, adjacency=None):
    """芯の隣接(#122 の仕様: m ≥ 4 は26、m ≤ 3 は6)。材料の隣接はいつも6。"""
    return _SIX if (adjacency or (26 if m >= 4 else 6)) == 6 else _TWENTY_SIX


# --- 参照実装: 仕様書 §2 を、`CoreConnectivity` も `core_cells` も使わずに書き直したもの ---------------------------------------

def _ref_cores(material, m):
    n = np.array(material.shape)
    out = np.zeros(material.shape, bool)
    for c in itertools.product(*(range(int(k) - m + 1) for k in n)):
        out[c] = material[tuple(slice(a, a + m) for a in c)].all()
    return out


def _bfs_components(cells, steps=_SIX):
    """セル集合(タプルの set)の `steps` 近傍の連結成分 → {セル: 成分番号}。"""
    comp, label = {}, 0
    for start in cells:
        if start in comp:
            continue
        label += 1
        comp[start] = label
        queue = deque([start])
        while queue:
            x = queue.popleft()
            for d in steps:
                y = tuple(np.add(x, d))
                if y in cells and y not in comp:
                    comp[y] = label
                    queue.append(y)
    return comp


def _ref_creates_neck(before, removed, m, keep=None, adjacency=None):
    shape = np.array(before.shape)
    keep = np.zeros_like(before) if keep is None else keep
    after = before.copy()
    for c in removed:
        after[tuple(c)] = False
    k_before, k_after = _ref_cores(before | keep, m), _ref_cores(after | keep, m)
    lost = {tuple(c) for c in np.argwhere(k_before & ~k_after)}
    if not lost:
        return False
    # 保持成分ごとの結び: 立方が「保持成分 ∪ その6近傍」に触れる角(大域で決まる)。
    keep_lab, keep_n = ndimage.label(keep)
    links = []
    for j in range(1, keep_n + 1):
        zone = ndimage.binary_dilation(keep_lab == j)
        links.append({c for c in itertools.product(*(range(int(k)) for k in shape))
                      if zone[tuple(slice(a, a + m) for a in c)].any()})
    removed_set = {tuple(c) for c in removed}
    cells = np.array(removed)
    margin = 2 * m
    while True:
        lo = np.maximum(cells.min(0) - (m - 1) - margin, 0)
        hi = np.minimum(cells.max(0) + 1 + margin, shape)
        whole = (lo == 0).all() and (hi == shape).all()
        if not whole and 8 * np.prod(hi - lo) >= np.prod(shape):
            lo, hi, whole = np.zeros(3, int), shape, True
        mat_hi = np.minimum(hi + m - 1, shape)
        inside = lambda c, a, b: all(a[i] <= c[i] < b[i] for i in range(3))
        mat = {tuple(c) for c in np.argwhere(after | keep) if inside(c, lo, mat_hi)}
        cores = {tuple(c) for c in np.argwhere(k_after) if inside(c, lo, hi)}
        steps = _core_steps(m, adjacency)
        near = lambda c, s, ds: any(tuple(np.add(c, d)) in s for d in ds)
        front_core = {c for c in cores if near(c, lost, steps)}
        if not front_core:
            return False
        front_mat = {c for c in mat if near(c, removed_set, _SIX)}
        if not front_mat:
            return False
        if len({_bfs_components(mat)[c] for c in front_mat}) == 1:
            comp = _bfs_components(cores, steps)
            parent = {v: v for v in set(comp.values())}

            def find(x):
                while parent[x] != x:
                    x = parent[x]
                return x
            for link in links:
                hit = sorted({comp[c] for c in cores & link})
                for other in hit[1:]:
                    parent[find(other)] = find(hit[0])
            return len({find(comp[c]) for c in front_core}) > 1
        if whole:
            return False
        margin *= 2


# --- 固定例 ------------------------------------------------------------------------------------------------------------------

def test_face_contact_neck_is_refused():
    # 2セル厚の板で A=[0,2)² と B=[2,4)×[0,3)。B の y=0 の列を抜くと、A と1セル幅の面で接するだけになる。
    before = _blocks((4, 3, 2), ((0, 0, 0), (2, 2, 2)), ((2, 0, 0), (4, 3, 2)))
    removed = [(2, 0, 0), (2, 0, 1), (3, 0, 0), (3, 0, 1)]
    assert _judge(before, removed, 2) and _ref_creates_neck(before, removed, 2)


def test_diagonal_overlap_neck_is_refused():
    # A=[0,3)² と B=[2,5)² の2立方(m=3)を、重なりを広げていた帯ごとつないだ形から、帯を抜くと1セルだけ斜めに重なる。
    before = _blocks((5, 5, 3), ((0, 0, 0), (3, 3, 3)), ((2, 2, 0), (5, 5, 3)), ((0, 2, 0), (3, 5, 3)))
    removed = [tuple(c) for c in np.argwhere(_blocks((5, 5, 3), ((0, 3, 0), (2, 5, 3))))]
    assert _judge(before, removed, 3) and _ref_creates_neck(before, removed, 3)


def test_cutting_a_bar_through_is_not_judged():
    before = _blocks((12, 2, 2), ((0, 0, 0), (12, 2, 2)))
    removed = [(5, j, k) for j in range(2) for k in range(2)] + [(6, j, k) for j in range(2) for k in range(2)]
    assert not _judge(before, removed, 2) and not _ref_creates_neck(before, removed, 2)


def test_carving_a_t_junction_corner_is_allowed():
    before = _blocks((14, 14, 3), ((0, 0, 0), (14, 3, 3)), ((5, 3, 0), (8, 14, 3)))
    removed = [(13, 0, k) for k in range(3)] + [(12, 0, k) for k in range(3)] + [(11, 0, k) for k in range(3)]
    assert not _judge(before, removed, 3) and not _ref_creates_neck(before, removed, 3)


def test_codex_counterexample_where_verdict_says_none_is_allowed():
    """`CoreConnectivity.verdict` は None を返すが、新しい定義ではくびれではない(仕様書 §4)。"""
    shape = (12, 24, 12)
    keep = _blocks(shape, ((2, 2, 2), (3, 22, 4)))
    before = keep | _blocks(shape, ((3, 2, 2), (7, 4, 4)), ((3, 4, 2), (5, 5, 3)))
    removed = [tuple(c) for c in np.argwhere(_blocks(shape, ((3, 2, 2), (4, 4, 4))))]
    assert not _judge(before, removed, 2, keep) and not _ref_creates_neck(before, removed, 2, keep)


def test_cutting_a_far_detour_that_leaves_a_crease_as_the_only_link_is_refused():
    """遠い迂回路を切る手は、材料が残りの1セル幅の接触面を回ってつながる窓まで広げて判定する(fable の指摘した抜け道)。"""
    m = 2
    shape = (6, 30, 2)
    # 左右の塊を、y=1 の1セル幅の接触面 C と、y=20..22 の太い棒 P(C から 2m より遠い)でつなぐ。
    before = _blocks(shape, ((0, 0, 0), (2, 24, 2)), ((3, 0, 0), (5, 24, 2)), ((2, 18, 0), (3, 24, 2)), ((2, 1, 0), (3, 2, 2)))
    removed = [(2, j, k) for j in range(18, 24) for k in range(2)]
    assert _judge(before, removed, m) and _ref_creates_neck(before, removed, m)


@pytest.mark.parametrize('m,step,neck_expected', [
    (4, (1, 1, 0), False), (4, (1, 1, 1), False), (5, (1, 1, 0), False),  # 26近傍の段は直径 m の球を通す(#122)
    (4, (3, 3, 0), True),  # 1セル幅の稜線の共有(角のずれ m−1)は26近傍でもない
    (3, (1, 1, 0), True), (3, (1, 1, 1), True),  # m ≤ 3 の斜めの段は √2(m−1) < m なので6のまま断る
])
def test_a_diagonal_step_between_two_cubes_is_a_neck_only_below_the_26_threshold(m, step, neck_expected):
    """2つの m 立方 A・B(角のずれ `step`)を、A の角から B の角まで1軸ずつ進む立方の列でつないだ形から、列だけの材料を抜いて
    A∪B を残す。落ちたら、芯の隣接の閾値(m ≥ 4 で26)が仕様とずれている。"""
    shape = tuple(m + s + 2 for s in step)
    a = np.zeros(shape, bool)
    a[:m, :m, :m] = True
    b = np.zeros(shape, bool)
    b[step[0]:step[0] + m, step[1]:step[1] + m, step[2]:step[2] + m] = True
    path = np.zeros(shape, bool)
    corner = [0, 0, 0]
    for axis in range(3):  # A から B へ面の段だけで進む立方の列(芯が6でもつながる太いつなぎ)
        for _ in range(step[axis]):
            corner[axis] += 1
            path[corner[0]:corner[0] + m, corner[1]:corner[1] + m, corner[2]:corner[2] + m] = True
    before = a | b | path
    removed = [tuple(int(v) for v in c) for c in np.argwhere(before & ~(a | b))]
    assert removed
    assert _judge(before, removed, m) == _ref_creates_neck(before, removed, m) == neck_expected


def test_cutting_a_far_detour_whose_remaining_link_is_a_diagonal_step_widens_the_window_and_is_allowed_from_m_4(monkeypatch):
    """m=4: 左右の板(x=4 の隙間で離れる)を、y=0..9 の (1,1,0) 段の階段と、y=22..25 の面の迂回路 P でつなぐ。P の隙間の列を切ると、
    材料は最初の窓(余白 2m)の中ではつながり直さず、窓を広げて階段まで届く。階段の芯は26でしかつながらないので、26ならくびれではなく、
    6ならくびれ。落ちたら、窓を広げる経路で芯の隣接が26になっていない。"""
    m = 4
    before = np.zeros((40, 64, 40), bool)
    before[0:4, 0:40, 0:4] = True
    before[5:9, 5:40, 0:4] = True
    for k in range(6):
        before[k:k + m, k:k + m, 0:4] = True
    before[0:9, 22:26, 0:4] = True
    removed = [tuple(int(v) for v in c) for c in np.argwhere(before) if c[0] == 4 and 22 <= c[1] < 26]
    calls = []
    real = neck.core_cells
    monkeypatch.setattr(neck, 'core_cells', lambda *a, **k: calls.append(1) or real(*a, **k))
    assert not _judge(before, removed, m)
    assert len(calls) > 2  # 窓を少なくとも1回広げた(1回の窓で core_cells を2回呼ぶ)
    assert not _ref_creates_neck(before, removed, m) and _ref_creates_neck(before, removed, m, adjacency=6)


def _cube_walk(rng, m, shape, *, walks=3, steps=10):
    """m 立方を、ずれ {−1,0,1}³(斜めを含む)と面の段で歩かせた骨格と、少し太らせた塊を重ねた形。"""
    mask = np.zeros(shape, bool)
    c = np.array([int(rng.integers(0, n - m + 1)) for n in shape])
    for _ in range(walks):
        for _ in range(steps):
            d = rng.integers(-1, 2, size=3) if rng.random() < 0.6 else np.eye(3, dtype=int)[int(rng.integers(3))] * rng.choice([-1, 1])
            c = np.clip(c + d, 0, np.array(shape) - m)
            mask[c[0]:c[0] + m, c[1]:c[1] + m, c[2]:c[2] + m] = True
    return mask


@pytest.mark.parametrize('m', [4, 5])
def test_judge_matches_the_26_reference_on_random_moves_where_6_would_differ(m):
    """m ≥ 4(芯は26連結)で、斜めの段を含む立方の骨格から一口を抜く手を参照実装と突き合わせる。落ちたら、前線の膨張か群の作り方の
    どちらかだけが26になっている。通す手・断る手と、同じ手を6の参照で判定すると答えが変わる手のすべてを踏んだことも確かめる
    (6との差が無ければ検査が効いていない)。"""
    answers = []
    for seed in range(8):
        rng = np.random.default_rng(1220 + seed)
        shape = (14, 12, 10)
        mask = _cube_walk(rng, m, shape)
        keep = np.zeros_like(mask)
        if seed % 3 == 0:
            keep[:2, :, :2] = mask[:2, :, :2]
        k = keep if keep.any() else None
        candidates = np.argwhere(mask & ~keep)
        for index in rng.choice(len(candidates), size=min(10, len(candidates)), replace=False):
            cell = candidates[index]
            box = np.array([cell, np.minimum(cell + rng.integers(0, 3, 3), np.array(shape) - 1)])
            removed = [tuple(c) for c in np.argwhere(mask & ~keep) if (c >= box[0]).all() and (c <= box[1]).all()]
            got = _judge(mask, removed, m, k)
            assert got == _ref_creates_neck(mask, removed, m, k)
            answers.append((got, _ref_creates_neck(mask, removed, m, k, adjacency=6)))
    assert len(answers) >= 60 and {got for got, _ in answers} == {True, False}
    assert any(got != six for got, six in answers)


@pytest.mark.parametrize('seed', range(10))
@pytest.mark.parametrize('m', [2, 3])
def test_judge_matches_reference_on_random_moves(seed, m):
    rng = np.random.default_rng(seed)
    mask = ndimage.binary_opening(rng.random((9, 8, 7)) < 0.8, structure=np.ones((m,) * 3, bool))
    keep = np.zeros_like(mask)
    if seed % 3 == 0:
        keep[:2, :, :2] = mask[:2, :, :2]
    candidates = np.argwhere(mask & ~keep)
    for index in rng.choice(len(candidates), size=min(12, len(candidates)), replace=False):
        cell = candidates[index]
        box = np.array([cell, np.minimum(cell + rng.integers(0, 2, 3), np.array(mask.shape) - 1)])
        removed = [tuple(c) for c in np.argwhere(mask & ~keep) if (c >= box[0]).all() and (c <= box[1]).all()]
        assert _judge(mask, removed, m, keep if keep.any() else None) == _ref_creates_neck(mask, removed, m, keep if keep.any() else None)


def test_static_diagnostic_counts_each_pair():
    """静的診断は組ごとに数える(塊を any でまとめない)。面接触のくびれだけの形は全組がつながらない。"""
    mask = _blocks((14, 14, 8), ((0, 0, 1), (6, 6, 7)), ((6, 5, 1), (12, 11, 7)))
    result = neck.static_necks(mask, min_width.CoreConnectivity(6))
    assert result['pairs_without_common_cube'] == result['disconnected_pairs'] == 6
