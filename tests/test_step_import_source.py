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


def test_refusal_for_faces_outside_the_solids_names_the_next_step(tmp_path):
    """落ちたら、ソリッドの外の面で断られても、次に何をすればよいかが文に無い。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    polygon = BRepBuilderAPI_MakePolygon()
    for point in ((30, 0, 0), (40, 0, 0), (40, 10, 0), (30, 10, 0)):
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    path = tmp_path / "mixed.step"
    kernel.write_step(kernel.make_compound([box, face]), str(path))

    with pytest.raises(ValueError, match="ソリッドだけにして書き出し直す"):
        read_step_source(path)
