"""並列のブーリアン(`brep_operations.build_boolean`)が直列と同じ形・同じ履歴を返すこと。落ちたら、保存済みの操作ログを
再生したときに面・辺の添字が別の面を指す(OCCT を上げたときなど。並列をやめて直列へ戻す)。"""
from __future__ import annotations

import io

import pytest
from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.TopTools import TopTools_ListOfShape
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import brep_operations, brep_topology


def _holed_plate():
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-30.0, -20.0, 0.0), 60.0, 40.0, 6.0).Shape()
    holes = [
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-25.0 + 10.0 * i, -15.0 + 10.0 * j, -1.0), gp_Dir(0.0, 0.0, 1.0)), 1.5, 8.0).Shape()
        for i in range(6)
        for j in range(4)
    ]
    for hole in holes:
        plate = _serial("cut", plate, hole).Shape()
    return plate


def _rounded_block():
    block = BRepPrimAPI_MakeBox(gp_Pnt(-10.0, -10.0, 0.0), 20.0, 20.0, 10.0).Shape()
    fillet = BRepFilletAPI_MakeFillet(block)
    for edge in brep_topology._face_edit_edges(block):
        fillet.Add(2.0, edge)
    fillet.Build()
    return fillet.Shape()


def _serial(kind, first, second):
    builder = {"fuse": BRepAlgoAPI_Fuse, "cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common}[kind]()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(first)
    tools.Append(second)
    builder.SetArguments(arguments)
    builder.SetTools(tools)
    builder.SetRunParallel(False)
    builder.Build()
    return builder


def _bytes(shape) -> bytes:
    stream = io.BytesIO()
    BinTools.Write_s(shape, stream, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
    return stream.getvalue()


def _face_history(builder, source, result) -> list:
    source_map, result_map = brep_topology._face_map(source), brep_topology._face_map(result)
    return [
        ([result_map.FindIndex(face) for face in brep_topology.listed(builder.Modified(source_map.FindKey(i)))],
         bool(builder.IsDeleted(source_map.FindKey(i))))
        for i in range(1, source_map.Extent() + 1)
    ]


CASES = {
    "穴あき板をトンネルで切る": (_holed_plate, lambda: BRepPrimAPI_MakeBox(gp_Pnt(-40.0, -3.0, 2.0), 80.0, 6.0, 2.0).Shape()),
    "穴あき板に球": (_holed_plate, lambda: BRepPrimAPI_MakeSphere(gp_Pnt(3.0, 2.0, 4.0), 9.0).Shape()),
    "丸めた箱を斜めの円柱で": (
        _rounded_block,
        lambda: BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-15.0, -12.0, 2.0), gp_Dir(1.0, 0.8, 0.3)), 3.0, 40.0).Shape(),
    ),
}


@pytest.mark.parametrize("kind", ["cut", "fuse", "common"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_parallel_boolean_gives_the_serial_shape_and_history(case, kind):
    """落ちたら、並列と直列で結果のバイト列か面の由来が違う。"""
    make_target, make_tool = CASES[case]
    target, tool = make_target(), make_tool()
    copies = [(BRepBuilderAPI_Copy(target, True, False).Shape(), BRepBuilderAPI_Copy(tool, True, False).Shape()) for _ in range(2)]
    parallel = brep_operations.build_boolean(kind, *copies[0])
    serial = _serial(kind, *copies[1])
    assert parallel.IsDone() and serial.IsDone()
    assert _bytes(parallel.Shape()) == _bytes(serial.Shape())
    assert _face_history(parallel, copies[0][0], parallel.Shape()) == _face_history(serial, copies[1][0], serial.Shape())
