"""verb `delete` — **確定選択のボディとスケッチ図形をまとめて消す**。

**対象は1つのverbで2種類ある**:

- **ボディ** … 面/辺/頂点/`kind="body"` のどれから指してもよい(`selection.require_deletable`
  が `require_bodies` と同じ逆引きをする)。`move_body` が既に採っている考え方で、
  「ボディを指すのに面を1枚選べばよい」。
- **スケッチ図形** … `sketch_entity` の参照。点(`point` が文字列)を指しても**図形1つ**が
  対象になる(点は図形の一部であって、それ自体が図形ではない)。

**混ざっていても1回のverb呼び出し・1行のログ・1回のundo**である。
ボディごと・図形ごとに分解しない —— 分解すると、
「ボディ1つと図形2つを選んで消す」が Ctrl+Z 3回になる。

--------------------------------------------------------------------------------------
undo の仕組みが対象によって別物になる(**ここがこのモジュールの主な設計**)
--------------------------------------------------------------------------------------

`oplog` の契約は「`undo(document, operation)` は `operation.args`(JSON化できる値だけ)から
自分で巻き戻す」であって、過去の状態のスナップショットが暗黙に付いてくるわけではない。
対象の性質が違うので、巻き戻し方も2通りになる:

- **ボディ** … `TopoDS_Shape` はJSONにできないので、`_common.SNAPSHOTS`(文書の寿命で
  消えるスナップショット表)へ **`Body` オブジェクトそのもの**を退避する。`fillet` が
  形状を退避しているのと同じ表・同じ規律で、違うのは退避するのが「前の形」ではなく
  「ボディまるごと」であることだけ。**並び順と active も一緒に控える** —— 末尾へ足し直す
  だけだと、undoしたのにパーツ一覧の順序だけが変わる(形が同じなので誰も落とせない差)。
- **スケッチ図形** … `kind` と**解決済みの生の数値**を args へ積めば足りる(スナップショットは
  要らない)。`sketch._do_add_entity` / `_undo_add_entity` が**ちょうど鏡像**になっており、
  **削除の do は「追加の undo」・削除の undo は「追加の do」**である。
  消す側は **`sketch.remove_entities` を通す**(名前つきの値を手放す。直接外すと値が誰にも
  参照されないまま文書に残り、`d1 d2 …` の連番が飛ぶ=保存して再生したときに
  `set_parameter` の記録した名前が別の値を指す)。

**素の数値のまま持つ図形がある**(`import_svg` の取り込んだベジエ。制御点は人が打ち直す
寸法ではないので名前つきの値に登録しない)。undo でそれを `register_value` へ通すと、
ロゴ1枚で数千個の `d1` が生えて元と違う文書になる —— どの鍵が名前つきだったかを
`named` に控えて、**元と同じ形で**戻す。

--------------------------------------------------------------------------------------
やらないこと(意図的な線引き)
--------------------------------------------------------------------------------------

- **スケッチを丸ごと消す口は無い**。「スケッチという入れ物そのもの」を指す選択の種別が
  無い(`selection.SELECTABLE_KINDS`)ので、作っても呼びようが無い。
- **領域(`region`)は消せない**。領域は描くものではなく図形の配置から毎回計算されるもの
  (`duocad/region.py`)で、永続化された実体が無い。選択に混ざっていたら
  `require_deletable` が**黙って無視する** —— 領域を作っている図形自体を
  消せば領域も自然に消えるので、拒否して選び直させるより無視するほうが安い。
  選択が**領域だけ**なら削除できる対象が無いので、その場合は従来どおり拒否する。
- **反復(`Pattern`)の元になっている図形を消しても、反復そのものは消さない**。消えた元は
  展開されなくなるだけで、undoで元が戻れば反復も戻る(`duocad/region.py` の
  `expanded_entities`)。図形どうしの依存グラフは作らない(本人判断)。
- **ボディを消してもスケッチ側は何もしない**。スケッチ平面は作成時点で焼き付けてある
  ので、貼った面のボディが消えても姿勢の計算は
  壊れない。ボディ側のキャッシュは `DocumentView.state_meta()` の生存集合差分が汎用的に
  落とす(専用の剪定は要らない)。

**OCP / build123d を直接importしない**(幾何はここでは要らない。消すだけ)。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ..sketch import Entity, get_entity, get_sketch, remove_entities
from ._common import deletable_body, detach_bodies, reattach_bodies, recall, remember

VERB_DELETE = "delete"

#: `_common.SNAPSHOTS` は `(body_id, seq)` を鍵にするが、この verb が控えるのは
#: 「この操作で消したボディ全部 + 並び順 + active」という**操作1回ぶんの1つの塊**である。
#: BodyId は必ず `body-<hex>` なので、この綴りと衝突しない。
_SNAPSHOT_KEY = "delete:removed"


def _entity_snapshots(doc: Document, entities: Sequence[dict]) -> List[dict]:
    """消す図形を**JSON安全な値だけ**で記録する(undoはこの記録だけから作り直す)。

    `params` は**解決済みの数値**で、鍵の順序は `Entity.params` の順序 = 作ったときに
    `register_value` を呼んだ順序である。undoで同じ順に登録し直すと**同じ名前**(`d3` 等)が
    振り直される(`parameters.register_value` は使われていない最小の番号を使うため)ので、
    後ろに載った `set_parameter` が記録した名前が別の値を指すようにはならない。

    `named` は**名前つきの値を持っていた鍵**。取り込んだベジエは素の数値のままなので、
    ここが空になる(モジュールdocstring)。

    存在しないスケッチ・図形は KeyError(**文書は何も変わらない**)。
    """
    specs: List[dict] = []
    for ref in entities:
        sketch_id = ref["sketch"]
        sketch = get_sketch(doc, sketch_id)
        entity = get_entity(sketch, ref["entity"])
        specs.append(
            {
                "sketch_id": sketch_id,
                "entity_id": entity.id,
                "kind": entity.kind,
                # 図形の並びの位置。undoで**元の位置へ差し戻す**ために要る。
                "index": sketch.entities.index(entity),
                "params": parameters.resolve_all(doc, entity.params),
                "named": [
                    key for key, value in entity.params.items() if isinstance(value, str)
                ],
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

    **全部検査してから1つも消さない** —— 途中で落ちると「半分だけ消えた文書」が残り、
    その状態はログに載っていないので undo でも戻らない。
    """
    body_ids = [str(body_id) for body_id in bodies]
    if not body_ids and not entities:
        raise ValueError(
            "delete requires at least one body or sketch entity "
            "(確定選択から対象を取る verb なので、呼ぶ前に選ぶこと)"
        )
    if len(set(body_ids)) != len(body_ids):
        raise ValueError(f"delete got duplicate body ids: {body_ids!r}")
    entity_keys = [(ref["sketch"], ref["entity"]) for ref in entities]
    if len(set(entity_keys)) != len(entity_keys):
        # **同じ図形を2回渡されると undo が壊れる**(2回目の差し戻しが
        # 「entity id already exists」で落ちる)。畳むのは `require_deletable` の役目で、
        # ここは畳まずに断る —— 黙って畳むと、呼び出し側の重複が見えなくなる。
        raise ValueError(f"delete got duplicate sketch entities: {entity_keys!r}")

    for body_id in body_ids:
        # 隠したボディ・形の無いボディを弾く(可視性を見るのは `fillet` / `move_body` と
        # 同じ理由 —— 選んだ後・verbを呼ぶ前に人がそのボディを隠す順序が、AIと人が並行して
        # 動くと実際に起きる)。**種別は問わない** —— メッシュボディは編集できないが消せる
        # (`_common.deletable_body` のdocstring)。
        deletable_body(doc, body_id)
    entity_specs = _entity_snapshots(doc, entities)

    removed = _remove(doc, body_ids, entity_specs)
    operation = doc.log.append(
        VERB_DELETE, {"bodies": body_ids, "entities": entity_specs}
    )
    if body_ids:
        remember(doc, _SNAPSHOT_KEY, operation.seq, removed)
    return operation


def _remove(
    document: Document, body_ids: Sequence[str], entity_specs: Sequence[dict]
) -> Dict[str, Any]:
    """実際に消す(**ログには触らない**)。戻り値がボディのundo用の退避。

    ボディを先に消しても図形を先に消しても結果は同じ(互いを参照しない)。
    """
    removed = detach_bodies(document, list(body_ids))
    for spec in entity_specs:
        remove_entities(document, [spec["entity_id"]], sketch_id=spec["sketch_id"])
    return removed


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_delete(document: Document, operation: Operation) -> None:
    """redo / replay: 記録した対象を消し直す(ログには触らない)。

    **スナップショットは毎回取り直す** —— `parameters.rebuild()` はログを頭から再生して
    **新しい `Body` オブジェクト**を作るので、初回に控えたものは同じ seq でも別の実体になる。
    `_common.reapply_shape` が同じ理由で `remember` を do の側にも書いている。
    """
    args = operation.args
    body_ids = [str(body_id) for body_id in args["bodies"]]
    entity_specs = [dict(spec) for spec in args["entities"]]
    for body_id in body_ids:
        deletable_body(document, body_id)
    removed = _remove(document, body_ids, entity_specs)
    if body_ids:
        remember(document, _SNAPSHOT_KEY, operation.seq, removed)


def _undo_delete(document: Document, operation: Operation) -> None:
    """undo: 図形は args から作り直し、ボディはスナップショット表から戻す。

    **図形の側は保存したログを再生していなくても戻せる**(args だけで完結する)。ボディの側は
    `TopoDS_Shape` をJSONに載せられない以上どうしても再生が要るので、控えが無ければ
    `recall` が KeyError で止まる(黙って空振りさせない)。
    """
    args = operation.args
    # 1. 図形を作り直す —— **削除のundoは「追加のdo」**(`sketch._do_add_entity` の写し)。
    #    元の添字の昇順に差し込むと元の並びが再現し、`register_value` を呼ぶ順序も
    #    作ったときと同じになるので**同じ名前**が振り直される。
    for spec in sorted(args["entities"], key=lambda item: int(item["index"])):
        sketch = get_sketch(document, spec["sketch_id"])
        entity_id = spec["entity_id"]
        if any(entity.id == entity_id for entity in sketch.entities):
            raise ValueError(f"entity id already exists: {entity_id!r}")
        named = set(spec.get("named") or ())
        refs: Dict[str, Any] = {}
        for key, value in spec["params"].items():
            refs[key] = (
                parameters.register_value(document, float(value))
                if key in named
                else float(value)
            )
        sketch.entities.insert(
            int(spec["index"]), Entity(id=entity_id, kind=spec["kind"], params=refs)
        )

    # 2. ボディを戻す。**元の添字と active も戻す**(並びが変わると「取り消したのに
    #    元に戻っていない」という説明のつかない差が残る)。
    if not args["bodies"]:
        return
    reattach_bodies(document, recall(document, _SNAPSHOT_KEY, operation.seq))


register_verb(VERB_DELETE, do=_do_delete, undo=_undo_delete)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を確定選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_DELETE)
def _enter_delete(document: Document) -> dict:
    """ボディ由来(面/辺/頂点/ボディ)と図形が混ざっていても1回で処理する —— `require_bodies` と
    `require_sketch_entities` を2つ呼ぶと、片方しか選ばれていないときに `SelectionKindMismatch` で壊れる。

    **`changed_bodies` は返さない** —— 消えたボディは配りものの `bodies` から丸ごと居なくなる
    (形が変わったのではない)。画面が形を取り直す対象は1つも無い。
    """
    targets = selection.require_deletable(document)
    args = delete(document, targets.bodies, targets.entities).args
    return {
        "bodies": list(args["bodies"]),
        "entities": [spec["entity_id"] for spec in args["entities"]],
        "sketch_ids": sorted({str(spec["sketch_id"]) for spec in args["entities"]}),
    }
