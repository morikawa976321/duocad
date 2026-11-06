"""名前つきの値の採番(`parameters.register_value`)は、表の取り除き方によらず「使われていない最小の `d<n>`」を返す。"""
from __future__ import annotations

import copy
import random
import time

from duocad import parameters
from duocad.document import Document
from duocad.parameter_table import ParameterTable


def _smallest_free(table: dict) -> str:
    number = 1
    while f"d{number}" in table:
        number += 1
    return f"d{number}"


def test_a_name_freed_by_any_removal_is_given_out_again() -> None:
    """落ちたら、取り除いた番号を飛ばして採番し、保存したログの再生で名前が別の値を指す。"""
    random.seed(7)
    doc = Document()
    for step in range(600):
        action = random.choice(["add", "add", "add", "pop", "del", "popitem", "clear", "update", "other-key"])
        table = doc.parameters
        if action == "add":
            expected = _smallest_free(table)
            assert parameters.register_value(doc, float(step)) == expected
        elif action == "pop" and table:
            table.pop(random.choice(list(table)), None)
        elif action == "del" and table:
            del table[random.choice(list(table))]
        elif action == "popitem" and table:
            table.popitem()
        elif action == "clear" and random.random() < 0.1:
            table.clear()
        elif action == "update":
            table.update({f"d{random.randint(1, 40)}": 1.0})
        elif action == "other-key":
            table["width"] = 2.0
            table.pop("width")


def test_a_restored_table_gives_the_same_names_as_the_original() -> None:
    """落ちたら、プレビュー後の復元(`clear` → `update`)や複製のあとで番号が飛ぶ。"""
    doc = Document()
    for value in range(10):
        parameters.register_value(doc, float(value))
    saved = copy.deepcopy(doc.parameters)
    del doc.parameters["d3"]
    doc.parameters.clear()
    doc.parameters.update(saved)
    assert parameters.register_value(doc, 0.0) == "d11"
    twin = copy.deepcopy(doc.parameters)
    twin.pop("d2")
    assert isinstance(twin, ParameterTable)
    assert twin.first_free_name() == "d2"


def test_a_plain_dict_assigned_to_the_document_is_still_numbered_from_the_smallest_free() -> None:
    """落ちたら、表を素の dict で差し替えた文書で採番が壊れる。"""
    doc = Document()
    doc.parameters = {"d1": 1.0, "d3": 3.0}
    assert parameters.register_value(doc, 0.0) == "d2"
    assert parameters.register_value(doc, 0.0) == "d4"


def test_numbering_thousands_of_values_is_not_quadratic() -> None:
    """落ちたら、採番が毎回 d1 から数え直す形に戻る(8000 個で 3 秒前後かかっていた。ここは 20000 個を 5 秒未満)。"""
    doc = Document()
    started = time.perf_counter()
    for value in range(20000):
        parameters.register_value(doc, float(value))
    assert time.perf_counter() - started < 5.0


def test_a_non_finite_count_is_not_a_positive_integer() -> None:
    """落ちたら、反復の個数に Infinity が来ると ValueError でなく OverflowError になり、MCP が invalid_argument でなく internal を返す。"""
    assert not parameters.is_positive_int(float("inf"))
    assert not parameters.is_positive_int(float("-inf"))
    assert not parameters.is_positive_int(float("nan"))
    assert parameters.is_positive_int(3.0)
    assert not parameters.is_positive_int(2.5)
