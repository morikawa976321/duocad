# 形状最適化の保持セル（keep）・最小幅の対象外の契約

保持ソリッド由来のセル（`SearchConditions.keep_cells` / `DesignSpace.keep`）を探索で扱う規則——最小幅 R(m) の対象外（`exempt`）と、決して抜かない（`protected`）——について、ソースを読んでも分からない不変条件・依存禁止・罠。
いまの挙動の正はコードとテスト（`tests/topopt/test_binary_search_keep.py`・`tests/topopt/test_binary_search.py`）、実測と決定の理由の正は [../TOPOPT_PRESERVE.md](../TOPOPT_PRESERVE.md)（冒頭の契約・節A・節B）。最小幅の規則そのものは [topopt-min-width-peel.md](topopt-min-width-peel.md)。

## 呼ぶ面と意味の線引き

- `binary_search.SearchConditions(..., keep_cells=<bool配列。設計空間と同形>)`（`None` は対象外なし）。形が違えば `ValueError`。製品では `optimize_run._build_search_conditions` が `space.keep` を渡す。
- `binary_search.keep_cells_without_room(design_space, keep, min_width_cells, obstacle=None) -> {cells, around_exclude, around_outside}`: 設計空間の m 立方 opening に入らない保持セル（どんな形を選んでも m 立方で覆えず、最小幅の保証が効かない場所）の数と、その周りの空きセルの内訳。**周りは26近傍で数える**（hanger で 146。6近傍だと 68）。格子の外は `around_outside`。
- `DesignSpace.obstacle`: 除外ソリッドの格子上の占有（読み取り専用。除外が無ければ全 False）。
- `optimize_run.run()` の戻り値 `min_width_keep_without_room`（常にある。m=1 なら全て0）。0 でなければ `warning` に「除外ソリッドに挟まれて／設計空間の端にあって」。trace のスナップショット名は `min-width-keep-without-room`（m>1 のときだけ）。**保証が効かない保持セルは黙って例外にせず、数と内訳を返して警告する。** 警告文の全文一致はテストで固定していない（語句だけ）。

## 最小幅の対象外（exempt）

- 「対象外」= そのセル自身が被覆されていなくても違反にしない、だけ。材料としては数える（**探索中のマスクにあるセルだけ**）。線引きは「保持ソリッドかどうか」の1本で、除外由来・条件面の節点の例外は無い。
- 設計空間の opening は `opened | (設計空間 & keep)`。残した設計空間も規則を満たす（絞った部分は被覆済み・足すのは対象外のセルだけ）。
- 判定関数は `exempt` を末尾の引数で受ける: `_mask_satisfies_min_width(mask, m, exempt=None)` / `peel(..., exempt=)` / `min_width.lost_coverage(mask, region, m, exempt=None)`。`_mask_satisfies_min_width` を位置引数2つだけで呼ぶ差し替えは壊れる（テストの monkeypatch は `exempt=None` を受ける形）。
- m=1 では判定関数（`min_width.cube_opening` など）を一切呼ばない。

## 保持セルは決して抜かない（protected）

- `find_best_feasible(..., min_width_cells=m)` と `find_initial_feasible` は `keep_cells` を読む。**段階3は全材料マスク（保持ごと）を1回評価するだけ**、**段階4は `peel(..., protected=keep)`**。
- `peel(mask, order, count, anchor_ids, *, min_width_cells, exempt, protected)`: `protected`（bool、`mask` に含まれること）のセルは順位に並んでいても飛ばし（`picked` に数えない）、**島として切り離す手も却下する**（`skipped_connectivity` に数える。別の計数名は作っていない）。m>1 で `protected` を渡すときは `protected ⊆ exempt`（でなければ `ValueError`。製品では `exempt` = 保持セルなので常に成り立つ）。
- `binary_search.resolve_islands(candidate, conditions)`: 生存成分は「全条件を満たし、かつ全保持セルを含む成分」だけ。保持セルが2つ以上の成分に分かれる形は生存成分が無い（段階3では `full_material_infeasible`）。
- `binary_search._evaluate_survivor`: `keep_cells` があり `survivor` が保持セルを欠くと `RuntimeError`（黙って塗り直さない）。段階3・段階4の全評価がここを通る。
- **不変条件**: `keep_cells` が与えられた探索が作る全候補・全合格形状で `mask[keep].all()`。テストは `tests/topopt/test_binary_search_keep.py`（m=1,2,3）。
- 変えていないもの: `keep_cells is None` の経路（`PeelStats` の項目・診断のキーも同じ）、`acceptance.inspect` の6条件の並び・報告の形、合格条件(3)（保持CADとの共通体積）、代表応力（99.5% 点）の母集団（保持セルも入る）、除外ソリッドのボクセル化。

## 依存してはいけないもの

- `mask | exempt` は保持セルが常に `mask` にあるので `mask` と同じになるが、`keep_cells is None` の経路と共有しているので畳んでいない。`exempt` を `mask` の外に置く呼び方（`peel` を `protected` なしで直接呼ぶ）は API と単体テストに残っており、`_mask_satisfies_min_width`・`lost_coverage` はそれを前提にしたまま。
- `skipped_support` は hanger では 0 になる（条件面の確定節点が全て保持セルの角にあるため）が、それは hanger の構成であって他の設計空間の保証ではない。
- 変更前後の測定の読み方（6本とも予算切れ時点の比較で、目標に達した形どうしではない）は [../TOPOPT_PRESERVE.md](../TOPOPT_PRESERVE.md) 節B。
