"""verb `set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` / `set_loads`
—— 形状最適化(第3のモード)の**条件を置く4つのverb**。

**最適化そのものとは接続しない**(実行verbは無い)。ここが作るのは「条件を表現でき、
保存でき、状態として配れる」ところまでである。

**4つとも「集合代入」**である。「1つ足す」ではなく「この役割の集合を、いま確定している
選択集合そのもので置き換える」。操作の流れがその裏返しになっている ——
verbを起動すると**既にその役割が付いている対象が選択状態で現れ**、クリックで付け外しし、
決定で確定する。したがって**解除専用のverbは無い**(verbの中で全部外して決定すれば消える)。
**確定選択が空でもよい**のはこのためで、空の集合を代入する = その役割を全部外す、である。

**排他は先勝ち**。保持に付いているボディを除外へ回そうとしたら失敗する(逆も同じ。面は
拘束と荷重で同じ規則)。**衝突したものを名指しして** `optimize.RoleConflict` を投げる ——
移したいときは「元の役割のverbで外す → 新しい役割のverbで付ける」の2手になる。これは
意図した仕様であって、ここで自動的に奪い取らない。

**v1の制限: 1文書につき荷重は1種類**(集合代入なので、選ばれた面すべてが同じ大きさ[N]と
向きを共有する)。複数の荷重ケースが要ると分かった時点で拡張する。

**メッシュボディは4つとも受け付けない**。保持/除外はここが `meshbody.reject` で
断り、拘束/荷重は `optimize.face_fingerprint` が断る。ボディや面を**名指しして**
「これをボクセル化できる/これに面がある」と主張する経路だからである
(`done/20260904-1703` の契約②の線)。

保存と復元:

- **ボディの参照は `body_id` だけ**でよい(`body-{uuid}` は形が変わっても動かない)。
- **面の参照には指紋(重心・法線・面積)を一緒に焼き込む**。添字は形が変われば別の面を指す
  ので、照合は `duocad/optimize.py` が配信のときに行い、合わない条件は使わずに `stale` で
  名指しする(**壊れないようにするのではなく、壊れたと気づけるようにする**)。
  **照合が見るのは面積と法線だけ**で、重心は記録するだけである(平行移動で誤検出しないため)。
- **undoは直前の集合を args に控えて戻す**(形状のスナップショット表は使わない ——
  戻す式がある操作なので、保存したログを再生していない文書でも undo が通るほうが素直)。

**OCP / build123d を直接importしない**(幾何は `optimize` → `query` 越し)。
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from .. import optimize
from ..document import BodyId, Document
from ..oplog import Operation, register_verb

VERB_SET_PRESERVE_BODIES = "set_preserve_bodies"
VERB_SET_EXCLUDE_BODIES = "set_exclude_bodies"
VERB_SET_FIXTURES = "set_fixtures"
VERB_SET_LOADS = "set_loads"

#: verb名 -> `OptimizeSetup` のどの役割を置き換えるか。
_BODY_VERB_ROLE = {
    VERB_SET_PRESERVE_BODIES: "preserve_bodies",
    VERB_SET_EXCLUDE_BODIES: "exclude_bodies",
}


# --------------------------------------------------------------------------------------
# 保持ソリッド / 除外ソリッド
# --------------------------------------------------------------------------------------
def set_preserve_bodies(doc: Document, body_ids: Sequence[BodyId]) -> Operation:
    """**保持ソリッド**の集合を `body_ids` そのものに置き換える。"""
    return _assign_bodies(doc, VERB_SET_PRESERVE_BODIES, body_ids)


def set_exclude_bodies(doc: Document, body_ids: Sequence[BodyId]) -> Operation:
    """**除外ソリッド**の集合を `body_ids` そのものに置き換える。"""
    return _assign_bodies(doc, VERB_SET_EXCLUDE_BODIES, body_ids)


def _assign_bodies(doc: Document, verb: str, body_ids: Sequence[BodyId]) -> Operation:
    """ボディの役割を集合代入する(**失敗したら文書にもログにも何も残さない**)。"""
    role = _BODY_VERB_ROLE[verb]
    wanted = _unique([str(body_id) for body_id in body_ids])
    for body_id in wanted:
        body = doc.get_body(body_id)  # 存在しないIDは KeyError(黙って残さない)
        if body.mesh is not None:
            # **メッシュボディは保持/除外に指定できない**(`20260904-1704` の判断)。
            # ボディを名指しして「これをボクセル化して塊に載せろ」と主張する経路であり、
            # 三角形メッシュにはB-repの面が無いのでその主張は成り立たない
            # (`done/20260904-1703` の契約②「名指ししたら断る / 導出した集合からは黙って外す」の
            # 名指し側)。**断り文の実体は `meshbody.reject` の1箇所**。
            # `_apply_bodies` ではなくここに置いてあるのは、`_apply_bodies` を
            # redo / replay も通るためである —— 過去に受け付けたログの再生を、あとから
            # 足した門で落として文書が開けなくなる、という壊し方をしない。
            from .. import meshbody

            meshbody.reject(body, f"{optimize.ROLE_LABEL[role]}に指定する")
    previous = list(getattr(doc.optimize, role))
    _apply_bodies(doc, role, wanted)
    return doc.log.append(verb, {"bodies": wanted, "previous": previous})


def _apply_bodies(doc: Document, role: str, wanted: Sequence[BodyId]) -> None:
    """先勝ちの検査をしてから代入する(do / redo / replay の共通部分)。"""
    setup = doc.optimize
    other = optimize.OPPOSITE_ROLE[role]
    taken = set(getattr(setup, other))
    conflicts = [
        {
            "body": body_id,
            "name": _body_name(doc, body_id),
            "role": other,
            "role_label": optimize.ROLE_LABEL[other],
        }
        for body_id in wanted
        if body_id in taken
    ]
    if conflicts:
        listed = "、".join(f"{item['name']}({item['body']})" for item in conflicts)
        raise optimize.RoleConflict(
            f"{listed} は既に{optimize.ROLE_LABEL[other]}に割り当てられている"
            f"({optimize.ROLE_LABEL[role]}にはできない)。"
            f"先に{optimize.ROLE_LABEL[other]}のverbで外してから割り当て直すこと",
            role,
            conflicts,
        )
    setattr(setup, role, list(wanted))
    setup.touch()


def _body_name(doc: Document, body_id: BodyId) -> str:
    try:
        return doc.get_body(body_id).name
    except KeyError:
        return body_id


# --------------------------------------------------------------------------------------
# 拘束面 / 荷重面
# --------------------------------------------------------------------------------------
def set_fixtures(doc: Document, face_refs: Sequence[dict]) -> Operation:
    """**拘束面**の集合を `face_refs`(面の ShapeRef)そのものに置き換える。howは無い。"""
    conditions = _conditions(doc, face_refs)
    previous = [dict(item) for item in doc.optimize.fixtures]
    _apply_faces(doc, "fixtures", conditions)
    return doc.log.append(
        VERB_SET_FIXTURES, {"faces": conditions, "previous": previous}
    )


def set_loads(
    doc: Document,
    face_refs: Sequence[dict],
    magnitude: Any,
    direction: Any,
) -> Operation:
    """**荷重面**の集合を置き換え、大きさ[N]と向きを1組だけ持たせる。

    向きは `"normal"` / `"-normal"` / `[x, y, z]`(世界座標)。棚受けのように面法線ではなく
    世界の -Z 方向へ受ける荷重があるので、面法線だけでは足りない。
    """
    conditions = _conditions(doc, face_refs)
    value = optimize.normalize_magnitude(magnitude)
    mode, vector = optimize.normalize_direction(direction)
    setup = doc.optimize
    # **入れ子のdictにしない**(`previous: {...}` にしない)。状態配信は操作ログの引数を
    # そのまま配るので、面の指紋のような**量のあるもの**は伏せられる形(リスト)で
    # 平らに置く必要がある(`duocad/view.py` の `_REDACTED_LOG_ARGS`)。
    previous_faces = [dict(item) for item in setup.loads]
    previous_magnitude = setup.load_magnitude
    previous_direction = setup.load_direction
    previous_vector = None if setup.load_vector is None else list(setup.load_vector)
    _apply_faces(doc, "loads", conditions)
    setup.load_magnitude = value
    setup.load_direction = mode
    setup.load_vector = None if vector is None else list(vector)
    return doc.log.append(
        VERB_SET_LOADS,
        {
            "faces": conditions,
            "magnitude": value,
            "direction": mode,
            "vector": None if vector is None else list(vector),
            "previous_faces": previous_faces,
            "previous_magnitude": previous_magnitude,
            "previous_direction": previous_direction,
            "previous_vector": previous_vector,
        },
    )


def _conditions(doc: Document, face_refs: Sequence[dict]) -> List[dict]:
    """面の ShapeRef を FaceCondition(指紋つき)へ均す。**重複は畳む**。

    `kind` が面でない参照は ValueError —— 黙って捨てると、辺を選んだまま拘束を確定した
    人に「0枚に置き換わった」だけが返る。
    """
    conditions: List[dict] = []
    seen: set = set()
    for ref in face_refs:
        if not isinstance(ref, dict) or ref.get("kind") != "face":
            raise ValueError(f"face reference required, got {ref!r}")
        condition = optimize.face_condition(doc, ref)
        key = optimize.face_key(condition)
        if key in seen:
            continue
        seen.add(key)
        conditions.append(condition)
    return conditions


def _apply_faces(doc: Document, role: str, conditions: Sequence[dict]) -> None:
    """先勝ちの検査をしてから代入する(do / redo / replay の共通部分)。"""
    setup = doc.optimize
    other = optimize.OPPOSITE_ROLE[role]
    taken = {optimize.face_key(item) for item in getattr(setup, other)}
    conflicts = [
        {
            "body": condition["body"],
            "index": int(condition["index"]),
            "role": other,
            "role_label": optimize.ROLE_LABEL[other],
        }
        for condition in conditions
        if optimize.face_key(condition) in taken
    ]
    if conflicts:
        listed = "、".join(f"{item['body']} の面 #{item['index']}" for item in conflicts)
        raise optimize.RoleConflict(
            f"{listed} は既に{optimize.ROLE_LABEL[other]}に割り当てられている"
            f"({optimize.ROLE_LABEL[role]}にはできない)。"
            f"先に{optimize.ROLE_LABEL[other]}のverbで外してから割り当て直すこと",
            role,
            conflicts,
        )
    setattr(setup, role, [dict(item) for item in conditions])
    setup.touch()


def _unique(values: Sequence[str]) -> List[str]:
    """順序を保ったまま重複を落とす(選択順は画面の一覧の並びでもある)。"""
    seen: set = set()
    ordered: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


# --------------------------------------------------------------------------------------
# verbの登録(redo / replay と undo)
# --------------------------------------------------------------------------------------
def _do_bodies(document: Document, operation: Operation) -> None:
    role = _BODY_VERB_ROLE[operation.verb]
    _apply_bodies(document, role, list(operation.args.get("bodies") or []))


def _undo_bodies(document: Document, operation: Operation) -> None:
    """直前の集合へ戻す。**排他の検査は通さない** —— 戻し先は以前に成立していた状態で、
    検査を挟むと「行きは通ったのに帰りが通らない」経路が生まれる。"""
    role = _BODY_VERB_ROLE[operation.verb]
    setup = document.optimize
    setattr(setup, role, list(operation.args.get("previous") or []))
    setup.touch()


def _do_fixtures(document: Document, operation: Operation) -> None:
    _apply_faces(document, "fixtures", list(operation.args.get("faces") or []))


def _undo_fixtures(document: Document, operation: Operation) -> None:
    setup = document.optimize
    setup.fixtures = [dict(item) for item in operation.args.get("previous") or []]
    setup.touch()


def _do_loads(document: Document, operation: Operation) -> None:
    args = operation.args
    setup = document.optimize
    _apply_faces(document, "loads", list(args.get("faces") or []))
    setup.load_magnitude = float(args["magnitude"])
    setup.load_direction = str(args["direction"])
    vector: Optional[list] = args.get("vector")
    setup.load_vector = None if vector is None else [float(item) for item in vector]


def _undo_loads(document: Document, operation: Operation) -> None:
    args = operation.args
    setup = document.optimize
    setup.loads = [dict(item) for item in args.get("previous_faces") or []]
    setup.load_magnitude = float(args.get("previous_magnitude") or 0.0)
    setup.load_direction = str(args.get("previous_direction") or "vector")
    vector = args.get("previous_vector")
    setup.load_vector = None if vector is None else [float(item) for item in vector]
    setup.touch()


register_verb(VERB_SET_PRESERVE_BODIES, do=_do_bodies, undo=_undo_bodies)
register_verb(VERB_SET_EXCLUDE_BODIES, do=_do_bodies, undo=_undo_bodies)
register_verb(VERB_SET_FIXTURES, do=_do_fixtures, undo=_undo_fixtures)
register_verb(VERB_SET_LOADS, do=_do_loads, undo=_undo_loads)
