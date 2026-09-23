"""生成CADの文書内保存と、最終ソリッドの幾何検査。"""
import io

from OCP.BRep import BRep_Builder
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepTools import BRepTools
from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_CheckerSI, BOPAlgo_Operation
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopoDS import TopoDS_Shape

from .mesh_geometry import subshapes


#: `boolean` が受ける操作名 → OCCT の演算。**呼び出し側に OCP の列挙を持たせない**
#: (層をまたぐ契約 —— ここが `duocad/` でOCPを触ってよい数少ない場所である)。
BOOLEAN_OPERATIONS = {'fuse': BOPAlgo_Operation.BOPAlgo_FUSE, 'cut': BOPAlgo_Operation.BOPAlgo_CUT}


def boolean(shape, tools, operation):
    """`shape` に `tools` を `operation`('fuse' / 'cut')で適用し、`(結果, 診断)` を返す。

    **警告も診断に入れて返し、ここでは投げない** —— OCCT のブーリアンは警告つきで
    それらしい形を返すことがあり、採否と記録は呼び出し側の責務。
    """
    builder = BOPAlgo_BOP()
    builder.SetNonDestructive(True)
    builder.SetFuzzyValue(0.0)      # あいまい判定を入れない(`kernel.common_volume` と同じ)
    builder.AddArgument(shape)
    for tool in tools:
        builder.AddTool(tool)
    builder.SetOperation(BOOLEAN_OPERATIONS[operation])
    builder.Perform()
    errors, warnings = io.BytesIO(), io.BytesIO()
    builder.DumpErrors(errors)
    builder.DumpWarnings(warnings)
    return builder.Shape(), dict(
        has_errors=builder.HasErrors(), has_warnings=builder.HasWarnings(),
        errors=errors.getvalue().decode('utf-8', errors='replace'),
        warnings=warnings.getvalue().decode('utf-8', errors='replace'))


def is_valid(shape):
    """`BRepCheck_Analyzer` が妥当と見るか(None・NULL は False)。"""
    return shape is not None and not shape.IsNull() and BRepCheck_Analyzer(shape).IsValid()


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


def _count_face_interferences(ds, pairs):
    """`pairs`(`BOPDS_Pair`の列)のうち、両端の形状が面であるものだけを数える。

    `BOPAlgo_CheckerSI` の `Interferences()` は種類混在の集計で、頂点/辺を含むペアは許容差が
    触れているだけで無条件にカウントされる(除外条件なし)。面-面だけ、OCCT自身が実際の交差点・
    交差線の有無と「同一面の単純接触でないか」を確認してから数える。型だけで判定し、
    数値のしきい値(距離・許容差)は使わない。
    """
    return sum(1 for pair in pairs
               if all(ds.Shape(index).ShapeType() == TopAbs_FACE for index in pair.Indices()))


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
        # `self_intersections` は面-面の交差数のみを表す(頂点・辺レベルの接触は自己干渉として数えない)。
        ds = checker.DS()
        intersections = _count_face_interferences(ds, ds.Interferences())
    return dict(watertight=valid and closed and intersections == 0,
                single_solid=len(solids) == 1, solids=len(solids),
                self_intersections=intersections, valid=valid, closed=closed)
