"""操作ログ(実装計画書 §3.6、§6予防1・2)。

M0の契約:
- 操作ログが唯一の正であり、文書の形状はここから導出される(§3.1)。
- Operation は provenance フィールドを持つ(値はNoneでよい。スロットの存在自体が契約。§6予防2)。
- 保存形式はJSON。SQLiteは使わない(理由: gitで差分が読める / AIが全履歴を読める / 1モデル=1ファイル)。
- append() はログへの記録のみを行う(副作用の実行はしない)。副作用(ボディの追加など)は
  呼び出し側(例: io_step.import_step)が document を直接操作したうえで append() を呼ぶ。
- undo()/redo()/replay() は verb ごとに document への反映方法を知っている必要がある。
  M0で実在する verb は "import_step" のみで、その args は
  {"path": str, "body_id": BodyId} を持つ(io_step.import_step が積む)。
  undo は document.remove_body(args["body_id"])、redo/replay は kernel.read_step(args["path"])
  で読み直した形状から args["body_id"] をそのまま使って document.add_body() する
  (**io_step.import_step の公開APIを内部から呼び直すと二重に append され破綻するので呼ばない**)。
  次タスクの実装がこのマッピングを持つこと(詳細は tests/README.md の契約表)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .document import Document


@dataclass
class Operation:
    """1件の操作記録。provenance は day1 で用意する空スロット(§6予防2)。"""

    seq: int
    verb: str
    args: dict[str, Any] = field(default_factory=dict)
    selection: list[Any] = field(default_factory=list)
    provenance: Optional[Any] = None


class OpLog:
    """操作ログ。append/undo/redo/to_json/from_json/replay を持つ。

    契約(次タスクが実装する振る舞い):
        append(verb, args, selection=None) -> Operation   seqは1から単調増加
        undo(document) -> Operation | None                取り消した操作(無ければNone)
        redo(document) -> Operation | None                やり直した操作(無ければNone)
        to_json() -> str                                  有効なエントリ全部をJSON化
        from_json(data) -> OpLog                           (classmethod)往復でエントリが完全一致
        replay(document) -> None                           先頭から実行し直し文書を再構築する
    """

    def __init__(self) -> None:
        self._entries: list[Operation] = []
        self._cursor = 0  # 有効なエントリ数。0 <= cursor <= len(_entries)。undoで減りredoで増える

    @property
    def entries(self) -> list[Operation]:
        """現在有効な(undoされていない)エントリの一覧。"""
        return list(self._entries[: self._cursor])

    def append(self, verb: str, args: dict, selection: Optional[list] = None) -> Operation:
        """新しい操作を積む。redo可能だった末尾があれば新規操作で破棄される。"""
        raise NotImplementedError

    def undo(self, document: "Document") -> Optional[Operation]:
        """直前の有効な操作を取り消し、documentへ反映する。何も無ければNone。"""
        raise NotImplementedError

    def redo(self, document: "Document") -> Optional[Operation]:
        """取り消した操作をやり直し、documentへ反映する。何も無ければNone。"""
        raise NotImplementedError

    def to_json(self) -> str:
        """現在有効なエントリ全部をJSON文字列へシリアライズする。"""
        raise NotImplementedError

    @classmethod
    def from_json(cls, data: str) -> "OpLog":
        """to_json()の出力からOpLogを復元する。往復でエントリが完全一致すること。"""
        raise NotImplementedError

    def replay(self, document: "Document") -> None:
        """ログを先頭から実行し直し、documentの状態(bodies等)を再構築する。"""
        raise NotImplementedError
