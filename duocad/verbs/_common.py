"""verb層が共有する道具(1 verb 1モジュールを保ったまま同型の部品だけを集める)。

**undo用スナップショット表を1つにしてある**のが要点。プレビューは文書を投機的に変えて戻す
ので(`duocad/speculative.py`)、戻し忘れた表が1つでもあると、中断したプレビューの形状が
後の実undoで復元されうる。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Union
from weakref import WeakKeyDictionary

from .. import meshbody, naming, selection
from ..document import MESH_KIND, Body, BodyId, Document, new_body_id
from ..oplog import Operation

#: undo用のスナップショット。`(body_id, seq) -> 直前の形状`。
#:
#: **文書ごとに持ち、文書が消えれば一緒に消える**(`WeakKeyDictionary`)。値は
#: `None` でもよい —— `extrude` の join が新しいボディを作った場合、「前の形状は無い」が
#: 正しい記録である。
SNAPSHOTS: "WeakKeyDictionary[Document, dict]" = WeakKeyDictionary()


def remember(doc: Document, body_id: BodyId, seq: int, shape: Any) -> None:
    """操作前の形状を覚える(同じseqで再適用されたら上書きする)。"""
    SNAPSHOTS.setdefault(doc, {})[(body_id, seq)] = shape


def recall(doc: Document, body_id: BodyId, seq: int) -> Any:
    """覚えていた形状を取り出して忘れる。

    無ければ **KeyError**(黙ってundoを空振りさせない)。保存したログを読んだだけで
    再生していない文書は、スナップショットを持たないのでここへ来る。
    """
    snapshots = SNAPSHOTS.get(doc)
    if snapshots is None or (body_id, seq) not in snapshots:
        raise KeyError(
            f"no snapshot for undo of seq={seq} on body {body_id!r}: "
            "保存したログを再生せずにundoしようとしている。"
            "OpLog.replay(document) してから undo すること"
        )
    return snapshots.pop((body_id, seq))


def reapply_shape(doc: Document, body: Body, result: dict, operation: Operation) -> None:
    """redo / replay: `body` の形状を kernel の結果に差し替え、undo 用に前の形状を覚える。

    **`remember` は do の側でも毎回取り直す** —— `parameters.rebuild()` はログを頭から再生して
    新しい `Body` を作るので、初回に控えたものは同じ seq でも別の実体になる。
    `provenance` は空のときだけ埋める。
    """
    previous_shape = body.shape
    body.shape = result["shape"]
    if operation.provenance is None:
        operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)


def undo_shape(doc: Document, operation: Operation) -> None:
    """形状を差し替えるverbの undo(`args["body_id"]` を覚えていた形状へ戻す)。"""
    body_id = operation.args["body_id"]
    doc.get_body(body_id).shape = recall(doc, body_id, operation.seq)


def editable_body(
    doc: Document,
    body_id: Optional[BodyId],
    *,
    what: str,
    reject_as: Optional[str] = None,
) -> Body:
    """操作対象のボディ。**編集できないもの・形状の無いものは例外**(黙って無視しない)。

    `what` は失敗のメッセージに入る動詞(`"fillet"` / `"extrude"` 等)。
    `reject_as` はメッシュボディを断る文に入れる言い方(省略時は `what`。`extrude` は
    「押し出しの対象にする」と言う)。

    **選択側の門番(`selection._visible_body`)だけでは足りない**。AIと人が並行して動くと、
    選択した後・verbを呼ぶ前に人がそのボディを隠す順序が起きる。`fillet`/`chamfer` がこの順序で
    隠したボディを黙って丸めることを実測した(`tests/test_agent_concurrency.py`)ので、
    見えないボディは常に断る(面・辺どちらを取るverbも同じ)。**新しく掛ける操作の入口だけの門**で、
    redo / 再生は `replayed_body`。
    """
    return _checked_body(doc, body_id, what=what, reject_as=reject_as, require_visible=True)


def replayed_body(
    doc: Document,
    body_id: BodyId,
    *,
    what: str,
    reject_as: Optional[str] = None,
) -> Body:
    """redo / 再生で操作を掛け直すボディ。`editable_body` と同じ検査(無い・編集できない・形状が無い)から、
    **「見えないものは指せない」だけを外したもの**。

    可視性は操作ログに載らず undo / redo でも戻らない状態なので、undo の後に人がボディを隠しただけで
    redo が通らなくなり、ログの再生の成否が画面の状態に左右される(`Body.visible`)。可視性の門が守るのは
    「選択した後・verbを呼ぶ前に隠された」新規の操作で、ログにある操作はその門を通っている。
    """
    return _checked_body(doc, body_id, what=what, reject_as=reject_as, require_visible=False)


def _checked_body(
    doc: Document,
    body_id: Optional[BodyId],
    *,
    what: str,
    reject_as: Optional[str],
    require_visible: bool,
) -> Body:
    if body_id is None:
        active = doc.active_body()
        if active is None:
            raise ValueError(f"no active body to {what} (document is empty)")
        body_id = active.id
    if not doc.is_editable(body_id):  # 存在しないIDは KeyError(document側の契約)
        # 編集verbがメッシュボディを断る唯一の入口(verbごとに分岐すると断り漏れが出る)。
        body = doc.get_body(body_id)
        if body.kind == MESH_KIND:
            meshbody.reject(body, what if reject_as is None else reject_as)
        raise ValueError(
            f"body {body_id!r} is not editable (kind={body.kind!r})"
        )
    body = doc.get_body(body_id)
    if require_visible and not body.visible:
        raise ValueError(
            f"body {body_id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape to {what}")
    return body


def deletable_body(doc: Document, body_id: BodyId, *, require_visible: bool = True) -> Body:
    """削除の対象になるボディ。**編集できるかは問わない**(メッシュボディも消せる)。

    `editable_body` と分けてあるのは、削除だけは種別に関係なく通すため(間違えて入れた形を
    取り除けなくしない)。可視性の門番は**選択から取る `delete` だけ**に課す(理由は
    `editable_body`)。ID で名指しする `delete_items` は `require_visible=False` —— ブラウザでは
    隠したボディの行も ✕ で消せる。
    """
    body = doc.get_body(body_id)  # 存在しないIDは KeyError(document側の契約)
    if require_visible and not body.visible:
        raise ValueError(
            f"body {body_id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    if body.shape is None and body.mesh is None:
        raise ValueError(f"body {body_id!r} has no shape to delete")
    return body


def editable_bodies(doc: Document, body_ids: Sequence[BodyId], *, verb: str, what: str) -> List[Body]:
    """動かす・鏡映するボディの並び(1つも無ければ ValueError)。編集できないもの・隠したものは例外
    (隠したものを断る理由は `editable_body`)。"""
    if not body_ids:
        raise ValueError(f"{verb} requires at least one body")
    return [editable_body(doc, body_id, what=what) for body_id in body_ids]


def resolve_shape_ref(doc: Document, ref: Any, *, kind: str, what: str, mesh_hint: str) -> tuple:
    """`{"body": BodyId, "index": int}`(選択の ShapeRef と同じ形)を (Body, 添字) にする。

    **選択を経由しない**(選択は「対象」の解決に使われているので、軸・中心・鏡映面・ターゲットを
    同じ経路に載せると区別が付かなくなる)。`kind` キーは省略できる(付いていれば要求した種別と
    一致すること。`get_selection` が返す参照をそのまま貼れる)。`mesh_hint` はメッシュボディを
    指されたときの文。
    """
    if not isinstance(ref, dict):
        raise ValueError(
            f"{what} must be a reference like "
            '{"body": "<body_id>", "index": 1}, got ' + repr(ref)
        )
    if "body" not in ref or "index" not in ref:
        raise ValueError(
            f'{what} needs both "body" and "index" '
            '(例: {"body": "<body_id>", "index": 1}), got ' + repr(ref)
        )
    declared = ref.get("kind")
    if declared is not None and str(declared) != kind:
        raise ValueError(f"{what} must be a {kind!r} reference, got kind={declared!r}")
    body = doc.get_body(str(ref["body"]))  # 存在しないIDは KeyError(document側の契約)
    if body.shape is None:
        raise ValueError(f"{what}: body {body.id!r} has no B-rep shape {mesh_hint}")
    try:
        index = int(ref["index"])
    except (TypeError, ValueError):
        raise ValueError(f"{what}: index must be an integer, got {ref['index']!r}") from None
    return body, index


def copy_ids(count: int, recorded: Optional[Sequence[BodyId]], *, verb: str) -> List[BodyId]:
    """複製に振る ID。`recorded`(ログに記録した並び。再生で同じ ID になる)が無ければ新しく振る。
    本数が合わなければ ValueError(`zip` が黙って短い方に合わせる形にしない)。"""
    new_ids = [new_body_id() for _ in range(count)] if recorded is None else [str(value) for value in recorded]
    if len(new_ids) != count:
        raise ValueError(f"{verb}: {count} bodies but {len(new_ids)} ids recorded")
    return new_ids


def add_transformed_copies(
    doc: Document,
    bodies: Sequence[Body],
    transform: Any,
    new_ids: Sequence[BodyId],
    numbers: Optional[Sequence[int]] = None,
    *,
    allocate: bool = False,
) -> Optional[List[int]]:
    """変換した複製を足し(元は動かさない)、名前に使った通し番号を返す(ログの `numbers` に記録する)。

    名前は、作成時(`allocate=True`)は新しく振った番号、redo / 再生では記録した `numbers`。
    番号の記録が無い古いログ(`None`)だけ元の名前を引き継ぐ。

    **全部の形状を作ってから番号を振って足す** —— 途中で落ちても1体目だけ足された文書が残らず、
    番号も飛ばない。
    """
    shapes = [transform(body.shape) for body in bodies]
    if allocate:
        numbers = [naming.allocate(doc, naming.BODY) for _ in bodies]
    if numbers is None:
        names = [body.name for body in bodies]
    else:
        names = [naming.recorded(doc, naming.BODY, number, None) for number in numbers]
    if len(names) != len(bodies):
        raise ValueError(f"{len(bodies)} bodies but {len(names)} numbers recorded")
    for body, new_id, name, shape in zip(bodies, new_ids, names, shapes):
        # 同じIDが既にあれば ValueError(黙って重複させない)
        doc.add_body(Body(id=new_id, name=name, kind=body.kind, shape=shape, visible=body.visible))
    return None if numbers is None else list(numbers)


def remove_created_bodies(doc: Document, body_ids: Sequence[BodyId]) -> None:
    """undo: 作った複製を消す(active は複製を足す前の値へ戻る。`Document.remove_added_body`)。
    既に消えている(別のundoが先に消した)場合は空振りでよい。足した順の逆に消す。"""
    for body_id in reversed(list(body_ids)):
        try:
            doc.remove_added_body(body_id)
        except KeyError:
            pass


def detach_bodies(doc: Document, body_ids: Sequence[BodyId]) -> dict:
    """ボディを消す前に undo 用の退避を取り、消す。戻りは `{"bodies": [(元の添字, Body), ...], "active": ...}`。

    **消す前の並びの昇順**で控える(この順に元の添字へ差し戻すと並びが再現する。末尾へ足し直すと
    パーツ一覧の順序が変わる)。`Document.remove_body` は消したのが active だったときだけ active を
    None に落とすが、消さなかったボディが active のままの場合もあるので、消す前の値をそのまま
    控える。`_active_body_id` に触るのはここと `reattach_bodies` だけにする。
    """
    positions = {body.id: index for index, body in enumerate(doc.bodies)}
    detached = {
        "bodies": sorted(((positions[body_id], doc.get_body(body_id)) for body_id in body_ids), key=lambda item: item[0]),
        "active": doc._active_body_id,
    }
    for _, body in detached["bodies"]:
        doc.remove_body(body.id)
    return detached


def reattach_bodies(doc: Document, detached: dict) -> None:
    """undo: `detach_bodies` の退避を元の添字へ戻し、active も戻す。"""
    for index, body in detached["bodies"]:
        doc.add_body(body, index=index)
    # `set_active_body` は None を受け付けない(KeyError)ので直接戻す。
    doc._active_body_id = detached["active"]


# --------------------------------------------------------------------------------------
# 移動の2verb(`move_body` / `move_sketch_entities`)が共有する mode と引数の検査
# --------------------------------------------------------------------------------------
#: 並進 / 回転の切り替え。**宣言はここが正**(`catalog.py` の `mode` の説明と `Session` の
#: 既定値がこれを写す)。引数の有無で暗黙に分岐させない(両方来たときの挙動が見えなくなる)。
#: 移動の2つのverbが同じ mode を取ること自体が契約。
MODE_TRANSLATE = "translate"
MODE_ROTATE = "rotate"
MOVE_MODES = (MODE_TRANSLATE, MODE_ROTATE)


def require_move_mode(mode: str, *, verb: str) -> None:
    if mode not in MOVE_MODES:
        raise ValueError(f"unknown {verb} mode: {mode!r} (one of {list(MOVE_MODES)})")


def reject_foreign_args(mode: str, given: dict, *, verb: str) -> None:
    """**別のモードの引数が来たら断る**(黙って無視も、黙って両方適用もしない)。
    `None` は「渡していない」と読む(WS の `_dispatch` は省略された引数を `None` のまま渡す)。"""
    foreign = sorted(name for name, value in given.items() if value is not None)
    if foreign:
        raise ValueError(f"{verb}(mode={mode!r}) does not take {foreign} (mode を切り替えるか、その引数を外すこと)")


def rotation_degrees(angle: Any, *, verb: str, noun: str) -> float:
    """回転の角度[度]を検査する(無い → 非有限 → 360の倍数)。`noun` は「元と重なる○○を作る」の目的語。"""
    if angle is None:
        raise ValueError(f"{verb}(mode='rotate') needs `angle` (度)")
    degrees = float(angle)
    if not math.isfinite(degrees):
        raise ValueError(f"angle must be a finite number of degrees, got {angle!r}")
    if degrees % 360.0 == 0.0:
        raise ValueError(
            f"{verb}(mode='rotate') needs an angle that is not a multiple of 360 "
            f"(0度・360度は copy=False では何も変えず、copy=True では元と重なる{noun}を作る)"
        )
    return degrees


def is_rotation(args: dict) -> bool:
    """ログの `args` が回転か。**`mode` キーが無いログは並進**として読む(2026-09-05より前に保存した
    文書は全部平行移動)。"""
    return args.get("mode", MODE_TRANSLATE) == MODE_ROTATE


def inverse_move_args(args: dict, keys: Sequence[str]) -> dict:
    """undo 用に**逆向きの変換**の args を作る(並進は `keys` の逆ベクトル、回転は逆角度・中心と軸はそのまま)。"""
    if is_rotation(args):
        return {**args, "angle": -float(args["angle"])}
    return {**args, **{key: -float(args[key]) for key in keys}}


_COUNT_WORDS = {2: "two", 3: "three"}


def finite_numbers(values: Any, count: int, *, what: str, form: str) -> List[float]:
    """`form`(`[x, y, z]` / `[u, v]`)の形の有限な数 `count` 個にする。形が違えば ValueError。"""
    word = _COUNT_WORDS[count]
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be {word} numbers {form}, got {values!r}") from None
    if len(numbers) != count or not all(math.isfinite(value) for value in numbers):
        raise ValueError(f"{what} must be {word} finite numbers {form}, got {values!r}")
    return numbers


def normalize_values(
    edges: Sequence[int],
    value: Union[float, Sequence[float]],
    *,
    label: str,
) -> tuple[List[float], Union[float, List[float]]]:
    """辺ごとの値をそろえる(スカラーなら全辺へ配る)。**0以下と非有限は例外**。

    戻り値は `(適用する値のリスト, ログへ積む形)`。ログには打った形のまま(スカラーならスカラー)を積む。
    """
    if isinstance(value, (int, float)):
        values = [float(value)] * len(edges)
        stored: Union[float, List[float]] = float(value)
    else:
        values = [float(item) for item in value]
        if len(values) != len(edges):
            raise ValueError(
                f"{label} list length {len(values)} != edges length {len(edges)}"
            )
        stored = values
    for item in values:
        if not math.isfinite(item) or item <= 0.0:
            raise ValueError(f"{label} must be finite and > 0, got {item!r}")
    return values, stored


def edge_entry(document: Document, verb: Any, value: Any) -> dict:
    """選択の辺に `verb`(`fillet` / `chamfer`)を掛ける入口の共通部分。

    辺は `selection.require_edges` が**選択順のまま**返す(値のリストと位置で対応させるため)。
    """
    edges, body_id = selection.require_edges(document)
    changed = verb(document, edges, value, body_id=body_id).args["body_id"]
    return {"body_id": changed, "changed_bodies": [changed]}
