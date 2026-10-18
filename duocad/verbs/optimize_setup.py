"""verb `set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` / `set_loads`
—— 形状最適化(第3のモード)の**条件を置く4つのverb**。

**最適化そのものとは接続しない**(実行verbは無い)。ここが作るのは「条件を表現でき、保存でき、状態として
配れる」ところまで。

**保持/除外/拘束の3つは「集合代入」**。「この役割の集合を、いま確定している選択集合そのもので置き換える」。
verbを起動すると既にその役割が付いている対象が選択状態で現れ、クリックで付け外しし、決定で確定する。
選択が空でもよい(空の集合を代入する = その役割を全部外す)。

**荷重だけは上書き代入(`set_loads`)**。大きさ[N]と向きを面ごとに持つので、集合代入にすると「面Bの20Nを
直すつもりで開いて決定した瞬間、面Aの50Nも上書きされる」事故が例外も警告も出ずに起きる。したがって:

- `set_loads` は選択した面だけに大きさと向きを焼き、選択外の荷重面は触らない。
- 起動時のプリセレクトをしない(画面側の規律。`web/src/App.svelte`)。
- 空の選択で確定しても何も外れないので、解除専用verb `clear_loads` を置く(3つの集合代入verbには無い)。

**排他は先勝ち**。保持に付いているボディを除外へ回そうとしたら失敗する(逆も同じ。面は拘束と荷重で
同じ規則)。衝突したものを名指しして `optimize.RoleConflict` を投げる。移したいときは「元の役割のverbで
外す → 新しい役割のverbで付ける」の2手(自動的に奪い取らない)。

**拘束面・荷重面は保持ソリッドの面に限る**。保持でないボディの面には置けず、条件が載っているボディを
保持から外すこともできない(`_require_preserved_bodies` / `_require_conditions_stay_on_preserved`)。

**荷重は「同時にかかる1つの荷重系」**。面ごとに違う大きさ・向きを持てるが、最適化はそれらを足し合わせた
1つの荷重として解く(`optimize_run.build_conditions`)。別シナリオ(複数荷重ケース)は表現できない
(合成するとどちらの単独荷重にも耐えない形が出る)。

**メッシュボディは4つとも受け付けない**。保持/除外はここが `meshbody.reject` で、拘束/荷重は
`optimize.face_fingerprint` が断る(ボディや面を名指しして「これをボクセル化できる/これに面がある」と
主張する経路のため。`docs/contracts/ui-optimize-integration.md`「編集 verb を効かせない」)。

保存と復元:

- ボディの参照は `body_id` だけ(形が変わっても動かない)。
- 面の参照には指紋(重心・法線・面積)を一緒に焼き込む。添字は形が変われば別の面を指すので、照合は
  `duocad/optimize.py` が配信のときに行い、合わない条件は使わず `stale` で名指しする。照合が見るのは
  面積と法線だけで、重心は記録するだけ(平行移動で誤検出しないため)。
- undoは直前の集合を args に控えて戻す(戻す式がある操作なので形状のスナップショット表は使わない。
  保存したログを再生していない文書でも通る)。
"""
from __future__ import annotations

from typing import Any, List, Sequence

from .. import optimize
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry

VERB_SET_PRESERVE_BODIES = "set_preserve_bodies"
VERB_SET_EXCLUDE_BODIES = "set_exclude_bodies"
VERB_SET_FIXTURES = "set_fixtures"
VERB_SET_LOADS = "set_loads"
VERB_CLEAR_LOADS = "clear_loads"

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
    # 順序を保ったまま重複を落とす(選択順は画面の一覧の並びでもある)
    wanted = list(dict.fromkeys(str(body_id) for body_id in body_ids))
    for body_id in wanted:
        body = doc.get_body(body_id)  # 存在しないIDは KeyError(黙って残さない)
        if body.mesh is not None:
            # メッシュボディは保持/除外に指定できない(断り文の実体は `meshbody.reject`)。ここに置くのは、
            # `_apply_bodies` を redo / replay も通るため(過去に受け付けたログの再生を後から足した門で
            # 落として文書が開けなくなる、という壊し方をしない)。理由の詳細はモジュールdocstring。
            from .. import meshbody

            meshbody.reject(body, f"{optimize.ROLE_LABEL[role]}に指定する")
    if role == "preserve_bodies":
        _require_conditions_stay_on_preserved(doc, wanted)
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
        raise _role_conflict(listed, role, other, conflicts)
    setattr(setup, role, list(wanted))
    setup.touch()


def _role_conflict(listed: str, role: str, other: str, conflicts: List[dict]) -> "optimize.RoleConflict":
    """`listed` が既に反対の役割 `other` にあるので `role` にできない、という例外。"""
    return optimize.RoleConflict(
        f"{listed} は既に{optimize.ROLE_LABEL[other]}に割り当てられている"
        f"({optimize.ROLE_LABEL[role]}にはできない)。"
        f"先に{optimize.ROLE_LABEL[other]}のverbで外してから割り当て直すこと",
        conflicts,
    )


def _body_name(doc: Document, body_id: BodyId) -> str:
    try:
        return doc.get_body(body_id).name
    except KeyError:
        return body_id


def _require_preserved_bodies(doc: Document, role: str, conditions: Sequence[dict]) -> None:
    """条件面は保持ソリッドの面に限る。

    保持でないボディの面に置くと、探索がそのボディの材料を削れる。さらに確定表は
    半径1要素の球で「生きている節点」を拾う(`topopt/anchor.py` の `build_anchors`)ので、
    元のボディが1要素も取れていなくても**周りの材料の節点**へ荷重が載り、条件が別の材料へ
    黙って移る(2026-09-18 実測: 54ケース中13件。うち数件は1節点だけに載った)。

    `_apply_faces` ではなくverbの入口に置く理由は `_assign_bodies` のメッシュボディの門と同じ。
    """
    preserved = set(doc.optimize.preserve_bodies)
    outside = [item for item in conditions if item["body"] not in preserved]
    if not outside:
        return
    listed = "、".join(
        f"{_body_name(doc, item['body'])} の面 #{int(item['index'])}" for item in outside
    )
    raise ValueError(
        f"{listed} は保持ソリッドの面ではない"
        f"({optimize.ROLE_LABEL[role]}は保持ソリッドの面にしか置けない)。"
        "先に set_preserve_bodies でそのボディを保持ソリッドにすること"
    )


def _require_conditions_stay_on_preserved(doc: Document, wanted: Sequence[BodyId]) -> None:
    """保持ソリッドから外そうとしているボディに条件面が残っていたら断る。

    `_require_preserved_bodies` の裏側 —— こちらを置かないと、条件を付けたあとで保持を
    外すだけで「保持でないボディの条件面」が作れてしまう。**自動で条件を落とさない**のは
    排他(`RoleConflict`)と同じ理由で、外すなら2手にする。
    """
    keeping = {str(body_id) for body_id in wanted}
    orphans = [
        (role, item)
        for role in optimize.FACE_ROLES
        for item in getattr(doc.optimize, role)
        if item["body"] not in keeping
    ]
    if not orphans:
        return
    listed = "、".join(
        f"{_body_name(doc, item['body'])} の面 #{int(item['index'])}"
        f"({optimize.ROLE_LABEL[role]})"
        for role, item in orphans
    )
    raise ValueError(
        f"{listed} が残っているので保持ソリッドから外せない"
        "(条件面は保持ソリッドの面にしか置けない)。"
        "先に set_fixtures / clear_loads でその条件を外すこと"
    )


# --------------------------------------------------------------------------------------
# 拘束面 / 荷重面
# --------------------------------------------------------------------------------------
def set_fixtures(doc: Document, face_refs: Sequence[dict]) -> Operation:
    """**拘束面**の集合を `face_refs`(面の ShapeRef)そのものに置き換える。howは無い。"""
    conditions = _conditions(doc, face_refs)
    _require_preserved_bodies(doc, "fixtures", conditions)
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
    """**選んだ面だけ**に大きさ[N]と向きを焼く(上書き代入。選択外の荷重面は触らない)。

    向きは `"normal"` / `"-normal"` / `[x, y, z]`(世界座標)。棚受けのように面法線ではなく
    世界の -Z 方向へ受ける荷重があるので、面法線だけでは足りない。

    **面を1枚も選ばずに確定するのは失敗にする。** 上書き代入なので空の選択は「何も起きない」
    であり、集合代入だった頃の「空で確定 = 全部外す」を期待した人が黙って何も外れないまま
    次へ進む。外すのは `clear_loads` である。
    """
    refs = list(face_refs)
    if not refs:
        raise ValueError(
            "荷重をかける面が1枚も選ばれていない"
            "(荷重を外すには clear_loads を使うこと。set_loads は選んだ面にだけ焼く)"
        )
    conditions = [
        optimize.load_condition(doc, ref, magnitude, direction)
        for ref in _face_refs(refs)
    ]
    _require_preserved_bodies(doc, "loads", conditions)
    # **入れ子のdictにしない**(`previous: {...}` にしない)。状態配信は操作ログの引数を
    # そのまま配るので、面の指紋のような**量のあるもの**は伏せられる形(リスト)で
    # 平らに置く必要がある(`duocad/view.py` の `_REDACTED_LOG_ARGS`)。
    previous_faces = [dict(item) for item in doc.optimize.loads]
    merged = _merge_loads(previous_faces, conditions)
    _apply_faces(doc, "loads", merged)
    return doc.log.append(
        VERB_SET_LOADS, {"faces": merged, "previous_faces": previous_faces}
    )


def clear_loads(doc: Document, face_refs: Sequence[dict]) -> Operation:
    """**荷重を外す**。選んだ面を荷重面から落とす(**空の選択なら全部外す**)。

    `set_loads` を上書き代入にした代償として置いてある解除専用verb(モジュール docstring)。
    荷重が付いていない面を指しても失敗しない —— 「外す」は結果を言っているので、
    既に外れている面を指したときに例外を出すと、複数選択で1枚だけ外れていたら全部落ちる。
    """
    previous_faces = [dict(item) for item in doc.optimize.loads]
    refs = list(face_refs)
    if refs:
        dropped = {optimize.face_key(ref) for ref in _face_refs(refs)}
        remaining = [
            item for item in previous_faces if optimize.face_key(item) not in dropped
        ]
    else:
        remaining = []
    _apply_faces(doc, "loads", remaining)
    return doc.log.append(
        VERB_CLEAR_LOADS, {"faces": remaining, "previous_faces": previous_faces}
    )


def _merge_loads(previous: Sequence[dict], incoming: Sequence[dict]) -> List[dict]:
    """上書き代入。**選択に入っている面だけ**を新しい値で置き換え、並びは既存を先に保つ。

    同じ面を2度指すことはない(`_face_refs` が重複を畳む)ので、突き合わせは面の鍵だけでよい。
    """
    replacing = {optimize.face_key(item): item for item in incoming}
    merged: List[dict] = []
    for item in previous:
        key = optimize.face_key(item)
        merged.append(dict(replacing.pop(key)) if key in replacing else dict(item))
    for item in incoming:
        if optimize.face_key(item) in replacing:
            merged.append(dict(item))
    return merged


def _face_refs(face_refs: Sequence[dict]) -> List[dict]:
    """面の ShapeRef の並びを検査して**重複を畳む**(順序は保つ)。

    `kind` が面でない参照は ValueError —— 黙って捨てると、辺を選んだまま確定した人に
    「0枚だった」だけが返る。
    """
    picked: List[dict] = []
    seen: set = set()
    for ref in face_refs:
        if not isinstance(ref, dict) or ref.get("kind") != "face":
            raise ValueError(f"face reference required, got {ref!r}")
        key = optimize.face_key(ref)
        if key in seen:
            continue
        seen.add(key)
        picked.append(ref)
    return picked


def _conditions(doc: Document, face_refs: Sequence[dict]) -> List[dict]:
    """面の ShapeRef を FaceCondition(指紋つき)へ均す。**重複は畳む**。"""
    return [optimize.face_condition(doc, ref) for ref in _face_refs(face_refs)]


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
        raise _role_conflict(listed, role, other, conflicts)
    setattr(setup, role, [dict(item) for item in conditions])
    setup.touch()


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


def _restore_faces(document: Document, role: str, previous) -> None:
    """条件面(`fixtures` / `loads`)を undo で元の並びへ戻す。"""
    setup = document.optimize
    setattr(setup, role, [dict(item) for item in previous or []])
    setup.touch()


def _undo_fixtures(document: Document, operation: Operation) -> None:
    _restore_faces(document, "fixtures", operation.args.get("previous"))


def _do_loads(document: Document, operation: Operation) -> None:
    _apply_faces(document, "loads", [dict(item) for item in operation.args.get("faces") or []])


def _undo_loads(document: Document, operation: Operation) -> None:
    # 鍵名は `previous_faces`(保存した文書の操作ログにこの名前で載っている。理由は `_do_loads` の上)。
    _restore_faces(document, "loads", operation.args.get("previous_faces"))


register_verb(VERB_SET_PRESERVE_BODIES, do=_do_bodies, undo=_undo_bodies)
register_verb(VERB_SET_EXCLUDE_BODIES, do=_do_bodies, undo=_undo_bodies)
register_verb(VERB_SET_FIXTURES, do=_do_fixtures, undo=_undo_fixtures)
register_verb(VERB_SET_LOADS, do=_do_loads, undo=_undo_loads)
# **`clear_loads` は `set_loads` と同じ do/undo を使う** —— どちらも操作ログには
# 「確定後の荷重集合そのもの」を書くので、再生も取り消しも集合の代入で足りる。
register_verb(VERB_CLEAR_LOADS, do=_do_loads, undo=_undo_loads)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
# **選択が空でもよい** —— 空の代入が「その役割を全部外す」を意味する。`changed_bodies` は
# 返さない(形は1つも変わらない。役割の色は配信される `state.optimize` から描く)。
@entry(VERB_SET_PRESERVE_BODIES)
def _enter_set_preserve_bodies(document: Document) -> dict:
    body_ids = selection.require_selected_bodies(document)
    return {"bodies": list(set_preserve_bodies(document, body_ids).args["bodies"])}


@entry(VERB_SET_EXCLUDE_BODIES)
def _enter_set_exclude_bodies(document: Document) -> dict:
    body_ids = selection.require_selected_bodies(document)
    return {"bodies": list(set_exclude_bodies(document, body_ids).args["bodies"])}


@entry(VERB_SET_FIXTURES)
def _enter_set_fixtures(document: Document) -> dict:
    faces = selection.require_selected_faces(document)
    return {"face_count": len(set_fixtures(document, faces).args["faces"])}


@entry(VERB_SET_LOADS)
def _enter_set_loads(document: Document, magnitude: Any, direction: Any) -> dict:
    """`face_count` は**確定後の荷重面の総数**であって、この操作で焼いた枚数(`applied_count`)ではない
    (画面は「いま何枚に荷重が付いているか」を出すのでこちらが要る)。"""
    faces = selection.require_selected_faces(document)
    operation = set_loads(document, faces, magnitude, direction)
    mode, vector = optimize.normalize_direction(direction)
    return {
        "face_count": len(operation.args["faces"]),
        "applied_count": len(faces),
        "magnitude": optimize.normalize_magnitude(magnitude),
        "direction": mode,
        "direction_vector": None if vector is None else list(vector),
    }


@entry(VERB_CLEAR_LOADS)
def _enter_clear_loads(document: Document) -> dict:
    faces = selection.require_selected_faces(document)
    return {"face_count": len(clear_loads(document, faces).args["faces"])}
