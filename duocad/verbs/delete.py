"""verb `delete` — **選択のボディとスケッチ図形をまとめて消す**。
verb `delete_items` — **IDで指したボディとスケッチ(入れ物ごと)をまとめて消す**。

2つは**対象の指し方だけが違い、do / undo は同じ関数**である(args は `{bodies, entities, sketches}` の
上位集合で、片方が使わない鍵は空)。`delete_items` が別の verb なのは、入口(`@entry`)が verb 名
ごとに1つで、「選択から取る」と「引数で指す」を混ぜると引数の有無で意味が変わるため。スケッチという
入れ物は選択集合の種別に無い(`selection.SELECTABLE_KINDS`)ので、`import_svg` と同じく ID を引数で
受ける。ブラウザの一覧の行から消す口がこれを使う。

**対象は2種類**:

- **ボディ** … 面/辺/頂点/`kind="body"` のどれから指してもよい(`selection.require_deletable`)。
- **スケッチ図形** … `sketch_entity` の参照。点(`point` が文字列)を指しても図形1つが対象になる。

混ざっていても1回のverb呼び出し・1行のログ・1回のundoである(分解すると Ctrl+Z が複数回になる)。

--------------------------------------------------------------------------------------
undo の仕組みが対象によって別物になる
--------------------------------------------------------------------------------------

`oplog` の契約は「`undo(document, operation)` は `operation.args`(JSON化できる値だけ)から自分で
巻き戻す」。

- **ボディ** … `TopoDS_Shape` はJSONにできないので、`_common.SNAPSHOTS` へ **`Body` オブジェクトそのもの**
  を退避する(`fillet` と同じ表・同じ規律)。並び順と active も一緒に控える。
- **スケッチ(入れ物ごと)** … 平面(焼き付けた姿勢ごと)・図形・反復・名前つきの値・名前を args に全部
  控え、undo は args だけで作り直す。**平面は `resolve_plane` で解き直さない**(貼った面がその後の
  面取り・フィレットで添字を変えていると、別の面へ貼り直す/失敗する)。名前つきの値は名前ごと直に
  戻す(1つのスケッチの中で図形と反復の登録順が時間軸で混ざるので `register_value` し直せない)。
  undo は削除の直後へ戻るので控えた名前は必ず空いており、空いていなければバグなので例外にする。
- **スケッチ図形** … `kind` と解決済みの生の数値を args へ積めば足りる。`sketch._do_add_entity` /
  `_undo_add_entity` のちょうど鏡像で、削除の do は「追加の undo」、削除の undo は「追加の do」。
  消す側は **`sketch.remove_entities` を通す**(名前つきの値を手放す。直接外すと `d1 d2 …` の連番が
  飛び、保存して再生したとき `set_parameter` の記録した名前が別の値を指す)。

**素の数値のまま持つ図形がある**(`import_svg` の取り込んだベジエ)。undo でそれを `register_value` へ
通すとロゴ1枚で数千個の `d1` が生えるので、どの鍵が名前つきだったかを `named` に、その名前を `names` に
控えて元と同じ形・同じ名前で戻す。`register_value` で振り直すと、先に別の削除が低い番号を空けているとき
元と別の名前を取り、ログの再生と食い違う(以後のスケッチ復元が「名前が使用中」で止まる)。

--------------------------------------------------------------------------------------
やらないこと(意図的な線引き)
--------------------------------------------------------------------------------------

- スケッチを消しても、そこから押し出したボディには触らない(ボディは既に独立した実体)。
- 確認は挟まない。可視性は控えない(セッション状態なので、undo で戻ったスケッチは可視で始まる)。
- **領域(`region`)は消せない**。領域は図形の配置から毎回計算されるもの(`duocad/region.py`)で、
  永続化された実体が無い。選択に混ざっていたら `require_deletable` が黙って無視する(領域を作る図形を
  消せば自然に消える)。領域だけの選択は削除できる対象が無いので拒否する。
- 反復(`Pattern`)の元の図形を消しても反復そのものは消さない(元が戻れば反復も戻る。
  `duocad/region.py` の `expanded_entities`)。図形どうしの依存グラフは作らない。
- ボディを消してもスケッチ側は何もしない(スケッチ平面は作成時点で焼き付けてある)。ボディ側の
  キャッシュは `DocumentView.state_meta()` の生存集合差分が落とす。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Sequence

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ..sketch import Entity, Pattern, Sketch, get_entity, get_sketch, remove_entities
from ._common import deletable_body, detach_bodies, editable_sketch, reattach_bodies, recall, remember

VERB_DELETE = "delete"
VERB_DELETE_ITEMS = "delete_items"

#: `_common.SNAPSHOTS` は `(body_id, seq)` を鍵にするが、この verb が控えるのは
#: 「この操作で消したボディ全部 + 並び順 + active」という操作1回ぶんの1つの塊。
#: BodyId は必ず `body-<hex>` なので、この綴りと衝突しない。
_SNAPSHOT_KEY = "delete:removed"


def _entity_snapshots(doc: Document, entities: Sequence[dict]) -> List[dict]:
    """消す図形をJSON安全な値だけで記録する(undoはこの記録だけから作り直す)。

    `params` は解決済みの数値。`named` は名前つきの値を持っていた鍵(取り込んだベジエは空)で、`names` は
    その鍵ごとの元の名前(`d3` 等)。undo は名前ごと直に戻す(`register_value` の「最小の空き番号」は、先に
    別の削除が低い番号を空けていると元と別の名前を取る)。`names` を持たない旧ログの undo だけは、
    `named` の鍵を `register_value` で登録し直す。存在しないスケッチ・図形は KeyError(文書は変わらない)。
    """
    specs: List[dict] = []
    for ref in entities:
        sketch_id = ref["sketch"]
        sketch = get_sketch(doc, sketch_id)
        entity = get_entity(sketch, ref["entity"])
        names = {key: value for key, value in entity.params.items() if isinstance(value, str)}
        specs.append(
            {
                "sketch_id": sketch_id,
                "entity_id": entity.id,
                "kind": entity.kind,
                # undoで元の位置へ差し戻すための並びの位置。
                "index": sketch.entities.index(entity),
                "params": parameters.resolve_all(doc, entity.params),
                "named": list(names),
                "names": names,
            }
        )
    return specs


def _sketch_snapshots(doc: Document, sketch_ids: Sequence[str]) -> List[dict]:
    """消すスケッチをJSON安全な値だけで記録する(undo はこの記録だけから作り直す)。存在しないスケッチは KeyError。"""
    specs: List[dict] = []
    for sketch_id in sketch_ids:
        target = get_sketch(doc, sketch_id)
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
        specs.append(
            {
                "sketch_id": target.id,
                "name": target.name,
                # undo で元の位置へ差し戻す並びの位置(ブラウザの一覧の順序を保つ)。
                "index": doc.sketches.index(target),
                "plane": copy.deepcopy(target.plane),
                "entities": entities,
                "patterns": patterns,
                "values": values,
            }
        )
    return specs


def delete(
    doc: Document,
    bodies: Sequence[str] = (),
    entities: Sequence[dict] = (),
) -> Operation:
    """ボディと図形をまとめて消す。**片方だけでもよいが、両方空は ValueError**。

    - `bodies` … BodyId の並び(`selection.require_deletable` が逆引き済みのもの)
    - `entities` … `{"sketch": SketchId, "entity": EntityId}` の並び

    全部検査してから消す(途中で落ちると、ログに載らない「半分だけ消えた文書」が残る)。
    """
    body_ids = [str(body_id) for body_id in bodies]
    if not body_ids and not entities:
        raise ValueError(
            "delete requires at least one body or sketch entity "
            "(選択から対象を取る verb なので、呼ぶ前に選ぶこと)"
        )
    if len(set(body_ids)) != len(body_ids):
        raise ValueError(f"delete got duplicate body ids: {body_ids!r}")
    entity_keys = [(ref["sketch"], ref["entity"]) for ref in entities]
    if len(set(entity_keys)) != len(entity_keys):
        # 同じ図形を2回渡されると undo の2回目の差し戻しが落ちる。畳むのは `require_deletable` の役目。
        raise ValueError(f"delete got duplicate sketch entities: {entity_keys!r}")

    for body_id in body_ids:
        # 隠したボディ・形の無いボディを弾く(理由は `_common.editable_body`)。種別は問わない
        # (メッシュボディは編集できないが消せる。`_common.deletable_body`)。
        deletable_body(doc, body_id)
    for ref in entities:
        editable_sketch(doc, ref["sketch"])
    entity_specs = _entity_snapshots(doc, entities)

    removed = _remove(doc, body_ids, entity_specs, [])
    operation = doc.log.append(
        VERB_DELETE, {"bodies": body_ids, "entities": entity_specs}
    )
    if body_ids:
        remember(doc, _SNAPSHOT_KEY, operation.seq, removed)
    return operation


def delete_items(
    doc: Document,
    bodies: Sequence[str] = (),
    sketches: Sequence[str] = (),
) -> Operation:
    """IDで指したボディとスケッチ(入れ物ごと)をまとめて消す。片方だけでもよいが、両方空は ValueError。検査は `delete` と同じ。"""
    body_ids = [str(body_id) for body_id in bodies]
    sketch_ids = [str(sketch_id) for sketch_id in sketches]
    if not body_ids and not sketch_ids:
        raise ValueError("delete_items requires at least one body or sketch id")
    if len(set(body_ids)) != len(body_ids):
        raise ValueError(f"delete_items got duplicate body ids: {body_ids!r}")
    if len(set(sketch_ids)) != len(sketch_ids):
        raise ValueError(f"delete_items got duplicate sketch ids: {sketch_ids!r}")
    for body_id in body_ids:
        deletable_body(doc, body_id, require_visible=False)
    sketch_specs = _sketch_snapshots(doc, sketch_ids)

    removed = _remove(doc, body_ids, [], sketch_ids)
    operation = doc.log.append(
        VERB_DELETE_ITEMS, {"bodies": body_ids, "entities": [], "sketches": sketch_specs}
    )
    if body_ids:
        remember(doc, _SNAPSHOT_KEY, operation.seq, removed)
    return operation


def _remove(
    document: Document,
    body_ids: Sequence[str],
    entity_specs: Sequence[dict],
    sketch_ids: Sequence[str],
) -> Dict[str, Any]:
    """実際に消す(ログには触らない)。戻り値がボディのundo用の退避。スケッチは名前つきの値を手放してから外す
    (忘れると `d1 d2 …` の連番が飛ぶ)。"""
    removed = detach_bodies(document, list(body_ids))
    for spec in entity_specs:
        remove_entities(document, [spec["entity_id"]], sketch_id=spec["sketch_id"])
    for sketch_id in sketch_ids:
        target = get_sketch(document, sketch_id)
        for entity in target.entities:
            parameters.release_param_refs(document, entity.params)
        for pattern in target.patterns:
            parameters.release_param_refs(document, pattern.params)
        document.sketches.remove(target)
    return removed


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_delete(document: Document, operation: Operation) -> None:
    """redo / replay: 記録した対象を消し直す(ログには触らない)。スナップショットは毎回取り直す(理由は `_common.reapply_shape`)。
    隠したボディも消し直す(可視性の門は新しく選択から消す `delete` の入口だけ。理由は `_common.replayed_body`)。"""
    args = operation.args
    body_ids = [str(body_id) for body_id in args["bodies"]]
    entity_specs = [dict(spec) for spec in args["entities"]]
    sketch_ids = [str(spec["sketch_id"]) for spec in args.get("sketches") or ()]
    for body_id in body_ids:
        deletable_body(document, body_id, require_visible=False)
    removed = _remove(document, body_ids, entity_specs, sketch_ids)
    if body_ids:
        remember(document, _SNAPSHOT_KEY, operation.seq, removed)


def _undo_delete(document: Document, operation: Operation) -> None:
    """undo: 図形は args から作り直し、ボディはスナップショット表から戻す。

    図形の側は args だけで戻せる。ボディの側は再生が要り、控えが無ければ `recall` が KeyError で止まる。
    **戻す前に全部検査する**(スケッチを戻してから図形やボディで落ちると、履歴が動かないのに文書だけ
    部分的に戻った状態が残り、以後の undo が「sketch id already exists」で止まる)。
    """
    args = operation.args
    sketch_specs = sorted(args.get("sketches") or (), key=lambda item: int(item["index"]))
    entity_specs = sorted(args["entities"], key=lambda item: int(item["index"]))
    _require_restorable(document, sketch_specs, entity_specs)
    detached = recall(document, _SNAPSHOT_KEY, operation.seq) if args["bodies"] else None

    # 0. スケッチを入れ物ごと作り直す。元の添字の昇順に差し込むと元の並びが再現する。
    for spec in sketch_specs:
        _restore_sketch(document, spec)
    # 1. 図形を作り直す(`sketch._do_add_entity` の写し)。元の添字の昇順に差し込むと並びが再現する。
    for spec in entity_specs:
        _restore_entity(document, spec)
    # 2. ボディを戻す(元の添字と active も戻す)。
    if detached is not None:
        reattach_bodies(document, detached)


def _require_restorable(
    document: Document, sketch_specs: Sequence[dict], entity_specs: Sequence[dict]
) -> None:
    """undo で戻すスケッチ・図形の ID と名前が衝突しないことを、文書を変える前に確かめる(衝突は ValueError)。"""
    sketch_ids = {sketch.id for sketch in document.sketches}
    claimed: set = set(document.parameters)
    for spec in sketch_specs:
        sketch_id = spec["sketch_id"]
        if sketch_id in sketch_ids:
            raise ValueError(f"sketch id already exists: {sketch_id!r}")
        sketch_ids.add(sketch_id)
        taken = [name for name in spec["values"] if name in claimed]
        if taken:
            raise ValueError(f"parameter names still in use when restoring sketch {sketch_id!r}: {taken!r}")
        claimed.update(spec["values"])
    for spec in entity_specs:
        sketch = get_sketch(document, spec["sketch_id"])
        entity_id = spec["entity_id"]
        if any(entity.id == entity_id for entity in sketch.entities):
            raise ValueError(f"entity id already exists: {entity_id!r}")
        taken = [name for name in (spec.get("names") or {}).values() if name in claimed]
        if taken:
            raise ValueError(f"parameter names still in use when restoring entity {entity_id!r}: {taken!r}")
        claimed.update((spec.get("names") or {}).values())


def _restore_entity(document: Document, spec: dict) -> None:
    """undo: `_entity_snapshots` の1件から図形を戻す。名前つきの値は元の名前ごと直に戻す(`names` が無い旧ログだけ
    `register_value` で登録し直す)。"""
    sketch = get_sketch(document, spec["sketch_id"])
    names = spec.get("names")
    named = set(spec.get("named") or ())
    refs: Dict[str, Any] = {}
    for key, value in spec["params"].items():
        if names is not None and key in names:
            document.parameters[names[key]] = float(value)
            refs[key] = names[key]
        elif key in named:
            refs[key] = parameters.register_value(document, float(value))
        else:
            refs[key] = float(value)
    sketch.entities.insert(
        int(spec["index"]), Entity(id=spec["entity_id"], kind=spec["kind"], params=refs)
    )


def _restore_sketch(document: Document, spec: dict) -> None:
    """undo: `_sketch_snapshots` の1件から、名前つきの値・図形・反復・平面・名前をそのまま戻す(衝突の検査は `_require_restorable`)。"""
    sketch_id = spec["sketch_id"]
    for name, value in spec["values"].items():
        document.parameters[name] = float(value)
    restored = Sketch(
        id=sketch_id,
        plane=copy.deepcopy(spec["plane"]),
        entities=[
            Entity(id=item["entity_id"], kind=item["kind"], params=dict(item["refs"]))
            for item in spec["entities"]
        ],
        patterns=[
            Pattern(
                id=item["pattern_id"],
                sources=list(item["sources"]),
                params=dict(item["refs"]),
                kind=item["kind"],
                options=copy.deepcopy(item.get("options") or {}),
            )
            for item in spec["patterns"]
        ],
        name=spec.get("name"),
    )
    document.sketches.insert(min(int(spec["index"]), len(document.sketches)), restored)


register_verb(VERB_DELETE, do=_do_delete, undo=_undo_delete)
register_verb(VERB_DELETE_ITEMS, do=_do_delete, undo=_undo_delete)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_DELETE)
def _enter_delete(document: Document) -> dict:
    """ボディ由来(面/辺/頂点/ボディ)と図形が混ざっていても1回で処理する(`require_bodies` と
    `require_sketch_entities` を2つ呼ぶと、片方しか選ばれていないとき `SelectionKindMismatch` で壊れる)。

    `changed_bodies` は返さない(消えたボディは配信の `bodies` から居なくなるだけ)。
    """
    targets = selection.require_deletable(document)
    args = delete(document, targets.bodies, targets.entities).args
    return {
        "bodies": list(args["bodies"]),
        "entities": [spec["entity_id"] for spec in args["entities"]],
        "sketch_ids": sorted({str(spec["sketch_id"]) for spec in args["entities"]}),
    }


@entry(VERB_DELETE_ITEMS)
def _enter_delete_items(
    document: Document, bodies: Sequence[str] = (), sketches: Sequence[str] = ()
) -> dict:
    """対象は選択ではなく引数の ID。選択は読まない。"""
    args = delete_items(document, list(bodies), list(sketches)).args
    return {
        "bodies": list(args["bodies"]),
        "sketches": [spec["sketch_id"] for spec in args["sketches"]],
    }
