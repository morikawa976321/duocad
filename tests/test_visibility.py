"""M3.5契約: 可視性のverbと、可視性が選択機構の一部であること(項番7〜10・2026-08-11)。

**なぜコアのverbなのか**: 表示・非表示をアプリ層のチェックボックスとして作ると、可視性は
文書の状態にならず、操作ログに載らず、undoできず、M4のMCPからも触れない。実装計画書§2
「コアはUIが無くても機能的に完全」の帰結として、**表示・非表示もverbである**。
タスクファイルにも「`set_visible` をコアのverbとして作れば、M4で自動的にMCPから使える
ようになる」と明記されている。

**なぜ選択機構の話が混ざるのか**: 2026-08-10に本人がPCケース(27部品・2835面)を実機で触った
ときの所見が「パーツごとの表示・非表示が実装されればすべての面を選択できるようになるはず」
であり、ピック失敗12回/押し出し7回(失敗率63%)の原因は**手前の部品が奥の面を隠していた**
ことにあった。つまり可視性は見た目の設定ではなく**何を指せるかの定義**である。
見えないものは指せない、をコアで保証する(項番10)。

`duocad/selection.py` は M3.5 で新設した層で、`selectable_bodies` / `select_face` を持つ。
ShapeRef の形は実装計画書§3.5 のとおり
`{"body": BodyId, "kind": "face", "index": int, "provenance": None}`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

# `_source_files` / `_imported_module_roots` はM0の境界検査の実体(test_sketch.py・
# test_parameters.py と同じ流儀で借りる。tests/ は sys.path に入っている)。
from test_boundaries import DUOCAD_DIR, _imported_module_roots, _source_files

from duocad import kernel, selection
from duocad.document import Document
from duocad.io_step import import_step
from duocad.oplog import OpLog
from duocad.verbs import extrude_face, make_editable_copy, set_visible

MODELS = Path(r"C:\Users\tm976\mf\3d-models")
SHELF = MODELS / "shelf_attachment_GD.step"

#: M3.5で新設したモジュール(import境界の走査対象に入っていることを固定する)
M35_MODULES = ("selection.py", "verbs/visibility.py")

#: 押し出し量(mm)。符号は正＝外向き法線方向へ足す。
EXTRUDE_MM = 1.0


def first_extrudable_face(shape) -> int:
    """`shape` の中で最初に押し出しに成功する面の1始まり添字を返す。

    面の添字を決め打ちにすると、分解の順序や実装の細部が変わっただけでテストが
    「別の面を押している」状態に静かに退化する。`extrude_face_on_shape` は入力の形状を
    変更しないので、成否の試行を安全に繰り返せる(kernel.py の契約)。
    """
    for index in range(1, kernel.face_count(shape) + 1):
        try:
            kernel.extrude_face_on_shape(shape, index, EXTRUDE_MM)
        except Exception:  # noqa: BLE001  曲面・退化など押せない面は飛ばす
            continue
        return index
    raise AssertionError("押し出せる面が1つも無い(テストの前提が崩れている)")


def _imported_shelf():
    """shelf_attachment_GD.step を読んだ文書を返す(M3.5では17個のBodyになる)。

    可視性の契約は「複数のBodyのうち一部だけを消す」ものなので、**分解が効いていることが
    前提**である。ここで数を確かめておかないと、分解前は文書にBodyが1個しか無いために
    各テストが `IndexError` で落ち、何の契約が破れたのか読めなくなる。
    """
    doc = Document()
    import_step(doc, str(SHELF))
    assert len(doc.bodies) == 17, (
        f"shelf_attachment_GD.step は17ソリッド(実際は{len(doc.bodies)} Body)。"
        "可視性のテストはソリッド分解(tests/test_import_split.py)の上に載っている"
    )
    return doc


# --------------------------------------------------------------------------------------
# 項番7: set_visible が verb として存在し visible を変える
# --------------------------------------------------------------------------------------
def test_set_visible_hides_one_body_and_leaves_the_others():
    """`set_visible(doc, body_id, False)` はそのBodyだけを不可視にする。"""
    doc = _imported_shelf()
    target = doc.bodies[3]

    set_visible(doc, target.id, False)

    assert target.visible is False
    assert all(
        body.visible is True for body in doc.bodies if body.id != target.id
    ), "他のBodyの可視性まで変わっている"


def test_set_visible_can_show_a_hidden_body_again():
    """不可視にしたBodyは `set_visible(..., True)` で戻せる(片道の操作ではない)。"""
    doc = _imported_shelf()
    target = doc.bodies[0]

    set_visible(doc, target.id, False)
    set_visible(doc, target.id, True)

    assert target.visible is True


# --------------------------------------------------------------------------------------
# 項番8: 操作ログに載り、undoできる
# --------------------------------------------------------------------------------------
def test_set_visible_is_logged_with_the_previous_value():
    """ログの args は {"body_id":.., "visible":.., "previous":..}。

    `previous` を記録するのは undo が元の値へ戻すため(M3の `set_parameter` と同じ流儀)。
    これが無いと undo は「True に戻す」しかできず、元から不可視だったBodyを
    勝手に表示してしまう。
    """
    doc = _imported_shelf()
    target = doc.bodies[2]
    log_length_before = len(doc.log.entries)

    operation = set_visible(doc, target.id, False)

    assert len(doc.log.entries) == log_length_before + 1
    assert operation.verb == "set_visible"
    assert operation.args == {
        "body_id": target.id,
        "visible": False,
        "previous": True,
    }


def test_set_visible_is_undoable():
    """undoで元の可視性に戻る。"""
    doc = _imported_shelf()
    target = doc.bodies[1]
    set_visible(doc, target.id, False)
    assert target.visible is False

    doc.log.undo(doc)

    assert target.visible is True


def test_undo_of_set_visible_restores_a_body_that_was_already_hidden():
    """元から不可視だったBodyを不可視にし直して undo しても、不可視のままである。

    `previous` を見ずに「undoは可視へ戻す」と実装すると、ここだけが静かに壊れる。
    """
    doc = _imported_shelf()
    target = doc.bodies[1]
    set_visible(doc, target.id, False)

    set_visible(doc, target.id, False)  # 既に不可視のものをもう一度不可視にする
    doc.log.undo(doc)

    assert target.visible is False


def test_set_visible_survives_redo_and_replay():
    """redoで再び不可視になり、保存したログの再生でも同じ可視性が復元される。"""
    doc = _imported_shelf()
    target_id = doc.bodies[4].id
    set_visible(doc, target_id, False)

    doc.log.undo(doc)
    doc.log.redo(doc)
    assert next(body for body in doc.bodies if body.id == target_id).visible is False

    restored = Document()
    restored.log = OpLog.from_json(doc.log.to_json())
    restored.log.replay(restored)

    hidden = [body.id for body in restored.bodies if not body.visible]
    assert hidden == [target_id]


# --------------------------------------------------------------------------------------
# 項番9: 存在しないbody_idなら失敗し、文書が変更されない
# --------------------------------------------------------------------------------------
def test_set_visible_rejects_an_unknown_body_and_changes_nothing():
    """存在しないbody_idは KeyError。documentにもログにも変更を残さない。

    `KeyError` にするのは `Document` の他のAPIと揃えるため(`is_editable` /
    `set_active_body` / `remove_body` はいずれも存在しないIDで KeyError)。
    黙って無視すると、取り消しや再生の空振りが静かに文書を壊す(§6予防6)。
    """
    doc = _imported_shelf()
    visibility_before = {body.id: body.visible for body in doc.bodies}
    log_before = len(doc.log.entries)

    with pytest.raises(KeyError):
        set_visible(doc, "body-does-not-exist", False)

    assert {body.id: body.visible for body in doc.bodies} == visibility_before
    assert len(doc.log.entries) == log_before


# --------------------------------------------------------------------------------------
# 項番10: 不可視のBodyは選択の対象にならない
# --------------------------------------------------------------------------------------
def test_selectable_bodies_lists_every_visible_body_in_order():
    """既定では全Bodyが選択対象で、順序は `doc.bodies` と一致する。"""
    doc = _imported_shelf()

    assert selection.selectable_bodies(doc) == [body.id for body in doc.bodies]


def test_hidden_bodies_are_not_selectable():
    """不可視にしたBodyは `selectable_bodies` から外れ、戻せば復帰する。

    見えないものは指せない。これがコアにあることが、PCケースで「手前の部品を消して
    奥の面を選ぶ」を成立させる(実機で失敗率63%だった原因への直接の対処)。
    """
    doc = _imported_shelf()
    hidden_id = doc.bodies[5].id

    set_visible(doc, hidden_id, False)
    assert hidden_id not in selection.selectable_bodies(doc)
    assert len(selection.selectable_bodies(doc)) == len(doc.bodies) - 1

    set_visible(doc, hidden_id, True)
    assert hidden_id in selection.selectable_bodies(doc)


def test_select_face_returns_a_shape_ref_for_a_visible_body():
    """可視Bodyの面は ShapeRef(§3.5の形)として指せる。"""
    doc = _imported_shelf()
    target = doc.bodies[0]

    ref = selection.select_face(doc, target.id, 1)

    assert ref == {
        "body": target.id,
        "kind": "face",
        "index": 1,
        "provenance": None,
    }


def test_select_face_refuses_a_hidden_body():
    """不可視Bodyの面は指せない(ValueError)。可視に戻せば同じ面が指せる。"""
    doc = _imported_shelf()
    target = doc.bodies[0]
    set_visible(doc, target.id, False)

    with pytest.raises(ValueError):
        selection.select_face(doc, target.id, 1)

    set_visible(doc, target.id, True)
    assert selection.select_face(doc, target.id, 1)["index"] == 1


def test_select_face_rejects_an_unknown_body_and_an_out_of_range_index():
    """存在しないBodyは KeyError、1..face_count の外の添字は ValueError。"""
    doc = _imported_shelf()
    target = doc.bodies[0]
    face_count = kernel.face_count(target.shape)

    with pytest.raises(KeyError):
        selection.select_face(doc, "body-does-not-exist", 1)
    with pytest.raises(ValueError):
        selection.select_face(doc, target.id, 0)
    with pytest.raises(ValueError):
        selection.select_face(doc, target.id, face_count + 1)


def test_extrude_face_refuses_a_hidden_body():
    """不可視Bodyは押し出せない(ValueError)。documentにもログにも変更を残さない。

    可視性が選択機構の一部である以上、**選択を受け取るverbも同じ判定に従う**。
    ここを緩めると、画面から消したつもりの部品がMCP(M4)や再生経路から黙って変形する。
    可視に戻せば同じ面が押せることまで確かめて、「たまたま押せない面だった」で
    通ってしまうのを防ぐ。
    """
    doc = _imported_shelf()
    source = doc.bodies[0]
    edit_id = make_editable_copy(doc, source.id)
    editable = next(body for body in doc.bodies if body.id == edit_id)
    face_index = first_extrudable_face(editable.shape)

    set_visible(doc, edit_id, False)
    shape_before = editable.shape
    log_before = len(doc.log.entries)

    with pytest.raises(ValueError):
        extrude_face(doc, face_index, EXTRUDE_MM, body_id=edit_id)

    assert editable.shape is shape_before, "拒否したのに形状が変わっている"
    assert len(doc.log.entries) == log_before, "拒否したのにログが増えている"

    set_visible(doc, edit_id, True)
    extrude_face(doc, face_index, EXTRUDE_MM, body_id=edit_id)
    assert editable.shape is not shape_before, (
        "可視に戻せば同じ面が押せること(押せない面を選んでいたのではない)"
    )


def test_redo_of_an_extrusion_refuses_a_hidden_body():
    """redo(再適用)の経路でも不可視Bodyは押し出せない。

    可視性の判定を公開API(`extrude_face`)にだけ置くと、redo/replay が呼ぶ
    `_do_extrude_face` から**不可視のBodyを黙って変形できる**(2026-08-11のQA指摘)。
    判定は両者が共有する入口(`_editable_body`)に置くこと。

    ここで `set_visible` verb ではなく `Body.visible` へ直接代入しているのは、
    `OpLog.append` が**やり直せる末尾を捨てる**ため — undo の直後に verb を積むと
    redo する対象そのものが消えて、この経路を通せない。
    """
    doc = _imported_shelf()
    source = doc.bodies[0]
    edit_id = make_editable_copy(doc, source.id)
    editable = next(body for body in doc.bodies if body.id == edit_id)
    face_index = first_extrudable_face(editable.shape)

    extrude_face(doc, face_index, EXTRUDE_MM, body_id=edit_id)
    doc.log.undo(doc)
    shape_before = editable.shape
    editable.visible = False  # ログを介さずに隠す(上記の理由)

    with pytest.raises(ValueError):
        doc.log.redo(doc)

    assert editable.shape is shape_before, "拒否したのに形状が変わっている"

    # 可視に戻せば同じredoが通ること(押せない面を選んでいたのではない)。
    # `OpLog.redo` は do を呼んでからカーソルを進めるので、拒否された時点では
    # やり直せる末尾が残っている。
    editable.visible = True
    doc.log.redo(doc)
    assert editable.shape is not shape_before


# --------------------------------------------------------------------------------------
# 境界(M2・M3と同じ理由。新規ファイルが走査から漏れていないこと自体を固定する)
# --------------------------------------------------------------------------------------
def test_m35_modules_are_covered_by_the_import_boundary_scan():
    """M3.5の新規ファイルにもOCP/build123dのimport境界が効いていること。"""
    scanned = {rel for rel, _ in _source_files()}
    assert set(M35_MODULES) <= scanned, (
        f"走査から漏れている: {sorted(set(M35_MODULES) - scanned)}"
    )
    for rel in M35_MODULES:
        roots = _imported_module_roots(DUOCAD_DIR / rel)
        assert "OCP" not in roots, f"{rel} がOCPを直接importしている(kernel.py越しに呼ぶこと)"
        assert "build123d" not in roots, f"{rel} がbuild123dを直接importしている(query.py越し)"
