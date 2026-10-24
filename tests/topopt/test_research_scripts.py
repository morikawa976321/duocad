"""`topopt_checks/` の測定・研究スクリプトの取りこぼし(段階4の `PeelWalk` への追随など)の回帰。

落ちたら、測定の列(peel の秒・数)が黙って0になる、研究の再現手順が TypeError / KeyError / AssertionError で落ちる。
"""
from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from duocad.paths import REPO_ROOT
from duocad.topopt import binary_search as bs
from duocad.topopt import trace, voxelize
from topopt_support import appendage_conditions

for folder in (REPO_ROOT / "topopt_checks", REPO_ROOT / "topopt_checks" / "idle_validation"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


# ---------------------------------------------------------------- peel の集計


def test_peel_stats_are_collected_from_the_walk_stage_four_uses() -> None:
    """落ちたら、測定スクリプトが `binary_search.peel` を包んで集めた peel の秒・数が、段階4が `PeelWalk` を
    直接使うので常に0になる。"""
    from _peel_stats import collecting_peel_stats

    conditions, design, _ = appendage_conditions(0.001)

    with collecting_peel_stats() as stats:
        result = bs.find_best_feasible(design, conditions, max_evaluations=6)

    reduction = result.diagnostics["reduction"]
    assert reduction["peel_calls"] > 0
    assert len(stats) == reduction["peel_calls"]
    assert sum(stat.picked for stat in stats) > 0
    assert sum(stat.seconds for stat in stats) == pytest.approx(reduction["peel_seconds"])


def test_neck_judge_comparison_reports_the_peel_seconds(tmp_path, monkeypatch) -> None:
    """落ちたら、compare_neck_judge_120 の `peel_seconds` の列が常に0になる。"""
    compare = importlib.import_module("compare_neck_judge_120")
    monkeypatch.setattr(compare, "OUT", tmp_path)
    conditions, design, _ = appendage_conditions(0.001)

    row = compare._run(SimpleNamespace(mask=design), conditions, 1, rule=True, name="tiny")

    assert row["peel_seconds"] > 0


def test_core_adjacency_comparison_reports_the_peel_seconds(tmp_path, monkeypatch) -> None:
    """落ちたら、compare_core_adjacency_122 の `peel_seconds` の列が常に0になる。"""
    compare = importlib.import_module("compare_core_adjacency_122")
    monkeypatch.setattr(compare, "OUT", tmp_path)
    monkeypatch.setattr(compare, "_final", lambda *args, **kwargs: {})
    conditions, design, _ = appendage_conditions(0.001)

    row = compare._run(SimpleNamespace(mask=design), conditions, 1, adj=26, name="tiny", doc=None, bundle=None, width_mm=10.0)

    assert row["peel_seconds"] > 0


# ---------------------------------------------------------------- 研究スクリプトの取りこぼし


def test_the_legacy_stop_policy_has_a_number_for_its_tolerance() -> None:
    """落ちたら、`stop_policy="legacy"` の許容が製品の既定(`None`)に連動し、`目標 × (1 + 許容)` が TypeError になる。"""
    methods = importlib.import_module("search_methods")
    config = methods.AdaptiveConfig(stop_policy="legacy")

    assert 1.0 * (1 + config.legacy_target_tolerance) > 1.0


def test_the_corpus_keeps_the_mask_the_product_meshed_and_counts_the_filled_cells() -> None:
    """落ちたら、空隙埋めで材料が1セル増えた条件を、捕まえたマスクが探索の採用マスクと違うと断って記録が error になる。"""
    sweep = importlib.import_module("sweep_smoothing_corpus")
    search = np.zeros((4, 4, 4), bool)
    search[1:3, 1:3, 1:3] = True
    filled = search.copy()
    filled[1, 1, 0] = True

    assert sweep.filled_cells_of(filled, search) == 1
    assert sweep.filled_cells_of(search, search) == 0
    with pytest.raises(AssertionError):
        sweep.filled_cells_of(search, filled)


def test_recorded_geometry_is_read_from_the_snapshot_that_has_the_raw_vertices(tmp_path, monkeypatch) -> None:
    """落ちたら、analyze_recorded_geometry が `raw_vertices` を持たない集合演算後の記録を読み、現行の全記録で KeyError になる。"""
    analyze = importlib.import_module("analyze_recorded_geometry")
    monkeypatch.setattr(trace, "DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(trace.Recorder, "provenance", lambda self: None)
    raw = np.array([[0.0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
    faces = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])
    with trace.recording() as recorder:
        trace.final_mesh(
            raw, raw * 0.9, faces, voxelize.Grid((0.0, 0.0, 0.0), 1.0, (2, 2, 2)),
            final_vertices=raw * 0.8, final_faces=faces,
        )

    world = analyze.load_world(recorder.path)

    assert np.array_equal(world["raw_vertices"], raw)
    assert np.allclose(world["vertices"], raw * 0.9)
    assert np.array_equal(world["faces"], faces)
