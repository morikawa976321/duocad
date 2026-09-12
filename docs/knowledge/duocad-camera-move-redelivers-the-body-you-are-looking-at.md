---
name: duocad-camera-move-redelivers-the-body-you-are-looking-at
description: 事実(2026-09-06実測) — duocadでカメラを右ドラッグ1回すると、見ているボディ自身のrevisionが上がって形状が配り直される(実測2→3)。可変解像度のface単位focusがbody_revisionの鍵に入っているため。「その配信は自分と無関係か」をボディIDの交差で判定する設計は常に「関係あり」になり、「この配信で形が変わったか」を判定する口はブラウザ側に無い(applied===nullも本物の形を配る経路がある)
created: 2026-09-06
updated: 2026-09-06
---

# カメラを動かすと、見ているボディ自身が配り直される

duocadの可変解像度([[face-culling-cuts-the-deep-zoom-cost]])は「画面外のボディをズームのたびに配り直さない」ための仕組みとして説明されている(`duocad/view.py::body_recipe` のdocstring)。**その裏は成り立たない** —— **見ているボディは、カメラを動かすたびに配り直される。**

`view.body_revision()` の版の鍵は `(id(body.shape), id(body.mesh), body.visible, body_recipe(body_id))` で、`body_recipe` に**目標精度とface単位の `focus`**(視錐台に入っている/正面を向いている面の集合)が入っている。カメラを回せば正面を向く面が変わるので `focus` が変わり、**形が1ミリも変わっていなくても版が上がって再テセレーションが配信される**。

**実測(2026-09-06、板1つの文書、ビューポート1280x860)**: 中心から右ドラッグ140x60pxを1回 → 約500ms後に状態が1通届き、そのボディの `revision` が **2 → 3**。

## これを踏み外すと何が起きるか

「その配信は自分と無関係か」を**配信に含まれるボディIDとの交差**で判定する設計は、**常に「関係あり」になる**。幽霊が無関係な再配信で消える不具合([[a-preview-cleared-by-an-unrelated-broadcast-never-comes-back]])のタスク本文が提案していた案がこれで、**実測しなければ緑のテストを書いて直っていない実装を納品するところだった**。

**「この配信で形が変わったか」をブラウザ側から判定する口は現状無い。** `applied === null`(何も適用されていない配信)も使えない —— `duocad/net/ws.py` の `_spawn_push()`(MCP/AIがコアを変えたときの `_on_core_change` 経路)と形状最適化の完了配信が、どちらも `applied=None` で**本物の形**を配る。
