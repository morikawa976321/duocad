"""STEP ファイルの読み込み口 `read_step_source` の入力の受け方。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel
from duocad.io_step import read_step_source


def test_utf8_bom_in_front_of_a_step_file_is_ignored(tmp_path):
    """落ちたら、BOM を足しただけで同じ STEP が読めなくなる。"""
    plain = tmp_path / "plain.step"
    kernel.write_step(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), str(plain))
    with_bom = tmp_path / "bom.step"
    with_bom.write_bytes(b"\xef\xbb\xbf" + plain.read_bytes())

    name, text, shape = read_step_source(with_bom)

    assert name == "bom.step"
    assert not text.startswith("﻿")
    assert shape is not None


@pytest.mark.parametrize("content", [b"this is not a STEP file\n", b"   \n"])
def test_unreadable_file_is_reported_by_its_own_name_without_the_staging_path(tmp_path, content):
    """落ちたら、画面で選んだ実ファイルなのに「埋め込まれた」と出て、一時パスが見える。"""
    broken = tmp_path / "broken.step"
    broken.write_bytes(content)

    with pytest.raises(ValueError) as caught:
        read_step_source(broken)

    message = str(caught.value)
    assert "broken.step" in message
    assert "埋め込まれた" not in message
    assert "embedded.step" not in message
    assert "duocad-step-" not in message
