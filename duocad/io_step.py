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

**読み込みのverb本体はここに無い** —— `import_step` は `duocad/verbs/import_editable.py` に
1本だけある(2026-08-27に統合。参照ボディだけを足す経路は消えた)。ここに残すのは
**読み込み経路が共有する部品**(`read_step_source` / `solid_names`)と書き出しである。
形の取り込み方(STEP本文を `args["step"]` へ生テキストで持ち、再生が外部ファイルを
一切読まないこと)の契約は、そのverbのモジュールdocstringが正。

失敗は黙って通さない: パス無し=FileNotFoundError / 壊れている=ValueError / 書き出す形状が
無い=ValueError(**ファイルを作らない**)。いずれも doc.bodies / doc.log に変更を残さない。

**書き出し対象は「いま表示中(`visible=True`)のBody」に固定してある**（対象を選ぶ引数は無い）。
候補は形状を持つ全Body。候補はあるのに全部非表示なら専用の ValueError
(`set_visible(visible=True)` で戻す手順つき)を出し、候補が最初から無い場合の文言とは分ける。
含めたくないBodyは呼び出し側が `set_visible` で隠してから書き出すのが唯一の経路。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from . import kernel
from .document import Body, BodyId, Document

PathLike = Union[str, Path]


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
    """編集用ボディの名前 `<元ファイル名の拡張子抜き>_1` … `_N`。

    verb `import_step` の `_names` の実体がこれである(命名を1箇所に持つ)。
    """
    stem = Path(source_name).stem
    return [f"{stem}_{n}" for n in range(1, count + 1)]


def export_step(doc: Document, path: PathLike) -> None:
    """対象は `_bodies_to_export` 参照(いま表示中のBodyのみ)。複数Bodyならcompoundへまとめる。"""
    shape = _shape_to_export(doc)
    kernel.write_step(shape, str(path))


def export_stl(doc: Document, path: PathLike, deviation: float) -> None:
    """deviation(mm)は**絶対**の線形偏差。対象は export_step と同じ。"""
    shape = _shape_to_export(doc)
    kernel.write_stl(shape, str(path), deviation)


def export_body_ids(doc: Document) -> list[BodyId]:
    return [body.id for body in _bodies_to_export(doc)]


def export_skipped_hidden(doc: Document) -> list[BodyId]:
    """書き出し候補のうち非表示(`visible=False`)で除外されたBodyのID一覧(除外0件なら空リスト)。

    `export_body_ids` と対で使う想定 —— 呼び出し側(server.py / mcp_server.py)が
    応答へ両方載せて「何を書き出し、何を除外したか」を必ず見えるようにする。
    """
    return [b.id for b in _export_candidates(doc) if not b.visible]


def _export_candidates(doc: Document) -> list[Body]:
    """書き出し候補(可視性はまだ見ない)。形状を持つ全Body。"""
    return [b for b in doc.bodies if b.shape is not None]


def _bodies_to_export(doc: Document) -> list[Body]:
    """書き出し対象の Body 一覧(表示中のものだけ)。無ければ ValueError(ファイルは作らない)。

    候補が最初から無い場合と、候補はあるが全部非表示の場合とで文言を分ける
    (前者は形が無い、後者は隠されているだけで復旧できる)。
    """
    candidates = _export_candidates(doc)
    if not candidates:
        raise ValueError("nothing to export: no body with a shape")
    bodies = [b for b in candidates if b.visible]
    if not bodies:
        raise ValueError(
            "nothing to export: all candidate bodies are hidden: "
            "見えないものは書き出せない。set_visible(visible=true) で戻してから呼ぶこと"
        )
    return bodies


def _shape_to_export(doc: Document):
    bodies = _bodies_to_export(doc)
    shapes = [b.shape for b in bodies]
    if len(shapes) == 1:
        return shapes[0]
    return kernel.make_compound(shapes)
