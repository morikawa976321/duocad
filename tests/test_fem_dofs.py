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
