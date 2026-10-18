"""非有限の数(NaN / ±inf / `1e999`)と符号化できない文字列を、入口で断る契約。
落ちたとき何を意味するか: これらが verb まで届くと、`extrude` は OCCT の体積計算でプロセスごと落ち、
`pattern_*` / `add_mesh_body` / `sketch_create_offset` は文書・状態配信・保存ファイルに裸の `NaN` を入れる。
"""
from __future__ import annotations

import json
import math

import pytest

from duocad import catalog, kernel_extrude
from duocad.catalog import COMMANDS, check_arguments
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session
from document_support import box_shape
from mcp_support import request
from session_support import select

NON_FINITE = [float("nan"), float("inf"), float("-inf")]
NUMERIC_TYPES = ("number", "integer", "array")


def _numeric_params():
    for command in COMMANDS:
        for param in command.params:
            types = param.type if isinstance(param.type, list) else [param.type]
            if any(t in NUMERIC_TYPES for t in types):
                yield pytest.param(command, param, id=f"{command.name}.{param.name}")


def _wrap(param, value):
    """引数 `param` の型に合う形に `value`(非有限の数)を包む。"""
    types = param.type if isinstance(param.type, list) else [param.type]
    return value if "number" in types or "integer" in types else [value]


@pytest.mark.parametrize("command, param", list(_numeric_params()))
@pytest.mark.parametrize("bad", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_every_numeric_argument_refuses_a_non_finite_number(command, param, bad):
    problem = check_arguments(command.partial_schema(), {param.name: _wrap(param, bad)})
    assert problem is not None and repr(param.name) in problem
    types = param.type if isinstance(param.type, list) else [param.type]
    if "integer" not in types or "number" in types or "array" in types:
        # 整数だけの引数は float を型違いで断る(NaN に限らない)ので、値の検査が断る理由は問わない。
        assert "non-finite" in problem


def test_a_non_finite_number_is_refused_wherever_it_is_nested():
    schema = COMMANDS[0].partial_schema()
    name = next(iter(schema["properties"]))
    # 入れ子の中は型の検査では見えないので、値の検査が配列と辞書の中まで見る。
    assert catalog._unusable_value([1.0, [2.0, float("nan")]]) is not None
    assert catalog._unusable_value({"where": [{"value": float("inf")}]}) is not None
    assert catalog._unusable_value({float("nan"): 1}) is not None
    assert name  # スキーマが空でないことの確認(上の2行は名前に依らない)


def test_a_number_outside_the_float_range_is_refused():
    assert catalog._unusable_value(10**400) is not None
    assert catalog._unusable_value([1, 10**400]) is not None


def test_ordinary_values_pass():
    for value in (0, 1.5, -3, 1e300, "名前", "emoji 😀", [0.0, 1], {"kind": "face", "index": 2}, True, None):
        assert catalog._unusable_value(value) is None, value


@pytest.mark.parametrize("text", ["\ud800", "a\udfffb", ["ok", "\ud83d"], {"name": "\ud800"}])
def test_a_string_that_cannot_be_encoded_as_utf8_is_refused(text):
    assert catalog._unusable_value(text) is not None


def test_a_lone_surrogate_in_a_verb_argument_is_refused_before_it_reaches_the_document():
    command = catalog.BY_NAME["rename"]
    problem = check_arguments(command.input_schema(), {"target": "box", "name": "\ud800"})
    assert problem is not None and "name" in problem and "UTF-8" in problem


@pytest.fixture()
def sketch_session() -> Session:
    session = Session(0.2)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    select(session, [{"sketch": sketch_id, "index": 1}])
    return session


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_extrude_over_mcp_refuses_a_non_finite_distance_and_changes_nothing(sketch_session: Session, bad: str):
    server = McpServer(DocumentTools(sketch_session))
    log_before = len(sketch_session.document.log.entries)
    # `NaN` / `Infinity` は標準外の JSON だが Python の json.loads は受理する。MCP のクライアントが送れる経路を再現する。
    arguments = json.loads('{"distance": %s, "mode": "new"}' % bad)
    response = request(server, "tools/call", {"name": "extrude", "arguments": arguments})
    assert response["error"]["code"] == -32602 and "distance" in response["error"]["message"]
    assert len(sketch_session.document.log.entries) == log_before
    assert not sketch_session.document.bodies


@pytest.mark.parametrize("bad", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_the_extrude_verb_itself_refuses_a_non_finite_distance(sketch_session: Session, bad: float):
    """`preview` はスキーマの検査を通らない経路を持つので、verb 自身も断る(通すと体積計算でプロセスごと落ちる)。"""
    with pytest.raises(ValueError, match="finite"):
        sketch_session.extrude(bad, mode="new")
    assert not sketch_session.document.bodies


@pytest.mark.parametrize("distance", [0.0, 1e-9, 1e-7, 5e-7, 1e7, 1e300])
def test_a_distance_outside_the_range_occt_can_sweep_is_a_value_error(sketch_session: Session, distance: float):
    with pytest.raises(ValueError):
        sketch_session.extrude(distance, mode="new")


@pytest.mark.parametrize("distance", [kernel_extrude.MIN_DISTANCE, 5.0, -5.0, kernel_extrude.MAX_DISTANCE])
def test_the_range_of_a_usable_distance_includes_its_ends(distance: float):
    assert kernel_extrude.check_distance(distance) == distance
    assert math.isfinite(kernel_extrude.check_distance(str(distance)))


def test_extrude_faces_refuses_a_non_finite_distance_before_the_kernel_touches_the_shape():
    shape = box_shape()
    with pytest.raises(ValueError, match="finite"):
        kernel_extrude.extrude_faces(shape, [1], float("nan"), "new")
