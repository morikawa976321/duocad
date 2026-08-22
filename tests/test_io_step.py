"""M0契約: STEP読み込み・STEP/STL書き出し(実装計画書 §7 落とし穴、§8 M0検証)。

face構成(56/34/22/0/0/0)は shelf_attachment_GD.step を実際にOCPで走査して実測済み
(BRepAdaptor_Surfaceの面種別 + 円筒のUVBounds U範囲>350度で全周判定)。
STEP往復の体積比較・STL偏差とファイルサイズの関係も同じ環境(cadquery-ocp-novtk 7.9.3.1.1 /
build123d 0.11.1)で実測しており、下記の許容誤差・アサーションは推測ではなく実測に基づく。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

# 参照用のSTEPをテスト側で作るためだけにOCPを使う(境界検査は `duocad/` 配下だけが対象)
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel, query
from duocad.document import Document
from duocad.io_step import export_step, export_stl, import_step
from duocad.oplog import OpLog
from duocad.query import describe_shape, volume
from duocad.verbs.import_editable import import_step_editable

def test_import_step_missing_file_raises_and_does_not_mutate_document(tmp_path):
    """存在しないSTEPパスはFileNotFoundErrorを送出し、documentにもログにも変更が残らない
    (失敗を黙って通さない。実装計画書§7・§6予防6)。

    実装時の注意: OCPのSTEPControl_Readerは、
    ReadFile()がIFSelect_RetDone以外を返した状態のまま TransferRoots()/OneShape() を呼ぶと、
    Python例外にならずプロセスごと落ちる(Windowsで終了コード0xC0000005 = アクセス違反)。
    import_stepの実装はReadFile()の戻り値を必ず先に検査し、失敗ならTransferRoots()を呼ぶ前に
    raiseすること(詳細はtests/README.md)。
    """
    doc = Document()
    bad_path = tmp_path / "does_not_exist.step"

    with pytest.raises(FileNotFoundError):
        import_step(doc, bad_path)

    assert doc.bodies == []
    assert doc.log.entries == []


def test_import_step_malformed_file_raises_value_error_and_does_not_mutate_document(tmp_path):
    """STEPとして壊れているファイル(存在はする)はValueErrorを送出し、documentにもログにも
    変更が残らない(失敗を黙って通さない。実装計画書§6予防6・§7)。

    このテストが上の「存在しないパス」のテストと別に要るのは、**存在チェックだけでは
    ReadFile()の戻り値検査を強制できない**ため。`Path.exists()` を見るだけの実装は上のテストを
    通してしまうが、中身が壊れたファイルではReadFile()がIFSelect_RetFailを返し、そのまま
    TransferRoots()を呼ぶと**プロセスごと落ちる**(存在しないパスは
    IFSelect_RetError、壊れた中身はIFSelect_RetFail、いずれもTransferRoots()で0xC0000005)。
    ReadFile()の戻り値を必ず検査させるのはこのテストの役目。
    """
    doc = Document()
    broken = tmp_path / "broken.step"
    broken.write_text("this is not a STEP file at all\njust plain text\n", encoding="utf-8")

    with pytest.raises(ValueError):
        import_step(doc, broken)

    assert doc.bodies == []
    assert doc.log.entries == []


def test_export_from_empty_document_raises_and_writes_no_file(tmp_path):
    """書き出す形状が無い文書からのexportは例外になり、半端なファイルを残さない
    (失敗を黙って通さない。実装計画書§6予防6)。

    黙って0バイトのSTEP/STLを吐くと、壊れた成果物が「成功」として残る。
    """
    doc = Document()
    step_path = tmp_path / "empty.step"
    stl_path = tmp_path / "empty.stl"

    with pytest.raises(ValueError):
        export_step(doc, step_path)
    assert not step_path.exists()

    with pytest.raises(ValueError):
        export_stl(doc, stl_path, 0.1)
    assert not stl_path.exists()




# --------------------------------------------------------------------------------------
# 埋め込み: 読み込んだSTEPの形はログの中にある(2026-08-23。
# engine `duocad-oplog-embeds-imported-step`)
#
# **ここが本命の契約** —— 再生が外部ファイルを読まない、を検査する。読まないことは
# 「元のSTEPを消してから開く」でしか確かめられない(パスを引数に残したままでも、
# ファイルがそこに在れば往復テストは緑になってしまう)。
# --------------------------------------------------------------------------------------
def _two_solid_step(tmp_path) -> Path:
    """2ソリッド(体積1000と216)のSTEPを作って返す。

    **`3d-models/` の私物STEPは使わない**(読み取り専用・リポジトリ外への依存を作らない。
    engine `duocad-drops-private-step-test-dependency`)。テスト側は境界検査の対象外なので
    OCPで直接作ってよい(`tests/test_extrude.py` と同じ流儀)。
    """
    box_a = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    box_b = BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 6.0, 6.0, 6.0).Shape()
    step_path = tmp_path / "two_solids.step"
    kernel.write_step(kernel.make_compound([box_a, box_b]), str(step_path))
    return step_path


def _shape_fingerprint(doc: Document) -> list:
    """形が同じであることの指紋(ボディ数・名前・面数・体積)。"""
    return [
        (body.name, body.kind, kernel.face_count(body.shape), round(query.volume(body.shape), 9))
        for body in doc.bodies
    ]


def test_import_step_logs_the_step_text_and_no_path(tmp_path):
    """`import_step` のログは**STEP本文**と元のファイル名を持ち、パスを持たない。

    パスが残っていると、それが「第二の正」になり得る(再生がそちらを読む実装へ
    いつでも戻れてしまう)。`source_name` は名前だけで、ディレクトリを含まないこと。
    """
    doc = Document()
    step_path = _two_solid_step(tmp_path)

    body_ids = import_step(doc, step_path)

    (operation,) = doc.log.entries
    assert operation.verb == "import_step"
    assert set(operation.args) == {"source_name", "step", "body_ids"}
    assert operation.args["source_name"] == "two_solids.step"
    assert operation.args["body_ids"] == body_ids
    step_text = operation.args["step"]
    assert isinstance(step_text, str), "base64やバイト列ではなく生テキストで持つこと"
    assert step_text.startswith("ISO-10303-21"), step_text[:40]
    assert str(tmp_path) not in json.dumps(operation.args), "ログに元のパスが残っている"


def test_replay_rebuilds_the_shape_after_the_original_step_is_gone(tmp_path):
    """**元のSTEPを消してから再生しても同じ形が出る**(本タスクの本体)。

    保存形式(JSON)を往復させたうえで、参照していたSTEPをリネーム(=移動・改名・削除・
    別PCへの持ち出しと同じ状況)してから `replay` する。ここが緑なら、
    「操作ログが唯一の正であり、文書の形状はここから導出される」(`duocad/oplog.py`)が
    実装として成立している。
    """
    doc = Document()
    step_path = _two_solid_step(tmp_path)
    import_step(doc, step_path)
    before = _shape_fingerprint(doc)
    assert len(before) == 2, "2ソリッドに分解されていること(前提の確認)"

    saved = doc.log.to_json()
    step_path.rename(tmp_path / "moved_away.step")  # 元のSTEPはもう無い
    assert not step_path.exists()

    reopened = Document()
    reopened.log = OpLog.from_json(saved)
    reopened.log.replay(reopened)

    assert _shape_fingerprint(reopened) == before
    assert [body.id for body in reopened.bodies] == [body.id for body in doc.bodies]


def test_import_step_editable_replays_without_the_original_step(tmp_path):
    """`import_step_editable` も同じ(参照ボディ＋編集用ボディ＋可視性まで復元する)。

    通常の読み込み経路はこちら(UI・MCPが呼ぶ)なので、こちらを外すと
    「保存ファイルが自己完結する」は実質成立しない。
    """
    doc = Document()
    step_path = _two_solid_step(tmp_path)
    body_ids = import_step_editable(doc, step_path)
    before = _shape_fingerprint(doc)
    visible_before = [body.visible for body in doc.bodies]
    assert len(before) == 4, "ソリッド2つ × (参照 + 編集用)"

    (operation,) = doc.log.entries
    assert set(operation.args) == {"source_name", "step", "reference_ids", "body_ids"}
    assert operation.args["body_ids"] == body_ids

    saved = doc.log.to_json()
    step_path.unlink()  # 元のSTEPを削除

    reopened = Document()
    reopened.log = OpLog.from_json(saved)
    reopened.log.replay(reopened)

    assert _shape_fingerprint(reopened) == before
    assert [body.visible for body in reopened.bodies] == visible_before
    assert [body.id for body in reopened.bodies] == [body.id for body in doc.bodies]


def test_replaying_a_broken_embedded_step_raises_instead_of_crashing(tmp_path):
    """埋め込みが壊れていたら ValueError(**プロセスごと落ちない**)。

    埋め込み経路も `kernel.read_step` の `ReadFile()` 戻り値検査を通ること —— 通らないと、
    失敗した状態で `TransferRoots()` を呼んで 0xC0000005 でプロセスごと死ぬ
    (engine `ocp-stepcontrol-reader-segfault-on-failed-readfile`)。**テストランナーごと
    落ちるので、失敗の記録すら残らない**種類の事故であり、ここで止めるしかない。
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
    with pytest.raises(ValueError):
        kernel.read_step_text("")
