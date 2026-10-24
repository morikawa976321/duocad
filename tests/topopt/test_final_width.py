"""完成形の最小幅(中軸上の太さ。`final_width`)。落ちたら、くびれを見逃すか、太い形の角・継ぎ目・表面のうねりを幅不足と誤る。"""
import numpy as np
import pytest

from duocad.topopt import final_width, mesh, voxelize

GRID = voxelize.Grid((0.0, 0.0, 0.0), 1.0, (20, 20, 8))


def _blocks(*boxes):
    mask = np.zeros(GRID.shape, bool)
    for lo, hi in boxes:
        mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]] = True
    return mask


@pytest.mark.parametrize('second, widest', [
    (((6, 5, 1), (12, 11, 7)), 1.5),   # 面で1セル幅だけ接する(#120 の hanger の形)
    (((5, 5, 1), (11, 11, 7)), 2.0),   # 斜めに1セルだけ重なる
])
def test_neck_between_blocks_is_thin(second, widest):
    result = final_width.measure_voxels(_blocks(((0, 0, 1), (6, 6, 7)), second), GRID, required_mm=6)
    assert result['status'] == 'thin'
    assert result['min_width_mm'] < widest


def test_t_junction_and_corners_are_not_thin():
    result = final_width.measure_voxels(_blocks(((0, 0, 1), (20, 6, 7)), ((7, 6, 1), (13, 20, 7))), GRID, required_mm=6)
    assert result['status'] == 'pass'
    assert result['min_width_mm'] == pytest.approx(6.0, abs=result['tolerance_mm'])


def test_exempt_cells_are_skipped():
    mask = _blocks(((0, 0, 1), (6, 6, 7)), ((6, 5, 1), (12, 11, 7)))
    exempt = np.zeros(GRID.shape, bool)
    exempt[4:8, 3:8, :] = True
    result = final_width.measure_voxels(mask, GRID, required_mm=6, exempt=exempt)
    assert result['status'] == 'pass' and result['exempt_points'] > 0


@pytest.mark.parametrize('thickness', [4, 6])
def test_smoothed_plate_keeps_its_width(thickness):
    surface = mesh.build_surface_from_mask(np.ones((32, 12, thickness), bool))
    result = final_width.measure(surface.verts_smooth, surface.faces, spacing=1.0, required_mm=thickness)
    assert result['status'] == 'pass'
