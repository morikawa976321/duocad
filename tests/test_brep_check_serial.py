"""妥当性の検査(`brep_checks._analyzer_valid`)を直列で走らせる。"""
from __future__ import annotations

from duocad import brep_checks


def test_the_validity_check_runs_serially(monkeypatch, box_session):
    """落ちたら、`BRepCheck_Analyzer` を並列で走らせている。並列は同じ形を時々「不正」と答え、再生や確定が揺れて断られる。"""
    calls = []
    real = brep_checks.BRepCheck_Analyzer

    def recording(shape, geometric_checks=True, parallel=False):
        calls.append(parallel)
        return real(shape, geometric_checks, parallel)

    monkeypatch.setattr(brep_checks, "BRepCheck_Analyzer", recording)
    assert brep_checks._analyzer_valid(box_session.document.bodies[0].shape)
    assert calls == [False]
