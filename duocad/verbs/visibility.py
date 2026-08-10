"""verb `set_visible` — Bodyの表示・非表示を切り替える(M3.5・2026-08-11)。

**なぜコアのverbなのか**: アプリ層のチェックボックスとして実装すると、可視性は文書の状態に
ならず、操作ログにも載らず、undoできず、MCP(M4)からも触れない。実装計画書§2「コアはUIが
無くても機能的に完全」の帰結として、**表示・非表示もverbである**。ここに置けばM4で自動的に
MCPから使えるようになる(タスクファイルの「やらないこと」に明記のとおり)。

契約:
    set_visible(doc, body_id, visible) -> Operation
        `body_id` のBodyの `visible` を `visible` にし、ログに
        `verb="set_visible"`, `args={"body_id":.., "visible": bool, "previous": bool}`
        を積んで Operation を返す。
        `previous` は undo が元の値へ戻すために要る(`set_parameter` と同じ流儀)。

失敗の契約(黙って通さない。§6予防6):
    存在しない body_id -> KeyError。**documentにもログにも変更を残さない**
    (`Document` の他のAPIと揃える。`is_editable` / `set_active_body` / `remove_body` は
     いずれも存在しないIDで KeyError を送出する)。

**可視性は選択機構の一部**であり、`visible=False` のBodyは選択の対象にならない
(`duocad.selection` を参照)。表示を消しただけのつもりで押し出しが通ってしまうと、
画面に出ていない部品が黙って変形する。
"""
from __future__ import annotations

from ..document import BodyId, Document
from ..oplog import Operation, register_verb

VERB_SET_VISIBLE = "set_visible"


def set_visible(doc: Document, body_id: BodyId, visible: bool) -> Operation:
    """Bodyの表示・非表示を切り替え、ログに積んで Operation を返す。"""
    raise NotImplementedError(
        "M3.5: set_visible は未実装。"
        "存在しないbody_idはKeyErrorで、documentにもログにも変更を残さないこと"
    )


# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない。登録は verb を定義したこちら側)
# --------------------------------------------------------------------------------------
def _do_set_visible(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された `visible` を当て直す(**ログには触らない**)。"""
    raise NotImplementedError("M3.5: set_visible の do は未実装")


def _undo_set_visible(document: Document, operation: Operation) -> None:
    """undo: `args["previous"]` の値へ戻す。"""
    raise NotImplementedError("M3.5: set_visible の undo は未実装")


register_verb(VERB_SET_VISIBLE, do=_do_set_visible, undo=_undo_set_visible)
