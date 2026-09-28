"""`_project_mask_exact_subdivide` の体積保存・整合チェック。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))

from refine_shape import _project_mask_exact_subdivide  # noqa: E402


class _GridStub:
    def __init__(self, origin, spacing, shape):
        self.origin = np.asarray(origin, dtype=float)
        self.spacing = np.asarray(spacing, dtype=float) if np.ndim(spacing) else float(spacing)
        self.shape = tuple(shape)


class _SpaceStub:
    def __init__(self, grid: _GridStub):
        self.grid = grid


def test_exact_subdivide_preserves_volume_no_crop_pad():
    rng = np.random.default_rng(0)
    src_mask = rng.random((3, 3, 3)) > 0.5
    level = 2
    src_origin = np.array([1.0, 2.0, 3.0])
    src_spacing = 4.0
    fine_spacing = src_spacing / level
    fine_shape = tuple(s * level for s in src_mask.shape)
    space = _SpaceStub(_GridStub(src_origin, fine_spacing, fine_shape))

    fine_mask, diag = _project_mask_exact_subdivide(src_mask, level, space, src_origin, src_spacing)

    assert fine_mask.shape == fine_shape
    assert fine_mask.sum() == src_mask.sum() * level**3
    assert diag["cropped_material_cells"] == 0
    assert diag["padded_cells"] == 0
    assert diag["volume_before_crop_mm3"] == pytest.approx(diag["volume_after_crop_mm3"])


def test_exact_subdivide_rejects_non_integer_origin_offset():
    src_mask = np.ones((2, 2, 2), bool)
    level = 2
    src_origin = np.zeros(3)
    src_spacing = 2.0
    fine_spacing = src_spacing / level
    bad_origin = src_origin + np.array([0.1, 0.0, 0.0])
    space = _SpaceStub(_GridStub(bad_origin, fine_spacing, (4, 4, 4)))

    with pytest.raises(ValueError, match="整数倍"):
        _project_mask_exact_subdivide(src_mask, level, space, src_origin, src_spacing)


def test_exact_subdivide_reports_cropped_material():
    src_mask = np.zeros((3, 3, 3), bool)
    src_mask[-1, :, :] = True
    level = 2
    src_origin = np.zeros(3)
    src_spacing = 2.0
    fine_spacing = src_spacing / level
    full_shape = tuple(s * level for s in src_mask.shape)
    fine_shape = (full_shape[0] - 2, full_shape[1], full_shape[2])
    space = _SpaceStub(_GridStub(src_origin, fine_spacing, fine_shape))

    fine_mask, diag = _project_mask_exact_subdivide(src_mask, level, space, src_origin, src_spacing)

    assert diag["cropped_material_cells"] > 0
    assert fine_mask.sum() < src_mask.sum() * level**3
