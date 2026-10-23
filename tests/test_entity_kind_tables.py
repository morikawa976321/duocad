"""図形種別と動かせる点の表が、Python・カーネル・TSで食い違わないこと。"""
from __future__ import annotations

import pytest

from duocad import kernel_sketch, sketch
from duocad.paths import REPO_ROOT
from node_support import run_node


@pytest.fixture(scope="module")
def ts_point_keys():
    return run_node(REPO_ROOT / "tests" / "eval_entity_kind_tables.mjs", "図形種別の表", json_out=True)


def test_the_three_entity_kind_tables_agree_on_their_key_set(ts_point_keys):
    canonical = set(sketch.ENTITY_KINDS)
    assert canonical, "sketch.ENTITY_KINDS が空"
    assert set(kernel_sketch._KERNEL_ENTITY_KIND_TABLE) == canonical
    assert set(ts_point_keys) == canonical


def test_the_movable_point_tables_agree(ts_point_keys):
    python_table = {kind: list(map(list, sketch.entity_point_keys(kind))) for kind in sketch.ENTITY_KINDS}
    assert ts_point_keys == python_table, "動かせる点のキーまたは順序がPythonとTSで違う"
