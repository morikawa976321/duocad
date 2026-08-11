"""述語による選択 — **AIが引数を作る側の経路**(A1・2026-08-11)。

実装計画書§2のアーキテクチャは「AIは述語で引数を作る / 人はポインティングで引数を作る」で
分かれている。人のクリックに当たるのが `duocad.selection.select_face` 等であり、
AIの述語に当たるのがこのモジュールである。§5の例は
`faces where normal·Z > 0.9` / `edges where parallel_to Z and length > 20`。

## 表現形式は**構造化(JSON)**。DSL文字列ではない

    {"kind": "edge",
     "body": "part-3",                       # 省略可。省略時は**可視Body全部**
     "where": [{"op": "parallel_to", "axis": "Z"},
               {"op": "length_gt", "value": 20}]}

DSL文字列(`"edges where parallel_to Z and length > 20"`)を採らないのは、**MCPのツールが
スキーマを持つ**ためである。構造化引数ならスキーマ検証が効き、AIが書式を外したときに
パーサの気分ではなく**その場で明確に落ちる**。文字列DSLは、パースに成功して意味が違う
という最悪の失敗(黙って別の辺を丸める)を作りうる。

`where` は**AND**(全条件を満たすものだけ)。空リストはその種別の全部を意味する。
ORが要るときは述語を2回評価して `modify_selection(add=...)` で足す — 述語言語に
論理演算子を持ち込まない(パーサを作り始めた時点で上の利点が消える)。

## 失敗と0件は別物(項番19・20)

- **一致0件はエラーではない**。空のリストを返し、件数は呼び出し元が数えられる。
  AIが述語を外したことに気づけるように、`Selection.to_dict()` は `count` を返す。
- **不正な述語は ValueError**(未知の `op`・型違い・`kind` に無い `op`)。
  `propose_selection` はこれを捕まえず、**選択を変更しないまま**落とす。
  黙って空集合にすると、上の2つが区別できなくなる。

## 不可視Bodyは走査しない(項番18)

M3.5で決めた「見えないものは選べない」の一貫性。`body` を明示して不可視Bodyを指した
場合も ValueError(`selection._visible_body` と同じ契約)。

## 幾何は `query.py` 越しに取る

このモジュールは **build123d も OCP も import しない**(`tests/test_boundaries.py` が機械検査)。
面の法線・面積は `query.describe_faces`、辺の長さ・向きは `query.describe_edges`、
面と辺の関係は `query.edges_of_face` から取る。**添字は kernel の数え方に揃っている**
(ここがずれると「選んだ辺と違う辺が丸まる」という例外も出ない事故になる。query.py の
`describe_edges` の警告を参照)。
"""
from __future__ import annotations

from typing import List

from .document import Document

#: 述語が対象にできる種別(領域の述語はスケッチ側の話なので持たない)
PREDICATE_KINDS = ("face", "edge")

#: 面の述語で使える `op`
FACE_OPS = ("normal_dot_gt", "area_gt", "kind_is")

#: 辺の述語で使える `op`(`on_boundary_of_face` が**関係述語**)
#:
#: `on_boundary_of_face` の `face` は**Body局所の1始まり添字**である。したがって
#: 可視Bodyが複数あるときは `body` の指定が要る(無ければ ValueError)。
#: 「全Bodyの3番目の面」という解釈は取らない —— 部品ごとに面の数え方は無関係なので、
#: その集合には意味が無い。可視Bodyがちょうど1つなら省略してよい。
EDGE_OPS = ("parallel_to", "length_gt", "length_lt", "kind_is", "on_boundary_of_face")

#: `axis` に書ける名前(3要素のベクトルを直接書いてもよい)
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}

#: `parallel_to` / `normal_dot_gt` の許容(単位ベクトルの内積の絶対値)。
#: STEPから来た面・辺は厳密に軸平行ではないので、素の等値比較にはしない。
PARALLEL_TOLERANCE = 1e-3


def validate(predicate: dict) -> dict:
    """述語を検査して正規化した写しを返す。不正なら ValueError(黙って通さない)。

    検査するもの: `kind` が `PREDICATE_KINDS` にある / `where` がリスト /
    各条件が `op` を持つ / `op` がその `kind` で使える / `op` ごとの引数の型と有無。

    **評価より前に全条件を検査する**。途中まで評価してから落ちると、
    「選択は変えない」(項番20)を守るのが実装依存になる。
    """
    raise NotImplementedError("A2で実装する(スタブ)")


def evaluate(doc: Document, predicate: dict) -> List[dict]:
    """述語に一致する ShapeRef のリストを返す(0件なら空リスト。**エラーにしない**)。

    順序は「`doc.bodies` の順 × 添字の昇順」で安定させる。UIの一覧と、テストの
    期待値の両方が順序に依存できるようにするため。
    """
    raise NotImplementedError("A2で実装する(スタブ)")


def describe_ops() -> dict:
    """MCPのツールスキーマ(F1)へそのまま出せる形の一覧を返す。

    `{"face": [{"op":.., "args": {..}, "doc": ..}, ..], "edge": [...]}`。
    **F1で書き直さずに済むように、opの定義はここ1箇所を正にする**
    (スキーマとバリデータが二重管理になると、片方だけ増えたopが黙って通る)。
    """
    raise NotImplementedError("A2で実装する(スタブ)")
