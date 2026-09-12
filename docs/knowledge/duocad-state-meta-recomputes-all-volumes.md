---
name: duocad-state-meta-recomputes-all-volumes
description: 【解消済み2026-08-26】duocadの state_meta() が配信のたび全ボディの体積を測り直していた問題。body_meta に _volume_cache を追加して解消(state_meta() 1回693ms→1.1ms、全非表示19.1s→0.03s)
created: 2026-08-26
updated: 2026-08-26
---

**2026-08-26に解消済み**: `duocad/server.py` の `Session.body_meta()` に `geometry()` と同じ規律
(形状の同一性`is`で判定)の `_volume_cache` を追加した(コミット `56049eb`)。実測は
`state_meta()` 1回 693ms→1.1ms、「全非表示」27コマンド合計(サーバ側) 16.99s→0.03s。
以下は問題発見時点(修正前)の分析記録として残す。

`Server._push` は配信のたびに `Session.state_meta()` を呼び、その中の `body_meta()` が
**全ボディぶん `query.volume()`(OCCT `BRepGProp.VolumeProperties_s`)を毎回計算する**。
体積にはキャッシュが無い。

実測(PC_case_20251122.step 4.6MB / 27ソリッド → 参照+編集用で54ボディ):

- `state_meta()` 1回 = **693ms**。うち体積 ~0.97s/54体(1体あたり平均13ms・最大83ms)、面数は合計2ms、JSONは10.3KB
- 「全部非表示」= 可視27体を1体ずつ `set_visible` → 27回の配信 → **サーバ側だけで19.1秒**。内訳は state_meta 18.72s / delta 0.33s / verb本体 0.00s / json 0.00s

**判定を誤らせた原因は「配る量」と「作る値段」の取り違え**。server.py の `_push` の
docstringは「代償は接続数ぶん state_meta() を計算すること(27部品で数KB)」と書いており、
出力が数KBであることを根拠に安いと見なしている。実際に高いのは転送量ではなく
中で回すOCCTの積分である。同じ `Session` の `geometry()` は「可視性だけ変えたときに
測り直すと高コスト」と明記してキャッシュしてあり、**その規律が体積にだけ適用されていない**。

直し方は既存の仕組みで足りる: `geometry()` と同じく形状の同一性(`id(body.shape)`)で
体積をキャッシュする。体積は形状が変われば変わり、可視性では変わらない。

**税は全配信にかかる**: `_push` に短絡は無いので、選択の変更(面のクリック)・undo/redo・
あらゆるverbの確定が1回693msを払っている。「全非表示」はその税がN回見えただけで、
遅いのはこのボタンではない。なお `volume` はブラウザでは表示していない
(web/src では型宣言に出るだけ)が、MCPの `_body_summary` が同じ `body_meta` を使うので
配信から落とすと2つの面が分裂する — キャッシュのほうが両方に効く。

**倍率はUI側が作る**: 「全非表示」([App.svelte] `hideAllParts`)は一括コマンドを持たず、
可視ボディぶん `set_visible` をWSへ1本ずつ撃つ。サーバは1コマンドごとに `_push` するので
N回の配信 × 全N体の体積 = **O(N^2)回のOCC積分**になる。フロント(session.ts/viewport.ts)は
差分更新が徹底されていて1メッセージあたりO(1)なので、フロントは原因ではない。

関連: [[embedding-into-the-log-also-inflates-the-live-feed]](同じ配信経路の別の太り方)、
[[hidden-bodies-stay-uneditable]]
