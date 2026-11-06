"""MCP の取り込みパスの引用符と相対パスの基準。"""
import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel
from duocad.mcp_server import DocumentTools, McpServer
from duocad.paths import resolve_user_path
from duocad.session import Session
from mcp_support import call


@pytest.mark.parametrize("verb", ["import_svg", "import_step"])
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("relative", [False, True])
def test_mcp_import_accepts_quoted_paths_from_the_working_directory(tmp_path, monkeypatch, verb, quote, relative):
    session = Session(0.2)
    server = McpServer(DocumentTools(session))
    arguments = {}
    if verb == "import_svg":
        arguments["sketch_id"] = session.sketch_create("XY")["sketch_id"]
        source = tmp_path / "part name.svg"
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm"><rect width="5" height="5"/></svg>', encoding="utf-8")
    else:
        source = tmp_path / "part name.step"
        kernel.write_step(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), str(source))
    monkeypatch.chdir(tmp_path)
    arguments["path"] = f"  {quote}{source.name if relative else source}{quote}  "

    result = call(server, verb, arguments)

    if verb == "import_svg":
        assert result["entity_count"] > 0
    else:
        assert len(result["body_ids"]) == 1


@pytest.mark.parametrize("path", ['"part.step', "'part.step\"", "part's.step"])
def test_unmatched_or_internal_quotes_are_preserved(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    assert resolve_user_path(path) == tmp_path / path


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("name", [" part.svg", "part .svg", " part.svg "])
def test_spaces_inside_quotes_are_preserved(tmp_path, monkeypatch, quote, name):
    monkeypatch.chdir(tmp_path)
    assert resolve_user_path(f"  {quote}{name}{quote}  ") == tmp_path / name
