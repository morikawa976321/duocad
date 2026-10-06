import numpy as np
import pytest

from duocad.topopt.thin_voids import MAX_ROUNDS, fill_tubes, sandwiched_axes


def plate(shape=(7, 7, 3)):
    return np.ones(shape, dtype=bool)


def test_sandwiched_axes_classifies_tube_pit_groove_and_wide_hole():
    """管(貫通 1×1)と窪み(深さ1の 1×1)は2軸、溝(1幅の切れ込み)は1軸、2×2 の穴は0軸。材料セルは常に0。"""
    mask = plate()
    mask[3, 3, :] = False          # z 方向の貫通 1×1 の管
    mask[1, 1, 2] = False          # 上面の 1×1 の窪み
    mask[5, :, 2] = False          # 上面を横切る 1幅の溝
    mask[1:3, 4:6, 0] = False      # 底面の 2×2 の穴
    axes = sandwiched_axes(mask)
    assert axes[3, 3, :].tolist() == [2, 2, 2]
    assert axes[1, 1, 2] == 2
    assert axes[5, :, 2].max() == 1 and axes[5, 3, 2] == 1
    assert axes[1:3, 4:6, 0].max() == 0
    assert axes[mask].max() == 0
    # 格子の縁の空セルは、外を材料と数えない
    edge = plate((3, 3, 3))
    edge[0, 1, 1] = False
    assert sandwiched_axes(edge)[0, 1, 1] == 2


def test_fill_tubes_fills_tubes_and_pits_but_not_grooves_or_wide_holes():
    mask = plate()
    mask[3, 3, :] = False
    mask[1, 1, 2] = False
    mask[5, :, 2] = False
    mask[1:3, 4:6, 0] = False
    before = mask.copy()
    filled, info = fill_tubes(mask, np.ones_like(mask))
    assert np.array_equal(mask, before)  # 入力は変えない
    assert info == dict(cells=4, rounds=[4])
    assert filled[3, 3, :].all() and filled[1, 1, 2]
    assert not filled[5, :, 2].any() and not filled[1:3, 4:6, 0].any()
    assert sandwiched_axes(filled).max() <= 1


def test_fill_tubes_respects_allowed_and_returns_copy_when_nothing_to_fill():
    mask = plate()
    mask[3, 3, :] = False
    allowed = np.ones_like(mask)
    allowed[3, 3, 1] = False       # 真ん中のセルは除外ソリッド由来(設計空間の外)
    filled, info = fill_tubes(mask, allowed)
    assert info == dict(cells=2, rounds=[2])
    assert filled[3, 3, 0] and filled[3, 3, 2] and not filled[3, 3, 1]
    same, info = fill_tubes(plate(), np.ones((7, 7, 3), dtype=bool))
    assert info == dict(cells=0, rounds=[]) and same.all() and same is not plate()
    with pytest.raises(ValueError, match='形が違う'):
        fill_tubes(mask, np.ones((7, 7, 4), dtype=bool))


def test_fill_tubes_cascades_when_filling_closes_a_neighbour():
    """窪みを埋めると、その下の 1幅の空セルが新たに四方を閉じられて次の回で埋まる(反復の実測は最大3回)。"""
    mask = plate((7, 7, 5))
    mask[3, 3, 4] = False          # 上面の 1×1 の窪み(2軸)
    mask[3, 3, 3] = False          # その直下(2軸): 止まり穴 深さ2
    mask[3, 2:5, 2] = False        # さらに下に y 方向へ3セルの 1幅の空隙(真ん中は上が空なので1軸)
    filled, info = fill_tubes(mask, np.ones_like(mask))
    assert len(info['rounds']) >= 2 and info['cells'] == 5
    assert filled.all()


def test_fill_tubes_refuses_to_stop_with_tubes_left(monkeypatch):
    import duocad.topopt.thin_voids as module
    monkeypatch.setattr(module, 'MAX_ROUNDS', 0)
    mask = plate()
    mask[3, 3, :] = False
    with pytest.raises(RuntimeError, match='埋めても残る'):
        fill_tubes(mask, np.ones_like(mask))
    assert MAX_ROUNDS >= 3  # 64条件の実測(最大3回)より大きいこと
