from pathlib import Path
from types import SimpleNamespace
import importlib

import numpy as np
import pytest


@pytest.mark.parametrize('status', ['pass', 'fail', 'unknown'])
def test_saved_mask_must_pass_reinspection_before_replaying_search(monkeypatch, status):
    from duocad.topopt import acceptance

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'topopt_checks'))
    verify = importlib.import_module('verify_tube_fill')
    report = dict(status=status, checks=dict(safety=dict(status=status, safety_factor=1.5)))
    monkeypatch.setattr(acceptance, 'inspect', lambda *args, **kwargs: report)
    conditions = SimpleNamespace(grid=None, inspect_kwargs=lambda: {})
    captured = {}
    result = verify._inspect_saved_mask(np.ones((3, 3, 3), dtype=bool), conditions, captured)
    assert captured['search_report'] is report
    if status == 'pass':
        assert result.status == 'feasible' and result.best_feasible is not None
    else:
        assert result.status == 'no_feasible_found' and result.best_feasible is None
