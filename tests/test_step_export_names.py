"""STEP 書き出しがボディ名を製品名に載せ、ASCII のまま書き戻せる。落ちたら受け取った CAD で部品が名前なしで並ぶ。"""
from __future__ import annotations

import re

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.gp import gp_Pnt

from duocad import kernel, query
from duocad.document import Body, Document, new_body_id
from duocad.io_step import export_step
from duocad.verbs.import_editable import import_step


def _box_body(doc: Document, name: str, origin: float, size: float) -> None:
    shape = BRepPrimAPI_MakeBox(gp_Pnt(origin, 0.0, 0.0), size, size, size).Shape()
    doc.add_body(Body(id=new_body_id(), name=name, kind="solid", shape=shape))


def _names_via_xde(path) -> list[str]:
    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    application.InitDocument(document)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.ReadFile(str(path))
    reader.Transfer(document)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    labels = TDF_LabelSequence()
    tool.GetFreeShapes(labels)
    names = []
    for index in range(1, labels.Length() + 1):
        attribute = TDataStd_Name()
        assert labels.Value(index).FindAttribute(TDataStd_Name.GetID_s(), attribute)
        names.append(attribute.Get().ToExtString())
    return names


def test_exported_step_carries_the_body_names_and_reads_back_the_same_shapes(tmp_path):
    doc = Document()
    _box_body(doc, "alpha", 0.0, 10.0)
    _box_body(doc, "ブラケット 𠮷 it's \\ x", 20.0, 5.0)
    path = tmp_path / "named.step"
    export_step(doc, path)

    text = path.read_bytes().decode("ascii")  # 非ASCIIの生バイトが残っていれば UnicodeDecodeError
    products = re.findall(r"PRODUCT\('([^']*(?:''[^']*)*)'", text)
    assert "alpha" in products
    assert any("X2" in name for name in products)
    assert not any(name.startswith("Open CASCADE STEP translator") for name in products)
    assert sorted(_names_via_xde(path)) == sorted(["alpha", "ブラケット 𠮷 it's \\ x"])

    reimported = Document()
    import_step(reimported, path)
    volumes = sorted(query.volume(body.shape) for body in reimported.bodies)
    assert volumes == [pytest.approx(125.0, rel=1e-6), pytest.approx(1000.0, rel=1e-6)]


def test_single_body_export_is_named_and_a_failed_write_leaves_no_file(tmp_path):
    doc = Document()
    _box_body(doc, "only", 0.0, 10.0)
    path = tmp_path / "one.step"
    export_step(doc, path)
    assert _names_via_xde(path) == ["only"]

    missing_dir = tmp_path / "no_such_dir" / "x.step"
    with pytest.raises(ValueError):
        kernel.write_step_named([("x", doc.bodies[0].shape)], str(missing_dir))
    assert not missing_dir.exists()
