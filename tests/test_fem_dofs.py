"""`fem.Model` の拘束・自由な自由度の集合。落ちたら荷重や拘束が別のDOFへ入る。"""
import numpy as np
import pytest

from duocad.topopt import fem


def _load_on(ndof, free):
    vector = np.zeros(ndof)
    vector[free[0]] = 1.0
    return vector


@pytest.mark.parametrize('shape', [(4, 3, 2), (6, 5, 4)])
def test_fixed_and_free_match_the_set_operations_they_replace(shape):
    """`Model` は候補ごとに作り直すので、ここは集合演算ではなくブール配列で取っている
    (`union1d` / `setdiff1d` は ndof が百万を超えると秒の桁になる)。同じ配列が出ること。"""
    rng = np.random.default_rng(0)
    ndof = 3 * (shape[0] + 1) * (shape[1] + 1) * (shape[2] + 1)
    for _ in range(5):
        fixed = np.unique(rng.integers(0, ndof, size=ndof // 5))
        extra = np.unique(rng.integers(0, ndof, size=ndof // 7))
        union = np.union1d(fixed, extra)
        free = np.setdiff1d(np.arange(ndof), union)
        if free.size == 0:
            continue
        model = fem.Model(*shape, fixed_dofs=fixed, load=_load_on(ndof, free), extra_fixed_dofs=extra)
        assert np.array_equal(model.fixed, union)
        assert np.array_equal(model.free, free)

    # 追加の拘束を渡さない経路(`extra_fixed_dofs=None`)も同じ定義であること。
    fixed = np.unique(rng.integers(0, ndof, size=ndof // 5))
    free = np.setdiff1d(np.arange(ndof), fixed)
    model = fem.Model(*shape, fixed_dofs=fixed, load=_load_on(ndof, free))
    assert np.array_equal(model.free, free)


def test_out_of_range_extra_dofs_are_rejected_before_they_are_used_as_indices():
    ndof = 3 * 5 * 4 * 3
    fixed = np.arange(12)
    with pytest.raises(ValueError, match='追加の拘束'):
        fem.Model(4, 3, 2, fixed_dofs=fixed, load=np.ones(ndof),
                  extra_fixed_dofs=np.array([ndof], dtype=np.int64))


@pytest.mark.parametrize('shape', [(1, 1, 1), (4, 3, 2), (7, 5, 6)])
def test_the_element_dof_table_keeps_its_numbering(shape):
    """落ちたら、要素の自由度の並び(頂点 (a,b,c) の辞書順・各頂点 3n,3n+1,3n+2)が変わり、荷重・拘束・組み立てが別の自由度へ入る。"""
    nelx, nely, nelz = shape
    ny1, nz1 = nely + 1, nelz + 1
    rows = []
    for x in range(nelx):
        for y in range(nely):
            for z in range(nelz):
                nodes = [(x + a) * ny1 * nz1 + (y + b) * nz1 + (z + c) for a in (0, 1) for b in (0, 1) for c in (0, 1)]
                rows.append([3 * n + d for n in nodes for d in range(3)])
    table = fem._build_edof(*shape)
    assert table.dtype == np.int32
    assert np.array_equal(table, np.array(rows))


@pytest.mark.parametrize('count', [1, 37, 4000])
def test_the_energy_of_some_elements_is_bit_identical_to_the_full_compliance_field(count):
    """落ちたら、材料の要素だけで計算した要素エネルギーが全要素で計算した値とずれ、探索の順位(=軌道)が変わる。"""
    rng = np.random.default_rng(count)
    model = fem.Model(30, 14, 12)
    u = rng.standard_normal(model.ndof)
    cells = np.sort(rng.choice(model.nele, size=count, replace=False))
    _, full = model.compliance(np.ones((30, 14, 12)), 3.0, u)
    assert np.array_equal(model.element_energy(u, cells), full.ravel()[cells])


def test_the_element_dof_table_is_int32_for_numpy_integer_shapes():
    """落ちたら、numpy の整数で格子の形を渡すと要素の自由度表が int64 になり、メモリが倍になる。"""
    assert fem._build_edof(np.int64(4), np.int64(3), np.int64(2)).dtype == np.int32
