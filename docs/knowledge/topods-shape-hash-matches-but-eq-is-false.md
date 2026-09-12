---
name: topods-shape-hash-matches-but-eq-is-false
description: 実測(2026-09-07・OCCT/OCP) — TopoDS_Shape は hash が一致しても __eq__ が False。素の dict と lru_cache はヒット率0%になり、遅いままメモリだけ増える
created: 2026-09-07
updated: 2026-09-07
---

OCP(cadquery-ocp 7.9)の `TopoDS_Shape` を**キャッシュの鍵にすると1回も当たらない**。
同じ面を `face_of` で2回取り出したときの実測(2026-09-07):

| 見方 | 結果 |
|---|---|
| `hash(a) == hash(b)` | True |
| `a == b` | **False** |
| `{a: v}[b]` | 引けない |

`dict` は「hash が一致 → `__eq__` で確認」の順で引くので、hash だけ一致しても別物として
扱われる。**`functools.lru_cache` を貼っても同じ**で、ヒット率0%のまま呼び出しごとに
エントリが増える —— **答えは正しいので誰も気づかない**。遅さとメモリだけが増える。

回避は `__hash__` = `hash(shape)`、`__eq__` = `shape.IsSame(other)` のラッパを鍵にすること
(duocad `kernel._ShapeKey`)。

**`IsSame` は TShape と Location を見て、Orientation(forward/reversed)を見ない。**
- 位置を変えた形は別物になる(`shape.Moved(TopLoc_Location(trsf))` で 1000mm 動かすと
  `hash` 不一致・`IsSame` False)。「動かした後に古い値を返す」罠は無い。
- 向きだけ違う面は同じ物になる。**向きに依存する値をこの鍵でキャッシュすると静かに壊れる。**
  bbox のように向きに依存しない値だけに使うこと。

関連: [[occt-addoptimal-does-not-get-faster-when-repeated]]
