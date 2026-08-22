"""操作ログ(実装計画書 §3.6、§6予防1・2)。

契約:
- 操作ログが唯一の正であり、文書の形状はここから導出される(§3.1)。
- Operation は provenance フィールドを持つ(値はNoneでよい。スロットの存在自体が契約。§6予防2)。
- 保存形式はJSON。SQLiteは使わない(理由: gitで差分が読める / AIが全履歴を読める / 1モデル=1ファイル)。
- append() はログへの記録のみを行う(副作用の実行はしない)。副作用(ボディの追加など)は
  呼び出し側(例: io_step.import_step)が document を直接操作したうえで append() を呼ぶ。
- undo()/redo()/replay() は verb ごとに document への反映方法を知っている必要がある。

**verb名の if 連鎖をこのモジュールに書かないこと**(tests/README.md の明示的な契約)。
verbの数が増えるほど「汎用のログ」が全verbの意味を知る層に化けてしまうため、ここに置くのは
**レジストリ(verb名 -> (do, undo))だけ**にし、登録は **verbを定義した側**(io_step.py 等)が
`register_verb()` で行う。

登録の実行タイミング: verbを定義したモジュールが import されたときに登録される。
`duocad/__init__.py` が verbモジュールを import することで、`duocad` を使えば必ず
レジストリが埋まっている状態になる(from_json したログを replay するだけの経路でも
verbが未登録にならないようにするため)。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .document import Document


@dataclass
class Operation:
    """1件の操作記録。provenance は day1 で用意する空スロット(§6予防2)。"""

    seq: int
    verb: str
    args: dict[str, Any] = field(default_factory=dict)
    provenance: Optional[Any] = None


#: verbの実体。do は「操作をdocumentへ適用する」、undo は「その適用を取り消す」。
#: どちらも (document, operation) を受け取り、**ログには一切触らない**
#: (触ると redo/replay で二重記録になる)。
VerbFn = Callable[["Document", Operation], None]


@dataclass(frozen=True)
class VerbHandler:
    """1つのverbに対する (do, undo) の組。"""

    do: VerbFn
    undo: VerbFn


_VERB_REGISTRY: Dict[str, VerbHandler] = {}


def register_verb(verb: str, do: VerbFn, undo: VerbFn) -> None:
    """verb名に (do, undo) を登録する。verbを定義した側から呼ぶ。

    既に別の実体が登録されている場合は ValueError(黙って上書きしない。§6予防6)。
    同じ実体の再登録(モジュールの二重import等)は無害なので許容する。
    """
    handler = VerbHandler(do=do, undo=undo)
    existing = _VERB_REGISTRY.get(verb)
    if existing is not None and existing != handler:
        raise ValueError(f"verb {verb!r} is already registered with a different handler")
    _VERB_REGISTRY[verb] = handler


def verb_handler(verb: str) -> VerbHandler:
    """登録済みの (do, undo) を引く。未登録なら KeyError(黙って何もしない、を避ける)。"""
    try:
        return _VERB_REGISTRY[verb]
    except KeyError:
        raise KeyError(
            f"verb {verb!r} has no registered handler "
            f"(registered: {sorted(_VERB_REGISTRY)}). "
            "verbを定義したモジュールで register_verb() を呼ぶこと。"
        ) from None


def registered_verbs() -> List[str]:
    """登録済みverb名の一覧(デバッグ・自己点検用)。"""
    return sorted(_VERB_REGISTRY)


class OpLog:
    """操作ログ。append/undo/redo/to_json/from_json/replay を持つ。

    契約:
        append(verb, args) -> Operation                    seqは1から単調増加
        undo(document) -> Operation | None                取り消した操作(無ければNone)
        redo(document) -> Operation | None                やり直した操作(無ければNone)
        to_json() -> str                                  有効なエントリ全部をJSON化
        from_json(data) -> OpLog                           (classmethod)往復でエントリが完全一致
        replay(document) -> None                           先頭から実行し直し文書を再構築する
    """

    #: to_json が出す入れ物のバージョン(将来フォーマットを変えたときの識別子)。
    #:
    #: **2 = 読み込んだSTEPの本文がログに入っている**(2026-08-23)。1は
    #: `import_step` / `import_step_editable` が `args={"path": ...}` だけを持ち、
    #: 再生が外部ファイルを読み直す形だった —— 形の出どころが違うので、
    #: **1は読まずに明示的なエラーにする**(`from_json`)。黙って再生すると
    #: `args["step"]` が無いところで KeyError になり、原因が版の違いだと分からない。
    #: 版1で保存されたファイルは実在しない(この形式で保存を始める前の版)ため、
    #: 移行の実装は置かない。
    FORMAT_VERSION = 2

    def __init__(self) -> None:
        self._entries: list[Operation] = []
        self._cursor = 0  # 有効なエントリ数。0 <= cursor <= len(_entries)。undoで減りredoで増える

    @property
    def entries(self) -> list[Operation]:
        """現在有効な(undoされていない)エントリの一覧。"""
        return list(self._entries[: self._cursor])

    @property
    def can_undo(self) -> bool:
        """undoできる操作があるか(M1追加。アプリ層がボタンの有効・無効を出すため)。"""
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        """redoできる操作があるか(M1追加。`_cursor` を外へ見せずに済ませるための読み取り専用)。"""
        return self._cursor < len(self._entries)

    @property
    def redo_count(self) -> int:
        """あと何回やり直せるか(操作履歴パネルが**位置**を出すために要る)。

        `can_redo` の真偽だけでは「どこまで戻れるか分かる」を満たせない —— 3回undoした
        画面と1回undoした画面が同じに見える。`entries` が返すのはカーソルまでの写しなので、
        外からは末尾に何本残っているかを知る手段が無い(だから `_cursor` を見せずにここで数える)。
        """
        return len(self._entries) - self._cursor

    def append(self, verb: str, args: dict) -> Operation:
        """新しい操作を積む。redo可能だった末尾があれば新規操作で破棄される。

        **記録のみを行い、副作用は実行しない**(副作用は呼び出し側が先に済ませる)。
        """
        del self._entries[self._cursor :]  # undo済みの「やり直せた末尾」を捨てる
        operation = Operation(
            seq=len(self._entries) + 1,
            verb=verb,
            args=dict(args or {}),
        )
        self._entries.append(operation)
        self._cursor = len(self._entries)
        return operation

    def undo(self, document: "Document") -> Optional[Operation]:
        """直前の有効な操作を取り消し、documentへ反映する。何も無ければNone。"""
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
    #
    # `duocad.speculative` だけが使う。**公開の契約ではない**ので
    # 名前を `_` で始める。それでもここに置くのは、`_entries` / `_cursor` を
    # 別モジュールから直に触らせないため —— 外から触ると、ログの不変条件
    # (「cursorは常に 0..len(entries)」「undo済みの末尾は次のappendで捨てる」)を
    # 知らない場所が増える。
    def _snapshot(self) -> tuple:
        """エントリ列とカーソルを控える(プレビューの巻き戻し用)。"""
        return (list(self._entries), self._cursor)

    def _restore(self, snapshot: tuple) -> None:
        """`_snapshot()` で控えた状態へ戻す。**リストは中身を入れ替える**。"""
        entries, cursor = snapshot
        self._entries[:] = entries
        self._cursor = cursor

    def to_json(self) -> str:
        """現在有効なエントリ全部をJSON文字列へシリアライズする。

        **前提**: `args` / `provenance` はJSON化できる値であること。
        `kernel.normalize_provenance()` の戻り(TopoDS_Shape を含むメモリ上の形)を
        そのまま入れるとここで TypeError になるので、永続化用の射影を挟むこと
        (kernel.normalize_provenance のdocstring参照)。
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

        **版の検査が先**(2026-08-23): 入れ物に `version` があって現在の
        `FORMAT_VERSION` と違えば、操作を1件も読まずに ValueError にする
        (`_check_version`)。

        **未知のキーは無視する**: 過去の `Operation` が持っていた `selection` のような
        余分なキーが入っていても読み込みは壊れない(値は使わない)。
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
        """to_json の入れ物から操作の配列を取り出す(素のリスト形式も受け付ける)。

        **版が今の形式と違えば、読む前に断る**(2026-08-23)。version 1 は
        `import_step` の形がログに入っていない(外部STEPのパスを読み直す)形式で、
        今の再生経路では復元できない —— 黙って進めると `args["step"]` の KeyError に
        なり、「保存形式の版が古い」という本当の理由が見えなくなる。
        """
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

        `version` が無い入れ物は版1相当ではなく「版を書かない手書き」として通す
        (テストが `{"operations": [...]}` だけを渡す経路がある)。**版を書いたのに
        違う値**であることだけを、はっきりした失敗として扱う。
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

        **自分のエントリは書き換えない**(再生のたびに履歴が増えないこと)。
        「開くときはログから再生する」(§3.1)運用では、再生したログがそのまま
        その文書のログになる — `document.log` を差し替えるのは呼び出し側の責務。
        """
        for operation in self.entries:  # entriesはコピーなので、再生中の追記事故が起きない
            verb_handler(operation.verb).do(document, operation)
