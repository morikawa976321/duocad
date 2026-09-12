---
name: occt-provenance-two-routes-one-adapter
description: 事実(2026-08-10実測) — OCCTの由来の取り出し方は操作で2経路(ブーリアンはHistory()経由のBRepTools_History、フィレット等のMakeShape系は自分がModified/Generated/IsDeletedを持つ)。hasattr(builder,"History")の1分岐で吸収でき、両経路とも同じ正規化形に落ちることを実測した
created: 2026-08-10
updated: 2026-08-10
---

# OCCTの由来は2経路あるが、正規化アダプタ1つで吸収できる

[[occt-provenance-enables-persistent-naming]] で「由来マップはOCPから直接叩ける」ことは
確認済みだが、**取り出し方が操作ごとに違う**点は実装の分岐として効く。
`cadquery-ocp-novtk==7.9.3.1.1` で両経路を実際に通して確かめた。

| 操作 | 由来の持ち主 | 持っているメソッド |
|---|---|---|
| ブーリアン `BRepAlgoAPI_Cut` 等 | `builder.History()` が返す `BRepTools_History` | `Modified` / `Generated` / **`IsRemoved`** |
| フィレット `BRepFilletAPI_MakeFillet` 等の `BRepBuilderAPI_MakeShape` 系 | builder 自身 | `Modified` / `Generated` / **`IsDeleted`** |

**削除判定のメソッド名が違う**（`IsRemoved` と `IsDeleted`）のが地味な罠で、
片方だけ実装すると「削除された部分形状を取りこぼす」形で静かに壊れる。

## 吸収の仕方

`hasattr(builder, "History")` の1分岐で由来の持ち主を決め、以降は同じコードで
`Modified` / `Generated` を引き、削除判定だけ2つの名前を順に試せばよい。
20mm立方体を入力にした実測（面・辺を重複除去して列挙し、それぞれ問い合わせた結果）:

```
BRepAlgoAPI_Cut (History経路)        : modified 15 / generated 8 / deleted 12
BRepFilletAPI_MakeFillet (直持ち経路) : modified  8 / generated 1 / deleted  8
由来が1件も無い場合                   : None
```

**ブーリアンは `Modified` が主・フィレットは `Generated` が少ない**という違いはあるが、
戻り値の形は同じ `{modified, generated, deleted}` に落ちる。
つまり **verb側にこの分岐を書く必要はなく、カーネル層の関数1つに閉じ込められる**。
verbが増えるたびに同じ分岐が写経されるのを防げるので、由来を使う設計では
**この正規化関数の置き場所をday1に決めておく**のが安い。

## 永続化するときの注意

正規化した結果は `TopoDS_Shape` を直接含む**メモリ上の形**であり、そのままJSONにはできない。
操作ログをJSONで持つ設計では、**永続化用の射影（添字や安定名などJSON化できる表現）を
別に用意する**必要がある。由来を「まだ空スロット」で運用している間は露呈しないので、
最初に由来を実際に積む時点で必ず踏む。

関連: [[occt-provenance-enables-persistent-naming]] / [[build123d-face-index-breaks-on-fillet]]
