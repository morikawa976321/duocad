# topopt-idle 検証(土台編: 小型題材・プロトコル・参照解・応力指標)の契約

不要材料(idle material)の2原因(原因1=応力指標の母数希釈 / 原因2=探索の経路喪失)の検証研究のうち、**小型題材・共通基盤・参照解と格子細分・評価式の比較・それらの訂正**の部分について、ソースを読んでも分からない不変条件・依存禁止・踏んだ罠を、元の作業タスクの契約から分野別に蒸留したもの。
いまの挙動の正はコードとテスト。実測値・再現コマンド・表は `topopt_idle_validation/00_COMPACT_MODELS.md`〜`03B_METRICS_RECHECK.md` にあるので、ここには繰り返さない(各節の出典のタスクIDが指す文書は `topopt_idle_validation/` の同番号の md)。
探索方式・認定・合成行列・hanger・最終形状・総括は [topopt-idle-validation-runs.md](topopt-idle-validation-runs.md)。
出典表記: 項目末尾の括弧は `(タスクID, 日付)`。タスクIDは先頭の `20260928-1426-topopt-idle-` を省略して `00-compact-models` のように書く。

全体の前提(全節に共通):
- コードは `topopt_checks/idle_validation/`(製品コード `duocad/` は無変更が原則)。出力は `output/idle_validation_20260928/`、モデルは `models/`。**`models/`・`output/` は `.gitignore` 対象でgit管理外**。コミットできるのは `topopt_checks/idle_validation/`・`tests/`・`docs/` だけ。生データはスクリプト再実行で作り直す。(00-compact-models / 01-protocol, 2026-09-28)
- 最終的な検証は、期限(2026-10-05再編)で縮小範囲に切られたため「元の全検証の完了」ではない。その事情は runs 側の冒頭に書く。

## 小型題材(topopt_idle_dilution_small.json / topopt_idle_path_small.json)

- 小型題材は2つ。原因1用 `models/topopt_idle_dilution_small.json`(保持リング付き・300N)、原因2用 `models/topopt_idle_path_small.json`。`build_compact_models.py`(引数なしで決定的に生成・往復検査。`--check` は保存せず要素数だけ)が作る。ジオメトリ定数(`DILUTION_*`/`PATH_*`)を書き換えたら**モデルJSONを再生成し、それに基づく実測をすべて取り直す**。古い実測と新しい形状が食い違う報告書が最も危険。(00-compact-models, 2026-09-28)
- 原因1モデルの荷重300Nは境界に近い(200Nは希釈の余地が無く、350N以上は全材料でも `full_material_infeasible`)。パラメータを動かす前に `optimize_run.build_conditions` → `design_space.derive` → `acceptance.inspect` で全材料の安全率を確認する。(00-compact-models, 2026-09-28)
  - 全材料の安全率は `safety_factor*load_n` が全条件で一定(1676.2988934…)=線形弾性と整合。正しい値は200N: 8.38、250N: 6.71(旧記述の4.77/5.97は誤り→訂正)。300N採用の最終判断は揺るがないが「200Nと比べて余地が無い」という比較根拠は失効。(03a-reference-repair, 2026-09-28)
- 原因2モデルの2.5mmが持つ軽さは**格子依存の局所解**由来で、「2.5mmが最適」ではない。新しい解像度・パラメータで実験するときは複数解像度を回さないと、局所解の1点だけを見て結論する罠を踏む。(00-compact-models, 2026-09-28)
- `tests/session_support.py` の two_pads / four_conditions は原因1の証拠に使わない(平面拘束のみで保持リング特有の応力集中が無く、不採用と判定済み)。(00-compact-models, 2026-09-28)
- 原因1(希釈)の反実仮想は `measure_dilution_counterfactual.py --radius R`(R=-1が基準)→ 熱いセル表は `measure_dilution_hot_cells.py`(**先に counterfactual を全R走らせてマスクを作る**)。原因2(経路喪失)の反実仮想は `measure_path_local_optimum.py {mesh | step | transplant}`(**`mesh` が先**: `transplant` が読む `.npz` マスクを作る)。(00-compact-models, 2026-09-28)
- 1回実行は `run_compact_search.py --model <ファイル名> --size <mm> [--min-width-mm | --cells] [--load-n] --out`(製品入口 `optimize_run.run` を直接呼ぶ)。(00-compact-models, 2026-09-28)

## 最小幅の扱い(全スクリプト共通の罠)

- **`binary_search.find_best_feasible` を低レベルで直接呼ぶときは `min_width_cells` を明示する。** `optimize_run._build_search_conditions` が返す `SearchConditions` は最小幅を持たず、製品入口 `optimize_run.run` 内部が `_resolve_min_width` の結果を `find_best_feasible` へ**別経路で**渡している(`duocad/optimize_run.py` の `search_kwargs["min_width_cells"]`)。渡し忘れると既定 `min_width_cells=1`(制約なし)で走り、製品より細い部材を許す(実バグとして検出・修正済み)。新しいスクリプトは `measure_path_local_optimum.py` の `_search_conditions`(`(space, cond, min_width_cells)` を返す)のパターンを踏襲する。(00-compact-models, 2026-09-28)
- **`acceptance.inspect` の6条件(watertight/single_solid/preserve/exclude/conditions/safety)に最小幅は含まれない。** 移植・反実仮想で作った候補マスクの最小幅は `binary_search._mask_satisfies_min_width(mask, min_width_cells, exempt)` を別途併用する。`exempt` は `binary_search._min_width_exempt(cond, mask.shape, min_width_cells)`。移植トラス(`F0200_h2.2_w5`→`F0200_h2.0_w5`)は6条件全pass・安全率8.87なのに最小幅不合格だった。(00-compact-models / 01-protocol, 2026-09-28)
- **`exempt` を渡さない厳格版は製品の受理基準と矛盾する**: `_mask_satisfies_min_width(mask, 5)` を `exempt` 無しで呼ぶと現行形 hanger_base 自身を含む3形状が不合格になった。保持セルを規則対象外にしてから判定する(`reproduce_stage1_transplant.py` の実装が正)。**委譲成果物の自己検証アサーションは、既知結果としか一致確認していなければ他形状の誤りを検出しない**。(03b-metrics-recheck, 2026-09-28)
- `--cells n`(セル数指定)は格子幅ごとに実mmが変わる(path_small で `cells=2` は 3.0mm格子→6mm・2.5mm→5mm・2.0mm→4mm・1.5mm→3mm)。**解像度をまたぐ比較は `--min-width-mm` で物理mmを固定**する。ただし指定値がどの解像度でも製品の下限 `max(2×要素サイズ, 1mm)` 以上でないと `_resolve_min_width` がエラーになる(path_small・3.0/2.5/2.0/1.5mm の4点なら 6.0mm が唯一の有効な整数mm値)。(00-compact-models, 2026-09-28)
- 旧64条件(`output/smoothing_corpus/`)は**セル数指定固定**、新規実験は**mm固定**(`optimize_run.OptimizeOptions(min_width_mm=...)`)。同じ表で「同条件」として比較しない。mm固定の新規実験は `min_width_cells_resolved`・`min_width_guaranteed_mm`・`element_size_resolved_mm`(スナップ後の実spacing)を必ず記録する。旧64条件へ新しいセル幅/荷重条件を追加しない。(01-protocol, 2026-09-28)
- `min_width_mm` に固定小数(例 4.8)を渡すと格子スナップ後の実spacingがずれて下限を割りエラーになりうる(2.4mm指定が2.406mmになり4.8mm指定が下限4.813mmを割った)。`None` を渡せば `max(2×実spacing, 1.0)` を自動計算する(製品既定と同じ式)ので、`None` のほうが安全。(04-searches, 2026-09-28)
- 要求格子2.4mmの解決後spacingは旧コード 2.3875mm → 現コード 2.4063mm に変わっており、旧コーパスと同一マスクにならない(別条件)。(07-hanger-loads, 2026-09-28)

## 共通基盤 _common.py(コピーせず直接import)

- `topopt_checks/idle_validation/_common.py`: `git_state(paths=("duocad",))`・`sha256_file`・`sha256_json`・`write_atomic`・`peak_memory_mb`・`available_memory_mb`・`ExecutionLock(lock_path, tag)`(`with` でOS排他ロック。同じ `lock_path` を横断指定して「重いFEMは1実行ずつ」を強制)・`condition_id(**fields)`・`load_ledger`/`upsert_case`/`is_case_done`(台帳の重複排除・再開判定)・`SingleFemRun()`(tracemalloc+psutilの二重ピークメモリ計測)。**コピーしない**(単一実行制御の穴・記録形式の食い違いが再発する)。(01-protocol, 2026-09-28)
- `git_state()` は `duocad/`(製品コード)の状態しか見ない。`output/idle_study/`・`output/smoothing_corpus/` 配下のスクリプト(`counterfactual.py`・`transplant.py` 等)はgit管理外なので、**版はファイル自体のsha256で特定**する。(01-protocol, 2026-09-28)
- `is_case_done` は status だけを見る。`--smoke`(小予算の疎通確認)で書いた記録が、`--smoke` 無しの再実行でもスキップされ、小予算のまま本番データに混入した。台帳ベースのスクリプトは各caseに `smoke` フィールドを持たせ、`--force` で強制再実行できるようにする(`run_05_main_matrix.py` の `_case_record`/`--force` が実装例)。(05-synthetic, 2026-09-28)
- 機械可読の `01_protocol.json`・`01_stage1_shapes.json` は**手で編集しない**。生成スクリプト(`build_01_protocol_json.py`・`inventory_stage1_shapes.py`)のソースを直して再実行する(手編集/非決定的生成を避けるQA指摘)。`reproduce_stage1_transplant.py --src --dst --dilate` は既定が `F0200_h2.2_w5`→`F0200_h2.0_w5`・dilate=1、`output/smoothing_corpus/voxel/` の該当npzが前提。(01-protocol, 2026-09-28)
- `design_space.derive` は原点を明示パラメータとして受け取らない(外接箱+余白から内部計算)。半セル原点移動の実験にはプロセス内パッチが要り、達成シフト量が意図した0.5セルからずれうる。(01-protocol, 2026-09-28)
- 実行条件の正は `docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`「0〜2レビュー後の訂正と実行順序」節。`01_PROTOCOL.md` 4節の「hangerの新規実行は2条件(H1: 200N・w5相当・原因2 / H2: 2000N・w2相当・原因1)に限る」表は実行条件の正としない。(01-protocol / 03a-reference-repair, 2026-09-28)
- 合成材料5種(S-T/S-B/S-TB/S-L/S-MS)・半セル原点移動8条件・初期形状3種・フィルタ実幅2条件・複数荷重・第3段階(平滑化・集合演算後の再解析)は01で「定義済み・未実行」。その後どこまで実行されたかは runs 側。(01-protocol, 2026-09-28)

## 移植トラスと原因2

- 移植トラス(`F0200_h2.2_w5`→`F0200_h2.0_w5`, dilate=1)は**「同条件での確定的な優越形」の証拠にしない**: 6条件全pass・安全率8.87だが最小幅規則(5セル=9.99mm)を満たさない。「格子で異なる骨組みに収束する」=原因2そのものの再現としてのみ有効。(01-protocol, 2026-09-28)
- 原因2の `topopt_idle_path_small.json` は00時点では**部分認定**(局所解の再現は確認、転写実験は目標の92〜93%止まり)だったが、**04aで認定済み**(2.0mm格子・3000N・パーセンタイルシナリオに限る。詳細は runs 側「経路喪失の認定」)。(00-compact-models, 2026-09-28。04a-path-qualification で上書き)
- hangerで残すべき検証(00の申し送り): 支持リング複数(取付穴3つ)での原因1の相互作用(小型は単一リングのみ)、実STEP形状の三角形分割誤差、hangerサイズでのメモリ上限挙動(`KB_PER_DESIGN_ELEMENT=18.6` の直線外挿の検証。定数の所在は `topopt_checks/measure_limit_run.py`/`docs/TOPOPT.md`)、原因2(低荷重・太幅)と原因1(高荷重)の両方で最終的に選んだ方式をhangerでも同一評価・条件で確認。(00-compact-models, 2026-09-28)

## 参照解・格子細分・解析解(_fem_probe.py / refine_shape.py / analytic_checks.py)

- `_fem_probe.py` の `evaluate_full(...)` は `acceptance.safety_check` と同じ入口で要素ごとの応力テンソル・主応力・反力・`Model`/変位まで返す。**`status` が `"ok"` 以外なら `von_mises_mpa` 等は空配列**なので必ず `status` を先に見る。`zone_masks`(保持部/接続部/一般部を距離基準で自動分類。ボックスは `face_world_boxes(table, faces)` で作る)・`zone_stats`・`section_force_moment`(要素中心応力の面積積分近似。せん断応力場は再構成しない)・`per_face_reactions`・`principal_stresses`。**コピーせず直接import**。(02-reference, 2026-09-28)
- `zone_masks_with_load(material, grid, *, fixed_world_boxes, load_world_boxes, distance_mm)` は荷重面近傍 `load` を足した4ゾーン版(既存 `zone_masks` は無変更で、保持部・接続部しか持たない)。荷重パッド端の応力集中が一般部へ希釈される問題への対処。(06-hanger-low, 2026-09-28)
- `refine_shape.py --shape {dilution_r-1|path_2.5mm|hanger_base|hanger_r2|hanger_step05|hanger_transplant} --levels <int...> --loads <float...> [--hanger-condition] [--distance-mm]` は**新しい探索をしない**(保存マスクを細分格子へ投影して同じ形のままFEMを解き直す)。出力は `refine_<shape>_load<N>_level<k>.json` と `02_reference_ledger.json` への追記。(02-reference, 2026-09-28)
  - `hanger_r2` の既定条件は `F0200_h2.4_w2`(200N側)。H2(高荷重)を再現するには `--hanger-condition F2000_h2.4_w2` を明示する(黙って違う条件を実行する)。(02-reference, 2026-09-28)
  - hanger の格子細分 level>=2 は未実施(design要素が約1200万で既知の安全上限約130万要素を一桁超える)。やるなら局所サブモデル・適応細分が要る。dilution level4(0.5mm)も安全上限に近く見送り。(02-reference / 03a-reference-repair, 2026-09-28)
  - `_project_mask(src_mask, src_origin, src_spacing, target_space)` の origin/spacing は**投影元(ソース)自身の格子のもの**を渡す。目的格子のものを渡すと位置がずれたまま投影される(`_snap_to_grid` が解像度ごとに bbox へ合わせて origin をずらすため一致しない。dilution 2.0mm の origin (-16,-16,-16) vs 3.0mm の (-16.97,-17.70,-17.76))。(05-synthetic, 2026-09-28)
  - `--exact-subdivide`(`_project_mask_exact_subdivide`。整数分割で体積保存、既定False)を検証するときは**必ず `--out-dir` で既存の `output/idle_validation_20260928/` 直下と別ディレクトリを指定**する。怠ると02/03が使う共有 `refine_*.json` を上書きする(実際に発生し、同一入力の再実行で復元)。出力 `exact_subdivide_diagnostics.cells_lost_to_design_space_mask` は `design_space.derive` の外枠再計算で失われる材料セル数。(03a-reference-repair, 2026-09-28)
- `analytic_checks.py {tension | bending --levels ... | kirsch --shape {dilution|path} --element-mm}`。**`analytic_tension.json`/`analytic_bending.json` は呼ぶたびに全体を上書きする**(`--levels` に渡した分しか残らない)。QAで `tension --levels 1 2` が実行され4レベル分が2レベル分に縮んだ事故あり。軽い確認で再実行したら、後で `--levels 1 2 3 4`(曲げは真の等比細分となる `--levels 1 2 4 8`)のフル実行をやり直す。kirschは解像度ごとに別名なので安全。(02-reference / 03a-reference-repair, 2026-09-28)
- 曲げの採点は**位置補正値**(`run_bending` の `sigma_theory_position_corrected_mpa`・`probe_x_actual_mm`・`probe_z_half_actual_mm`)を使う。名目理論値 `sigma_max_theory_mpa`(固定端から20mm・表面ちょうど)は互換のため残すが参考値。正しい曲げ収束は 23.98 MPa(理論24.0の0.075%以内、p≈1.0)。**旧 `02_REFERENCE.md` 5-1節の収束次数1.5〜1.7・外挿値20.6〜21.7 MPaは使わない**(採点位置未補正+非等比Richardson)。本文は書き換えず冒頭に訂正ポインタのみ。(03a-reference-repair, 2026-09-28)
- `richardson_extrapolate` は等比細分を前提にした3点公式で、非等比細分(dilution 2.0/1.0/0.6667mm=比2.0,1.5)の収束次数は ratios[i] と ratios[i+1] のどちらを使うかで変わる近似値(外挿値自体は選び方に依存しない)。**非等比には `richardson_extrapolate_general(values, spacings)`**(二分探索で次数pを解く。等比では旧関数と数値一致。解が `p∈(1e-3,10)` に無ければ `window_diagnostics` に `no_solution=True`)を使う。`no_solution` は発散の証明ではなく「単純べき乗収束モデルが当てはまらない」の確認にとどまる。(02-reference / 03a-reference-repair, 2026-09-28)
- `zone_masks` の距離パラメータ `D` に対し、保持部ゾーンのmaxは頑健だが接続部/一般部のmaxは敏感。ゾーンmaxを比較に使うなら `D` の根拠を明記する。(02-reference, 2026-09-28)
- `raw_max`(空セルを含む全要素max。`stress.py` の `RepresentativeStress.raw_max`、材料の外 ρ<rho_min で計算された値)は製品評価・保持部の物理的根拠に使わない。台帳の `raw_max_mpa` は M5b raw が0でも数百〜数千MPaになり判定に使えない。(03a-reference-repair / 07-hanger-loads, 2026-09-28)
- **保持部ゾーンの raw_max・ゾーンmaxは格子細分で増加し続ける**(dilution 300N の raw_max 224.7→478.3→692.4 MPa、pathも同様)。断面力・反力の釣り合いは全レベルで一致しており実装バグではなく局所特異点。収束済み参照値として使わない。ただし**「発散」の数学的確定は未了**(一般化Richardsonが `no_solution`=判定不能。確定には dilution level4 の新規実行が要る)。(02-reference / 03a-reference-repair, 2026-09-28)
- Kirsch/Peterson比較(`kirsch --shape dilution`)の `kt_fem` を保持部応力集中の絶対値の根拠にしない(荷重条件の不一致〈曲げ支配 vs 同軸引張〉と局所maxの格子依存が混ざった未校正値)。hangerの `section_forces` は独立検算の根拠にしない(axis=x固定の断面カットが実形状で意味を持たない箇所あり)。hangerの独立検算は `per_face_reactions_n`(支持別反力分担)。(02-reference, 2026-09-28)
- 02の成果は5%形(hanger_step05)・移植トラスの格子細分とゾーン解析を持たない。引用しない。必要なら `refine_shape.py --shape hanger_step05 / hanger_transplant` で新規実行(未検証のコード経路)。実CAD/平滑化境界に沿う独立コンフォーマルメッシュ検証は未実装(gmsh等が無い。Richardson外挿と解析解比較で代替)。(02-reference, 2026-09-28)
- hanger H1 の一般部48.72 MPaピークは**荷重パッド接触端の応力集中**(荷重面から4.99mm・拘束面から190mm以上)と特定。ゾーン分類に荷重面近傍が無いため一般部へ希釈される。H1の1ピークでの確認にとどまり、他条件・他ピークへの一般化は未検証。(03a-reference-repair, 2026-09-28)
- 本文を訂正済みで引用してはならない箇所: 00の200N(4.77)/250N(5.97)安全率、02 5-1節の曲げ収束、01 4節の実行条件表。各文書冒頭の「訂正(03a)」ポインタから `03A_REFERENCE_REPAIR.md` を辿る。(03a-reference-repair, 2026-09-28)
- 03aの検証は path_2.5mm・dilution_r-1・hanger H1 の限定題材のみ。`no_solution` 判定・局所平均の収束・load-zone仮説を hanger H2 や他の小型題材へ無条件に一般化しない。`keep` セル合成・`resolve_islands` の体積寄与は03aでは未定量化(03bで定量、下記)。(03a-reference-repair, 2026-09-28)

## 応力指標(stress_metrics.py / metric_eval.py)

- 純粋関数(FEMを呼ばない)は `stress_metrics.py`: M1 `metric_full_population_percentile`(現行製品と同じ)・M2 `metric_excluded_population_percentile`・M3 `metric_zone_percentile`・M4a/b `metric_top_volume_rank`/`metric_top_volume_average`・M5a/b `metric_exceedance_volume`/`metric_exceedance_integral`(**合否ゲートの本体はM5b**)・M6 `metric_p_mean`・M7a/b `metric_p_norm_unnormalized`/`metric_ks_aggregation`。戻りは `MetricResult(name, value, allowable_mpa, passed, population_count, detail)`。(03-metrics, 2026-09-28)
- `metric_eval.solve_shape(shape_cfg, load_n, level, *, origin_shift_mm=0.0, distance_mm=None)` は形状→FEM解→ゾーン付き生の場。`refine_shape.py` の `_resolve_shape`/`_project_mask`/`HANGER_MODEL` を import して再利用する(コピーしない。`refine_shape.py` は無変更で、既存task02出力と完全一致を確認済み)。(03-metrics, 2026-09-28)
- **採否(03、03bで変更なし)**: M5b(限度超過量積分、tolerance=0)=合否ゲートの本体 / M3=診断内訳 / M7b=探索補助 / 他は不採用。**M5b は反例7件・全34実形状ケースで一度も危険を見逃さなかった唯一の候補**。(03-metrics / 03b-metrics-recheck, 2026-09-28)
  - ただし粗い解析メッシュでの M5b=PASS は安全の証明ではない: path_2.5mm の格子細分反例(f)で level1〜3 は M5b=0 だったのが level4 で突然8セルの超過が出て不合格。このセルは holding zone 内・境界から13セル内部の局所超過と分かったが、それは物理的収束の証明ではなく、level5以降で増える可能性は排除できない。(03-metrics / 03b-metrics-recheck, 2026-09-28)
- **M1(全材料99.5%点=現行製品)を単独の安全判定に使わない。** hanger H1・H2とも合格と誤判定する(H1: M1=8.29 MPa PASS だが一般部に48.72 MPaピーク。H2: M1=14.7 MPa PASS だが holding zone は 42.47 MPaで不合格水準)。(03-metrics, 2026-09-28)
- **M3(部位別99.5%点)は、ゾーンが数百セル以上あるとゾーン内で同じ母数希釈を再現する。** 許容応力の1.25倍の1セルを holding(1,064セル)/joint(684)/general(9,298)のどこへ注入しても3ゾーンとも合格のまま反応しない(反例e)。H1の一般部ピーク48.72 MPa(一般部29,224セル)もM3一般部99.5%点(7.31 MPa)に現れない。「部位別に分ければ希釈が消える」は単独では成立しない。M3を使うならゾーンごとのM5bも併用する(**ゾーン別M5bの実装は03では未実装** (要確認: 後のタスクで実装されたか未確認)。)(03-metrics, 2026-09-28)
- **M4(固定物理体積の上位順位/平均)は窓より小さい局所超過を見逃す**(許容応力の2倍=80 MPaの1セルが216mm³窓〈k=27セル〉の順位・平均で8.7/11.8 MPaに薄まる〈反例d〉)。窓を小さくするとraw maxの格子依存発散に戻る。使うなら窓サイズの物理的根拠(製造上の最小欠陥サイズ等)を明記。(03-metrics, 2026-09-28)
- M7a(非正規化pノルム)は母数希釈にほぼ免疫(20倍水増しで+0.23%)だが、個々には合格で多数がやや高い健全な高密度形状を過剰に不合格にする(独立探索1.5mm・6000N、材料24,631セルで M1・M3・M5b はPASSなのに M7a だけ41.9でFAIL)。M7b(KS集約)は過反応が穏やか。(03-metrics, 2026-09-28)
- M2(R2除外99.5%点)は除外域の外で操作するとM1と完全に同じ挙動(除外は母数希釈の防御にならず、保持部の危険を構造的に見えなくするだけ)。**合否判定に使わない**。M2・M4・M6を単独の合否ゲートにしない。(03-metrics, 2026-09-28)
- `metric_zone_percentile` は全ゾーンが空(材料なし)なら `passed=False` を返す(`all([])` の空虚な真のバグを修正済み)。(03-metrics, 2026-09-28)
- allowable_mpa は全34ケースで 40.0 だったが**製品定数としてハードコードしない**(コードは `cond.allowable_mpa` をケースごとに渡す)。M4/M6/M7のパラメータ(top_volume_mm3={8,216}・p=8・ρ=1.0)は研究用初期値で未較正。(03-metrics, 2026-09-28)
- 指標比較の標本(path独立探索4解像度1.5/2.0/2.5/3.0mm、dilution R-sweep 5形状R=-1..3)は「評価式感度を測る多様な実形状サンプル」であって、探索方式の優劣や原因2解決の証拠ではない。`path_transplant_mesh_2.5mm_to_2.0mm`(close0/close2)は採用マスクnpzが無く生の場を再構成していない(`metric_comparison.json` に無い)。hanger 5%形・移植トラスの生の場評価は03時点で未実行(03bで共通条件採点を実施)。(03-metrics, 2026-09-28)
- 実行: 主比較 `run_metric_comparison.py`(34ケース→`metric_comparison.json`)、反例 `counterexamples_field_only.py`(a/d/e)・`counterexamples_material_edit.py`(b)・`counterexamples_origin_shift.py`(g)、まとめ `build_03_metrics_json.py`。(03-metrics, 2026-09-28)

## 03b: hanger共通条件採点と局所超過の追加検証

- 03の採否は03aの訂正（M1〜M7bの計算パスに無関係。git差分ゼロ、2ケース再計算がビット一致）を受けても変更なし。**04は03の凍結をそのまま入口に使ってよい**。(03b-metrics-recheck, 2026-09-28)
- `hanger_common_condition_scoring.py`(現行/R2/5%/移植を共通条件200N・w5で採点)は **`hanger_r2` を明示的に `"F0200_h2.0_w5"` で解く**(既定は `"F0200_h2.4_w2"`)。R2の全母集団再採点(安全率3.726)は200N/w5の1条件のみで、R2探索の元条件(w2・2.4mm)へ外挿しない。「R2は探索目標4.8未達」と「製品floor2.0は満たす」は併記する(単独で「R2は危険」と引用しない)。(03b-metrics-recheck, 2026-09-28)
- `counterexamples_hanger_extension.py` は `counterexamples_field_only.py` の `counterexample_a_far_low_stress`・`counterexample_e_hotspot_transfer` を再利用する形が標準パターン(04以降が新題材へ反例を広げるときも倣う)。(03b-metrics-recheck, 2026-09-28)
- keep/resolve_islands の体積寄与は0セル(`keep_islands_volume_contribution.py`、path_2.5mm・level2固定)。構造的理由(keepはsrcグリッド上でmaskの厳密な部分集合)で他でも成り立つと考えられるが実測は1件のみ→「常に0」と一般化しない。`level4_exceedance_locality.py` は path_2.5mm/6000N/level4 固定。(03b-metrics-recheck, 2026-09-28)
- hanger_step05 は M5b=0(合格)、現行形 hanger_base は M5b=69.5(不合格)だが、歩幅2点(20% vs 5%)の比較にすぎず、歩幅と安全性の因果の証明ではない。04以降の歩幅比較の初期観測として使う。(03b-metrics-recheck, 2026-09-28)
