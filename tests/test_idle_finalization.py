from pathlib import Path
from types import SimpleNamespace
import importlib

import numpy as np
import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox


def load_script(monkeypatch, name):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'topopt_checks' / 'idle_validation'))
    return importlib.import_module(name)


def test_stage09_convert_saves_mesh_boolean_result(monkeypatch, tmp_path):
    fs = load_script(monkeypatch, 'final_shapes_09')
    mask = np.ones((4, 4, 4), dtype=bool)
    grid = SimpleNamespace(shape=mask.shape, spacing=1., element_volume=1., origin=np.zeros(3))
    monkeypatch.setattr(fs, 'WORK', tmp_path)
    monkeypatch.setattr(fs, 'load_shape', lambda _: dict(
        ctx=SimpleNamespace(grid=grid), mask=mask,
        bundle=SimpleNamespace(keep_body_ids=[], exclude_body_ids=[]), doc=None,
        spec=dict(load_n=1., width_mm=1.)))
    monkeypatch.setattr(fs, 'saved_reference', lambda _: dict(mask_hash='test'))
    monkeypatch.setattr(fs, '_mask_stats', lambda *args: dict(mask_hash='test'))
    monkeypatch.setattr(fs, '_conditions', lambda *args: {})
    monkeypatch.setattr(fs, '_occupancy', lambda *args: (mask, {}))
    monkeypatch.setattr(fs, '_fine_width_report', lambda *args: {})
    monkeypatch.setattr(fs._common, 'peak_memory_mb', lambda: {})
    result = fs.convert('test')
    assert result['final']['status'] == 'ok', result['final']
    assert result['final']['mesh_checks']['failed'] == []
    with np.load(tmp_path / 'test_final_mesh.npz') as saved:
        assert len(saved['faces']) == result['final']['triangles']


def test_stage09_legacy_cut_is_independent_of_product_api(monkeypatch):
    diagnosis = load_script(monkeypatch, 'diagnose_cut_09')
    source = BRepPrimAPI_MakeBox(2, 2, 2).Shape()
    tool = BRepPrimAPI_MakeBox(1, 1, 1).Shape()
    result, diagnostics = diagnosis.boolean(source, [tool], 'cut')
    report = diagnosis.describe(result, diagnostics)
    assert report['valid'] and report['solid_count'] == 1
    assert not report['has_errors'] and not report['has_warnings']
    assert report['total_volume_mm3'] == pytest.approx(7)
