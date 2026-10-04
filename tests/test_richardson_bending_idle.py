"""Richardson一般化と曲げパッチの位置補合理論値のスモーク。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))

import _fem_probe  # noqa: E402
from analytic_checks import run_bending  # noqa: E402


def test_richardson_general_round_trip_non_geometric():
    """非等比格子幅で合成データから次数pと収束先v_infを復元する。"""
    p_true = 2.0
    v_inf = 100.0
    c_coef = 3.0
    spacings = [4.0, 2.0, 1.0, 0.6]
    values = [v_inf + c_coef * h ** p_true for h in spacings]
    out = _fem_probe.richardson_extrapolate_general(values, spacings)
    assert out is not None
    for p_est, v_ext in zip(out["order_estimates"], out["extrapolated"]):
        assert p_est is not None
        assert v_ext is not None
        assert p_est == pytest.approx(p_true, rel=1e-6, abs=1e-6)
        assert v_ext == pytest.approx(v_inf, rel=1e-6, abs=1e-6)


def test_richardson_general_matches_legacy_on_geometric_refinement():
    spacings = [4.0, 2.0, 1.0]
    p_true = 2.0
    v_inf = 50.0
    c_coef = 1.5
    values = [v_inf + c_coef * h ** p_true for h in spacings]
    ratios = [spacings[i] / spacings[i + 1] for i in range(len(spacings) - 1)]
    legacy = _fem_probe.richardson_extrapolate(values, ratios)
    general = _fem_probe.richardson_extrapolate_general(values, spacings)
    assert legacy is not None and general is not None
    for a, b in zip(legacy["order_estimates"], general["order_estimates"]):
        if a is None:
            assert b is None
        else:
            assert a == pytest.approx(b, rel=1e-9, abs=1e-9)
    for a, b in zip(legacy["extrapolated"], general["extrapolated"]):
        if a is None:
            assert b is None
        else:
            assert a == pytest.approx(b, rel=1e-9, abs=1e-9)


def test_richardson_general_converged_near_zero_diff():
    values = [1.0, 1.0 + 1e-13, 1.0 + 2e-13]
    spacings = [4.0, 2.0, 1.0]
    out = _fem_probe.richardson_extrapolate_general(values, spacings)
    assert out is not None
    assert len(out["window_diagnostics"]) == 1
    assert out["window_diagnostics"][0]["converged_no_extrapolation_needed"] is True
    assert out["order_estimates"][0] is None


def test_bending_position_corrected_theory_approaches_nominal_when_refined():
    """名目理論24MPaは細分極限でのみ正しく、補正値は細分するほど24に近づく。"""
    records = run_bending([1, 2, 4])
    nominal = records[0]["sigma_max_theory_mpa"]
    assert nominal == pytest.approx(24.0, abs=1e-9)
    corrected = [r["sigma_theory_position_corrected_mpa"] for r in records]
    assert all(v is not None for v in corrected)
    assert corrected[0] != pytest.approx(nominal, rel=0.01)
    assert abs(corrected[-1] - nominal) < abs(corrected[0] - nominal)
