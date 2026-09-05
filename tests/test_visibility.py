"""可視性のverbと、**可視性が選択機構の一部であること**の契約。

PCケース(27部品・2835面)の実機テストではピック失敗12回/押し出し7回(失敗率63%)の原因が
**手前の部品が奥の面を隠していた**ことだった。可視性は見た目の設定ではなく**何を指せるかの
定義**であり、見えないものは指せないことをコアで保証する。

ShapeRef の形は `{"body": BodyId, "kind": "face", "index": int, "provenance": None}`。
"""
from __future__ import annotations

# `_source_files` / `_imported_module_roots` はM0の境界検査の実体(test_sketch.py・
# test_parameters.py と同じ流儀で借りる。tests/ は sys.path に入っている)。
from test_boundaries import DUOCAD_DIR, _imported_module_roots, _source_files

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad.document import Body
from duocad.server import Session

#: M3.5のモジュール(import境界の走査対象に入っていることを固定する)。
#: 可視性はverbではなく `selection.py` 側にある。
M35_MODULES = ("selection.py",)

#: 押し出し量(mm)。符号は正＝外向き法線方向へ足す。
EXTRUDE_MM = 1.0


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


# --------------------------------------------------------------------------------------
# 可視性は undo / redo の対象外
# --------------------------------------------------------------------------------------
def _box_session() -> Session:
    """30×30×40 の直方体を1つ持つセッション(操作ログは空の状態から始まる)。"""
    session = Session(None, 0.2)
    session.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(30.0, 30.0, 40.0).Shape())
    )
    return session


def test_hiding_a_body_is_not_undoable():
    """**Ctrl+Z で表示は戻らない**。undoが戻すのは直前の「形を変えた操作」であり、
    可視性はその対象ではない。
    """
    session = _box_session()
    session.sketch_create("XY")  # 戻る先の本物のverbを1つ積んでおく

    session.set_visible("box", False)
    assert session.document.get_body("box").visible is False

    session.undo()

    assert session.document.get_body("box").visible is False, "undoが可視性まで戻している"
    assert session.document.log.entries == [], "undoが戻したのは直前のverbのはず"


def test_hiding_a_body_does_not_discard_the_redo_stack():
    """隠しただけでredoが消えない。

    `OpLog.append` は**redo可能だった末尾を新しい操作で捨てる**。可視性がverbとして
    ログに乗ると、隠しただけで「やり直せるはずのもの」が黙って消える。
    """
    session = _box_session()
    session.sketch_create("XY")
    session.undo()
    assert session.document.log.can_redo, "検査の前提: やり直せる末尾があること"

    session.set_visible("box", False)

    assert session.document.log.can_redo, "隠しただけでredoが消えた"
    session.redo()
    assert len(session.document.log.entries) == 1
