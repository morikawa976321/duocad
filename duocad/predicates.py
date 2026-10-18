"""述語による選択 — **AIが引数を作る側の経路**(人のクリックに当たるのは `selection.select_face` 等)。

表現形式は**構造化(JSON)**。DSL文字列ではない:

    {"kind": "edge",
     "body": "part-3",                       # 省略可。省略時は**可視Body全部**
     "where": [{"op": "parallel_to", "axis": "Z"},
               {"op": "length_gt", "value": 20}]}

構造化引数なら**MCPのスキーマ検証が効き**、書式違いはその場で落ちる。文字列DSLは、
パースに成功して意味が違う失敗(黙って別の辺を丸める)を作りうる。

`where` は**AND**(全条件を満たすものだけ)。空リストはその種別の全部を意味する。ORが要るときは
述語を2回評価して `modify_selection(add=...)` で足す —— 述語言語に論理演算子を持ち込まない。

**失敗と0件は別物**:

- **一致0件はエラーではない**。空のリストを返し、件数は呼び出し元が数えられる
  (`Selection.to_dict()` の `count`)。
- **不正な述語は ValueError**(未知の `op`・型違い・`kind` に無い `op`)。`set_selection` は
  これを捕まえず、**選択を変更しないまま**落とす。黙って空集合にすると2つが区別できなくなる。

**不可視Bodyは走査しない**。`body` を明示して不可視Bodyを指した場合も ValueError
(`selection._visible_body` と同じ契約)。

幾何は `query.py` 越しに取る(**build123d も OCP も import しない**)。**添字は kernel の
数え方に揃っている** —— ずれると「選んだ辺と違う辺が丸まる」事故が例外も出ずに起きる。
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from . import kernel, meshbody, query
from .document import Document
from .selection import selectable_bodies

#: 述語が対象にできる種別(領域の述語はスケッチ側の話なので持たない)
PREDICATE_KINDS = ("face", "edge")

#: 述語に書けるキー
PREDICATE_KEYS = ("kind", "body", "where")

#: 面の述語で使える `op`
FACE_OPS = ("normal_dot_gt", "area_gt", "kind_is")

#: 辺の述語で使える `op`(`on_boundary_of_face` が**関係述語**)
#:
#: `on_boundary_of_face` の `face` は**Body局所の1始まり添字**。可視Bodyが複数あるときは
#: `body` の指定が要る(無ければ ValueError)。ちょうど1つなら省略してよい。
EDGE_OPS = ("parallel_to", "length_gt", "length_lt", "kind_is", "on_boundary_of_face")

#: `axis` に書ける名前(3要素のベクトルを直接書いてもよい)
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}

#: `parallel_to` / `normal_dot_gt` の許容(単位ベクトルの内積の絶対値)。
#: STEPから来た面・辺は厳密に軸平行ではないので、素の等値比較にはしない。
PARALLEL_TOLERANCE = 1e-3

_OPS_BY_KIND = {"face": FACE_OPS, "edge": EDGE_OPS}


def validate(predicate: dict) -> dict:
    """述語を検査して正規化した写しを返す。不正なら ValueError(黙って通さない)。

    検査するもの: `kind` が `PREDICATE_KINDS` にある / `where` がリスト /
    各条件が `op` を持つ / `op` がその `kind` で使える / `op` ごとの引数の型と有無。

    **評価より前に全条件を検査する**(途中で落ちても選択を変えないため)。
    """
    if not isinstance(predicate, dict):
        raise ValueError(f"predicate must be a dict, got {type(predicate).__name__}")
    # 知らないキーを黙って捨てない(`body_id` を無視すると全ボディの辺が選ばれたまま成功する)。
    unknown = sorted(set(predicate) - set(PREDICATE_KEYS))
    if unknown:
        raise ValueError(f"unknown predicate key(s): {unknown} (allowed: {', '.join(PREDICATE_KEYS)})")
    if "kind" not in predicate:
        raise ValueError("predicate is missing 'kind'")
    kind = predicate["kind"]
    if kind not in PREDICATE_KINDS:
        # 実測で、スケッチの領域を述語で選ぼうとして `kind: "region"` を繰り返す。選び方を返す。
        raise ValueError(
            f"unknown predicate kind: {kind!r} (a predicate selects {' or '.join(PREDICATE_KINDS)}; "
            'a sketch region is selected with refs=[{"sketch": sketch_id, "index": 1}])'
        )
    if "where" not in predicate:
        # 実測で、「全部の辺」を選ぶときに `where` を省く書き方が繰り返し出る。直し方を返す。
        raise ValueError(f'predicate is missing \'where\' (use "where": [] to select every {kind})')
    where = predicate["where"]
    if not isinstance(where, list):
        raise ValueError(f"'where' must be a list, got {type(where).__name__}")

    normalized_where = [_normalize_condition(kind, condition) for condition in where]
    result = {"kind": kind, "where": normalized_where}
    if "body" in predicate:
        result["body"] = predicate["body"]
    return result


def evaluate(doc: Document, predicate: dict) -> List[dict]:
    """述語に一致する ShapeRef のリストを返す(0件なら空リスト。**エラーにしない**)。

    順序は「`doc.bodies` の順 × 添字の昇順」。
    """
    normalized = validate(predicate)
    bodies = _target_bodies(doc, normalized.get("body"), normalized["where"])
    kind = normalized["kind"]
    matched: List[dict] = []
    for body in bodies:
        if kind == "face":
            matched.extend(_match_faces(body, normalized["where"]))
        else:
            matched.extend(_match_edges(body, normalized["where"]))
    return matched


def describe_ops() -> dict:
    """MCPのツールスキーマへそのまま出せる形の一覧を返す。

    `{"face": [{"op":.., "args": {..}, "doc": ..}, ..], "edge": [...]}`。
    opの定義はここ1箇所を正にする(二重管理だと片方だけ増えたopが黙って通る)。
    """
    return {
        "face": [
            {
                "op": "normal_dot_gt",
                "args": {"axis": "X|Y|Z|[x,y,z]", "value": "number"},
                "doc": "面の外向き法線と軸の内積が value より大きい",
            },
            {
                "op": "area_gt",
                "args": {"value": "number"},
                "doc": "面積が value より大きい(厳密な不等号)",
            },
            {
                "op": "kind_is",
                "args": {"value": "|".join(kernel.FACE_KINDS)},
                "doc": "面の種別が value と一致する(kernel.classify_face)",
            },
        ],
        "edge": [
            {
                "op": "parallel_to",
                "args": {"axis": "X|Y|Z|[x,y,z]"},
                "doc": "直線の辺が軸に平行(単位方向の内積の絶対値が 1±PARALLEL_TOLERANCE)。円弧・B-spline など直線でない辺は対象外",
            },
            {
                "op": "length_gt",
                "args": {"value": "number"},
                "doc": "長さが value より大きい(厳密な不等号。境界は含まない)",
            },
            {
                "op": "length_lt",
                "args": {"value": "number"},
                "doc": "長さが value より小さい(厳密な不等号。境界は含まない)",
            },
            {
                "op": "kind_is",
                "args": {"value": "|".join(kernel.EDGE_KINDS)},
                "doc": "辺の種別が value と一致する(kernel.classify_edge)",
            },
            {
                "op": "on_boundary_of_face",
                "args": {"face": "int (Body局所・1始まり)"},
                "doc": "指定面の外周(outer wire)を成す辺。穴の縁は含まない",
            },
        ],
    }


# --------------------------------------------------------------------------------------
def _normalize_condition(kind: str, condition: dict) -> dict:
    if not isinstance(condition, dict):
        raise ValueError(f"where condition must be a dict, got {type(condition).__name__}")
    if "op" not in condition:
        raise ValueError("where condition is missing 'op'")
    op = condition["op"]
    allowed = _OPS_BY_KIND[kind]
    if op not in allowed:
        raise ValueError(f"op {op!r} is not valid for kind {kind!r}")

    if op == "normal_dot_gt":
        if "axis" not in condition:
            raise ValueError("condition is missing 'axis'")
        axis = _normalize_axis(condition["axis"])
        value = _require_number(condition, "value")
        return {"op": op, "axis": list(axis), "value": value}
    if op == "area_gt":
        return {"op": op, "value": _require_number(condition, "value")}
    if op == "kind_is":
        if "value" not in condition:
            raise ValueError("condition is missing 'value'")
        value = condition["value"]
        if not isinstance(value, str):
            raise ValueError(f"'value' for kind_is must be a str, got {type(value).__name__}")
        known = kernel.FACE_KINDS if kind == "face" else kernel.EDGE_KINDS
        if value not in known:
            raise ValueError(f"unknown kind value: {value!r}")
        return {"op": op, "value": value}
    if op == "parallel_to":
        if "axis" not in condition:
            raise ValueError("condition is missing 'axis'")
        axis = _normalize_axis(condition["axis"])
        return {"op": op, "axis": list(axis)}
    if op in ("length_gt", "length_lt"):
        return {"op": op, "value": _require_number(condition, "value")}
    if op == "on_boundary_of_face":
        if "face" not in condition:
            raise ValueError("on_boundary_of_face is missing 'face'")
        face = condition["face"]
        if isinstance(face, bool) or not isinstance(face, int):
            raise ValueError(f"'face' must be an int, got {type(face).__name__}")
        return {"op": op, "face": int(face)}
    raise ValueError(f"unhandled op: {op!r}")


def _require_number(condition: dict, name: str) -> float:
    if name not in condition:
        raise ValueError(f"condition is missing '{name}'")
    return _finite_number(condition[name], f"'{name}'")


def _finite_number(value, what: str) -> float:
    """真偽値・文字列でない有限の数だけを通す(`float()` 任せだと `"1"`・`True` が数として通り、`inf`・`nan` は比較が常に偽/真になる)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{what} is too large to be a number: {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{what} must be finite, got {value!r}")
    return number


def _normalize_axis(axis) -> Tuple[float, float, float]:
    if isinstance(axis, str):
        if axis not in AXES:
            raise ValueError(f"unknown axis: {axis!r}")
        return AXES[axis]
    if isinstance(axis, (list, tuple)) and len(axis) == 3:
        vector = tuple(_finite_number(component, "axis component") for component in axis)
        # 2乗の和は 1e154 を超える成分で溢れる(OverflowError)。`hypot` は溢れない。
        magnitude = math.hypot(*vector)
        if magnitude <= 0:
            raise ValueError("axis vector must be non-zero")
        if not math.isfinite(magnitude):
            raise ValueError(f"axis vector is too large to normalize: {axis!r}")
        return (vector[0] / magnitude, vector[1] / magnitude, vector[2] / magnitude)
    raise ValueError(f"axis must be X/Y/Z or a 3-vector, got {axis!r}")


def _target_bodies(doc: Document, body_id, where: Sequence[dict]) -> list:
    """走査対象のBody列。不可視は除外。明示指定の契約は `_visible_body` と同じ。

    **メッシュボディは「名指ししたら断る / 導出した集合からは黙って外す」**。

    - `body` を明示されたら断る(`meshbody.reject`)。黙って0件を返すと、AIは「条件に合う辺が
      無かった」と読んで条件を緩める側へ動く(実際には辺という概念が無い)。
    - `body` を省略されたら黙って外す。走査集合は導出で、断ると最適化結果のメッシュが
      1つあるだけで述語による選択が丸ごと使えなくなる。
    """
    from .selection import _visible_body

    if body_id is not None:
        body = _visible_body(doc, body_id)
        if body.mesh is not None:
            meshbody.reject(body, "述語で面や辺を絞る")
        return [body]

    # メッシュボディを先に外してから単一Body検査へ掛ける(後だと可視メッシュ1つで断られる)。
    mesh_ids = {body.id for body in doc.bodies if body.mesh is not None}
    visible_ids = [
        candidate for candidate in selectable_bodies(doc) if candidate not in mesh_ids
    ]
    if _needs_single_body(where) and len(visible_ids) != 1:
        # 0件と複数件で言うことを変える(0件は `body` を明示しても解決しない)。
        raise ValueError(
            "on_boundary_of_face needs a visible body to count faces on, "
            "but no body is visible (set_visible で表示してから選ぶこと)"
            if not visible_ids
            else "on_boundary_of_face needs an explicit 'body' when multiple bodies are "
            f"visible (visible: {visible_ids})"
        )
    id_set = set(visible_ids)
    return [body for body in doc.bodies if body.id in id_set]


def _needs_single_body(where: Sequence[dict]) -> bool:
    return any(condition["op"] == "on_boundary_of_face" for condition in where)


def _match_faces(body, where: Sequence[dict]) -> List[dict]:
    faces = query.describe_faces(body.shape)
    matched = []
    for face in faces:
        if all(_face_matches(face, condition) for condition in where):
            matched.append(
                {
                    "body": body.id,
                    "kind": "face",
                    "index": face["index"],
                    "provenance": None,
                }
            )
    return matched


def _match_edges(body, where: Sequence[dict]) -> List[dict]:
    edges = query.describe_edges(body.shape)
    boundary_sets = _boundary_sets(body, where)
    matched = []
    for edge in edges:
        if all(_edge_matches(edge, condition, boundary_sets) for condition in where):
            matched.append(
                {
                    "body": body.id,
                    "kind": "edge",
                    "index": edge["index"],
                    "provenance": None,
                }
            )
    return matched


def _boundary_sets(body, where: Sequence[dict]) -> dict:
    """関係述語の面添字 → 外周辺の集合。評価前に全件を解決して範囲外を落とす。"""
    resolved = {}
    for condition in where:
        if condition["op"] != "on_boundary_of_face":
            continue
        face_index = condition["face"]
        try:
            resolved[face_index] = set(query.edges_of_face(body.shape, face_index))
        except ValueError as error:
            # 有効な範囲まで言う(`select_*` と揃える)。
            raise ValueError(
                f"on_boundary_of_face face out of range: {face_index} "
                f"(1..{kernel.face_count(body.shape)} on body {body.id!r})"
            ) from error
    return resolved


def _face_matches(face: dict, condition: dict) -> bool:
    op = condition["op"]
    if op == "normal_dot_gt":
        axis = condition["axis"]
        normal = face["normal"]
        return (
            normal[0] * axis[0] + normal[1] * axis[1] + normal[2] * axis[2]
            > condition["value"]
        )
    if op == "area_gt":
        return face["area"] > condition["value"]
    if op == "kind_is":
        return face["kind"] == condition["value"]
    raise ValueError(f"unhandled face op: {op!r}")


def _edge_matches(edge: dict, condition: dict, boundary_sets: dict) -> bool:
    op = condition["op"]
    if op == "parallel_to":
        return _is_parallel(edge, condition["axis"])
    if op == "length_gt":
        return edge["length"] > condition["value"]
    if op == "length_lt":
        return edge["length"] < condition["value"]
    if op == "kind_is":
        return edge["kind"] == condition["value"]
    if op == "on_boundary_of_face":
        return edge["index"] in boundary_sets[condition["face"]]
    raise ValueError(f"unhandled edge op: {op!r}")


def _is_parallel(edge: dict, axis: Sequence[float]) -> bool:
    # 弦(始点→終点)は直線のときだけ辺の向き。半円・アーチは弦が軸に揃っても直線の辺ではない。
    if edge["kind"] != "line":
        return False
    start, end = edge["start"], edge["end"]
    direction = (end[0] - start[0], end[1] - start[1], end[2] - start[2])
    magnitude = math.sqrt(direction[0] ** 2 + direction[1] ** 2 + direction[2] ** 2)
    if magnitude <= 0:
        return False
    unit = (direction[0] / magnitude, direction[1] / magnitude, direction[2] / magnitude)
    dot = abs(unit[0] * axis[0] + unit[1] * axis[1] + unit[2] * axis[2])
    return abs(dot - 1.0) <= PARALLEL_TOLERANCE
