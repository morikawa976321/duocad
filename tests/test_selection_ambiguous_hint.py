"""「1つだけ要るのに複数選ばれている」失敗が、直し方の hint を返すことの回帰テスト。"""

from duocad.mcp_server import DocumentTools
from duocad.server import Session


def test_two_faces_selected_for_a_sketch_returns_a_hint_to_narrow_the_selection(box_session: Session):
    """落ちたら、面を2枚選んで sketch_create を呼んだ AI が、どう直せばよいか分からず選択を探し回る。"""
    tools = DocumentTools(box_session)
    tools.call("set_selection", {"refs": [{"body": "box", "kind": "face", "index": 1}, {"body": "box", "kind": "face", "index": 2}]})

    _, error, _ = tools.call_and_seq("sketch_create", {})
    envelope = tools.error_envelope("sketch_create", error)

    assert envelope["code"] == "invalid_argument"
    assert envelope["required_kind"] == "face"
    assert "set_selection" in envelope["hint"] and "modify_selection" in envelope["hint"]
