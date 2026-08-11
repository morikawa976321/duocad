"""STEP読み込み・STEP/STL書き出し(実装計画書 §5 入出力、§7 落とし穴、§8 M0検証)。

M3.5の契約:
- import_step(doc, path) -> list[BodyId]
    STEPをソリッドごとに分解し、各ソリッドを kind="reference" のボディとして
    doc.bodies へ追加する。操作ログは1行
    (verb="import_step", args={"path": str(path), "body_ids": [BodyId, ...]})。
    undo1回で追加した全Bodyがまとめて消える。単一ソリッドでも1要素のリストを返す。
- export_step(doc, path, body_ids=None) -> None
    body_ids省略時は既定で kind="solid" の全Bodyを書き出す(1つも無ければ形状を持つ全Bodyに
    フォールバック)。複数ならcompoundへまとめる。詳細は `_shape_to_export` のdocstring。
- export_stl(doc, path, deviation, body_ids=None) -> None
    body_ids省略時の対象は export_step と同じ。deviation(mm)は**絶対**の線形偏差。
- export_body_ids(doc, body_ids=None) -> [BodyId, ...]
    書き出し対象のID一覧。規則は `_bodies_to_export` に集約し、MCPの戻り値と共有する。

失敗時の契約(黙って通さない。実装計画書 §6予防6):
- パスが存在しない            -> FileNotFoundError
- 存在するがSTEPとして壊れている -> ValueError
- 書き出す形状が無い(空の文書) -> ValueError。**ファイルを作らない**
いずれも doc.bodies / doc.log に変更を残さないこと。

**重要 — 実測済みの危険な落とし穴(2026-08-10・監査で再現確認)**:
OCPの STEPControl_Reader は ReadFile() が IFSelect_RetDone 以外を返した状態のまま
TransferRoots() を呼ぶと、Python例外にはならず **プロセスごと落ちる**
(Windowsで終了コード 0xC0000005 = アクセス違反)。存在しないパス(IFSelect_RetError)でも
中身が壊れたファイル(IFSelect_RetFail)でも同じ。これは実装計画書 §7 が警告する
「OCCTの操作は例外を投げず黙って失敗する」の中でも最悪の形(例外どころかプロセスが死ぬ)。
import_step の実装は **必ず ReadFile() の戻り値を先に検査し、失敗なら TransferRoots()/OneShape()
を呼ぶ前に例外を送出すること**。`Path.exists()` のチェックだけでは壊れたファイルを防げない。
実体は kernel.read_step にあり、この安全弁もそこに実装されている。詳細は tests/README.md 参照。

**deviation の意味**: BRepMesh_IncrementalMesh へ **isRelative=False**(絶対mm)で渡すこと。
isRelative=True にすると 0.2 / 0.05 / 0.01 のどれでも同じサイズのSTLになり、
「偏差を小さくすると細かくなる」という契約が成立しない(2026-08-10監査で実測)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union
from uuid import uuid4

from . import kernel
from .document import Body, BodyId, Document
from .oplog import Operation, register_verb

PathLike = Union[str, Path]

VERB_IMPORT_STEP = "import_step"


def import_step(doc: Document, path: PathLike) -> list[BodyId]:
    """STEPをソリッドごとに分解して参照ボディとして読み込み、doc.bodiesへ追加してdoc.logに記録し、
    追加した全BodyIdをリストで返す(単一ソリッドでも1要素のリスト)。

    操作ログは1行(verb="import_step", args={"path": str(path), "body_ids": [BodyId, ...]})。
    undo1回で全Bodyがまとめて消える。

    失敗時は例外を送出し、documentにもログにも変更を残さない
    (パス無し=FileNotFoundError / 中身が壊れている=ValueError)。
    """
    shape = kernel.read_step(str(path))  # 失敗すればここで例外、doc/logは無傷
    solids = kernel.split_solids(shape)
    body_ids = [_new_body_id() for _ in solids]
    stem = Path(path).stem
    names = [f"{stem}_{n}" for n in range(1, len(solids) + 1)]
    _apply_import_step_to(doc, body_ids, solids, names)
    doc.log.append(VERB_IMPORT_STEP, {"path": str(path), "body_ids": body_ids})
    return body_ids


def export_step(doc: Document, path: PathLike, body_ids: list[BodyId] | None = None) -> None:
    """body_ids省略時の既定対象は `_shape_to_export` のdocstring参照
    (kind="solid"優先・無ければ形状を持つ全Bodyにフォールバック)。
    複数Bodyならcompoundへまとめる。書き出す形状が無ければValueError、ファイルは作らない。
    """
    shape = _shape_to_export(doc, body_ids)
    kernel.write_step(shape, str(path))


def export_stl(
    doc: Document, path: PathLike, deviation: float, body_ids: list[BodyId] | None = None
) -> None:
    """body_ids省略時の既定対象は export_step と同じ(`_shape_to_export` 参照)。
    deviation(mm)は絶対の線形偏差。
    """
    shape = _shape_to_export(doc, body_ids)
    kernel.write_stl(shape, str(path), deviation)


def export_body_ids(doc: Document, body_ids: list[BodyId] | None = None) -> list[BodyId]:
    """書き出し対象の body_id 一覧。規則は `_shape_to_export` と共有する。"""
    return [body.id for body in _bodies_to_export(doc, body_ids)]


def _bodies_to_export(doc: Document, body_ids: list[BodyId] | None):
    """書き出し対象の Body 一覧。無ければ ValueError(ファイルは作らない)。

    `body_ids` 省略時は既定で **`kind="solid"` の全Body**(編集可能なもの)を対象にする。
    `kind="solid"` が1つも無ければ(=まだ編集用コピーを作っていない、参照だけの文書)、
    形状を持つ全Body(=参照)にフォールバックする。

    **なぜ「形状を持つ全Body」を単純な既定にしなかったか(2026-08-11のQAで発見)**:
    `Session.load_step` は各ソリッドについて参照ボディ(隠す)と編集用コピー(可視)の
    両方を文書に持つ。単純に「形状を持つ全Body」を書き出すと、同じソリッドが参照側と
    編集用コピー側の2回、二重に書き出されてしまう。`kind="solid"` を優先することで、
    「実際に編集対象になっているもの」だけを書き出す。
    """
    if body_ids is not None:
        bodies = []
        for body_id in body_ids:
            body = next((b for b in doc.bodies if b.id == body_id), None)
            if body is None:
                raise KeyError(f"no such body: {body_id!r}")
            bodies.append(body)
    else:
        bodies = [b for b in doc.bodies if b.kind == "solid" and b.shape is not None]
        if not bodies:
            bodies = [b for b in doc.bodies if b.shape is not None]
    if not bodies:
        raise ValueError("nothing to export: no body with a shape")
    return bodies


def _shape_to_export(doc: Document, body_ids):
    """書き出し対象の形状(複数なら compound)。対象選びは `_bodies_to_export` に集約。"""
    bodies = _bodies_to_export(doc, body_ids)
    shapes = [b.shape for b in bodies]
    if len(shapes) == 1:
        return shapes[0]
    return kernel.make_compound(shapes)


def _new_body_id() -> BodyId:
    """新しいBodyIdを作る。再生時は記録済みのIDをそのまま使うのでここは通らない。"""
    return f"body-{uuid4().hex[:12]}"


def _apply_import_step_to(
    doc: Document, body_ids: list[BodyId], solids: list, names: list[str]
) -> list[Body]:
    """ソリッド群を指定IDのボディとしてdocへ追加する(ログには触らない)。

    公開APIの `import_step` と、redo/replay の実体が共有する非公開ヘルパ。
    """
    bodies = []
    for body_id, solid, name in zip(body_ids, solids, names):
        body = Body(id=body_id, name=name, kind="reference", shape=solid)
        doc.add_body(body)
        bodies.append(body)
    return bodies


def _do_import_step(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用: パスを読み直して分解し、記録されたbody_idsをそのまま使って復元する。

    **`io_step.import_step`(公開API)を呼び直してはいけない**(呼ぶとログに二重記録される)。
    """
    args = operation.args
    shape = kernel.read_step(args["path"])
    solids = kernel.split_solids(shape)
    stem = Path(args["path"]).stem
    names = [f"{stem}_{n}" for n in range(1, len(solids) + 1)]
    _apply_import_step_to(document, args["body_ids"], solids, names)


def _undo_import_step(document: Document, operation: Operation) -> None:
    """undo 時の取り消し: 追加した全ボディを取り除く(1回のundoでまとめて消える)。"""
    for body_id in operation.args["body_ids"]:
        document.remove_body(body_id)


register_verb(VERB_IMPORT_STEP, do=_do_import_step, undo=_undo_import_step)