---
name: uniform-fixtures-hide-cross-body-cache-key-bugs
description: 複数ボディを扱うキャッシュ・重複判定のバグは、テストfixtureが全て同一寸法・同一形状のとき変異検査でも既存pytestでも検出できない。埋めるには「数えている量」自体が異なる形状のfixtureを新設する
created: 2026-09-04
updated: 2026-09-05
---

duocadの `duocad/selection.py` に `_coerce_refs`(ボディごとに面数・辺数を1回だけ数えるキャッシュ)
を新設したとき(タスク `20260904-0212`)、キャッシュ鍵から `body_id` を落とす変異
(`(body_id, kind)` → `kind` のみ)を試したところ、**既存pytest 799件が全て緑のまま**だった。
同様に、可視性の門番(`_visible_body`)をキャッシュ経路でスキップする変異も既存pytest全緑
のまま通った。

原因: `tests/test_selection.py` / `tests/test_selection_ref_key.py` の複数ボディ fixture
(`two_bodies` / `many_bodies`)が**全て同一寸法の箱**(`BRepPrimAPI_MakeBox`)で、面6・辺12・
頂点8が全ボディで一致する。異なるボディの数え上げ結果を取り違えても、値がたまたま同じなので
検証(範囲外添字の `ValueError`)が発火しない。不可視ボディの経路も `modify_selection`/
`propose_selection` 経由でバッチ処理を通すテストが単に存在していなかった。

一方、キャッシュ鍵から `kind`(種別)を落とす変異は既存pytestが**即座に**捕まえた ——
同じボディでも面数(6)と辺数(12)は違う値なので、取り違えが必ず範囲外扱いになる。

**対策**: 複数ボディにまたがるキャッシュ・重複判定・集約ロジックの変異検査は、fixtureの
寸法違いだけでは不十分で、**面数・辺数など「数えている量」自体が異なる形状**(箱+円柱等)を
混ぜたfixtureで確認しないと、ボディをまたぐ取り違えクラスのバグを見逃す。

**この穴は同タスク内で埋めた**: `tests/test_face_count_once_per_body.py`(duocad `b8e64e3`)
に、箱(6面12辺)+円柱(3面)の `two_shapes` fixture を新設し、上記2クラスの変異が実際に
赤くなることを確認した7テストを追加(799→806、回帰0)。完了条件「pytestが着手前と同じ本数で緑」を
実装者が新規テスト追加の禁止と誤読して一度は見送った経緯は
[[same-test-count-condition-means-no-regression-not-no-addition]]。

関連: `20260904-0211`(同ファイルの `ref_key` 導入)は種別ごとの等価判定という別クラスの
バグで、既存fixtureの均一さが問題にならなかった(鍵の要素数・型の違いを見るテストだった
ため)。今回のクラス(「同じ形の鍵だが値の由来が違う」)は fixture の均一さが直接コストになる。
