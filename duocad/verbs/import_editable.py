"""verb `import_step` — STEPを読んで**そのまま編集できる状態**まで作る。

ソリッドごとに編集用ボディ(`kind="solid"` / 可視)を1つ追加し、そのIDを返す。

**STEPの読み込みはこの1 verbしかない**。参照ボディ(`kind="reference"`・読み込んだ原本を
不可視のまま持つ二重管理)という概念は撤去済みである。

**ログ1行・undo1回・MCP1呼び出しに収める**。何部品あっても1操作である —— 部品ごとに
verbを発行する形にすると、27部品を読んだ後の Ctrl+Z が最後の1個だけを戻す挙動になる。
**この規律(verb粒度)は名前が1本になっても変わらない**: 上の層(コマンド・MCP・UI)は
この verb を1回呼ぶだけで、読み込みを複数の操作へ分解してはならない。

**作った全IDを `args` に記録する**(`body_ids`)。記録が無いと再生
(redo / `open_document`)で別のIDが振られ、保存した選択集合や押し出しの `body_id` が
指す先を失う(`sketch_create` と同じ理由)。

**形はログの中にある**: `args["step"]` にSTEP本文を生テキストで取り込むので**再生は外部
ファイルを一切読まない**。`args["source_name"]` は元のファイル名でパスではない。失敗の契約
(パス無し=FileNotFoundError / 壊れている=ValueError。**埋め込み経路も同じ1本の安全弁を
通る**)ともども `io_step.py` のモジュールdocstringを参照。

**旧保存ファイルの`reference_ids`キーは無視される**(`oplog.OpLog`の契約「未知のキーは
無視する」)。移行スクリプトは書かない。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from .. import kernel
from ..document import Body, BodyId, Document, new_body_id
from ..io_step import read_step_source, solid_names
from ..oplog import Operation, register_verb

PathLike = Union[str, Path]

VERB_IMPORT_STEP = "import_step"


def import_step(doc: Document, path: PathLike) -> list[BodyId]:
    """STEPを読み、ソリッドごとに編集用ボディを作ってIDを返す。

    **単一ソリッドでも1要素のリストを返す** —— 戻り値の型が入力(部品数)に依存すると、
    呼び出し側が毎回分岐を書く羽目になる。
    """
    source_name, text = read_step_source(path)
    shape = kernel.read_step_text(text)  # 壊れていればここで例外、doc/logは無傷
    solids = kernel.split_solids(shape)
    body_ids = [new_body_id() for _ in solids]
    names = solid_names(source_name, len(solids))
    _apply_to(doc, body_ids, solids, names)
    doc.log.append(
        VERB_IMPORT_STEP,
        {
            "source_name": source_name,
            "step": text,
            "body_ids": body_ids,
        },
    )
    return body_ids


def _apply_to(
    doc: Document,
    body_ids: list[BodyId],
    solids: list,
    names: list[str],
) -> None:
    """指定IDで編集用ボディを追加する(ログには触らない)。"""
    for body_id, solid, name in zip(body_ids, solids, names):
        doc.add_body(Body(id=body_id, name=name, kind="solid", shape=solid))


def _do_import_step(document: Document, operation: Operation) -> None:
    """redo / replay: **ログに埋め込まれたSTEP本文**から、記録されたIDをそのまま使って復元する。

    公開APIの `import_step` を呼び直してはいけない(ログに二重記録される)。
    """
    args = operation.args
    shape = kernel.read_step_text(args["step"])
    solids = kernel.split_solids(shape)
    names = solid_names(args["source_name"], len(solids))
    _apply_to(document, args["body_ids"], solids, names)


def _undo_import_step(document: Document, operation: Operation) -> None:
    args = operation.args
    for body_id in args["body_ids"]:
        document.remove_body(body_id)


register_verb(
    VERB_IMPORT_STEP,
    do=_do_import_step,
    undo=_undo_import_step,
)
