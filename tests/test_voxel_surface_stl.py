"""`topopt_checks/measure_min_width_floor.voxel_surface`(ボクセルのままのSTL)。落ちたら、書き出した形が解析した形と違う。"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "topopt_checks"))
from measure_min_width_floor import voxel_surface  # noqa: E402


def _signed_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    tri = verts[faces]
    return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def test_only_faces_touching_an_empty_cell_or_the_grid_edge_are_written_and_they_face_outward() -> None:
    rng = np.random.default_rng(0)
    for _ in range(20):
        mask = rng.random((5, 4, 6)) > 0.5
        verts, faces = voxel_surface(mask, (1.0, 2.0, 3.0), 0.5)
        padded = np.pad(mask, 1)
        exposed = sum(
            int((mask & ~np.roll(padded, -sign, axis=axis)[1:-1, 1:-1, 1:-1]).sum()) for axis in range(3) for sign in (-1, 1)
        )
        assert len(faces) == 2 * exposed
        # 外向きの巻き順なら、符号つき体積が材料の体積に一致する(裏返っていれば負になる)。
        assert abs(_signed_volume(verts, faces) - mask.sum() * 0.5**3) < 1e-9


def test_coordinates_are_origin_plus_spacing_times_the_index() -> None:
    mask = np.zeros((3, 3, 3), dtype=bool)
    mask[1, 2, 0] = True
    verts, _ = voxel_surface(mask, (10.0, 20.0, 30.0), 2.0)
    assert verts.min(axis=0).tolist() == [12.0, 24.0, 30.0]
    assert verts.max(axis=0).tolist() == [14.0, 26.0, 32.0]
