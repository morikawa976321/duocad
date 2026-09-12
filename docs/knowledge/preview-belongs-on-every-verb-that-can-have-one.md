---
name: preview-belongs-on-every-verb-that-can-have-one
description: 判断(2026-09-05・本人) — duocadは「プレビューがつけられる操作には基本的にすべてつける」。残っていた4verb(pattern_rectangular/pattern_circular/move_body/move_sketch_entities)をブラウザだけで描くローカル方式で実装し、move_bodyの「幽霊は本体と重なって見分けが付かない」という旧判断を覆した
created: 2026-09-05
updated: 2026-09-06
---

# duocad: プレビューは「つけられる操作には基本的にすべてつける」

本人の判断(2026-09-05): duocadで**確定前の姿(幽霊)を出せる操作には基本的にすべて出す**。そのうえで、当時 `preview: 'none'` のまま残っていた `pattern_rectangular` / `pattern_circular` / `move_body` / `move_sketch_entities` の4つを名指しで依頼した。

**旧判断の撤回**: `move_body` には「動かす前後で形は同じなので、幽霊を出しても本体と重なって見分けが付かない」という設計判断が宣言(`duocad/catalog.py` の note)に書いてあったが、本人の今回の依頼でこれを覆した。移動量が0でない限り本体と幽霊は別の場所に描かれるので、元をそのまま残して移動先に半透明の幽霊を足せば見分けは付く(サーバ投機実行のプレビューが本物を隠すのとは逆の扱い)。

**方式は4つともローカル(ブラウザだけで描く)**。サーバの投機実行(`previewable=True`)は増やしていない —— 変わるのがスケッチの図形か位置だけなので、投機実行しても「形が変わったBody」が1つも返らず、応答が常に空になる。

ローカル幽霊が無関係な再配信で消える落とし穴は [[a-preview-cleared-by-an-unrelated-broadcast-never-comes-back]]。move系の数値連動プレビューの経緯は [[duocad-move-verbs-gain-live-preview-and-default-zero]]。
