"""STEP読み込み・STEP/STL書き出しの契約。

下記の許容誤差・アサーションは推測ではなく実測(cadquery-ocp-novtk 7.9.3.1.1 /
build123d 0.11.1)に基づく。
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
from duocad.document import Body, Document, new_body_id
from duocad.io_step import (
    export_body_ids,
    export_skipped_hidden,
    export_step,
    export_stl,
)
from duocad.oplog import OpLog
from duocad.query import describe_shape, volume
from duocad.selection import set_visible
from duocad.verbs.import_editable import import_step

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
    """書き出す形状が無い文書からのexportは例外になり、半端なファイルを残さない。

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


def _add_solid_body(doc: Document, name: str, box) -> str:
    """テスト用に `kind="solid"` のBodyを直接足す(既定 `visible=True`)。"""
    body_id = new_body_id()
    doc.add_body(Body(id=body_id, name=name, kind="solid", shape=box))
    return body_id


def _editable_bodies(doc: Document) -> list:
    """`import_step` が作った**編集用ボディ**だけ(参照は同じ形の原本なので数えない)。

    読み込みは1ソリッドにつき参照+編集用の2つを作るので、書き出したファイルに何ソリッド
    入っていたかを数えるにはこちらを見る。
    """
    return [body for body in doc.bodies if body.kind == "solid"]


def test_export_excludes_a_hidden_solid_from_step_and_stl(tmp_path):
    """`visible=False` のソリッドはSTEP/STLのどちらからも除外される。

    非表示のものが混じっていると書き出しの面数・体積で気づけるが、それを機械で
    固定するのがこのテスト —— `_bodies_to_export` が可視性を見ていない実装への回帰検査。
    """
    doc = Document()
    shown = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    hidden = BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 6.0, 6.0, 6.0).Shape()
    shown_id = _add_solid_body(doc, "shown", shown)
    hidden_id = _add_solid_body(doc, "hidden", hidden)
    set_visible(doc, hidden_id, False)

    step_path = tmp_path / "visible_only.step"
    export_step(doc, step_path)
    reimported = Document()
    import_step(reimported, step_path)
    solids = _editable_bodies(reimported)
    assert len(solids) == 1, "非表示のボディがSTEPに混入している"
    assert math.isclose(query.volume(solids[0].shape), 20.0 * 10.0 * 5.0, rel_tol=1e-6)

    stl_path = tmp_path / "visible_only.stl"
    export_stl(doc, stl_path, 0.1)
    assert stl_path.exists() and stl_path.stat().st_size > 0
    # STEP/STLは同じ `_bodies_to_export` を通るので、STEP側の面数(6=単一ボックス)が
    # 非表示除外の効果を代表する。STLはファイルが実際に書けたことだけを確認する。
    assert kernel.face_count(solids[0].shape) == 6

    assert export_body_ids(doc) == [shown_id]
    assert export_skipped_hidden(doc) == [hidden_id]


def test_export_raises_a_dedicated_error_when_all_candidates_are_hidden(tmp_path):
    """候補はあるが全部非表示 → `ValueError` かつファイルを作らない。

    「形が無い」場合の文言(`nothing to export: no body with a shape`)とは分ける
    ——こちらは `set_visible(visible=True)` で戻せば直る、という別種の状態だから。
    """
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
        export_stl(doc, stl_path, 0.1)
    assert not stl_path.exists()

    # 「形が無い」既存の文言とは異なるエラーであることも確認する
    with pytest.raises(ValueError, match="no body with a shape"):
        export_step(Document(), step_path)


def test_export_from_a_reference_only_document_still_works(tmp_path):
    """`kind="reference"` しか無い可視の文書でも書き出せる —— フォールバックの順序
    (kindで候補→可視でフィルタ)を守れているかの検査。ここを落とすと「可視→kind」の
    逆順に戻った事故に気づけない。

    **この状態はBodyを直接足して作る。** 2026-08-27に読み込みが `import_step` 1本へ
    統合され、参照ボディは必ず編集用ボディと対で・`visible=False` で入るようになったので、
    verbを通しては「参照だけ・全部可視」の文書をもう作れない。`_export_candidates` の
    フォールバック(solidが1つも無ければ形状を持つ全Body)はコードに残っており、
    その分岐を実行時に踏むにはここで直に組み立てるしかない。
    """
    doc = Document()
    boxes = [
        BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 6.0, 6.0, 6.0).Shape(),
    ]
    body_ids = []
    for number, box in enumerate(boxes, start=1):
        body_id = new_body_id()
        doc.add_body(Body(id=body_id, name=f"ref_{number}", kind="reference", shape=box))
        body_ids.append(body_id)
    assert all(body.visible for body in doc.bodies), "この検査の前提(全部可視)が崩れている"
    assert not _editable_bodies(doc), "フォールバックへ落ちる前提(solidが無い)が崩れている"

    out_path = tmp_path / "from_reference.step"
    export_step(doc, out_path)
    assert out_path.exists()
    assert export_body_ids(doc) == body_ids
    assert export_skipped_hidden(doc) == []


def test_export_after_import_step_exports_only_the_editable_copy(tmp_path):
    """`import_step` 直後は編集用コピーだけが書き出される(同じ形が二重に出ない)。

    参照ボディは `visible=False` で追加されるので、可視フィルタで落ちる。
    """
    doc = Document()
    source = _two_solid_step(tmp_path)
    body_ids = import_step(doc, source)
    reference_ids = [b.id for b in doc.bodies if b.kind == "reference"]
    assert len(reference_ids) == 2 and all(
        not b.visible for b in doc.bodies if b.kind == "reference"
    )

    out_path = tmp_path / "editable_only.step"
    export_step(doc, out_path)
    assert export_body_ids(doc) == body_ids
    assert export_skipped_hidden(doc) == []

    reimported = Document()
    import_step(reimported, out_path)
    assert len(_editable_bodies(reimported)) == 2, "参照ボディが紛れ込んでいる"


# --------------------------------------------------------------------------------------
# 埋め込み: 読み込んだSTEPの形はログの中にある
#
# **ここが本命の契約** —— 再生が外部ファイルを読まない、を検査する。読まないことは
# 「元のSTEPを消してから開く」でしか確かめられない(パスを引数に残したままでも、
# ファイルがそこに在れば往復テストは緑になってしまう)。
# --------------------------------------------------------------------------------------
def _two_solid_step(tmp_path) -> Path:
    """2ソリッド(体積1000と216)のSTEPを作って返す。

    **`3d-models/` の私物STEPは使わない**(読み取り専用・リポジトリ外への依存を作らない)。
    テスト側は境界検査の対象外なのでOCPで直接作ってよい。
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

    **`args` の顔ぶれそのものが契約**である —— 作った全ID(`reference_ids` / `body_ids`)が
    記録されていないと、再生で別のIDが振られて選択集合や押し出しの参照先が迷子になる。
    **ソリッドが何個あってもログは1行**(`(operation,) = ...` がそれを押さえている)。
    """
    doc = Document()
    step_path = _two_solid_step(tmp_path)

    body_ids = import_step(doc, step_path)

    (operation,) = doc.log.entries
    assert operation.verb == "import_step"
    assert set(operation.args) == {"source_name", "step", "reference_ids", "body_ids"}
    assert operation.args["source_name"] == "two_solids.step"
    assert operation.args["body_ids"] == body_ids
    assert len(operation.args["reference_ids"]) == len(body_ids) == 2
    step_text = operation.args["step"]
    assert isinstance(step_text, str), "base64やバイト列ではなく生テキストで持つこと"
    assert step_text.startswith("ISO-10303-21"), step_text[:40]
    assert str(tmp_path) not in json.dumps(operation.args), "ログに元のパスが残っている"


def test_replay_rebuilds_the_shape_after_the_original_step_is_gone(tmp_path):
    """**元のSTEPを消してから再生しても同じ形が出る**(参照+編集用+可視性まで)。

    保存形式(JSON)を往復させたうえで、参照していたSTEPをリネーム(=移動・改名・削除・
    別PCへの持ち出しと同じ状況)してから `replay` する。**読み込みの経路はこれ1本**なので、
    ここが通らなければ「保存ファイルが自己完結する」は実質成立しない。

    **可視性まで見る** —— 参照ボディは `visible=False` で入るので、再生がそれを落とすと
    同じ形が二重に描かれる(手前を消しても参照側のメッシュが残る)。
    """
    doc = Document()
    step_path = _two_solid_step(tmp_path)
    import_step(doc, step_path)
    before = _shape_fingerprint(doc)
    visible_before = [body.visible for body in doc.bodies]
    assert len(before) == 4, "ソリッド2つ × (参照 + 編集用)であること(前提の確認)"

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
    with pytest.raises(ValueError):
        kernel.read_step_text("")
