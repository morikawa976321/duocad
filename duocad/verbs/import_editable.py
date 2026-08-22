"""verb `import_step_editable` — STEPを読んで**そのまま編集できる状態**まで作る。

**なぜ `import_step` と別に要るのか**: 同じ「STEPを読む」でも、起動時とアプリ内で必要な
ものが違う。起動時(`Session.load_step`)はソリッドごとに参照ボディ＋編集用コピーを
作って参照を隠すので読んだ直後から触れるが、アプリ内の `import_step` は参照ボディ
(`kind="reference"`・読み取り専用)しか作らないので、PCケース27ソリッドを読むと
`make_editable_copy` を**27回**打たないと触れる状態にならない。**起動時にしかできない
ことを無くす**のがこのverbであり、サーバは常に空の文書で起動し、STEPはアプリ内・MCPから
読み込む。

契約:
    import_step_editable(doc, path) -> list[BodyId]
        STEPをソリッドごとに分解し、各ソリッドについて
          - `kind="reference"` の参照ボディ(**visible=False**。原本として文書に残す)
          - `kind="solid"` の編集用ボディ(可視。押し出し等の対象になる)
        の2つを追加する。戻り値は**編集用ボディのID**(単一ソリッドでも1要素のリスト)。

        操作ログは**1行**:
          verb="import_step_editable",
          args={"source_name": str, "step": <STEP本文>,
                "reference_ids": [BodyId, ...], "body_ids": [BodyId, ...]}
        `reference_ids[i]` と `body_ids[i]` は同じソリッドの参照・編集用で、並びは
        `doc.bodies` へ追加した順に対応する。

**形はログの中にある**(2026-08-23): `args["step"]` にSTEP本文を生テキストで取り込むので、
**再生は外部ファイルを一切読まない**。`args["source_name"]` は元のファイル名(パスではない)で、
ボディの命名にしか使わない。理由と代償は `io_step.py` のモジュールdocstringに書いてある。

**ログ1行・undo1回・MCP1呼び出しに収める**(実装計画書§2「verbの粒度」)。コマンド層から
`import_step` と `make_editable_copy` をN回発行する形にしてはならない —— 27部品を読んだ後の
Ctrl+Z が最後の1個だけを戻す挙動になる。

**作った全IDを `args` に記録する**(`reference_ids` / `body_ids`)。記録が無いと再生
(redo / `open_document`)で別のIDが振られ、保存した選択集合や押し出しの `body_id` が
指す先を失う(`sketch_create` と同じ理由)。

失敗の契約(黙って通さない。§6予防6): パスが無い→FileNotFoundError / STEPとして壊れている→
ValueError。いずれも **document にもログにも変更を残さない** —— `kernel.read_step_text` が
`kernel.read_step` 経由で `ReadFile()` の戻り値を先に検査してから例外にするので、
`doc` を触る前にここで止まる(この検査を飛ばすとPython例外ではなくプロセスごと落ちる。
io_step.py の落とし穴の節を参照。**埋め込み経路も同じ1本の安全弁を通る**)。

**参照ボディを残す理由**: 原本をログの上に持っておくと、編集用ボディを壊しても元の形が
文書の中に残る(`make_editable_copy` の位置づけと同じ)。可視のまま持つと同じ形が二重に
テセレーション・二重に送信され、手前を消しても参照側のメッシュが残ってピック失敗が再発する
ので、**追加した時点で visible=False にする**。`Session.load_step` はこれを `set_visible`
verb で別行にしているが、このverbでは1行の中で完結する ——
再生時も `_do_...` が同じ状態を作り直すので、「隠した」という操作が失われることはない。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from .. import kernel
from ..document import Body, BodyId, Document, new_body_id
from ..io_step import read_step_source, solid_names
from ..oplog import Operation, register_verb

PathLike = Union[str, Path]

VERB_IMPORT_STEP_EDITABLE = "import_step_editable"


def import_step_editable(doc: Document, path: PathLike) -> list[BodyId]:
    """STEPを読み、ソリッドごとに参照ボディ(隠す)と編集用ボディを作って後者のIDを返す。"""
    source_name, text = read_step_source(path)  # 無ければ FileNotFoundError
    shape = kernel.read_step_text(text)  # 壊れていればここで例外、doc/logは無傷
    solids = kernel.split_solids(shape)
    reference_ids = [new_body_id() for _ in solids]
    body_ids = [new_body_id() for _ in solids]
    names = _reference_names(source_name, len(solids))
    _apply_to(doc, reference_ids, body_ids, solids, names)
    doc.log.append(
        VERB_IMPORT_STEP_EDITABLE,
        {
            "source_name": source_name,
            "step": text,
            "reference_ids": reference_ids,
            "body_ids": body_ids,
        },
    )
    return body_ids


def _reference_names(source_name: str, count: int) -> list[str]:
    """参照ボディの名前 `<ファイル名>_1` … `_N`(`io_step.import_step` と同じ付け方)。

    編集用ボディはこれに `-edit` を足す(`make_editable_copy` の既定と同じ)。**どの経路で
    読み込んでも同じ命名にそろえてある** —— 名前はパーツ一覧にそのまま並ぶので、
    読み込み経路によって見え方が変わらないようにする。**実体は `io_step.solid_names`**
    (2つの読み込みverbで命名が分岐しないよう、1か所にまとめてある)。
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

    公開APIと redo/replay が共有する非公開ヘルパ。**形状は共有する(深いコピーをしない)** ——
    verbは常に新しい形状を作って `body.shape` を差し替えるので、編集用を変形しても参照側は
    変わらない(`make_editable_copy` と同じ)。

    追加の順序は「参照 → 編集用」をソリッドごとに繰り返す。最後に追加した編集用ボディが
    `Document.add_body` の契約でアクティブになる(起動時の `load_step` と同じ状態)。
    """
    for reference_id, body_id, solid, name in zip(reference_ids, body_ids, solids, names):
        doc.add_body(Body(id=reference_id, name=name, kind="reference", shape=solid, visible=False))
        doc.add_body(Body(id=body_id, name=f"{name}-edit", kind="solid", shape=solid))


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_import_step_editable(document: Document, operation: Operation) -> None:
    """redo / replay: **ログに埋め込まれたSTEP本文**から、記録されたIDをそのまま使って復元する。

    **外部ファイルを読まない**(2026-08-23)。公開APIの `import_step_editable` を
    呼び直してはいけない(ログに二重記録される)。
    """
    args = operation.args
    shape = kernel.read_step_text(args["step"])
    solids = kernel.split_solids(shape)
    names = _reference_names(args["source_name"], len(solids))
    _apply_to(document, args["reference_ids"], args["body_ids"], solids, names)


def _undo_import_step_editable(document: Document, operation: Operation) -> None:
    """undo: 追加した全ボディ(参照・編集用の両方)を取り除く(1回のundoでまとめて消える)。"""
    args = operation.args
    for body_id in list(args["body_ids"]) + list(args["reference_ids"]):
        document.remove_body(body_id)


register_verb(
    VERB_IMPORT_STEP_EDITABLE,
    do=_do_import_step_editable,
    undo=_undo_import_step_editable,
)
