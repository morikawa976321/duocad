"""STEP読み込み・STEP/STL書き出し(実装計画書 §5 入出力、§7 落とし穴、§8 M0検証)。

契約:
- import_step(doc, path) -> list[BodyId]
    STEPをソリッドごとに分解し、各ソリッドを kind="reference" のボディとして
    doc.bodies へ追加する。操作ログは1行
    (verb="import_step",
     args={"source_name": str, "step": <STEP本文>, "body_ids": [BodyId, ...]})。
    undo1回で追加した全Bodyがまとめて消える。単一ソリッドでも1要素のリストを返す。

**形はログの中にある**(2026-08-23。engine `duocad-oplog-embeds-imported-step`):
    以前は `args={"path": ...}` だけを積み、再生のたびに**同じ絶対パスを読み直して**いた。
    これは「操作ログが唯一の正であり、文書の形状はここから導出される」(oplog.py)という
    明文の契約を破っていた —— 外部STEPという**第二の正**があり、参照先を移動・改名・削除
    するか別PCへ持って行くと文書が開けなくなる。いまは**読み込んだ時点でSTEP本文を
    `args["step"]` へ生テキストで取り込む**(base64にしない —— 保存形式をJSONにしたのは
    gitで差分が読める・AIが履歴を全部読めるからで、base64はその両方を潰す)。
    元のファイル名は `args["source_name"]` に残る(ボディの命名に使う。**パスではない**)。
    したがって**再生は外部ファイルを一切読まない**。取り込みなので、
    元のSTEPを後から更新しても文書は追随しない(Fusionの「挿入」と同じ。これは意図)。
- export_step(doc, path, body_ids=None) -> None
    body_ids省略時は既定で kind="solid" の全Bodyを書き出す(1つも無ければ形状を持つ全Bodyに
    フォールバック)。複数ならcompoundへまとめる。詳細は `_shape_to_export` のdocstring。
- export_stl(doc, path, deviation, body_ids=None) -> None
    body_ids省略時の対象は export_step と同じ。deviation(mm)は**絶対**の線形偏差。
- export_body_ids(doc, body_ids=None) -> [BodyId, ...]
    書き出し対象のID一覧。規則は `_bodies_to_export` に集約し、MCPの戻り値と共有する。
- read_step_source(path) -> (source_name, text) / solid_names(source_name, count) -> [str, ...]
    **2つの読み込みverbが共有する**(verb `import_step_editable` も同じ取り込み・同じ命名に
    なるように、ここが唯一の出どころ。分けると読み込み経路で名前や取り込み方が食い違う)。

失敗時の契約(黙って通さない。実装計画書 §6予防6):
- パスが存在しない            -> FileNotFoundError
- 存在するがSTEPとして壊れている -> ValueError
- 書き出す形状が無い(空の文書) -> ValueError。**ファイルを作らない**
いずれも doc.bodies / doc.log に変更を残さないこと。

**重要 — 危険な落とし穴**:
OCPの STEPControl_Reader は ReadFile() が IFSelect_RetDone 以外を返した状態のまま
TransferRoots() を呼ぶと、Python例外にはならず **プロセスごと落ちる**
(Windowsで終了コード 0xC0000005 = アクセス違反)。存在しないパス(IFSelect_RetError)でも
中身が壊れたファイル(IFSelect_RetFail)でも同じ。これは実装計画書 §7 が警告する
「OCCTの操作は例外を投げず黙って失敗する」の中でも最悪の形(例外どころかプロセスが死ぬ)。
import_step の実装は **必ず ReadFile() の戻り値を先に検査し、失敗なら TransferRoots()/OneShape()
を呼ぶ前に例外を送出すること**。`Path.exists()` のチェックだけでは壊れたファイルを防げない。
実体は kernel.read_step にあり、この安全弁もそこに実装されている。詳細は tests/README.md 参照。
**ログに埋め込んだ本文からの復元(kernel.read_step_text)も、一時ファイルへ書いてから
同じ kernel.read_step を通す** —— リーダを立てる場所を2つに増やすと、安全弁の無い経路が
1本できてしまうため(埋め込みが壊れていれば ValueError で止まる。
tests/test_io_step.py::test_replaying_a_broken_embedded_step_raises_instead_of_crashing)。

**deviation の意味**: BRepMesh_IncrementalMesh へ **isRelative=False**(絶対mm)で渡すこと。
isRelative=True にすると 0.2 / 0.05 / 0.01 のどれでも同じサイズのSTLになり、
「偏差を小さくすると細かくなる」という契約が成立しない。
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
    """STEPファイルを読み、`(source_name, text)` を返す(ログへ埋め込むための取り込み)。

    - `source_name` … **ファイル名だけ**(拡張子を含む)。ボディの命名に使う。パスは残さない
      —— 残すと「開ける文書」の判定に他人のディレクトリ構成が混ざる。
    - `text` … STEP本文。

    **バイト列で読んでから復号する**。STEPはASCIIが原則だが、ヘッダのコメントや
    説明文字列にASCII外のバイトが入っている実ファイルがあるため、UTF-8で読めなければ
    latin-1へ落とす(latin-1はどのバイト列でも失敗しない)。ここで例外にすると、
    「読めるはずのSTEPが読めない」が復号の都合で起きる。

    パスが無ければ FileNotFoundError(`kernel.read_step` と同じ契約。中身がSTEPとして
    壊れている場合は、後段の `kernel.read_step_text` が ValueError にする)。
    """
    data = Path(path).read_bytes()  # 無ければ FileNotFoundError
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return Path(path).name, text


def solid_names(source_name: str, count: int) -> list[str]:
    """参照ボディの名前 `<元ファイル名の拡張子抜き>_1` … `_N`。

    **`source_name` はファイル名**(`read_step_source` が入れる)なので、ここでの
    `Path(...).stem` はパスの分解ではなく拡張子落としである。verb `import_step_editable`
    の `_reference_names` と同じ付け方にそろえてある(読み込み経路で名前が変わらないこと)。
    """
    stem = Path(source_name).stem
    return [f"{stem}_{n}" for n in range(1, count + 1)]


def import_step(doc: Document, path: PathLike) -> list[BodyId]:
    """STEPをソリッドごとに分解して参照ボディとして読み込み、doc.bodiesへ追加してdoc.logに記録し、
    追加した全BodyIdをリストで返す(単一ソリッドでも1要素のリスト)。

    操作ログは1行(verb="import_step",
    args={"source_name": str, "step": <STEP本文>, "body_ids": [BodyId, ...]})。
    **形はここに入る**ので、再生は元のパスを読み直さない(モジュールdocstring参照)。
    undo1回で全Bodyがまとめて消える。

    失敗時は例外を送出し、documentにもログにも変更を残さない
    (パス無し=FileNotFoundError / 中身が壊れている=ValueError)。
    """
    source_name, text = read_step_source(path)  # 無ければ FileNotFoundError
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

    **なぜ「形状を持つ全Body」を単純な既定にしなかったか**:
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
    """redo / replay 時の再適用: **ログに埋め込まれたSTEP本文**から復元する。

    **外部ファイルを読まない**(2026-08-23)。`args["step"]` が形の出どころで、
    `args["source_name"]` は名前にしか使わない。記録された body_ids をそのまま使う。

    **`io_step.import_step`(公開API)を呼び直してはいけない**(呼ぶとログに二重記録される)。
    """
    args = operation.args
    shape = kernel.read_step_text(args["step"])
    solids = kernel.split_solids(shape)
    names = solid_names(args["source_name"], len(solids))
    _apply_import_step_to(document, args["body_ids"], solids, names)


def _undo_import_step(document: Document, operation: Operation) -> None:
    """undo 時の取り消し: 追加した全ボディを取り除く(1回のundoでまとめて消える)。"""
    for body_id in operation.args["body_ids"]:
        document.remove_body(body_id)


register_verb(VERB_IMPORT_STEP, do=_do_import_step, undo=_undo_import_step)