"""干渉チェックのテストが共有する形の組み立て(OCP を直に使う。`duocad/` の外なので境界検査の対象外)。"""
from __future__ import annotations

import math

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf


def box(x, y, z, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def cylinder(x, y, z, radius, height):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1)), radius, height).Shape()


def cut(shape, tool):
    operation = BRepAlgoAPI_Cut(shape, tool)
    operation.Build()
    return operation.Shape()


def rotated(shape, degrees):
    transformation = gp_Trsf()
    transformation.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(degrees))
    return BRepBuilderAPI_Transform(shape, transformation, True).Shape()


def filleted_box(x, y, z, dx, dy, dz, radius):
    shape = box(x, y, z, dx, dy, dz)
    maker = BRepFilletAPI_MakeFillet(shape)
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        maker.Add(radius, TopoDS.Edge_s(explorer.Current()))
        explorer.Next()
    maker.Build()
    return maker.Shape()


def plate_with_hole(hole_radius, size=20.0, height=10.0):
    """中心 (0,0) に半径 `hole_radius` の貫通穴を持つ、`size` 角・高さ `height` の板。"""
    return cut(box(-size / 2, -size / 2, 0, size, size, height), cylinder(0, 0, -1, hole_radius, height + 2))


def nested_boxes(clearance):
    """角を丸めた外箱(40×30×20・壁 2・丸め 3+2)に、丸めを同心にして隙間 `clearance` だけ小さい中子を入れた組。"""
    radius = 3.0
    outer = filleted_box(0, 0, 0, 40, 30, 20, radius + 2.0)
    pocket = filleted_box(2, 2, 2, 36, 26, 30, radius)
    core = filleted_box(2 + clearance, 2 + clearance, 2 + clearance, 36 - 2 * clearance, 26 - 2 * clearance, 25, radius - clearance)
    return cut(outer, pocket), core
