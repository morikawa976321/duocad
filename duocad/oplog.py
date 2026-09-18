"""操作ログ。

- **操作ログが唯一の正**であり、文書の形状はここから導出される。
- `append()` は記録のみを行う。副作用(ボディの追加など)は呼び出し側が先に済ませる。
- **verb名の if 連鎖をこのモジュールに書かない**。ここに置くのはレジストリ
  (verb名 -> (do, undo))だけで、登録は**verbを定義した側**が `register_verb()` で行う。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .document import Document


@dataclass
class Operation:
    """1件の操作記録(`provenance` は当面 None でよい空スロット)。

    **実行主体(人かAIか)は持たない。** UI が「自分がやった操作」に印を付けられるのは、同じ接続の
    応答待ちで `client_op` を突き合わせているからであって、ログを読んで分かることではない
    (層をまたぐ契約 —— ログに主体を足す設計へ動かすなら `net/ws.py` の配信側も一緒に変わる)。
    """

    seq: int
    verb: str
    args: dict[str, Any] = field(default_factory=dict)
    provenance: Optional[Any] = None


#: verbの実体。(document, operation) を受け取り、**ログには一切触らない**
#: (触ると redo/replay で二重記録になる)。
VerbFn = Callable[["Document", Operation], None]


@dataclass(frozen=True)
class VerbHandler:

    do: VerbFn
    undo: VerbFn


_VERB_REGISTRY: Dict[str, VerbHandler] = {}


def register_verb(verb: str, do: VerbFn, undo: VerbFn) -> None:
    """verb名に (do, undo) を登録する。verbを定義した側から呼ぶ。

    既に別の実体が登録されていれば ValueError(黙って上書きしない)。同じ実体の再登録は許容する。
    """
    handler = VerbHandler(do=do, undo=undo)
    existing = _VERB_REGISTRY.get(verb)
    if existing is not None and existing != handler:
        raise ValueError(f"verb {verb!r} is already registered with a different handler")
    _VERB_REGISTRY[verb] = handler


def verb_handler(verb: str) -> VerbHandler:
    """登録済みの (do, undo) を引く。未登録なら KeyError。"""
    try:
        return _VERB_REGISTRY[verb]
    except KeyError:
        raise KeyError(
            f"verb {verb!r} has no registered handler "
            f"(registered: {sorted(_VERB_REGISTRY)}). "
            "verbを定義したモジュールで register_verb() を呼ぶこと。"
        ) from None


def registered_verbs() -> List[str]:
    return sorted(_VERB_REGISTRY)


class OpLog:
    """操作ログ。append/undo/redo/to_json/from_json/replay を持つ(seqは1から単調増加)。

    保存形式はJSON(SQLiteは使わない —— gitで差分が読める・AIが全履歴を読める・
    1モデル=1ファイルでコピーできる、が理由)。
    """

    #: to_json が出す入れ物のバージョン。**2 = 読み込んだSTEPの本文がログに入っている**。
    #: 版1(外部STEPのパスだけを持つ)で保存されたファイルは実在しないので、移行は実装しない。
    FORMAT_VERSION = 2

    def __init__(self) -> None:
        self._entries: list[Operation] = []
        self._cursor = 0  # 有効なエントリ数。0 <= cursor <= len(_entries)

    @property
    def entries(self) -> list[Operation]:
        return list(self._entries[: self._cursor])

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor < len(self._entries)

    @property
    def redo_count(self) -> int:
        """あと何回やり直せるか(操作履歴パネルが**位置**を出すために要る)。"""
        return len(self._entries) - self._cursor

    def append(self, verb: str, args: dict) -> Operation:
        """新しい操作を積む。redo可能だった末尾があれば新規操作で破棄される。"""
        del self._entries[self._cursor :]
        operation = Operation(
            seq=len(self._entries) + 1,
            verb=verb,
            args=dict(args or {}),
        )
        self._entries.append(operation)
        self._cursor = len(self._entries)
        return operation

    def undo(self, document: "Document") -> Optional[Operation]:
        """直前の有効な操作を取り消し、documentへ反映する。何も無ければNone。

        **undoハンドラの中でログを再生する実装(`parameters.rebuild` など)は範囲を
        `seq < operation.seq` に限ること** —— ここはハンドラを呼んでからカーソルを戻すため、
        全entriesを再生すると取り消す対象の操作まで再適用されてしまう。
        """
        if self._cursor == 0:
            return None
        operation = self._entries[self._cursor - 1]
        # 先に反映してからカーソルを動かす。反映が例外なら履歴は動かない(黙って壊さない)。
        verb_handler(operation.verb).undo(document, operation)
        self._cursor -= 1
        return operation

    def redo(self, document: "Document") -> Optional[Operation]:
        """取り消した操作をやり直し、documentへ反映する。何も無ければNone。

        既存のエントリを有効化し直すだけで、**新しい操作を積み直さない**
        (積み直すと同じ操作が二重に記録され、保存・再生が破綻する)。
        """
        if self._cursor >= len(self._entries):
            return None
        operation = self._entries[self._cursor]
        verb_handler(operation.verb).do(document, operation)
        self._cursor += 1
        return operation

    # ------------------------------------------------------------------ 投機的実行の巻き戻し
    # `duocad.speculative` だけが使う非公開の口。`_entries` / `_cursor` の不変条件
    # (「cursorは常に 0..len(entries)」「undo済みの末尾は次のappendで捨てる」)を
    # 知らない場所を増やさないために、外から直に触らせずここに置く。
    def _snapshot(self) -> tuple:
        return (list(self._entries), self._cursor)

    def _restore(self, snapshot: tuple) -> None:
        """`_snapshot()` で控えた状態へ戻す。**リストは中身を入れ替える**。"""
        entries, cursor = snapshot
        self._entries[:] = entries
        self._cursor = cursor

    def to_json(self) -> str:
        """現在有効なエントリ全部をJSON文字列へシリアライズする。

        **前提**: `args` / `provenance` はJSON化できる値であること。
        `kernel.normalize_provenance()` の戻り(TopoDS_Shape を含むメモリ上の形)をそのまま
        入れるとここで TypeError になるので、永続化用の射影を挟むこと。
        黙って落とすと由来が静かに失われるため、変換の失敗は例外のまま上げる。
        """
        payload = {
            "version": self.FORMAT_VERSION,
            "operations": [asdict(operation) for operation in self.entries],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: str) -> "OpLog":
        """to_json()の出力からOpLogを復元する。往復でエントリが完全一致すること。

        **版の検査が先** — 現在の `FORMAT_VERSION` と違えば、操作を1件も読まずに ValueError。
        **未知のキーは無視する**(過去の `Operation` が持っていた余分なキーがあっても壊れない)。
        """
        payload = json.loads(data)
        raw_operations = cls._extract_operations(payload)

        log = cls()
        for item in raw_operations:
            if not isinstance(item, dict):
                raise ValueError(f"operation entry must be a JSON object, got {type(item).__name__}")
            log._entries.append(
                Operation(
                    seq=item["seq"],
                    verb=item["verb"],
                    args=dict(item.get("args") or {}),
                    provenance=item.get("provenance"),
                )
            )
        log._cursor = len(log._entries)
        return log

    @classmethod
    def _extract_operations(cls, payload: Any) -> list:
        """to_json の入れ物から操作の配列を取り出す(素のリスト形式も受け付ける)。"""
        if isinstance(payload, list):
            # 版の無い素のリスト(テスト・手書き)。版の判定はできないので通す。
            return payload
        if isinstance(payload, dict):
            cls._check_version(payload)
            operations = payload.get("operations")
            if isinstance(operations, list):
                return operations
            raise ValueError("JSON object must contain an 'operations' array")
        raise ValueError(f"unsupported oplog JSON payload: {type(payload).__name__}")

    @classmethod
    def _check_version(cls, payload: dict) -> None:
        """入れ物の `version` を検査する。今の版でなければ ValueError(黙って読まない)。

        `version` が無い入れ物は「版を書かない手書き」として通す(テストが
        `{"operations": [...]}` だけを渡す経路がある)。**版を書いたのに違う値**だけを失敗にする。
        """
        if "version" not in payload:
            return
        version = payload["version"]
        if version == cls.FORMAT_VERSION:
            return
        if version == 1:
            raise ValueError(
                "この保存ファイルは旧形式(version 1)で、読み込んだSTEPの形が入っていない"
                "(外部ファイルへのパスだけを持つ形式)。現在の形式は "
                f"version {cls.FORMAT_VERSION} で、変換の手立ては用意していない —— "
                "元のSTEPを読み込み直して保存し直すこと。"
            )
        raise ValueError(
            f"未知の保存形式 version {version!r}(このduocadが読めるのは "
            f"version {cls.FORMAT_VERSION})"
        )

    def replay(self, document: "Document") -> None:
        """ログを先頭から実行し直し、documentの状態(bodies等)を再構築する。

        **自分のエントリは書き換えない**(再生のたびに履歴が増えないこと)。再生したログを
        その文書のログにするかは呼び出し側の責務 —— 保存ファイルを開く経路は
        `doc = Document(); doc.log = OpLog.from_json(data); doc.log.replay(doc)` の形になる。
        """
        for operation in self.entries:  # entriesはコピーなので、再生中の追記事故が起きない
            verb_handler(operation.verb).do(document, operation)
