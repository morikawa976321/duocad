"""STEP読み込み・STEP/STL書き出し(実装計画書 §5 入出力、§7 落とし穴、§8 M0検証)。

M0の契約:
- import_step(doc, path) -> BodyId
    STEPを kind="reference" のボディとして doc.bodies へ追加し、
    doc.log に verb="import_step", args={"path": str(path), "body_id": <新BodyId>} を積んで
    そのBodyIdを返す。
- export_step(doc, path) -> None
    doc.active_body() の形状をSTEPとして書き出す。
- export_stl(doc, path, deviation) -> None
    doc.active_body() の形状をSTLとして書き出す。deviation(mm)は**絶対**の線形偏差。

**書き出しの対象を `doc.active_body()` にするのは「M0の決定」であって仕様書由来ではない**
(実装計画書§5は対象ボディを規定していない。tests/README.md に明記のとおり)。M0の文書は
参照STEP1ボディだけなので「activeだけ」と「全ボディ」に差は出ない。複数ボディが同時に
存在するようになったら(M2以降)`export_step(doc, path, body_ids=None)` の方向で拡張する。

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


def import_step(doc: Document, path: PathLike) -> BodyId:
    """STEPを参照ボディとして読み込み、doc.bodiesへ追加してdoc.logに記録し、BodyIdを返す。

    失敗時は例外を送出し、documentにもログにも変更を残さないこと
    (パス無し=FileNotFoundError / 中身が壊れている=ValueError)。
    そのために **読み込み(失敗しうる処理)を先に済ませてから** append() する。
    """
    body_id = _new_body_id()
    # 読み込みに失敗すればここで送出され、doc.bodies も doc.log も無傷のまま抜ける。
    _apply_import_step_to(doc, str(path), body_id)
    doc.log.append(VERB_IMPORT_STEP, {"path": str(path), "body_id": body_id})
    return body_id


def export_step(doc: Document, path: PathLike) -> None:
    """doc.active_body() の形状をSTEPとして書き出す。読み戻すと面数・体積が一致することが契約。

    書き出す形状が無ければ ValueError を送出し、ファイルを作らない。
    """
    shape = _shape_to_export(doc)
    kernel.write_step(shape, str(path))


def export_stl(doc: Document, path: PathLike, deviation: float) -> None:
    """doc.active_body() の形状をSTLとして書き出す。deviationが小さいほどファイルは大きくなる。

    deviation は**絶対**の線形偏差(mm)。BRepMesh_IncrementalMesh には isRelative=False で渡す。
    書き出す形状が無ければ ValueError を送出し、ファイルを作らない。
    """
    shape = _shape_to_export(doc)
    kernel.write_stl(shape, str(path), deviation)


# --------------------------------------------------------------------------------------
# 内部ヘルパ
# --------------------------------------------------------------------------------------
def _new_body_id() -> BodyId:
    """新しいBodyIdを作る。再生時は記録済みのIDをそのまま使うのでここは通らない。"""
    return f"body-{uuid4().hex[:12]}"


def _shape_to_export(doc: Document):
    """書き出し対象の形状を取り出す。無ければ ValueError(ファイルは1バイトも作らない)。"""
    body = doc.active_body()
    if body is None:
        raise ValueError("nothing to export: the document has no active body")
    if body.shape is None:
        raise ValueError(f"nothing to export: body {body.id!r} has no shape")
    return body.shape


def _apply_import_step_to(doc: Document, path: str, body_id: BodyId) -> Body:
    """STEPを読み、指定IDのボディとしてdocへ追加する(**ログには触らない**)。

    公開APIの `import_step` と、redo/replay の実体が共有する非公開ヘルパ。
    `import_step` 自身が `doc.log.append()` を行うので、redo/replay から `import_step` を
    呼び直すと同じ操作が二重に記録され、保存・再生が破綻する(tests/README.md の契約)。
    """
    shape = kernel.read_step(path)
    body = Body(id=body_id, name=Path(path).stem, kind="reference", shape=shape)
    doc.add_body(body)
    return body


# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない。登録するのは verb を定義したこちら側)
# --------------------------------------------------------------------------------------
def _do_import_step(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用: 記録されたパスを読み直し、記録されたBodyIdで復元する。"""
    args = operation.args
    _apply_import_step_to(document, args["path"], args["body_id"])


def _undo_import_step(document: Document, operation: Operation) -> None:
    """undo 時の取り消し: 追加したボディを取り除く。"""
    document.remove_body(operation.args["body_id"])


register_verb(VERB_IMPORT_STEP, do=_do_import_step, undo=_undo_import_step)
