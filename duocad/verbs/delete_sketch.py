"""verb `delete_sketch` — **スケッチを入れ物ごと消す**(図形・反復・その名前つきの値も一緒に)。

`delete`(確定選択のボディ/図形)とは**対象の指し方が違う**: 「スケッチという入れ物」は
選択集合の種別に無い(`selection.SELECTABLE_KINDS`)ので、`import_svg` / `pattern_*` と
同じく **`sketch_id` を引数で受ける**(対象を指すIDは target であって how ではない。
README §0 の設計原則2)。

--------------------------------------------------------------------------------------
undo は args だけから作り直す(スナップショット表 `_common.SNAPSHOTS` は使わない)
--------------------------------------------------------------------------------------

- **平面は解決済みの姿勢(`frame`)つきで控え、そのまま戻す。** スケッチの姿勢は作成時点で
  焼き付ける契約なので、undo で `resolve_plane` に解き直させると、貼った面がその後の
  面取り・フィレットで添字を変えていたときに**別の面へ貼り直す/失敗する**。控えた姿勢を
  そのまま戻すのが「作成時点で固定」に忠実である(`sketch.plane_log_args` が `frame` を
  ログから外すのは `sketch_create` の do が解き直すためで、ここの undo には当たらない)。
- **名前つきの値は名前ごと控えて直に戻す**(`refs`: 鍵 -> 名前 or 素の数値、`values`:
  名前 -> 値)。`delete` は「作った順に `register_value` し直すと同じ名前が振り直される」に
  依存しているが、スケッチ丸ごとでは図形と反復の登録順が時間軸で混ざる(図形→反復→図形)
  ので、順序からは再現できない。undo は削除の直後の状態へ戻る(その間の操作は先に undo
  されている)ので、控えた名前は必ず空いている —— 空いていなければ**バグなので例外**にする
  (黙って別名を振ると `set_parameter` の記録した名前が別の値を指す)。

**消す側(do)は `_undo_sketch_create` と同じ**: 図形・反復の名前つきの値を手放してから
入れ物を外す。手放し忘れると `d1 d2 …` の連番が飛ぶ(`delete.py` と同じ理由)。

やらないこと(意図的な線引き):

- **そのスケッチから押し出したボディには触らない**(`delete` の「ボディを消してもスケッチ側は
  何もしない」の鏡。形は導出物でありボディは既に独立した実体である)。
- **確認は挟まない**(1回の undo で全部戻る)。
- **可視性(`Sketch.visible`)は控えない**。セッション状態なので undo で戻ったスケッチは
  可視で始まる(開き直した文書と同じ)。

**OCP / build123d を直接importしない**(幾何はここでは要らない。消すだけ)。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from ..sketch import Entity, Pattern, Sketch, get_sketch

VERB_DELETE_SKETCH = "delete_sketch"


def _snapshot(doc: Document, target: Sketch) -> Dict[str, Any]:
    """消すスケッチを**JSON安全な値だけ**で記録する(undo はこの記録だけから作り直す)。"""
    values: Dict[str, float] = {}

    def refs_of(params: Dict[str, Any]) -> Dict[str, Any]:
        refs: Dict[str, Any] = {}
        for key, ref in params.items():
            if isinstance(ref, str):
                values[ref] = float(doc.parameters[ref])
                refs[key] = ref
            else:
                refs[key] = float(ref)
        return refs

    entities = [
        {"entity_id": entity.id, "kind": entity.kind, "refs": refs_of(entity.params)}
        for entity in target.entities
    ]
    patterns = [
        {
            "pattern_id": pattern.id,
            "kind": pattern.kind,
            "sources": list(pattern.sources),
            "refs": refs_of(pattern.params),
            "options": copy.deepcopy(pattern.options),
        }
        for pattern in target.patterns
    ]
    return {
        "sketch_id": target.id,
        # 並びの位置。undo で**元の位置へ差し戻す**(ブラウザの一覧の順序が変わらないため)。
        "index": doc.sketches.index(target),
        "plane": copy.deepcopy(target.plane),
        "entities": entities,
        "patterns": patterns,
        "values": values,
    }


def _remove(document: Document, sketch_id: str) -> Sketch:
    """実際に消す(**ログには触らない**)。名前つきの値を手放してから入れ物を外す。"""
    target = get_sketch(document, sketch_id)
    for entity in target.entities:
        parameters.release_param_refs(document, entity.params)
    for pattern in target.patterns:
        parameters.release_param_refs(document, pattern.params)
    document.sketches = [item for item in document.sketches if item.id != sketch_id]
    return target


def delete_sketch(doc: Document, sketch_id: str) -> Operation:
    """スケッチ `sketch_id` を入れ物ごと消してログに積む。存在しなければ KeyError(文書は不変)。"""
    target = get_sketch(doc, str(sketch_id))
    spec = _snapshot(doc, target)
    _remove(doc, target.id)
    return doc.log.append(VERB_DELETE_SKETCH, spec)


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_delete_sketch(document: Document, operation: Operation) -> None:
    """redo / replay: 記録したスケッチを消し直す(ログには触らない)。"""
    _remove(document, operation.args["sketch_id"])


def _undo_delete_sketch(document: Document, operation: Operation) -> None:
    """undo: 名前つきの値・図形・反復・平面を args からそのまま戻し、元の位置へ差し込む。"""
    args = operation.args
    sketch_id = args["sketch_id"]
    if any(item.id == sketch_id for item in document.sketches):
        raise ValueError(f"sketch id already exists: {sketch_id!r}")
    taken = [name for name in args["values"] if name in document.parameters]
    if taken:
        raise ValueError(
            f"parameter names still in use when undoing delete_sketch: {taken!r}"
        )
    for name, value in args["values"].items():
        document.parameters[name] = float(value)
    entities: List[Entity] = [
        Entity(id=spec["entity_id"], kind=spec["kind"], params=dict(spec["refs"]))
        for spec in args["entities"]
    ]
    patterns: List[Pattern] = [
        Pattern(
            id=spec["pattern_id"],
            sources=list(spec["sources"]),
            params=dict(spec["refs"]),
            kind=spec["kind"],
            options=copy.deepcopy(spec.get("options") or {}),
        )
        for spec in args["patterns"]
    ]
    restored = Sketch(
        id=sketch_id,
        plane=copy.deepcopy(args["plane"]),
        entities=entities,
        patterns=patterns,
    )
    index = min(int(args["index"]), len(document.sketches))
    document.sketches.insert(index, restored)


register_verb(VERB_DELETE_SKETCH, do=_do_delete_sketch, undo=_undo_delete_sketch)
