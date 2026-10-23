"""操作ログ。

- **操作ログが唯一の正**であり、文書の形状はここから導出される。
- `append()` は記録のみを行う。副作用(ボディの追加など)は呼び出し側が先に済ませる。
- **verb名の if 連鎖をこのモジュールに書かない**。ここに置くのはレジストリ
  (verb名 -> (do, undo))だけで、登録は**verbを定義した側**が `register_verb()` で行う。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .document import Document


@dataclass
class Operation:
    """1件の操作記録(`provenance` は当面 None でよい空スロット)。

    **実行主体(人かAIか)は持たない。** UI が「自分がやった操作」に印を付けられるのは同じ接続の
    応答待ちで `client_op` を突き合わせているから(ログに主体を足すなら `net/ws.py` の配信側も変わる)。
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

    保存形式はJSON(gitで差分が読める・AIが全履歴を読める・1モデル=1ファイルでコピーできる)。
    """

    #: to_json が出す入れ物のバージョン。**2 = 読み込んだSTEPの本文がログに入っている**、**3 = 円を直径で持つ**。
    #: 版2は読むときに今の版へ書き換える(`oplog_upgrade`)。版1(外部STEPのパスだけを持つ)で保存されたファイルは
    #: 実在しないので、移行は実装しない。
    FORMAT_VERSION = 3

    def __init__(self) -> None:
        self._entries: list[Operation] = []
        self._cursor = 0  # 有効なエントリ数。0 <= cursor <= len(_entries)
        #: 自動で振った名前の通し番号の、種類(`naming.BODY` 等)ごとの最大値。一度振った番号は
        #: 再利用しないので undo でも戻さず、保存にも載せる(載せないと開き直した文書が同じ番号をもう一度振る)。
        self._numbering: Dict[str, int] = {}
        #: 大きな操作の JSON(`_operation_json`)。id(操作) -> (操作, args, provenance, 文字列, ファイルに書くバイト列)。
        self._serialized: Dict[int, tuple] = {}

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
        """あと何回やり直せるか(操作履歴パネルが位置を出す)。"""
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
        `seq < operation.seq` に限ること**(ハンドラを呼んでからカーソルを戻すため、全entriesを
        再生すると取り消す対象まで再適用される)。
        """
        if self._cursor == 0:
            return None
        operation = self._entries[self._cursor - 1]
        # 反映が例外なら履歴は動かない。
        verb_handler(operation.verb).undo(document, operation)
        self._cursor -= 1
        return operation

    def redo(self, document: "Document") -> Optional[Operation]:
        """取り消した操作をやり直し、documentへ反映する。何も無ければNone。

        既存のエントリを有効化し直すだけで、新しい操作を積み直さない(二重に記録される)。
        """
        if self._cursor >= len(self._entries):
            return None
        operation = self._entries[self._cursor]
        verb_handler(operation.verb).do(document, operation)
        self._cursor += 1
        return operation

    # ------------------------------------------------------------------ 名前の通し番号
    def allocate_number(self, kind: str) -> int:
        """`kind` の次の番号を振る(振った時点で使用済み)。"""
        number = self._numbering.get(kind, 0) + 1
        self._numbering[kind] = number
        return number

    def note_number(self, kind: str, number: int) -> None:
        """ログに記録済みの番号を使用済みにする(redo / 再生の側。振り直さない)。"""
        self._numbering[kind] = max(self._numbering.get(kind, 0), int(number))

    # ------------------------------------------------------------------ 投機的実行の巻き戻し
    # `duocad.speculative` だけが使う非公開の口(`_entries` / `_cursor` の不変条件を外に漏らさない)。
    def _snapshot(self) -> tuple:
        # 通し番号も戻す(プレビューが振った番号が残ると確定したボディの番号が1つ飛ぶ)。
        return (list(self._entries), self._cursor, dict(self._numbering))

    def _restore(self, snapshot: tuple) -> None:
        """`_snapshot()` で控えた状態へ戻す。**リストは中身を入れ替える**。"""
        entries, cursor, numbering = snapshot
        self._entries[:] = entries
        self._cursor = cursor
        self._numbering = dict(numbering)

    def to_json(self, created: Optional[float] = None) -> str:
        """現在有効なエントリ全部をJSON文字列へシリアライズする。

        **前提**: `args` / `provenance` はJSON化できる値であること。
        `kernel.normalize_provenance()` の戻り(TopoDS_Shape を含むメモリ上の形)をそのまま
        入れるとここで TypeError になるので、永続化用の射影を挟むこと。
        黙って落とすと由来が静かに失われるため、変換の失敗は例外のまま上げる。

        `created` は文書のファイルを最初に書いた時刻(UNIX秒。`Session` が持つ)。`operations` の外に置き、
        None なら載せない。ファイルシステムの作成時刻は保存のたびに上書きされ代わりにならない
        (実測は docs/UI.md のファイル一覧の項)。版は上げていない(`from_payload` は未知のキーを読まない)。
        """
        # 出力は `json.dumps({..., "operations": [asdict(op), ...]}, ensure_ascii=False, indent=2)` とバイト単位で
        # 同じ(`tests/test_oplog.py`)。操作を1件ずつ文字列にして、入れ物の2段の字下げ(4桁)で繋ぐ。
        head, operations = self._json_parts(created)
        body = "[\n    " + ",\n    ".join(text for text, _ in operations) + "\n  ]" if operations else "[]"
        return head[:-2] + ',\n  "operations": ' + body + "\n}"

    def to_file_chunks(self, created: Optional[float] = None) -> List[bytes]:
        """保存するバイト列の断片(繋がずにそのまま順に書く。自動保存の口 `Session._write_document_file`)。

        文字列で組むと、日本語を含む小さな操作と ASCII だけの巨大な操作(STEP 本文・三角形)を繋いだ瞬間に全体が1文字2〜4バイトへ
        広がって写し直され、さらに書き込みで改行を変換する(14.9 MB の文書で連結 16 ms・書き込み 26 ms)。バイト列で繋いでも
        14 MB の確保と写しが連結1回ごとに 5 ms かかる(実測)。操作ごとに変換済みのバイト列を覚え、繋がずに渡す。
        """
        head, operations = self._json_parts(created)

        def encoded(text: str) -> bytes:
            return text.replace("\n", os.linesep).encode("utf-8")

        if not operations:
            return [encoded(head[:-2] + ',\n  "operations": []\n}')]
        chunks = [encoded(head[:-2] + ',\n  "operations": [\n    ')]
        separator = encoded(",\n    ")
        for number, (_, data) in enumerate(operations):
            if number:
                chunks.append(separator)
            chunks.append(data)
        chunks.append(encoded("\n  ]\n}"))
        return chunks

    def _json_parts(self, created: Optional[float]) -> tuple:
        """入れ物の頭(`operations` 抜きの JSON)と、操作ごとの (文字列, ファイルに書くバイト列)。"""
        payload: dict = {"version": self.FORMAT_VERSION}
        if created is not None:
            payload["created"] = created
        if self._numbering:
            payload["numbering"] = dict(self._numbering)
        head = json.dumps(payload, ensure_ascii=False, indent=2)
        if self._serialized:
            # 新しい操作で捨てられた redo の山の分を手放す(STEP 本文のような大きな文字列を持ち続けない)。
            alive = {id(entry) for entry in self._entries}
            for key in [key for key in self._serialized if key not in alive]:
                del self._serialized[key]
        return head, [self._operation_json(operation) for operation in self.entries]

    #: これより長い操作の JSON は覚えておく。自動保存は操作のたびに全ログを書くので、STEP 本文(4.6 MB)・
    #: メッシュボディの三角形(6.9 MB)のような大きな操作を毎回 `asdict` + `json.dumps` し直すと、14.9 MB の文書で
    #: 1回 117 ms かかる(実測)。小さな操作は作り直しても安いので覚えない。
    SERIALIZED_CACHE_MIN = 32 * 1024

    def _operation_json(self, operation: Operation) -> tuple:
        """1件の操作の (JSON 文字列(入れ物の中の字下げ済み), ファイルに書くバイト列)。

        大きなものは、操作・`args`・`provenance` が同じ物である限り使い回す。操作の `args` はその場で書き換えられない
        (`append` が写しを持ち、verb は読むだけ)。`provenance` は再生のたびに新しい物へ差し替わる
        (`operation.provenance = ...`)ので、同一性で見れば足りる。
        """
        cached = self._serialized.get(id(operation))
        if cached is not None and cached[0] is operation and cached[1] is operation.args and cached[2] is operation.provenance:
            return cached[3], cached[4]
        text = json.dumps(asdict(operation), ensure_ascii=False, indent=2).replace("\n", "\n    ")
        data = text.replace("\n", os.linesep).encode("utf-8")
        if len(text) >= self.SERIALIZED_CACHE_MIN:
            self._serialized[id(operation)] = (operation, operation.args, operation.provenance, text, data)
        return text, data

    @classmethod
    def from_json(cls, data: str) -> "OpLog":
        """to_json()の出力からOpLogを復元する。往復でエントリが完全一致すること。"""
        return cls.from_payload(json.loads(data))

    @staticmethod
    def created_of(payload: Any) -> Optional[float]:
        """入れ物の `created`(`to_json` 参照)。無い・数でない・素のリスト形式なら None。"""
        if not isinstance(payload, dict):
            return None
        created = payload.get("created")
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            return None
        return float(created)

    @classmethod
    def from_payload(cls, payload: Any) -> "OpLog":
        """`json.loads` 済みの入れ物から復元する(`from_json` の本体)。

        版の検査が先: 読めない版なら操作を読まずに ValueError。古い版は `oplog_upgrade` で今の版へ写してから読む。
        未知のキーは無視する。
        """
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
        log._numbering = cls._numbering_of(payload)
        return log

    @staticmethod
    def _numbering_of(payload: Any) -> Dict[str, int]:
        """入れ物の `numbering`。無ければ空(番号は再生した操作の側からも拾われる)。"""
        raw = payload.get("numbering") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {
            str(kind): int(number)
            for kind, number in raw.items()
            if isinstance(number, int) and not isinstance(number, bool)
        }

    @classmethod
    def _extract_operations(cls, payload: Any) -> list:
        """to_json の入れ物から操作の配列を取り出す(素のリスト形式も受け付ける)。"""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            cls._check_version(payload)
            operations = payload.get("operations")
            if isinstance(operations, list):
                version = payload.get("version", cls.FORMAT_VERSION)
                if version != cls.FORMAT_VERSION:
                    from .oplog_upgrade import upgrade_operations

                    return upgrade_operations(version, operations)
                return operations
            raise ValueError("JSON object must contain an 'operations' array")
        raise ValueError(f"unsupported oplog JSON payload: {type(payload).__name__}")

    @classmethod
    def _check_version(cls, payload: dict) -> None:
        """入れ物の `version` を検査する。今の版でなければ ValueError(黙って読まない)。

        `version` が無い入れ物は手書きとして通し(テストが `{"operations": [...]}` を渡す)、版を書いて違う値だけを失敗にする。
        """
        if "version" not in payload:
            return
        version = payload["version"]
        if version in (2, cls.FORMAT_VERSION):
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
            f"version 2〜{cls.FORMAT_VERSION})"
        )

    def replay(self, document: "Document") -> None:
        """ログを先頭から実行し直し、documentの状態(bodies等)を再構築する。

        自分のエントリは書き換えない。再生したログをその文書のログにするかは呼び出し側の責務
        (`doc = Document(); doc.log = OpLog.from_json(data); doc.log.replay(doc)`)。
        """
        for operation in self.entries:  # entriesはコピー(再生中の追記事故を避ける)
            verb_handler(operation.verb).do(document, operation)
