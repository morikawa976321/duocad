"""`catalog.Param` の型宣言と `check_arguments` の型検査が噛み合うことの回帰テスト。"""

import pytest

from duocad import catalog


def test_a_param_declared_with_a_tuple_type_is_refused_at_declaration():
    """落ちたら、tuple で書いた型が `check_arguments` の型検査を素通りして、型違いの値が黙って通る。"""
    with pytest.raises(TypeError, match="focus"):
        catalog.Param("focus", ("object", "null"), "x")


def test_every_declared_param_type_is_a_string_or_a_list():
    """落ちたら、カタログに型検査の対象外の宣言(tuple など)が入っている。"""
    for command in catalog.COMMANDS:
        for param in command.params:
            assert isinstance(param.type, (str, list)), (command.name, param.name)


def test_set_view_resolution_focus_rejects_a_string():
    """落ちたら、`focus: "abc"` が引数検査を通る。"""
    schema = next(c for c in catalog.COMMANDS if c.name == "set_view_resolution").input_schema()

    base = {"world_per_pixel": 0.1}
    assert catalog.check_arguments(schema, {**base, "focus": "abc"}) is not None
    assert catalog.check_arguments(schema, {**base, "focus": None}) is None
    assert catalog.check_arguments(schema, {**base, "focus": {"b": [1]}}) is None
