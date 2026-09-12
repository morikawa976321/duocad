---
name: duocad-body-cache-pruning-follows-sketch-cache-pattern
description: duocadのSession.body_id索きキャッシュ(geometry/edge/vertex/volume)の剪定は、既存のsketches_state()の剪定と同じ形(state_metaの中で生存集合の差分をpop)に揃え、_revision_counterだけは触らず、_suppress_events中は呼ばない
created: 2026-08-28
updated: 2026-08-28
---

duocadの `Session`(`duocad/server.py`)には、`body_id` 索きのキャッシュが4種
(`_geometry_cache` / `_edge_cache` / `_vertex_cache` / `_volume_cache`)あり、undoでBodyが
文書から消えてもエントリが自然には落ちず、開きっぱなしの文書で作成・undoを繰り返すと線形に
溜まり続ける問題があった(CODE_SURVEY_ACTIONS 1.1)。

**削除verbの実装箇所でpopする案は取れない** —— Bodyを削除する専用verbは無く、
`document.remove_body` を呼ぶのは複数のundoハンドラ(`extrude.py`/`import_editable.py`)と
文書まるごと差し替え(`parameters.py`/`speculative.py`)に散っており、verbs層はSessionの
キャッシュを知るべきでもない。

**正しい直し方は生存集合での剪定**。`sketches_state()`(`_sketch_cache`)が既に同じ問題を
同じ形で解いていたので、それに揃えた: `Session._prune_body_caches()` を新設し、
`state_meta()`(WebSocket配信のたびに呼ばれる)の先頭で呼ぶ。生存する `body_id` の集合との
差分でpopするだけ。

守るべき2条件:
- **`_revision_counter` は絶対に剪定しない**。版が1へ戻ると、同じ `body_id` を知っている
  クライアントが「版が同じ=変わっていない」と誤判定し、前の文書の形を描き続ける
  (`_replace_document` のコメントと同じ理由)。
- **プレビュー中(`_suppress_events` > 0)は剪定しない**。`preview()` は
  `duocad.speculative.speculative` で文書を投機的に書き換えて必ず元へ戻す。その最中の
  生存集合で消すと、戻した後に高コストな再測定(`_volume_cache` はPCケース27部品で
  693ms/回)が走ってしまう。

**今後Sessionにbody_id索きのキャッシュを足すなら、この2点を必ず引き継ぐこと**。
`server.py` は分割予定(queueに2件控えている)だが、`_prune_body_caches()` は
`state_meta()` の直前1行で呼ぶだけの独立した小さいメソッドなので、分割の妨げにはならない。
