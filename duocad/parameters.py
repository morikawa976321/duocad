"""名前つきの寸法値(実装計画書 §3.1 文書 / §3.4 パラメトリック・プリミティブ / §6予防5)。M3で追加。

**day1の予防5そのもの**: 打った数値を図形の座標に**焼き込まず**、名前つきの値として別に持つ。
これが無いと「あとから値を編集して形が追随する」が成立せず、後から入れるには全操作の
書き直しになる。**拘束ソルバは持たない**(§3.4) — 図形どうしの関係は解かず、各図形が
自分の名前つき数値を持つだけで足りる、というのがv1の判断である。

**M3のスタブ段階(2026-08-11)**: `Document.parameters` のデータモデルは実装済み、
振る舞いは全部 `NotImplementedError`。

契約:
    register_value(doc, value) -> str
        数値を新しい名前つきの値として登録し、その名前を返す。名前は `d1` `d2` … で、
        **その時点で使われていない最小の正整数**を使う(undoで消えた名前は再利用される)。
        この決定性が無いと、保存したログを再生したときに `set_parameter` が記録した名前が
        別の値を指す。
    resolve(doc, ref) -> float
        パラメータ名(str)なら現在の値、素の数値ならそのまま返す。
    parameter_of(doc, sketch_id, owner_id, key) -> str
        図形または反復の `key`(例 "radius" / "dx")に結びついた**パラメータ名**を返す。
        `owner_id` は EntityId でも PatternId でもよい。存在しなければ KeyError。
    set_parameter(doc, name, value) -> Operation
        名前つきの値を書き換え、**形状を追随させる**。

`set_parameter` の契約(ここがM3の本体):

- 存在しない名前は `KeyError`。文書にもログにも変更を残さない。
- 成功したら `verb="set_parameter"`, `args={"name":.., "value":.., "previous":..}` を積む。
  `previous` を持つのは undo が値を戻せるようにするため(スナップショットと同じ役割)。
- **形状は導出物**(§3.1「操作ログが唯一の正であり、形状はそこから導出される」)。値を変えたら
  **ログを再生して bodies を作り直す**こと。作り直さないと「形が追随する」が嘘になる。
- **再生で領域が分裂・合体・消失して押し出しの対象が失われたら、黙って通さない**(§3.3の注意点・
  §7「領域はパラメータ編集で変わる」)。その場合は例外を送出し、**文書を元の状態に戻す**
  (値もログも形状も変更前のまま)。壊れたモデルを静かに残さない。
- undo は `previous` の値に戻し、同じく形状を作り直す。
  **落とし穴**: `OpLog.undo` はハンドラを呼んでからカーソルを戻すので、undoの中で
  ログ全体を再生すると**取り消すはずの操作まで再適用される**。再生する範囲は
  `seq < operation.seq` に限ること。
"""
from __future__ import annotations

from typing import Any, Dict

from .document import Document
from .oplog import Operation, register_verb

#: パラメータ名の接頭辞(Fusion360の d1/d2 と同じ流儀。将来の改名機能はv1の範囲外)
PARAMETER_PREFIX = "d"

VERB_SET_PARAMETER = "set_parameter"


def register_value(doc: Document, value: float) -> str:
    """数値を名前つきの値として登録し、その名前(`d1` `d2` …)を返す。"""
    raise NotImplementedError("M3で実装する: register_value")


def resolve(doc: Document, ref: Any) -> float:
    """パラメータ名なら現在の値を、素の数値ならそのまま返す。"""
    raise NotImplementedError("M3で実装する: resolve")


def parameter_of(doc: Document, sketch_id: str, owner_id: str, key: str) -> str:
    """図形/反復の `key` に結びついたパラメータ名。無ければ KeyError。"""
    raise NotImplementedError("M3で実装する: parameter_of")


def set_parameter(doc: Document, name: str, value: float) -> Operation:
    """名前つきの値を書き換え、ログを再生して形状を追随させる。"""
    raise NotImplementedError("M3で実装する: set_parameter")


def rebuild(doc: Document, upto_seq: int) -> None:
    """`seq <= upto_seq` のログから文書(bodies / sketches / parameters)を作り直す。

    `set_parameter` と そのundoが共有する再生の実体。**ログには触らない**。
    """
    raise NotImplementedError("M3で実装する: rebuild")


# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない)
# --------------------------------------------------------------------------------------
def _do_set_parameter(document: Document, operation: Operation) -> None:
    raise NotImplementedError("M3で実装する: set_parameter の redo/replay")


def _undo_set_parameter(document: Document, operation: Operation) -> None:
    raise NotImplementedError("M3で実装する: set_parameter の undo")


register_verb(VERB_SET_PARAMETER, do=_do_set_parameter, undo=_undo_set_parameter)
