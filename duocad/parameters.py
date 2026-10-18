"""名前つきの寸法値。打った数値を図形の座標に**焼き込まず**、名前つきの値として別に持つ。
**拘束ソルバは持たない** —— 図形どうしの関係は解かず、各図形が自分の名前つき数値を持つだけ。

`register_value` が振る名前は `d1` `d2` … で、**その時点で使われていない最小の正整数**
(undoで消えた名前は再利用される)。この決定性が無いと、保存したログの再生で
`set_parameter` が記録した名前が別の値を指す。

`set_parameter` の契約(**公開面(カタログ・Session・MCP・WebSocket)からは外してある**。
UIに打ち直し口ができたら人とAIの両方へ同時に出す。ここに残るのは保存済みログの再生用の実装と
`register_verb` の登録):

- 存在しない名前は `KeyError`。文書にもログにも変更を残さない。
- 成功したら `args={"name":.., "value":.., "previous":..}` を積む(`previous` は undo 用)。
- **形状は導出物**。値を変えたら**ログを再生して bodies を作り直す**こと。
- **再生で領域が分裂・合体・消失して押し出しの対象が失われたら、黙って通さない**。その場合は
  例外を送出し、**文書を元の状態に戻す**(値もログも形状も変更前のまま)。
- undo は `previous` の値に戻し、同じく形状を作り直す。**落とし穴**: `OpLog.undo` はハンドラを
  呼んでからカーソルを戻すので、undoの中でログ全体を再生すると**取り消すはずの操作まで
  再適用される**。再生する範囲は `seq < operation.seq` に限ること。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from .document import Document, unknown_id_message
from .oplog import Operation, register_verb, verb_handler

#: パラメータ名の接頭辞(Fusion360の d1/d2 と同じ流儀。改名機能は未実装)
PARAMETER_PREFIX = "d"

VERB_SET_PARAMETER = "set_parameter"

#: `rebuild` 実行中は `_do_set_parameter` が値の代入だけ行い、再帰的に rebuild しない
_REBUILDING = False


def register_value(doc: Document, value: float) -> str:
    """数値を名前つきの値として登録し、その名前(`d1` `d2` …)を返す。"""
    number = 1
    while f"{PARAMETER_PREFIX}{number}" in doc.parameters:
        number += 1
    name = f"{PARAMETER_PREFIX}{number}"
    doc.parameters[name] = float(value)
    return name


def register_all(doc: Document, values: Mapping[str, Any]) -> Dict[str, str]:
    """`values` の数値を**キーの並びどおりに**登録し、キー -> 名前 を返す(並びが `d<n>` の振り順になる)。"""
    return {key: register_value(doc, value) for key, value in values.items()}


def resolve(doc: Document, ref: Any) -> float:
    """パラメータ名なら現在の値を、素の数値ならそのまま返す。"""
    if isinstance(ref, str):
        return float(doc.parameters[ref])
    return float(ref)


def resolve_all(doc: Document, refs: Mapping[str, Any]) -> Dict[str, float]:
    """図形・反復の `params`(名前つきの値への参照を含む)を、解決済みの数値で返す。"""
    return {key: resolve(doc, ref) for key, ref in refs.items()}


def parameter_of(doc: Document, sketch_id: str, owner_id: str, key: str) -> str:
    """図形/反復の `key` に結びついたパラメータ名。無ければ KeyError。"""
    from .sketch import get_sketch

    sketch = get_sketch(doc, sketch_id)
    for entity in sketch.entities:
        if entity.id == owner_id:
            if key not in entity.params:
                raise KeyError(
                    f"entity {owner_id!r} has no parameter {key!r}"
                )
            return entity.params[key]
    for pattern in sketch.patterns:
        if pattern.id == owner_id:
            if key not in pattern.params:
                raise KeyError(
                    f"pattern {owner_id!r} has no parameter {key!r}"
                )
            return pattern.params[key]
    raise KeyError(f"no such owner: {owner_id!r} in sketch {sketch_id!r}")


def is_positive_int(value: Any) -> bool:
    """1以上の整数か。boolは除外。floatでも整数値なら受ける(反復の個数の検査)。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 1
    if isinstance(value, float):
        return value >= 1 and value == int(value)
    return False


def _require_usable_value(doc: Document, name: str, value: float) -> None:
    """`name` を `value` にしてよいかを、**その値を使っている図形/反復の側の規則**で検査する。

    作成時に弾く値(NaN・inf・半径0以下・辺0・長さ0の線分・個数1未満)は後から書き込む経路でも
    弾く。素通りすると、半径0は以後 `regions()` が毎回失敗し、NaNは**同じスケッチの正常な
    領域まで黙って消し**、`to_json()` が裸のNaNを吐いてJSONが壊れる(復旧はundoだけ)。
    """
    from .sketch import require_valid_params

    if not math.isfinite(value):
        raise ValueError(f"parameter {name!r} must be a finite number, got {value!r}")

    for sketch in doc.sketches:
        for entity in sketch.entities:
            if name in entity.params.values():
                candidate = {
                    key: (value if ref == name else resolve(doc, ref))
                    for key, ref in entity.params.items()
                }
                require_valid_params(entity.kind, candidate)
        for pattern in sketch.patterns:
            for key, ref in pattern.params.items():
                if ref == name and key in ("nx", "ny", "count") and not is_positive_int(value):
                    raise ValueError(
                        f"pattern count {key} must be a positive integer, got {value!r}"
                    )
                if ref == name and key == "total_angle" and not 0.0 < value <= 360.0:
                    raise ValueError(
                        f"pattern total_angle must be in (0, 360], got {value!r}"
                    )


def set_parameter(doc: Document, name: str, value: float) -> Operation:
    """名前つきの値を書き換え、ログを再生して形状を追随させる。"""
    if name not in doc.parameters:
        raise KeyError(
            unknown_id_message("parameter", name, list(doc.parameters))
        )
    previous = doc.parameters[name]
    value = float(value)
    _require_usable_value(doc, name, value)

    upto_seq = doc.log.entries[-1].seq if doc.log.entries else 0
    rebuild(doc, upto_seq, overrides={name: value})

    return doc.log.append(
        VERB_SET_PARAMETER,
        {"name": name, "value": value, "previous": previous},
    )


def rebuild(
    doc: Document,
    upto_seq: int,
    overrides: Optional[Dict[str, float]] = None,
) -> None:
    """`seq <= upto_seq` のログから文書(bodies / sketches / parameters)を作り直す。

    `set_parameter` とそのundoが共有する再生の実体。**ログには触らない**。
    `overrides` はログへ積む前の試し再生用(成功したら採用、失敗したら元に戻す)。

    **値を「先に」確定させてから幾何を作り直す**: `set_parameter` はログ上で影響先の図形や
    押し出しより**後ろ**に載るので、順番どおり再生すると古い値の形状のまま値だけ新しくなる。
    そこで範囲内の `set_parameter` を先に走査して最終値の表を作り、各操作の直後に当てる。
    パラメータ名は決定的に振り直されるので、表の名前は再生後の同じスカラーを指す。
    """
    global _REBUILDING
    source = doc.log

    values: Dict[str, float] = {}
    # その名前を最後に書いた `set_parameter` の seq。値の強制はこの seq より前の操作にだけ効かせる。
    forced_before: Dict[str, int] = {}
    for operation in source._entries:
        if operation.seq > upto_seq:
            break
        if operation.verb == VERB_SET_PARAMETER:
            values[operation.args["name"]] = float(operation.args["value"])
            forced_before[operation.args["name"]] = operation.seq
    if overrides:
        # 試し再生の値はログの一番後ろ相当なので全操作へ効かせる。
        values.update({key: float(value) for key, value in overrides.items()})
        for key in overrides:
            forced_before[key] = upto_seq + 1

    # 失敗時に文書を元へ戻すための退避。再生は必ず新しい Body / Sketch を作るので、
    # ここで持つ参照は再生中に書き換わらない。
    saved_bodies = list(doc.bodies)
    saved_sketches = list(doc.sketches)
    saved_parameters = dict(doc.parameters)
    saved_active = doc._active_body_id

    doc.bodies.clear()
    doc._active_body_id = None
    doc.sketches.clear()
    doc.parameters.clear()

    _REBUILDING = True
    try:
        # カーソルではなく seq で切る(undo 中は取り消す操作自身が `.entries` に残っている)。
        for operation in list(source._entries):
            if operation.seq > upto_seq:
                break
            verb_handler(operation.verb).do(doc, operation)
            for key, value in values.items():
                # その `set_parameter` を再生し終えた後は強制しない。強制し続けると、
                # 後ろの `move_sketch_entities` が同じ名前へ書いた座標が毎回上書きされて黙って消える。
                if key in doc.parameters and operation.seq < forced_before.get(key, 0):
                    doc.parameters[key] = value
    except Exception:
        doc.bodies[:] = saved_bodies
        doc.sketches[:] = saved_sketches
        doc.parameters.clear()
        doc.parameters.update(saved_parameters)
        doc._active_body_id = saved_active
        raise
    finally:
        _REBUILDING = False

    # アクティブボディの選択はログに載らないので、ここで戻さないと最後に add_body されたボディへ移る。
    if saved_active is not None and any(body.id == saved_active for body in doc.bodies):
        doc._active_body_id = saved_active


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_set_parameter(document: Document, operation: Operation) -> None:
    name = operation.args["name"]
    value = float(operation.args["value"])
    document.parameters[name] = value
    if _REBUILDING:
        return
    # redo / OpLog.replay: 値を入れたうえで、この操作まで含めて作り直す。
    # `OpLog.replay(document)` は document.log を差し替えないので、外のログだと
    # 中身の違うログを黙って再生してしまう。止める。
    if not any(entry is operation for entry in document.log._entries):
        raise ValueError(
            "set_parameter を含むログを再生するときは、そのログを document.log へ"
            "束ねてから replay すること: doc.log = OpLog.from_json(...); doc.log.replay(doc)"
        )
    rebuild(document, operation.seq)


def _undo_set_parameter(document: Document, operation: Operation) -> None:
    # OpLog.undo はハンドラのあとでカーソルを戻すので、再生は seq < operation.seq
    rebuild(document, operation.seq - 1)


register_verb(VERB_SET_PARAMETER, do=_do_set_parameter, undo=_undo_set_parameter)


def release_param_refs(document: Document, params: dict) -> None:
    """図形/反復が持っていたパラメータ名を文書から消す(undo用。名前つきの値の持ち主はここ)。"""
    for ref in params.values():
        if isinstance(ref, str):
            document.parameters.pop(ref, None)
