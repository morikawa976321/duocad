"""STEP読み込み・STEP/STL書き出しの契約。

下記の許容誤差・アサーションは推測ではなく実測(cadquery-ocp-novtk 7.9.3.1.1 /
build123d 0.11.1)に基づく。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# 参照用のSTEPをテスト側で作るためだけにOCPを使う(境界検査は `duocad/` 配下だけが対象)
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel, query
from duocad.document import Body, Document, new_body_id
from duocad.io_step import (
    export_body_ids,
    export_skipped_hidden,
    export_step,
    export_stl,
)
from duocad.oplog import OpLog
from duocad.selection import set_visible
from duocad.verbs.import_editable import import_step

from duocad import (
    brep_topology as _brep_topology,
)

def test_import_step_missing_file_raises_and_does_not_mutate_document(tmp_path):
    """存在しないSTEPパスは FileNotFoundError で、documentにもログにも変更が残らない。

    実装は `ReadFile()` の戻り値を必ず先に検査すること(失敗した状態で `TransferRoots()` を
    呼ぶと Python例外にならず**プロセスごと落ちる**)。
    """
    doc = Document()
    bad_path = tmp_path / "does_not_exist.step"

    with pytest.raises(FileNotFoundError):
        import_step(doc, bad_path)

    assert doc.bodies == []
    assert doc.log.entries == []


def test_import_step_malformed_file_raises_value_error_and_does_not_mutate_document(tmp_path):
    """STEPとして壊れているファイル(存在はする)は ValueError で、変更が残らない。

    上の「存在しないパス」と別に要るのは、**存在チェックだけでは `ReadFile()` の戻り値検査を
    強制できない**ため。`Path.exists()` を見るだけの実装は上のテストを通してしまうが、中身が
    壊れたファイルでは `ReadFile()` が IFSelect_RetFail を返し、そのまま `TransferRoots()` を
    呼ぶと**プロセスごと落ちる**。
    """
    doc = Document()
    broken = tmp_path / "broken.step"
    broken.write_text("this is not a STEP file at all\njust plain text\n", encoding="utf-8")

    with pytest.raises(ValueError):
        import_step(doc, broken)

    assert doc.bodies == []
    assert doc.log.entries == []


def test_export_from_empty_document_raises_and_writes_no_file(tmp_path):
    """書き出す形状が無い文書からのexportは例外になり、半端なファイルを残さない。"""
    doc = Document()
    step_path = tmp_path / "empty.step"
    stl_path = tmp_path / "empty.stl"

    with pytest.raises(ValueError):
        export_step(doc, step_path)
    assert not step_path.exists()

    with pytest.raises(ValueError):
        export_stl(doc, stl_path)
    assert not stl_path.exists()


def _add_solid_body(doc: Document, name: str, box) -> str:
    """テスト用に `kind="solid"` のBodyを直接足す(既定 `visible=True`)。"""
    body_id = new_body_id()
    doc.add_body(Body(id=body_id, name=name, kind="solid", shape=box))
    return body_id


def test_export_excludes_a_hidden_solid_from_step_and_stl(tmp_path):
    """`visible=False` のソリッドはSTEP/STLのどちらからも除外され、表示中の2つはcompoundで書き出される。"""
    doc = Document()
    shown = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    also_shown = BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 6.0, 6.0, 6.0).Shape()
    hidden = BRepPrimAPI_MakeBox(gp_Pnt(100.0, 0.0, 0.0), 3.0, 3.0, 3.0).Shape()
    shown_ids = [_add_solid_body(doc, "shown", shown), _add_solid_body(doc, "also_shown", also_shown)]
    hidden_id = _add_solid_body(doc, "hidden", hidden)
    set_visible(doc, hidden_id, False)

    step_path = tmp_path / "visible_only.step"
    export_step(doc, step_path)
    reimported = Document()
    import_step(reimported, step_path)
    volumes = sorted(query.volume(body.shape) for body in reimported.bodies)
    assert volumes == [pytest.approx(216.0, rel=1e-6), pytest.approx(1000.0, rel=1e-6)]

    stl_path = tmp_path / "visible_only.stl"
    export_stl(doc, stl_path)
    only_shown = Document()
    _add_solid_body(only_shown, "shown", shown)
    _add_solid_body(only_shown, "also_shown", also_shown)
    reference_path = tmp_path / "reference.stl"
    export_stl(only_shown, reference_path)
    assert stl_path.read_bytes() == reference_path.read_bytes(), "非表示のソリッドがSTLに入っている"

    assert export_body_ids(doc) == shown_ids
    assert export_skipped_hidden(doc) == [hidden_id]


def test_export_raises_a_dedicated_error_when_all_candidates_are_hidden(tmp_path):
    """候補はあるが全部非表示 → `ValueError` かつファイルを作らない。"""
    doc = Document()
    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    body_id = _add_solid_body(doc, "box", box)
    set_visible(doc, body_id, False)

    step_path = tmp_path / "none.step"
    stl_path = tmp_path / "none.stl"

    with pytest.raises(ValueError, match="all candidate bodies are hidden"):
        export_step(doc, step_path)
    assert not step_path.exists()

    with pytest.raises(ValueError, match="all candidate bodies are hidden"):
        export_stl(doc, stl_path)
    assert not stl_path.exists()

    # 「形が無い」既存の文言とは異なるエラーであることも確認する
    with pytest.raises(ValueError, match="no body with a shape"):
        export_step(Document(), step_path)


# 埋め込み: 読み込んだSTEPの形はログの中にある
#
# **ここが本命の契約** —— 再生が外部ファイルを読まない、を検査する。読まないことは
# 「元のSTEPを消してから開く」でしか確かめられない(パスを引数に残したままでも、
# ファイルがそこに在れば往復テストは緑になってしまう)。
def _two_solid_step(tmp_path) -> Path:
    """2ソリッド(体積1000と216)のSTEPを作って返す。"""
    box_a = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    box_b = BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 6.0, 6.0, 6.0).Shape()
    step_path = tmp_path / "two_solids.step"
    kernel.write_step(kernel.make_compound([box_a, box_b]), str(step_path))
    return step_path


def _shape_fingerprint(doc: Document) -> list:
    """形が同じであることの指紋(ボディ数・名前・面数・体積)。"""
    return [
        (body.name, body.kind, _brep_topology.face_count(body.shape), round(query.volume(body.shape), 9))
        for body in doc.bodies
    ]


def test_import_step_logs_the_step_text_and_no_path(tmp_path):
    """`import_step` のログは**STEP本文**と元のファイル名を持ち、パスを持たない。"""
    doc = Document()
    step_path = _two_solid_step(tmp_path)

    body_ids = import_step(doc, step_path)

    (operation,) = doc.log.entries
    assert operation.verb == "import_step"
    assert set(operation.args) == {"source_name", "step", "body_ids"}
    assert operation.args["source_name"] == "two_solids.step"
    assert operation.args["body_ids"] == body_ids
    assert len(body_ids) == 2
    step_text = operation.args["step"]
    assert isinstance(step_text, str), "base64やバイト列ではなく生テキストで持つこと"
    assert step_text.startswith("ISO-10303-21"), step_text[:40]
    assert str(tmp_path) not in json.dumps(operation.args), "ログに元のパスが残っている"


def test_replay_rebuilds_the_shape_after_the_original_step_is_gone(tmp_path):
    """**元のSTEPを消してから再生しても同じ形が出る**(名前・面数・体積・可視性まで)。"""
    doc = Document()
    step_path = _two_solid_step(tmp_path)
    import_step(doc, step_path)
    before = _shape_fingerprint(doc)
    visible_before = [body.visible for body in doc.bodies]
    assert len(before) == 2, "ソリッド2つぶんの編集用ボディであること(前提の確認)"

    saved = doc.log.to_json()
    step_path.rename(tmp_path / "moved_away.step")  # 元のSTEPはもう無い
    assert not step_path.exists()

    reopened = Document()
    reopened.log = OpLog.from_json(saved)
    reopened.log.replay(reopened)

    assert _shape_fingerprint(reopened) == before
    assert [body.visible for body in reopened.bodies] == visible_before
    assert [body.id for body in reopened.bodies] == [body.id for body in doc.bodies]


def test_replaying_a_broken_embedded_step_raises_instead_of_crashing(tmp_path):
    """埋め込みが壊れていたら ValueError(**プロセスごと落ちない**)。

    埋め込み経路も `kernel.read_step` の `ReadFile()` 戻り値検査を通ること —— 通らないと
    0xC0000005 で**テストランナーごと落ちる**(失敗の記録すら残らない)。
    """
    doc = Document()
    import_step(doc, _two_solid_step(tmp_path))
    payload = json.loads(doc.log.to_json())
    payload["operations"][0]["args"]["step"] = "this is not a STEP file at all"

    broken = Document()
    broken.log = OpLog.from_json(json.dumps(payload))
    with pytest.raises(ValueError):
        broken.log.replay(broken)


def test_reading_an_empty_embedded_step_is_refused_before_occt(tmp_path):
    """埋め込みが空文字なら、OCCTへ渡す前に ValueError(原因が分かる文言で断る)。"""
    with pytest.raises(ValueError, match="本文が空"):
        kernel.read_step_text("")


def _box_step_text(tmp_path) -> str:
    path = tmp_path / "box.step"
    kernel.write_step(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), str(path))
    return path.read_text(encoding="utf-8")


def _step_with_a_broken_face_reference(tmp_path) -> str:
    """箱のSTEPの面 `ADVANCED_FACE` の1つが、存在しない面(#99999)を参照する。OCCT は面が1枚落ちた開いたシェルを成功として返す。"""
    import re

    text = _box_step_text(tmp_path)
    face = re.search(r"^#\d+ = ADVANCED_FACE\('',\(#\d+\),(#\d+),\.[TF]\.\);", text, flags=re.M)
    return text.replace(face.group(0), face.group(0).replace(face.group(1), "#99999"))


def _box_and_a_loose_face_step(tmp_path) -> str:
    """ソリッド(箱)と、ソリッドに属さない面1枚(サーフェスボディ)を含む STEP。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon

    polygon = BRepBuilderAPI_MakePolygon()
    for point in ((100, 0, 0), (110, 0, 0), (110, 10, 0), (100, 10, 0)):
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(10, 20, 30)).Shape()
    path = tmp_path / "mixed.step"
    kernel.write_step(kernel.make_compound([box, face]), str(path))
    return path.read_text(encoding="utf-8")


def test_a_plain_solid_step_has_no_import_problem(tmp_path):
    """通常の STEP(ソリッドだけ)は何も断らない。落ちたら検査が正常な取り込みまで断っている。"""
    assert kernel.step_import_problems(_box_step_text(tmp_path)) == []
    path = tmp_path / "plain.step"
    path.write_text(_box_step_text(tmp_path), encoding="utf-8")
    assert len(import_step(Document(), path)) == 1


def test_import_refuses_a_step_whose_elements_failed_to_transfer(tmp_path):
    """一部の面の転送が失敗した STEP は、面が欠けた形を solid として黙って入れずに断る。ログにもボディにも残さない。
    落ちたら転送のチェック一覧を見ていない(`NbRootsForTransfer` の比較では、失敗がルートの中の面なので検出できない)。"""
    text = _step_with_a_broken_face_reference(tmp_path)
    assert _brep_topology.face_count(kernel.read_step_text(text)) == 5, "前提: OCCT は面が5枚の形を成功として返す"
    path = tmp_path / "broken_face.step"
    path.write_text(text, encoding="utf-8")
    doc = Document()

    with pytest.raises(ValueError, match="転送できず"):
        import_step(doc, path)

    assert doc.bodies == [] and doc.log.entries == []


def test_import_refuses_faces_outside_the_solids_instead_of_dropping_them(tmp_path):
    """ソリッドの外の面(サーフェスボディ)があるのに、ソリッドだけを取り込んで黙って落とさない。
    落ちたら各ボディの面数の合計が分解前の全面数と合わないまま取り込まれる。"""
    path = tmp_path / "mixed.step"
    path.write_text(_box_and_a_loose_face_step(tmp_path), encoding="utf-8")
    doc = Document()

    with pytest.raises(ValueError, match="外に面が 1 枚"):
        import_step(doc, path)

    assert doc.bodies == [] and doc.log.entries == []


def test_a_surface_only_step_is_still_one_body(tmp_path):
    """ソリッドの無い STEP(面だけ)は、形全体を1ボディにする救済のまま。ソリッドの外の面の検査はソリッドがあるときだけ。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon

    polygon = BRepBuilderAPI_MakePolygon()
    for point in ((0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)):
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    path = tmp_path / "surface.step"
    kernel.write_step(BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face(), str(path))

    assert len(import_step(Document(), path)) == 1


def test_replaying_a_log_imported_before_the_check_still_opens(tmp_path):
    """検査は取り込みだけにかかり、ログの再生にはかけない。落ちたら、検査を足す前に取り込んだ(面が欠けた・面を落とした)
    形を含む保存済みの文書が開けなくなる。"""
    document = Document()
    for text in (_box_and_a_loose_face_step(tmp_path), _step_with_a_broken_face_reference(tmp_path)):
        document.log.append("import_step", {"source_name": "old.step", "step": text, "body_ids": [new_body_id()]})

    reopened = Document()
    reopened.log = OpLog.from_json(document.log.to_json())
    reopened.log.replay(reopened)

    assert len(reopened.bodies) == 2
