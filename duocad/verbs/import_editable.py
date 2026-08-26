"""verb `import_step` — STEPを読んで**そのまま編集できる状態**まで作る。

ソリッドごとに参照ボディ(`kind="reference"` / **visible=False**)と編集用ボディ
(`kind="solid"` / 可視)の2つを追加し、戻り値は編集用ボディのID。`reference_ids[i]` と
`body_ids[i]` は同じソリッドの参照・編集用で、並びが対応する。

**STEPの読み込みはこの1 verbしかない**(2026-08-27に統合)。参照ボディだけを足す読み込みと、
それを後から編集可能にする複製のverbは消えた —— 参照ボディを起点にする手はもう無い。

**ログ1行・undo1回・MCP1呼び出しに収める**。何部品あっても1操作である —— 部品ごとに
verbを発行する形にすると、27部品を読んだ後の Ctrl+Z が最後の1個だけを戻す挙動になる。
**この規律(verb粒度)は名前が1本になっても変わらない**: 上の層(コマンド・MCP・UI)は
この verb を1回呼ぶだけで、読み込みを複数の操作へ分解してはならない。

**作った全IDを `args` に記録する**(`reference_ids` / `body_ids`)。記録が無いと再生
(redo / `open_document`)で別のIDが振られ、保存した選択集合や押し出しの `body_id` が
指す先を失う(`sketch_create` と同じ理由)。

**形はログの中にある**: `args["step"]` にSTEP本文を生テキストで取り込むので**再生は外部
ファイルを一切読まない**。`args["source_name"]` は元のファイル名でパスではない。失敗の契約
(パス無し=FileNotFoundError / 壊れている=ValueError。**埋め込み経路も同じ1本の安全弁を
通る**)ともども `io_step.py` のモジュールdocstringを参照。

**参照ボディは追加した時点で visible=False にする**。可視のまま持つと同じ形が二重に
テセレーション・二重に送信され、手前を消しても参照側のメッシュが残ってピック失敗が再発する。
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
    """STEPを読み、ソリッドごとに参照ボディ(隠す)と編集用ボディを作って後者のIDを返す。"""
    source_name, text = read_step_source(path)
    shape = kernel.read_step_text(text)  # 壊れていればここで例外、doc/logは無傷
    solids = kernel.split_solids(shape)
    reference_ids = [new_body_id() for _ in solids]
    body_ids = [new_body_id() for _ in solids]
    names = _reference_names(source_name, len(solids))
    _apply_to(doc, reference_ids, body_ids, solids, names)
    doc.log.append(
        VERB_IMPORT_STEP,
        {
            "source_name": source_name,
            "step": text,
            "reference_ids": reference_ids,
            "body_ids": body_ids,
        },
    )
    return body_ids


def _reference_names(source_name: str, count: int) -> list[str]:
    """参照ボディの名前 `<ファイル名>_1` … `_N`(**実体は `io_step.solid_names`**)。

    編集用ボディはこれに `-edit` を足す。**どの読み込み経路でも同じ命名にそろえてある**。
    """
    return solid_names(source_name, count)


def _apply_to(
    doc: Document,
    reference_ids: list[BodyId],
    body_ids: list[BodyId],
    solids: list,
    names: list[str],
) -> None:
    """指定IDで参照ボディ(隠す)と編集用ボディを追加する(ログには触らない)。

    **形状は共有する(深いコピーをしない)** —— verbは常に新しい形状を作って `body.shape` を
    差し替えるので、編集用を変形しても参照側は変わらない。追加は「参照 → 編集用」の順で、
    最後に追加した編集用ボディが `Document.add_body` の契約でアクティブになる。
    """
    for reference_id, body_id, solid, name in zip(reference_ids, body_ids, solids, names):
        doc.add_body(Body(id=reference_id, name=name, kind="reference", shape=solid, visible=False))
        doc.add_body(Body(id=body_id, name=f"{name}-edit", kind="solid", shape=solid))


def _do_import_step(document: Document, operation: Operation) -> None:
    """redo / replay: **ログに埋め込まれたSTEP本文**から、記録されたIDをそのまま使って復元する。

    公開APIの `import_step` を呼び直してはいけない(ログに二重記録される)。
    """
    args = operation.args
    shape = kernel.read_step_text(args["step"])
    solids = kernel.split_solids(shape)
    names = _reference_names(args["source_name"], len(solids))
    _apply_to(document, args["reference_ids"], args["body_ids"], solids, names)


def _undo_import_step(document: Document, operation: Operation) -> None:
    args = operation.args
    for body_id in list(args["body_ids"]) + list(args["reference_ids"]):
        document.remove_body(body_id)


register_verb(
    VERB_IMPORT_STEP,
    do=_do_import_step,
    undo=_undo_import_step,
)
