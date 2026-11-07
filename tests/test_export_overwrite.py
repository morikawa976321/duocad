"""書き出しの応答が、同名のファイルを上書きしたかを言う。落ちたら、前の書き出しを潰したことを画面の知らせが黙る。"""
from __future__ import annotations

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import io_step
from duocad.document import Body, Document, new_body_id


def _document() -> Document:
    doc = Document()
    doc.add_body(Body(id=new_body_id(), name="box", kind="solid", shape=BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape()))
    return doc


def test_export_reports_overwrote_only_when_the_file_already_existed(tmp_path):
    doc = _document()
    for fmt in ("step", "stl"):
        path = tmp_path / f"part.{fmt}"
        assert io_step.export(doc, path, fmt)["overwrote"] is False
        assert io_step.export(doc, path, fmt)["overwrote"] is True


def test_session_export_responses_carry_overwrote(box_session, tmp_path):
    for export in (box_session.export_step_file, box_session.export_stl_file):
        name = "part.step" if export == box_session.export_step_file else "part.stl"
        assert export(str(tmp_path / name))["overwrote"] is False
        assert export(str(tmp_path / name))["overwrote"] is True
