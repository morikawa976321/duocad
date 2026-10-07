"""verb `rename` — ボディかスケッチの名前を変える(種別を問わない。メッシュボディも)。

**操作ログに載せる**(可視性と違う)。保存されるのはログだけなので、ログの外で変えた名前は
保存されず、開き直すと作成時の名前へ戻る。名前を変えることは Ctrl+Z の1手を使う編集である。

対象は ID 1つで指す(`target`)。ボディとスケッチの ID は綴りが違い(`body-…` / `sketch-…`)、
どちらも uuid なので、両方から引いて見つかった方を変える。名前で引く口はどこにも無い
(参照はすべて ID)ので、**同じ名前が並ぶのは許す**。
"""
from __future__ import annotations

from typing import Any, Optional

from ..document import Document
from ..entry import entry
from ..oplog import Operation, register_verb
from ..sketch import Sketch

VERB_RENAME = "rename"


def _target(doc: Document, target: str) -> Any:
    """ID のボディかスケッチ。どちらにも無ければ KeyError。"""
    for item in doc.bodies:
        if item.id == target:
            return item
    for item in doc.sketches:
        if item.id == target:
            return item
    raise KeyError(
        f"no body or sketch with id {target!r} "
        f"(bodies: {[body.id for body in doc.bodies]}, sketches: {[sketch.id for sketch in doc.sketches]})"
    )


def check_name(name: Any) -> str:
    """名前として受け付けるか。違反は ValueError。

    前後の空白は**削らずに断る**(画面のファイル名の欄と同じ線。削ると、打った名前と
    付いた名前が黙って食い違う)。改行などの制御文字は一覧の1行を壊すので断る。
    """
    if not isinstance(name, str):
        raise ValueError(f"name must be a string, got {type(name).__name__}")
    if not name.strip():
        raise ValueError("name must not be empty")
    if name != name.strip():
        raise ValueError(f"name must not start or end with whitespace: {name!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise ValueError(f"name must not contain control characters: {name!r}")
    return name


def rename(doc: Document, target: str, name: str) -> Operation:
    """`target`(BodyId / SketchId)の名前を `name` にする。**今と同じ名前は ValueError**
    (何も変えない行をログに積まない)。失敗したら文書もログも変えない。"""
    item = _target(doc, str(target))
    new_name = check_name(name)
    previous: Optional[str] = item.name
    if previous == new_name:
        raise ValueError(f"{item.id!r} is already named {new_name!r}")
    item.name = new_name
    return doc.log.append(
        VERB_RENAME, {"target": item.id, "name": new_name, "previous": previous}
    )


def _do_rename(document: Document, operation: Operation) -> None:
    _target(document, operation.args["target"]).name = operation.args["name"]


def _undo_rename(document: Document, operation: Operation) -> None:
    # `previous` が None になるのはスケッチだけ(番号の記録が無い古いスケッチ。`Sketch.name`)。
    _target(document, operation.args["target"]).name = operation.args["previous"]


register_verb(VERB_RENAME, do=_do_rename, undo=_undo_rename)


@entry(VERB_RENAME)
def _enter_rename(document: Document, target: str, name: str) -> dict:
    """対象は選択ではなく引数の ID(ブラウザの一覧の行・AI)。"""
    args = rename(document, str(target), name).args
    kind = "sketch" if isinstance(_target(document, args["target"]), Sketch) else "body"
    return {"target": args["target"], "kind": kind, "name": args["name"]}
