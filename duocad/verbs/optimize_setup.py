"""verb `set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` / `set_loads`
—— 形状最適化(第3のモード)の**条件を置く4つのverb**。

**最適化そのものとは接続しない**(実行verbは無い)。ここが作るのは「条件を表現でき、
保存でき、状態として配れる」ところまでである。

**保持/除外/拘束の3つは「集合代入」**である。「1つ足す」ではなく「この役割の集合を、いま
確定している選択集合そのもので置き換える」。操作の流れがその裏返しになっている ——
verbを起動すると**既にその役割が付いている対象が選択状態で現れ**、クリックで付け外しし、
決定で確定する。**確定選択が空でもよい**のはこのためで、空の集合を代入する = その役割を
全部外す、である。

**荷重だけは集合代入ではない(`set_loads` は上書き代入)**。大きさ[N]と向きを**面ごとに**
持つので、集合代入にすると「面Bの20Nを直すつもりで開いて決定した瞬間、面Aの50Nも同じ値に
上書きされる」という、例外も警告も出ない事故が構造的に避けられない。したがって荷重では:

- `set_loads` は**選択した面だけ**に大きさと向きを焼き、選択に入っていない荷重面は触らない。
- 起動時のプリセレクトを**しない**(画面側の規律。`web/src/App.svelte`)。触っていない面が
  選択に入りようがなくなるので、上の事故が起きる経路そのものが消える。
- 空の選択で確定しても何も外れないので、**`clear_loads`(解除専用verb)を置く**。
  3つの集合代入verbには解除専用verbは無い —— 荷重だけの例外であり、
  「上書き代入にした以上、外す手段が別に要る」という代償である。

**排他は先勝ち**。保持に付いているボディを除外へ回そうとしたら失敗する(逆も同じ。面は
拘束と荷重で同じ規則)。**衝突したものを名指しして** `optimize.RoleConflict` を投げる ——
移したいときは「元の役割のverbで外す → 新しい役割のverbで付ける」の2手になる。これは
意図した仕様であって、ここで自動的に奪い取らない。

**荷重は「同時にかかる1つの荷重系」である。** 面ごとに違う大きさ・向きを持てるが、
最適化はそれらを**足し合わせた1つの荷重**として解く(`optimize_run.build_conditions`)。
「あるときは面A、別のときは面B」という**別シナリオ(複数荷重ケース)は表現できない** ——
それを合成すると、どちらの単独荷重にも耐えない形が出る。要ると分かった時点で拡張する。

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
    _apply_faces(document, "loads", _loads_from_args(args, "faces", ""))


def _undo_loads(document: Document, operation: Operation) -> None:
    setup = document.optimize
    setup.loads = _loads_from_args(operation.args, "previous_faces", "previous_")
    setup.touch()


def _loads_from_args(args: dict, faces_key: str, prefix: str) -> List[dict]:
    """操作ログの引数 → LoadCondition の並び。**古い形のログもここで受ける**。

    2026-09-06までの `set_loads` は、大きさ[N]と向きを面ではなく**引数の直下**に1組だけ
    持っていた(1文書に1荷重だった頃の形)。過去に受け付けたログの再生を、あとから足した
    型で落として文書が開けなくなる、という壊し方をしない —— 古い形は「全ての面が同じ
    大きさ・向きを持つ」という**新しい形の特別な場合**なので、ここで焼き直せる。
    """
    faces = [dict(item) for item in args.get(faces_key) or []]
    magnitude = args.get(f"{prefix}magnitude")
    if magnitude is None:
        return faces
    direction = str(args.get(f"{prefix}direction") or "vector")
    vector: Optional[list] = args.get(f"{prefix}vector")
    for face in faces:
        face["magnitude"] = float(magnitude)
        face["direction"] = direction
        face["vector"] = None if vector is None else [float(item) for item in vector]
    return faces


register_verb(VERB_SET_PRESERVE_BODIES, do=_do_bodies, undo=_undo_bodies)
register_verb(VERB_SET_EXCLUDE_BODIES, do=_do_bodies, undo=_undo_bodies)
register_verb(VERB_SET_FIXTURES, do=_do_fixtures, undo=_undo_fixtures)
register_verb(VERB_SET_LOADS, do=_do_loads, undo=_undo_loads)
# **`clear_loads` は `set_loads` と同じ do/undo を使う** —— どちらも操作ログには
# 「確定後の荷重集合そのもの」を書くので、再生も取り消しも集合の代入で足りる。
register_verb(VERB_CLEAR_LOADS, do=_do_loads, undo=_undo_loads)
