"""作業空間 —— 起動中のコマンドとモード(issue #153)。

選択・可視性と同じ「操作ログに載らず、undo でも戻らず、保存もされない共有の短命状態」。`Session` が1つ持ち、
人(WebSocket)も AI(MCP・アプリ内AI)も同じ操作で触る(`Session.begin_command` / `set_command_args` /
`end_command` / `apply_command` / `enter_sketch` / `leave_sketch` …)。`Document` には置かない —— verb はモードを
知らず、ツールの顔ぶれもモードで変わらない(docs/DESIGN_DECISIONS.md「層の分け方」)。

- `command`  … 起動中の verb と how の途中入力。`args` は verb のサーバ側引数名そのままで、**入っているキーだけ**持つ
  (必須の検査は確定のときだけ)。出所(誰が起動したか)は持たない —— 選択と同じで、区別は変更イベントの `client_op`。
  「確定したか」も持たない: 確定の成功で `command` が無くなり操作ログが1行増える、がその唯一の表現。
- `mode`     … `{"kind": "idle"}` / `{"kind": "sketch", "sketch_id": ...}` / `{"kind": "optimize"}`。画面のモードはこれに従う。
- `revision` … 変更ごとに進む。アプリ内AIの楽観ロック(`DocumentTools.current_seq`)が読み、`command` にも載せる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import catalog

#: 起動して引数を集められる verb の条件。スケッチとフィーチャの verb(画面のコマンド行を通るもの)だけ。
#: 測定(`query`)は起動時に揃っていれば即走り、ファイル操作はパスのダイアログなので、構える対象ではない。
STAGEABLE_CATEGORIES = ("sketch", "feature")

IDLE: dict = {"kind": "idle"}


class NoCommand(ValueError):
    """起動中のコマンドが無いのに、その引数や確定を求めた。MCP では `no_command` の封筒になる。"""


def stageable(command: catalog.Command) -> bool:
    return command.category in STAGEABLE_CATEGORIES and command.ws == "table"


def stageable_verb_names() -> tuple[str, ...]:
    return tuple(command.name for command in catalog.COMMANDS if stageable(command))


#: ボディの形の添字(面・辺・頂点の番号)で指す参照の種別。ボディの形が変わると添字が別の部分を指す。
SHAPE_REF_KINDS = ("face", "edge", "vertex")


def shape_ref_bodies(args: dict) -> dict[str, list[str]]:
    """引数名 -> その値が形の添字で指しているボディの id。参照1つ(`{"body", "kind", "index"}`)かその配列を見る。
    `kind` が無いか `None` の参照(`plane_face` は `{"body", "index"}` でも渡せる)も数える。原点パーツ(`{"datum"}`)・ボディそのもの
    (`kind: "body"`)・スケッチの参照は形の添字を持たないので入らない。
    `index` は型を見ずあれば数える(解決側 `selection._as_index` が `1.0`・`"1"` も受けるので、`int` に絞ると外れ損なう)。
    """
    found: dict[str, list[str]] = {}
    for name, value in args.items():
        refs = value if isinstance(value, list) else [value]
        bodies = [
            ref["body"]
            for ref in refs
            if isinstance(ref, dict)
            and isinstance(ref.get("body"), str)
            # 解決側 `verbs._common.resolve_shape_ref` は `kind` が無い参照も `None` の参照も受ける。
            and (ref.get("kind") is None or ref["kind"] in SHAPE_REF_KINDS)
            and "index" in ref
        ]
        if bodies:
            found[name] = bodies
    return found


def merge_args(command: catalog.Command, current: dict, update: dict) -> dict:
    """`current` に `update` を併合して検査した新しい辞書を返す。値が `None` のキーは外す(「まだ決めていない」へ戻す)。

    検査は未知のキーと型だけ(`Command.partial_schema`)。必須は見ない —— 途中入力を置く口なので、欠けているのが普通。
    違反は ValueError(引数名を含む)。
    """
    known = {param.name for param in command.params}
    unknown = sorted(name for name in update if name not in known)
    if unknown:
        # 外すだけのキー(`None`)も名前を見る: 誤記(`radios: null`)や添え物(`client_op`)を黙って通すと、消したつもりの値が残る。
        raise ValueError(f"unknown argument(s): {unknown} (expected: {sorted(known)})")
    merged = dict(current)
    for name, value in update.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    problem = catalog.check_arguments(command.partial_schema(), merged)
    if problem is not None:
        raise ValueError(problem)
    return merged


@dataclass
class CommandState:
    verb: str
    args: dict = field(default_factory=dict)


@dataclass
class Workspace:
    command: Optional[CommandState] = None
    mode: dict = field(default_factory=lambda: dict(IDLE))
    revision: int = 0

    def bump(self) -> None:
        self.revision += 1

    def reset(self) -> None:
        """文書を差し替えたとき(開く・新規・閉じる)。構えていたコマンドもモードも前の文書のもの。"""
        self.command = None
        self.mode = dict(IDLE)
        self.bump()

    def command_state(self) -> Optional[dict]:
        """配信・イベント・MCP が返す `command` の形。`None` は無起動。"""
        if self.command is None:
            return None
        return {"verb": self.command.verb, "args": dict(self.command.args), "revision": self.revision}

    def to_state(self) -> dict:
        """状態配信(`STATE_KEYS` の `command` / `mode`)に載せる分。"""
        return {"command": self.command_state(), "mode": dict(self.mode)}

    def editing_sketch(self) -> Optional[str]:
        return self.mode.get("sketch_id") if self.mode.get("kind") == "sketch" else None
