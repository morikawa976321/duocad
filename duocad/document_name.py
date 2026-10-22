"""文書のファイル名と書き出し先の名前の、文字と長さの検査。`Session._resolve_document_name` が呼ぶ
(区切り・コロン・予約名・拡張子・`models_dir` 内への包含はそちらが持つ)。画面の入力欄
(`web/src/FilesPanel.svelte` の `nameProblem`)は同じ規則の写しで、食い違えば早く言う側が間違っているだけ(最終判定はここ)。
"""
from __future__ import annotations

import os

#: Windowsのファイル名に使えない文字のうち、区切り(`/` `\\`)とコロンを除いたもの(それらは呼び手が先に断る)。
FORBIDDEN_CHARACTERS = '<>"|?*'

#: ファイル名1つ(構成要素)の上限。NTFSはUTF-16の符号単位255個、Linuxはバイト255個。
_COMPONENT_LIMIT = 255

#: 書き込みが名前の後ろへ足す最長の接尾辞。文書は `<名前>.writing`、サムネイルは `<名前>.png.writing` を一時ファイルにする。
#: 名前そのものが255以内でも、これを足した一時ファイル名が255を超えると保存が OSError で落ち、
#: 以後その文書は自動保存も Ctrl+S もできない(2026-10-18 実測: 249文字の名前の改名は通り、保存だけが落ちる)。
_LONGEST_WRITE_SUFFIX = ".png.writing"
MAX_NAME_UNITS = _COMPONENT_LIMIT - len(_LONGEST_WRITE_SUFFIX)


def name_units(text: str) -> int:
    """`text` がファイル名の上限に対して占める大きさ(Windows は UTF-16 の符号単位、それ以外は UTF-8 のバイト)。"""
    if os.name == "nt":
        return len(text.encode("utf-16-le", "surrogatepass")) // 2
    return len(text.encode("utf-8", "surrogateescape"))


def check_characters(text: str) -> None:
    """ファイル名に使えない文字(制御文字・`<>"|?*`・UTF-8 に符号化できない孤立サロゲート)を含めば ValueError。
    読む側(開く・消す)にも掛ける: これらを含む名前のファイルは Windows には作れず、あれば別のOSで作られた物で一覧にも出ない。
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("ファイル名に使えない文字(UTF-8に符号化できない文字)が入っている") from None
    for char in text:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise ValueError(f"ファイル名に制御文字は使えない: {text!r}")
        if char in FORBIDDEN_CHARACTERS:
            raise ValueError(
                f"ファイル名に {char!r} は使えない(Windowsが使えない文字 {FORBIDDEN_CHARACTERS} は不可): {text!r}"
            )


def check_writable_length(text: str) -> None:
    """書き込む名前(保存・改名・複製先)が、一時ファイルとサムネイルの接尾辞を足しても上限に収まらなければ ValueError。
    読む側には掛けない(今ある長い名前のファイルを開けなくしないため)。
    """
    units = name_units(text)
    if units > MAX_NAME_UNITS:
        shown = text if len(text) <= 40 else text[:40] + "…"
        raise ValueError(
            f"ファイル名が長すぎる({units} > {MAX_NAME_UNITS}。保存の一時ファイル名とサムネイルの分が名前の後ろに足される): {shown!r}"
        )
