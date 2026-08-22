"""STEP読み込み・STEP/STL書き出し。

**危険な罠**: OCPの STEPControl_Reader は ReadFile() が IFSelect_RetDone 以外を返した状態のまま
TransferRoots() を呼ぶと、Python例外にはならず**プロセスごと落ちる**(Windowsで終了コード
0xC0000005 = アクセス違反)。存在しないパスでも中身が壊れたファイルでも同じ。**必ず ReadFile()
の戻り値を先に検査すること**(`Path.exists()` のチェックだけでは壊れたファイルを防げない)。
実体と安全弁は `kernel.read_step` にあり、**リーダを立てる場所はここ1本に保つ** ——
ログ埋め込み本文からの復元(`kernel.read_step_text`)も一時ファイルへ書いてから同じ
`kernel.read_step` を通す。増やすと安全弁の無い経路が1本できる。

**deviation は BRepMesh_IncrementalMesh へ isRelative=False(絶対mm)で渡す**。True にすると
0.2 / 0.05 / 0.01 のどれでも同じサイズのSTLになり、「偏差を小さくすると細かくなる」が成立しない。

**形はログの中にある**: `import_step` は読んだ時点でSTEP本文を `args["step"]` へ生テキストで
取り込む(base64にしない —— JSON保存はgitで差分が読めることが目的で、base64がそれを潰す)。
`args["source_name"]` はファイル名だけで**パスではない**。したがって**再生は外部ファイルを
一切読まない**。取り込みなので、元のSTEPを後から更新しても文書は追随しない(意図)。

失敗は黙って通さない: パス無し=FileNotFoundError / 壊れている=ValueError / 書き出す形状が
無い=ValueError(**ファイルを作らない**)。いずれも doc.bodies / doc.log に変更を残さない。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from . import kernel
from .document import Body, BodyId, Document, new_body_id
from .oplog import Operation, register_verb

PathLike = Union[str, Path]

VERB_IMPORT_STEP = "import_step"


def read_step_source(path: PathLike) -> tuple[str, str]:
    """STEPファイルを読み、`(source_name, text)` を返す。`source_name` は**ファイル名だけ**。

    **バイト列で読んでから復号する**。STEPはASCIIが原則だが、ヘッダのコメントや説明文字列に
    ASCII外のバイトが入っている実ファイルがあるため、UTF-8で読めなければ latin-1 へ落とす
    (latin-1はどのバイト列でも失敗しない)。
    """
    data = Path(path).read_bytes()  # 無ければ FileNotFoundError
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return Path(path).name, text


def solid_names(source_name: str, count: int) -> list[str]:
    """参照ボディの名前 `<元ファイル名の拡張子抜き>_1` … `_N`。

    verb `import_step_editable` の `_reference_names` と同じ付け方にそろえてある。
    """
    stem = Path(source_name).stem
    return [f"{stem}_{n}" for n in range(1, count + 1)]


def import_step(doc: Document, path: PathLike) -> list[BodyId]:
    """STEPをソリッドごとに分解して参照ボディとして読み込み、追加した全BodyIdを返す。

    undo1回で全Bodyがまとめて消える。失敗時は例外を送出し、documentにもログにも何も残さない。
    """
    source_name, text = read_step_source(path)
    shape = kernel.read_step_text(text)  # 壊れていればここで例外、doc/logは無傷
    solids = kernel.split_solids(shape)
    body_ids = [new_body_id() for _ in solids]
    names = solid_names(source_name, len(solids))
    _apply_import_step_to(doc, body_ids, solids, names)
    doc.log.append(
        VERB_IMPORT_STEP,
        {"source_name": source_name, "step": text, "body_ids": body_ids},
    )
    return body_ids


def export_step(doc: Document, path: PathLike, body_ids: list[BodyId] | None = None) -> None:
    """対象の既定は `_bodies_to_export` 参照。複数Bodyならcompoundへまとめる。"""
    shape = _shape_to_export(doc, body_ids)
    kernel.write_step(shape, str(path))


def export_stl(
    doc: Document, path: PathLike, deviation: float, body_ids: list[BodyId] | None = None
) -> None:
    """deviation(mm)は**絶対**の線形偏差。対象の既定は export_step と同じ。"""
    shape = _shape_to_export(doc, body_ids)
    kernel.write_stl(shape, str(path), deviation)


def export_body_ids(doc: Document, body_ids: list[BodyId] | None = None) -> list[BodyId]:
    return [body.id for body in _bodies_to_export(doc, body_ids)]


def _bodies_to_export(doc: Document, body_ids: list[BodyId] | None):
    """書き出し対象の Body 一覧。無ければ ValueError(ファイルは作らない)。

    既定は `kind="solid"` の全Body、1つも無ければ形状を持つ全Bodyへフォールバックする。
    `Session.load_step` は各ソリッドを参照ボディと編集用コピーの両方で持つので、
    「形状を持つ全Body」を素の既定にすると同じソリッドが二重に書き出される。
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
    bodies = _bodies_to_export(doc, body_ids)
    shapes = [b.shape for b in bodies]
    if len(shapes) == 1:
        return shapes[0]
    return kernel.make_compound(shapes)


def _apply_import_step_to(
    doc: Document, body_ids: list[BodyId], solids: list, names: list[str]
) -> list[Body]:
    """ソリッド群を指定IDのボディとしてdocへ追加する(ログには触らない)。"""
    bodies = []
    for body_id, solid, name in zip(body_ids, solids, names):
        body = Body(id=body_id, name=name, kind="reference", shape=solid)
        doc.add_body(body)
        bodies.append(body)
    return bodies


def _do_import_step(document: Document, operation: Operation) -> None:
    """redo / replay: **ログに埋め込まれたSTEP本文**から復元する(外部ファイルを読まない)。

    **公開APIの `io_step.import_step` を呼び直してはいけない**(ログに二重記録される)。
    """
    args = operation.args
    shape = kernel.read_step_text(args["step"])
    solids = kernel.split_solids(shape)
    names = solid_names(args["source_name"], len(solids))
    _apply_import_step_to(document, args["body_ids"], solids, names)


def _undo_import_step(document: Document, operation: Operation) -> None:
    for body_id in operation.args["body_ids"]:
        document.remove_body(body_id)


register_verb(VERB_IMPORT_STEP, do=_do_import_step, undo=_undo_import_step)