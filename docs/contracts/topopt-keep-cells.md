# 形状最適化の保持セル（keep）・最小幅の対象外の契約

この文書は、保持ソリッド由来のセル（`SearchConditions.keep_cells` / `DesignSpace.keep`）を探索で扱う規則——最小幅 R(m) の対象外（`exempt`）と、決して抜かない（`protected`）——について、
**ソースを読んでも分からない不変条件・依存禁止・踏んだ罠**を、元の作業タスクの契約から分野別に蒸留したもの。
いまの挙動の正はコードとテスト（`tests/test_binary_search_keep.py`・`tests/test_binary_search.py`）、実測と決定の理由の正は [../TOPOPT_PRESERVE.md](../TOPOPT_PRESERVE.md)（冒頭の契約・節A・節B）。
最小幅の規則そのものは topopt-min-width-peel.md。各項目末尾の括弧は出典 `(タスクID, 日付)`。

## 呼ぶ面と意味の線引き

- `binary_search.SearchConditions(..., keep_cells=<bool配列。設計空間と同形>)`（`None` は対象外なし）。形が違えば `ValueError`。製品では `optimize_run._build_search_conditions` が `space.keep` を渡す。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20; 20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- `binary_search.keep_cells_without_room(design_space, keep, min_width_cells, obstacle=None) -> {cells, around_exclude, around_outside}`: 設計空間の m 立方 opening に入らない保持セル（どんな形を選んでも m 立方で覆えず、最小幅の保証が効かない場所）の数と、その周りの空きセルの内訳。**周りは26近傍で数える**（146 は26近傍の数。6近傍だと 68。定義は26近傍）。格子の外は `around_outside`。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)
- `DesignSpace.obstacle`: 除外ソリッドの格子上の占有（読み取り専用。除外が無ければ全 False）。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)
- `optimize_run.run()` の戻り値 `min_width_keep_without_room`（常にある。m=1 なら全て0）。0 でなければ `warning` に「除外ソリッドに挟まれて／設計空間の端にあって」。trace のスナップショット名は `min-width-keep-without-room`（m>1 のときだけ）。**保証が効かない保持セルは黙って例外にせず、数と内訳を返して警告する。** 警告文の全文一致はテストで固定していない（語句だけ）。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)

## 最小幅の対象外（exempt）

- 「対象外」= そのセル自身が被覆されていなくても違反にしない、だけ。材料としては数える（**探索中のマスクにあるセルだけ**）。線引きは「保持ソリッドかどうか」の1本で、除外由来・条件面の節点の例外は無い。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)
- 設計空間の opening は `opened | (設計空間 & keep)`。残した設計空間も規則を満たす（絞った部分は被覆済み・足すのは対象外のセルだけ）。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)
- 判定関数は `exempt` を末尾の引数で受ける: `_mask_satisfies_min_width(mask, m, exempt=None)` / `peel(..., exempt=)` / `min_width.lost_coverage(mask, region, m, exempt=None)`。`_mask_satisfies_min_width` を位置引数2つだけで呼ぶ差し替えは壊れる（テストの monkeypatch は `exempt=None` を受ける形）。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)
- m=1 では判定関数（`min_width.cube_opening` など）を一切呼ばない。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)

## 保持セルは決して抜かない（protected）

- `find_best_feasible(..., min_width_cells=m)` と `find_initial_feasible` は `keep_cells` を読む。**段階3は全材料マスク（保持ごと）を1回評価するだけ**（`58c9d62`）、**段階4は `peel(..., protected=keep)`**。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- `peel(mask, order, count, anchor_ids, *, min_width_cells, exempt, protected)`: `protected`（bool、`mask` に含まれること）のセルは順位に並んでいても飛ばし（`picked` に数えない）、**島として切り離す手も却下する**（`skipped_connectivity` に数える。別の計数名は作っていない）。
  m>1 で `protected` を渡すときは `protected ⊆ exempt`（でなければ `ValueError`。製品では `exempt` = 保持セルなので常に成り立つ）。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- `binary_search.resolve_islands(candidate, conditions)`: 生存成分は「全条件を満たし、かつ全保持セルを含む成分」だけ。保持セルが2つ以上の成分に分かれる形は生存成分が無い（段階3では `full_material_infeasible`）。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- `binary_search._evaluate_survivor`: `keep_cells` があり `survivor` が保持セルを欠くと `RuntimeError`（黙って塗り直さない）。段階3・段階4の全評価がここを通る。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- **不変条件**: `keep_cells` が与えられた探索が作る全候補・全合格形状で `mask[keep].all()`。テストは `tests/test_binary_search_keep.py`（m=1,2,3）。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- 変えていないもの: `keep_cells is None` の経路（`PeelStats` の項目・診断のキーも同じ）、`acceptance.inspect` の6条件の並び・報告の形、合格条件(3)（保持CADとの共通体積）、代表応力（99.5% 点）の母集団（保持セルも入る）、除外ソリッドのボクセル化、`mesh.apply_density_masks`。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)

## 依存してはいけないもの・測定の読み方

- `mask | exempt` は保持セルが常に `mask` にあるので `mask` と同じになるが、`keep_cells is None` の経路と共有しているので畳んでいない（畳むなら別タスク）。`exempt` を `mask` の外に置く呼び方（`peel` を `protected` なしで直接呼ぶ）は API と単体テストに残っており、`_mask_satisfies_min_width`・`lost_coverage` はそれを前提にしたまま。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- `skipped_support` は hanger では 0 になる（条件面の確定節点が全て保持セルの角にあるため）が、それは hanger の構成であって他の設計空間の保証ではない。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- 変更前後の比較（`docs/TOPOPT_PRESERVE.md` 節B）は**6本とも `evaluation_limit` で止まった予算切れ時点の比較**で、目標に達した形どうしの比較ではない（安全率が目標 2.0 を上回ったまま止まる。変更後 m=1 は 5.12）。変更前 m=2・3 の採用マスクは節Aに保存されたものと一致しない（`peel` がその後に変わったため）。基準にするときは同じ条件で回し直す。変更前 m=1 は節Aの `cold.npz` と一致した（5,436 セル）。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- 納品形状（`mask | keep`）のセル数は m=1 と m=3 で変更後のほうが増えた（6,284 → 6,795、6,148 → 6,481）。保持ソリッドが探索時の削減可能対象から外れているなら問題ない、が本人の判断（2026-09-22。`docs/TOPOPT_PRESERVE.md` 節B「本人判断」）。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)

## 罠

- **実 spacing**: 要求 2.4mm は 2.387506mm（設計空間 377,075）、要求 2.406306mm は 2.406306mm（367,390・保持 1,475）。節I1〜I4 と I5 は格子が違う。m=3 の要求は `3×実spacing`（7.2189mm）。「7.25mm」は 3×2.4063=7.219 を超えるので m=4 になる。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)
- m=3 の1評価は約144秒。予算60は約2.4時間。探索の結果は最後にしか出ない（途中経過のログが無い）。 (20260920-2123-topopt-min-width-exempt-preserve, 2026-09-20)
- bash の `python -c "…"` の中にバッククォートを書くと、シェルがその中身をコマンドとして実行する。日本語とバッククォートが混ざる文書（cursor 用の依頼書など）は Write ツールで書く。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
- 長時間の測定プロセスを `Start-Process` で python 直起動すると、使用量制限で親が死んでも測定は最後まで走る。再開のときは `out/progress*.txt` の `ALL DONE` を見て、回し直さない。 (20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)
