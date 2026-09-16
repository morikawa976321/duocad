"""生成CADの文書内保存と、最終ソリッドの幾何検査。"""
import io

from OCP.BRep import BRep_Builder
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepTools import BRepTools
from OCP.BOPAlgo import BOPAlgo_CheckerSI
from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
from OCP.TopoDS import TopoDS_Shape

from .mesh_geometry import subshapes


def encode_brep(shape):
    stream = io.BytesIO()
    BRepTools.Write_s(shape, stream)
    return stream.getvalue().decode('ascii')


def decode_brep(text):
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, io.BytesIO(text.encode('ascii')), BRep_Builder())
    if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError('保存されたCADソリッドが不正')
    return shape


def inspect_solid(shape):
    solids = subshapes(shape, TopAbs_SOLID) if shape is not None and not shape.IsNull() else []
    valid = bool(solids) and BRepCheck_Analyzer(shape).IsValid()
    shells = subshapes(shape, TopAbs_SHELL) if solids else []
    closed = bool(shells) and all(shell.Closed() for shell in shells)
    intersections = None
    if valid and closed:
        checker = BOPAlgo_CheckerSI()
        checker.AddArgument(shape)
        checker.SetLevelOfCheck(5)
        checker.Perform()
        if checker.HasErrors() or checker.HasWarnings():
            raise ValueError('最終ソリッドの自己干渉検査が完了しなかった')
        intersections = checker.DS().Interferences().Extent()
    return dict(watertight=valid and closed and intersections == 0,
                single_solid=len(solids) == 1, solids=len(solids),
                self_intersections=intersections, valid=valid, closed=closed)
