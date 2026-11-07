"""縮退辺を添字を変えずに識別し、半径0の円として測らない。"""
import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone, BRepPrimAPI_MakeSphere

from duocad import brep_topology, query
from duocad.document import Body
from duocad.mcp_server import DocumentTools, McpServer
from duocad.session import Session
from mcp_support import call


@pytest.mark.parametrize("make_shape", [
    lambda: BRepPrimAPI_MakeCone(10.0, 0.0, 20.0).Shape(),
    lambda: BRepPrimAPI_MakeSphere(10.0).Shape(),
], ids=["cone", "sphere"])
def test_degenerate_edges_remain_indexed_and_are_marked_in_delivery_and_measurement(make_shape):
    shape = make_shape()
    session = Session(0.2)
    session.document.add_body(Body(id="body", name="body", kind="solid", shape=shape))
    server = McpServer(DocumentTools(session))
    described = query.describe_edges(shape)
    tessellated = session.view.edges("body")["edges"]
    count = brep_topology.edge_count(shape)
    assert [edge["index"] for edge in described] == list(range(1, count + 1))
    assert [edge["index"] for edge in tessellated] == list(range(1, count + 1))
    degenerate = []
    for edge, polyline in zip(described, tessellated):
        expected = BRep_Tool.Degenerated_s(brep_topology.edge_of(shape, edge["index"]))
        assert edge["degenerate"] is expected
        assert polyline["degenerate"] is expected
        if expected:
            degenerate.append(edge["index"])
            session.modify_selection(add=[{"body": "body", "kind": "edge", "index": edge["index"]}], remove=[])
            item = call(server, "measure")["items"][0]
            assert item["degenerate"] is True
            assert item["length"] == pytest.approx(0.0, abs=1e-12)
            assert "radius" not in item
            session.clear_selection()
        elif edge["kind"] == "circle":
            session.modify_selection(add=[{"body": "body", "kind": "edge", "index": edge["index"]}], remove=[])
            item = call(server, "measure")["items"][0]
            assert item["degenerate"] is False
            assert item["radius"] == pytest.approx(10.0)
            session.clear_selection()
    assert degenerate
