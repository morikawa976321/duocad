"""verb `import_step_editable` — STEPを読んで**そのまま編集できる状態**まで作る(2026-08-15)。

**なぜ `import_step` と別に要るのか(本人の指摘)**: 同じ「STEPを読む」が起動時とアプリ内で
違う意味になっていた。起動時(`Session.load_step`)はソリッドごとに参照ボディ＋編集用コピーを
作って参照を隠すので読んだ直後から触れるが、アプリ内(`import_step`)は参照ボディ
(`kind="reference"`・読み取り専用)しか作らないので、PCケース27ソリッドを読むと
`make_editable_copy` を**27回**打たないと触れる状態にならない。この非対称のせいで
起動オプション `--step` が消せなかった。**起動時にしかできないことを無くす**のがこのverbで、
これで `--step` は廃止できる(常に空で起動し、アプリ内・MCPから好きなSTEPを読む)。

契約:
    import_step_editable(doc, path) -> list[BodyId]
        STEPをソリッドごとに分解し、各ソリッドについて
          - `kind="reference"` の参照ボディ(**visible=False**。原本として文書に残す)
          - `kind="solid"` の編集用ボディ(可視。押し出し等の対象になる)
        の2つを追加する。戻り値は**編集用ボディのID**(単一ソリッドでも1要素のリスト)。

        操作ログは**1行**:
          verb="import_step_editable",
          args={"path": str, "reference_ids": [BodyId, ...], "body_ids": [BodyId, ...]}
        `reference_ids[i]` と `body_ids[i]` は同じソリッドの参照・編集用で、並びは
        `doc.bodies` へ追加した順に対応する。

**ログ1行・undo1回・MCP1呼び出しに収める**(実装計画書§2「verbの粒度」)。コマンド層から
`import_step` と `make_editable_copy` をN回発行する形にしてはならない —— 27部品を読んだ後の
Ctrl+Z が最後の1個だけを戻す挙動になる。

**作った全IDを `args` に記録する**(`reference_ids` / `body_ids`)。記録が無いと再生
(redo / `open_document`)で別のIDが振られ、保存した選択集合や押し出しの `body_id` が
指す先を失う(M2の `sketch_create` と同じ理由)。

失敗の契約(黙って通さない。§6予防6): パスが無い→FileNotFoundError / STEPとして壊れている→
ValueError。いずれも **document にもログにも変更を残さない** —— `kernel.read_step` が
`ReadFile()` の戻り値を先に検査してから例外にするので、`doc` を触る前にここで止まる
(この検査を飛ばすとPython例外ではなくプロセスごと落ちる。io_step.py の落とし穴の節を参照)。

**参照ボディを残す理由**: 原本をログの上に持っておくと、編集用ボディを壊しても元の形が
文書の中に残る(`make_editable_copy` の位置づけと同じ)。可視のまま持つと同じ形が二重に
テセレーション・二重に送信され、手前を消しても参照側のメッシュが残ってピック失敗が再発する
(2026-08-11のQA指摘)ので、**追加した時点で visible=False にする**。`Session.load_step` は
これを `set_visible` verb で別行にしていたが、このverbでは1行の中で完結する ——
再生時も `_do_...` が同じ状態を作り直すので、「隠した」という操作が失われることはない。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union
from uuid import uuid4

from .. import kernel
from ..document import Body, BodyId, Document
from ..oplog import Operation, register_verb

PathLike = Union[str, Path]

VERB_IMPORT_STEP_EDITABLE = "import_step_editable"


def import_step_editable(doc: Document, path: PathLike) -> list[BodyId]:
    """STEPを読み、ソリッドごとに参照ボディ(隠す)と編集用ボディを作って後者のIDを返す。"""
    shape = kernel.read_step(str(path))  # 失敗すればここで例外、doc/logは無傷
    solids = kernel.split_solids(shape)
    reference_ids = [_new_body_id() for _ in solids]
    body_ids = [_new_body_id() for _ in solids]
    names = _reference_names(path, len(solids))
    _apply_to(doc, reference_ids, body_ids, solids, names)
    doc.log.append(
        VERB_IMPORT_STEP_EDITABLE,
        {"path": str(path), "reference_ids": reference_ids, "body_ids": body_ids},
    )
    return body_ids


def _reference_names(path: PathLike, count: int) -> list[str]:
    """参照ボディの名前 `<ファイル名>_1` … `_N`(`io_step.import_step` と同じ付け方)。

    編集用ボディはこれに `-edit` を足す(`make_editable_copy` の既定と同じ)。**起動時の
    `--step` が作っていた名前とそろえてある** —— 名前はパーツ一覧にそのまま並ぶので、
    読み込み経路によって見え方が変わらないようにする。
    """
    stem = Path(path).stem
    return [f"{stem}_{n}" for n in range(1, count + 1)]


def _new_body_id() -> BodyId:
    """新しいBodyId。再生時は記録済みのIDをそのまま使うのでここは通らない。"""
    return f"body-{uuid4().hex[:12]}"


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
    """redo / replay: パスを読み直し、**記録されたIDをそのまま使って**復元する。

    公開APIの `import_step_editable` を呼び直してはいけない(ログに二重記録される)。
    """
    args = operation.args
    shape = kernel.read_step(args["path"])
    solids = kernel.split_solids(shape)
    names = _reference_names(args["path"], len(solids))
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
