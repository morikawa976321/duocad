# topopt-idle 検証(実行編: 探索方式・認定・合成行列・hanger・最終形状・総括)の契約

不要材料の2原因(原因1=応力指標の母数希釈 / 原因2=探索の経路喪失)の検証研究のうち、**4探索方式の研究用実装、path_small の認定、合成行列、hanger低荷重・荷重行列、頑健性、最終形状、総括**について、ソースを読んでも分からない不変条件・依存禁止・踏んだ罠を、元の作業タスクの契約から分野別に蒸留したもの。
いまの挙動の正はコードとテスト。実測値・再現コマンド・表は `topopt_idle_validation/04_SEARCHES.md`〜`10_SYNTHESIS.md` にある。共通基盤・参照解・指標は [topopt-idle-validation-setup.md](topopt-idle-validation-setup.md)。
出典表記: 項目末尾の括弧は `(タスクID, 日付)`。タスクIDは先頭の `20260928-1426-topopt-idle-` を省略して書く。

**縮小範囲であること(全節の前提)**: 07〜10は本人の期限変更で縮小範囲の報告になっており、「元の全検証の完了」でも製品安全の保証でもない。未実行・中断・lost を合格として扱わない。延期項目は各節と末尾にまとめる。全成果物の `output/idle_validation_20260928/*` は `.gitignore`(`/output/`)で追跡されない。(07-hanger-loads / 08-robustness / 09-final-shapes / 10-synthesis, 2026-09-28)

## 研究用探索の実装(search_methods.py)

製品コード(`binary_search` 等)は無変更で、`topopt_checks/idle_validation/search_methods.py` が研究用の4方式を持つ。呼ぶ面の引数はソース参照。
- 方式1・2 `run_fixed_step`(歩幅20%/5%。製品 `binary_search.find_best_feasible` を無変更で呼ぶ)/ 方式3 `run_adaptive`(`AdaptiveConfig`)/ 方式4 `run_reallocation`(`ReallocationConfig`)/ `run_adaptive_from_mask`(既存の合格形から段階3を経由せず削減継続。**`start_mask` の構造的妥当性は呼び出し側が保証**、内部で再検査しない)/ `build_context`・`score_mask`・`load_checkpoint`。(04-searches, 2026-09-28)
- 除去候補は製品コードの `peel`/`resolve_islands`/`_geometry_rejection` の不変条件(単一成分・保持・アンカー到達・最小幅)をそのまま満たす。追加候補は `_growth_geometry_ok`(保持・除外・アンカー・単一成分・正則境界・芯の連結・最小幅・閉空洞禁止の全項目)を満たしたものだけを採用する。(04-searches, 2026-09-28)
- `evaluations` は「実際に FEM(`score_mask`)を呼んだ回数」(幾何のみで却下した候補は数えない)。`fem_solves` は内部の付随FEM(初期スコア・revert再スコア等)まで数える広い集計。(04-searches, 2026-09-28)
- 04a追加の `run_adaptive`/`run_adaptive_from_mask` の `extra_protected`(`np.ndarray`、`peel` の `protected` へOR結合、`min_width_cells>1` では `exempt` へ自動算入)・`on_accept`(受理ごとの callback)は既定 `None`=既存呼び出しは無変更。`peel` の m>1 では **`protected` は `exempt` の部分集合が不変条件**(違反で `ValueError`)。(04a-path-qualification, 2026-09-28)
- 08追加の研究用パラメータ(既定は旧挙動): `AdaptiveConfig.prediction_error_floor`(0.0=旧)・`ReallocationConfig.grow_max_fraction_of_removed`(None=旧)・`AdaptiveConfig.stop_if_start_fails_gate`(False=旧)。(08-robustness, 2026-09-28)
- **ゲート**: `AdaptiveConfig.gate` は `"m5b_raw"`(既定。03b凍結定義=材料の生の許容応力に対する超過)/`"m5b_target"`(`allowable/target_safety_factor` 換算)/`"percentile"`(製品と同じ)の3種で**異なる安全の定義を測る**。方式間・修正前後の体積比較は同一ゲートのシナリオ内だけで行い、跨いで優劣を主張しない。(04-searches / 05-synthetic / 08-robustness / 10-synthesis, 2026-09-28)
  - **`m5b_raw` 単独では製品の目標安全率(4.8)を守れない**: M5bの `allowable_mpa` に生の許容応力を使うとゲートが緩く、既定のまま回すと SF4.46 まで削れた。dilution_small の全材料形は `m5b_raw` で合格(5.59)するが `m5b_target` では不合格(192.9 MPa·mm³超過)。dilution 250N・path各warm-start条件で SF3.85〜4.56 に沈む形が複数。08でも m5b_raw 受理50実行中27実行が製品SF目標4.8未達。**結論: m5b_raw 単独ゲートは採用しない**。hangerへ配線するなら `m5b_target` か `percentile` との併用が要る。軽い形が見つかっても、併記される `safety_factor_product` と `m5b_target_*` で必ず確認する。(04-searches / 05-synthetic / 08-robustness, 2026-09-28)
- 単一FEM実行ロック `output/idle_validation_20260928/_locks/fem_single_run.lock`(`_common.ExecutionLock`)を `hanger_external_check.py` と `run_04_searches_compact.py` 以降のスクリプトが共有する。**並行して走らせない**。`ExecutionLock` は解放してもロックファイルを消さないので、ロックの消滅を待つ待機スクリプトは終わらない(11.7時間停滞した)。待つのはプロセスの終了。(04-searches / 06-hanger-low, 2026-09-28)
- 実行順序(04): `run_04_searches_compact.py` → `search_methods_mechanism_checks.py` → `hanger_external_check.py --condition H1|H2|both [--resume] [--max-evaluations N]`(既定60・実運用は20)→ `build_04_searches_json.py`(前3つの出力が前提)。生データ・チェックポイント(`04_<tag>_adaptive.checkpoint.json` 等)があれば打ち切りの延長・再開は可能。(04-searches, 2026-09-28)

### 探索方式の罠(コードの設計理由)

- **歩幅の下限 `min_step_ratio` を後退倍率(`multiplier`)にも床として適用すると無限ループ**する(同じ複数セル候補が却下され続け、`requested` が床で頭打ち→`peel` が毎回同じ候補を返す)。`min_step_ratio` は目標歩幅(`gap_ratio`)だけに床を敷く(`_run_adaptive_impl` 内コメント参照)。(04-searches, 2026-09-28)
- 受理判定: compliance/変位は `sudden_jump` 判定に組み込む(記録だけで未使用だった)。`finish_patience` は `finish_total_evals`/`finish_counter` を分離して配線し `finish_stagnation` を新設。`finish_counter` は best_ever 改善時のみリセットする。この修正で path 適応方式は 120→43 評価に減った。主実証(120評価予算)は `finish_stagnation`/`evaluation_limit` で止まっており「理論的収束」ではない。(04-searches, 2026-09-28)
- **一次予測係数 `calibrated_k` は最初に受理された1手だけから較正され以後固定**(`_run_adaptive_impl`)。粗い格子・小さい候補セル数が続く終盤で実態と合わなくなり、`prediction_error` ロールバックが暴走して局所解に留まる(dilution 3.0mm・path 1.5mm)。08の `prediction_error_floor` で症状は減ったが、**較正そのものは未修正**(`deferred_by_user_deadline`)。次にこのコードへ手を入れるならこの単点校正の脆弱性を先に直す。(05-synthetic / 08-robustness, 2026-09-28)
- 予測誤差の小分母による過剰拒否: 保存台帳の実測で 52実行・6,791手のうち2,119手が拒否、うち2,100手が予測誤差だけの理由(実ジャンプは受理手と同じ桁)。製品の適応(`prediction_error_multiplier=2.5`)は実変化が相対1e-6でも拒否し続けて材料が動かなくなる(hangerで再現、長予算でも解消しない)。floorの値(0.01)は未調整の丸め値で感度未測定。(06-hanger-low / 08-robustness, 2026-09-28)
- 再配置(方式4)の追加成分を分割するとほぼ必ず幾何検査で落ちるため、`grow_target` は規模の目安にとどめ連結成分は分割せずまるごと足す。単純な箱型形状では追加候補が「設計空間の残りほぼ全体」を最長連結成分に選び、部材単位の局所復元にならない(main_matrix・ablation の51件の `grow_accepted` が全て `initial_all_material` 由来で、ほぼ全材料形へ回帰。hangerでも5.10M mm³への回帰)。追加上限(`grow_max_fraction_of_removed`)を入れると全材料復帰は止まるが同条件の適応より重く(64,496 vs 57,768 mm³)、内側の適応で予測誤差拒否が再発する(78件。floor併用は未検証)。**再配置は採用候補から外す**。(05-synthetic / 06-hanger-low / 08-robustness, 2026-09-28)
- 再配置の追加候補ソースは「初期全材料形」「実行内履歴」「局所表面」だけを実使用。「移植トラス」「粗い解像度の探索結果」は `ReallocationConfig.external_candidate_masks`/`external_candidate_labels` で受け付ける設計だが実データでは一度も渡していない。(04-searches, 2026-09-28)
- 再配置(方式4)のwarm-start版は無い(常に `run_adaptive` の削減→追加を内部で辿る)。比較は `run_adaptive_from_mask`(方式3)限定。(05-synthetic, 2026-09-28)
- `hanger_base`(2.0mm)を粗い格子(2.4mm、H2条件)へ投影すると連結成分が条件面を失う(touches_condition=Trueの17,811セルの成分が丸ごと落ち、`resolve_islands` が0件)。H2の「現行」は投影でなく製品コード(20%)をH2条件で新規実行した結果にする。(04-searches, 2026-09-28)
- hanger本体の設計空間は大きい(H1条件で64万要素、全材料1回のFEMに約157秒)。合格が分かっている形から続けるなら `run_adaptive_from_mask`(`_SeedResult` で段階3を経由しない)を使う。(04-searches, 2026-09-28)
- `binary_search.ranked_cells` が参照する `ENERGY_FILTER_SIZE` はモジュール属性として呼び出し時に解決される(デフォルト引数に焼き込まれていない)ので `unittest.mock.patch.object` で一時差し替えが効く(`synthetic_matrix_support.patched_filter_size` で実測確認)。(05-synthetic, 2026-09-28)

## 経路喪失(原因2)の認定: path_small (qualify_path_small.py)

- **path_small は04aで認定済み**(代替終了ではない): 2.0mm格子・3000N条件で、現行 `step20`(71,928 mm³・SF4.842)より軽い 67,800 mm³(SF4.800)の優越形を構成し、6条件+最小幅(3セル)に全合格。2700N/3300N(±10%)でも同方向(-5.0%/-2.2%)。(04a-path-qualification, 2026-09-28)
- **認定はパーセンタイル評価シナリオに限る。** 03bのm5b_target評価器では現行・優越形とも不合格(超過387.98/396.64 MPa·mm³)で優劣は未確定。m5b_target 前提の後続は67,800 mm³を「安全」の根拠に使わない。(04a-path-qualification, 2026-09-28)
- 経路喪失の対照(3000N): 製品 `_reduce_volume` を忠実に再現した無保護対照が現行と厳密一致(71,928 mm³、corridor保持率0%)。優越形固有の605セルを保護しても71,544 mm³止まり(単純な1手の誤りではなく構造的傾向)。削除直前への復帰+単一代替手でも数手後には corridor保持率8.4%まで低下。(04a-path-qualification, 2026-09-28)
- 因果検査(対照)は3000Nのみ。2700N/3300Nは優越形の構成(移植+成長+仕上げ)だけで因果検査は再実行していない。原点シフトでの頑健性・2.0mm以外の格子での構成法の一般性は未検証(延期)。(04a-path-qualification, 2026-09-28)
- 軸方向シフトによる局所成長は、この形状固有(細い部材が支持点近くで太いパッドへ接続)で有効だった手法で、hanger等で同じ効率の保証は無い(球状/立方体膨張中心成長は非効率だった)。(04a-path-qualification, 2026-09-28)
- 罠: 最小幅3セルの離散化では孤立点/表面セル単位の追加はほぼ確実に min_width 検査で落ちる。現在の材料全体を軸方向へ1セルシフトして重ねる方式に切り替えて初めて優越形が出た。仕上げ削減に `stop_policy="legacy"`(製品の `target_tolerance` 即時停止)を使うとオーバーシュートが小さいと何も削れない。`stop_policy="adaptive_finish"` で初めて追加削減が得られた。(04a-path-qualification, 2026-09-28)
- 実行: `qualify_path_small.py`(3000N主実証+因果検査)→ `robustness_path_qualification.py`(2700N/3300N)→ `build_04a_json.py`。(04a-path-qualification, 2026-09-28)
- 04の再配置実証(長い部材の復元)は04a未認定のモデルで行ったので、機構の動作証拠であって「原因2を修復した証明」ではない(04a認定後は上記の限定付きで読む)。(04-searches, 2026-09-28。04a-path-qualification で上書き: path_small 自体は認定済み)

## 合成行列(05: 小型題材での主マトリクス)

- **合成材料5種(S-T/S-B/S-TB/S-L/S-MS)は構築していない**(小型題材優先の指定)。主題材は `topopt_idle_dilution_small.json`(原因1)・`topopt_idle_path_small.json`(原因2、04a認定の2.0mm/3000N/percentile)。これらを前提にした後続実装を「用意済み」として呼ばない。(05-synthetic, 2026-09-28)
- `synthetic_matrix_support.py`: `DILUTION_BASELINE`/`PATH_BASELINE`・`step_only_config`/`revert_config`/`reallocation_config`・`build_context_with_origin_shift`(新規探索を原点シフト下で行う。`metric_eval` の原点シフトは既存マスクの再投影専用)・`patched_filter_size`・`filter_size_cells_for_mm`・`best_so_far_curve`・`record_summary`。(05-synthetic, 2026-09-28)
- 実行順序: `run_05_main_matrix.py`(15条件×4方式。`--only <case_id...>`/`--smoke`/`--force`)→ `run_05_ablation.py` → `run_05_sensitivity.py` → `run_05_grid_origin.py` → `run_05_initshape.py`(main_matrix の `res3.0_w6` の保存マスクに依存)→ `analyze_05_mechanism_history.py`(新規FEMなし)→ `build_05_synthetic_json.py`。(05-synthetic, 2026-09-28)
- path側の受理判定(gate既定 `m5b_raw`)は04aの認定範囲(percentile)とは別シナリオ。dilution の「baseline」は2つある: 1節の `dilution_base_w6`(幅6.0mm)とアブレーションの baseline(幅4.0mm)は別条件で、語だけで指すと幅を取り違える。(05-synthetic, 2026-09-28)
- main_matrix の評価予算は4方式で揃えていない(adaptive=100評価、reallocation 内側=60評価×grow_cycles=3)。評価回数だけを公平性の基準にせず、材料量・安全率・機構ログを対で読む。(05-synthetic, 2026-09-28)
- パラメータ感度の「選定値」は既定値のまま(default=chosen で数値が同一になる設計)。「未使用検証で汎化を確認した」の意味に使わない。late_response・fine_grained_creep の0件検出は「起きなかった」の証明ではない(`gate="m5b_raw"` の構造上、受理手に現れないだけの可能性。percentileゲート×広い予算での追試が要る)。(05-synthetic, 2026-09-28)

## hanger 低荷重(06)

- 範囲: 小型の広い行列は05が担当し、hangerは H1(200N・2.0mm・最小幅は実寸10mm固定)の代表条件の外部確認。H2(5000N)は4ゾーン診断のみ。(06-hanger-low, 2026-09-28)
- 共有ヘルパー `hanger_low_support.py`: `BASELINE`(H1条件)・`EXTRA_RESOLUTIONS_MM`・`project_mask_to_context`/`project_mask_raw`(島処理なしの素の投影)・`repair_projection`(保持セルの補修量を返す)・`truss_only_cells(ctx, *, resolve_islands=True)`・`truss_residual_fraction`・`capture_unknown_safety`/`unknown_safety_summary`(FEM判定不能の原因文の捕捉)。05の `synthetic_matrix_support.py`・`search_methods.py` は無変更で再利用。(06-hanger-low, 2026-09-28)
- 実行順序(すべて冪等で `--force` を持つ。`.venv\Scripts\python.exe topopt_checks\idle_validation\<name>`): `run_06_main_matrix.py` → `run_06_grid_resolution.py`(1.8/2.2/2.4mm。2.4mm結果を保存)→ `run_06_origin_shift.py` → `run_06_initshape.py`(2.4mm結果に依存)→ `run_06_adaptive_variants.py` → `run_06_w5_reproduction.py` → `run_06_w5_trajectory.py` → `run_06_origin_diag.py` → `run_06_long_budget.py`(`--only`)→ `analyze_06_load_zone.py` → `build_06_json.py`。再生成後は**台帳に `status: error` が残っていないかを必ず確認**する。各スクリプトは例外を握りつぶして終了コード0で終わるので、待機チェーンの `COMPLETE` は成果の証拠にならない(1.8/2.2/2.4mmと粗い骨組みで再実行した)。(06-hanger-low, 2026-09-28)
- **最小幅は実寸mmで固定**し、セル数が違う形(5セル=9.99mmと6セル=11.98mm)を同条件で比較しない。保存形 hanger_base(31,337セル・249,716 mm³)は最小幅5セルの w5 条件の形、最小幅6セルの製品 step20 は別の形(139,230 mm³)。(06-hanger-low, 2026-09-28)
- 「凍結版」の主成績(`prediction_error_multiplier=2.5`・gate=m5b_raw)と、拒否を外した版(1e6)・gate=m5b_target 版は別枠。長予算(`06_long_budget.json`)も別枠で主マトリクス(縮小予算)の行を上書きしない。(06-hanger-low, 2026-09-28)
- トラス専有セルの残存率は「2.2mmトラスの投影−5セル版の現行形」の13,481セル(2.0mm格子)を最終形が含む率。5セル版は定義上0%(循環)で、重い形は高く出る(適応・再配置の98%は全材料に近い)。荷重経路としての寄与は測っていない。2.0mm格子のトラス残存率定義を他格子へ投影すると保持セルが欠けて条件不成立になる(`project_mask_raw`・`repair_projection` で対処)。(06-hanger-low, 2026-09-28)
- **全軸+0.5セルの原点シフトの結果(5,127,648 mm³・SF164.8)は収束した形として使わない**: 探索は22,915セル・SF4.812まで進んだが、最終の除外体ブーリアンが `unknown`(common-volume boolean did not finish cleanly)になり製品が全材料へ戻した。「何も削れなかった」ではなく、捨てられた形のマスクは未保存。(06-hanger-low, 2026-09-28)
- 保存形の48.72 MPa荷重パッド端ピークは5セル最小幅の形(hanger_base/R2)の性質で、6セル形・1.8〜2.4mm・原点移動には出ない。形の寄与と離散化の寄与は未分離(保存形を細分格子へ厳密投影して解き直す検証が残る)。最小幅6セルの step20 の評価ごとの軌跡は製品が返さないので無い(軌跡はw5の1本だけ)。(06-hanger-low, 2026-09-28)
- 製品 step20 は最小幅5セルで安全率が高い(55→17)うちに斜材を失い、評価13の1手で安全率 16.99→5.21・最大変位 0.53→7.64 mm として顕在化するが、目標4.8を上回るので受理する。(06-hanger-low, 2026-09-28)

## hanger 荷重行列(07、縮小範囲)

- 範囲: **保存済み台帳の集計と固定形状診断2件の報告**。`07_hanger_loads.json` は `build_07_json.py` で再生成(新規FEMなし)。生台帳 `07_corpus_rescore.json`(64 ok)・`07_grid.json`(28 ok)・`07_methods.json`(5 ok)・`07_full_material_probe.json`(500N・1000N全材料、2.0mm)・`07_masks/*.npz`(33個)。(07-hanger-loads, 2026-09-28)
- 使える結論: ①保存結果は再現(64/64でSF・材料体積一致、同格子同セル数15/15で同一マスク) ②製品SFゲートは92/92通るが目標換算M5b・M3は92/92不合格、M5b raw は2000Nで0/24 ③局所ピークは荷重が上がるほど固定面近傍(holding)に集中 ④全材料自身が目標換算ゲートを500N以上で、rawゲートを2000Nで通らない(算術外挿の目安: 約300N・約1,430N。**実走していない外挿を実測として引用しない**) ⑤2000N太い幅の適応/再配置3本は出発点(全材料)がゲートを満たさず全候補拒否=出発点を返しただけ(5,098,682 mm³は探索成果ではなく出発点の値で、06の「prediction_error拒否暴走」とは別機構)。結論は「高荷重(500N以上)への適用は採用保留」。(07-hanger-loads, 2026-09-28)
- 旧64条件(セル数指定)と新28条件(実寸mm指定)を混ぜて順位付けしない。最小幅は実現値(セル丸め)で併記し、格子と幅の効果を分離できたと言わない。パーセンタイルゲート(step05/step20)と M5b ゲート(適応・再配置)の体積を比べて優劣を付けない。(07-hanger-loads, 2026-09-28)
- `07_full_material_probe.json` の500N・1000Nは設計空間全体(5,102,427 mm³)で、2000Nの全材料(適応の出発点5,098,682 mm³)と同一マスクではない(差0.07%)。(07-hanger-loads, 2026-09-28)
- 2000N細い幅・適応(凍結)は約9.7時間走ったあと台帳に書かず exit=-1(2026-10-05 19:04:49)で消えた(原因未特定・checkpointなし)。**長い探索の前に、出発点(全材料)がゲートを満たすかを数分で確かめる**(全材料診断は約2.5分)。その結果(lost)・origin/init(未実行)・methods 残り19件は未実施で引用しない。(07-hanger-loads, 2026-09-28)
- 06契約「荷重行列は07が担当」は失効: 500N・1000Nの4方式と原点・初期形状の行列は `deferred_by_user_deadline`(methods 19件・origin 15〜16件・init 8件。shift_all は interrupted_by_user_deadline)。旧全行列再開は禁止。(07-hanger-loads, 2026-09-28。06 の契約を上書き)

## 頑健性(08、縮小範囲: 05/06/07の悪化原因の監査と小型8実行の修正前後比較)

- `08_robustness.json` は `build_08_json.py` で再生成(新規FEMなし)。`08_runs.json`(8件: ok 7・no_solution 1=意図した早期停止・timeout 0)・`08_audit.json`・`08_mask_checks.json`・`08_masks/*_best_ever.npz`。(08-robustness, 2026-09-28)
- 使える結論: ①小分母の予測誤差過剰拒否を保存台帳で実測(上記) ②再配置の追加59件が全件初期全材料へ復帰 ③m5b_raw 受理50実行中27実行が製品SF目標4.8未達 ④修正後、同一ゲート(m5b_raw)で dilution3.0mm 63,013→22,288 mm³・path1.5mm 152,385→54,466 mm³、ただし製品SF 4.04・4.03で目標未達 ⑤同一ゲート(percentile)の dilution3.0mm で修正後の適応は step20 より+6.0%重い(24,854 vs 23,440 mm³) ⑥再配置は追加上限で全材料復帰が止まるが同条件の適応より重い ⑦出発点不成立の早期停止は小型で動作確認(FEM1回・2秒)。(08-robustness, 2026-09-28)
- **結論: 適応(floor付き)は採用保留、再配置は採用候補から外す、m5b_raw単独ゲートは採用しない、早期停止は研究用の安全弁。** 3条件・各1実行の小型限定。比較は同一ゲート内だけ。最小幅は実現値(dilution3.0mmは3セル=8.91mm)で併記し要求値(6.0mm)と混同しない。修正後の数値は08の新規実行だけを根拠にし、07までの旧結果を修正版の性能根拠にしない。(08-robustness, 2026-09-28)
- 未測定・未実施(依存しない): hangerでの効果、floor=0.01の最適性、再配置の修正前対照(05の保存値を使用、同一コード再実行ではない)、percentileゲートでの floor 有無の対照、早期停止の修正前対照、`calibrated_k` の再較正、m5b_raw と製品percentileを同時に課す複合ゲート。(08-robustness, 2026-09-28)
- 08の d3・p15 の「修正後」形は m5b_raw ゲートで製品SF目標未達なので**完成形候補にしない**。代表改善形は製品SFゲートを満たす保存形(05/06の製品 step20 形、`08_masks/d3_adaptive_percentile_after_best_ever.npz`=percentile受理の適応形)から選ぶ。(08-robustness, 2026-09-28)
- 罠: 台帳JSON・結果JSONは UTF-8。Windowsの `open(path)` 既定は cp932 で `UnicodeDecodeError` / 文字化けするので `encoding="utf-8"` と `PYTHONIOENCODING=utf-8` を付ける。`_common.write_atomic` の `os.replace` が一時的な `PermissionError` で落ちることがある(台帳に書かず落ちる→`LEDGER_NOT_OK` と台帳の `status` を見て再起動)。venv の `python.exe` は起動スタブで実体との2プロセスが並ぶ→待機/停止はプロセスの木で扱い、親だけ止めても子は走り続ける。(08-robustness / 09-final-shapes / 10-synthesis, 2026-09-28)

## 最終形状(09、縮小範囲: 保存済み完成形候補3形+小型対照1形)

- 対象: S1_dil25_realloc・S1c_dil25_step20(対照)・S2_hanger200_step20・S3_hanger2000_step20(2000Nは太い幅2.2mm。M5b raw超過が最小の118.6で、07案の2.0mmは1,670)。コード: `final_shapes_09.py`(convert/resolve)・`diagnose_cut_09.py`(material-cutの切り分け)・`run_09_final_shapes.py`(wall timeout つき。ジョブ名 `convert:<形>` / `cutdiag:<形>` / `resolve:<形>:<final|smoothed|smoothed_keep>`)。`09_final_shapes.json` は `build_09_json.py`(FEMなし)で再生成、生データ `09_runs.json`・`09_work/`。(09-final-shapes, 2026-09-28)
- 使える結論: ①小型2形は製品の最終形(平滑化→保持・除外のブーリアン)が約5秒で成立し、水密・単一ソリッド・自己交差0 ②**hangerの2形(200N H1 step20・2000N 2.2mm step20)は最終ブーリアンが不成立**: 保持ボディを引く `material-cut` で保持表面に沿った薄い破片(0.001〜266 mm³)が残り単一ソリッドにならない(13個・10個。2000Nは結果が不正)。旧コーパス64形でも57形が最終形の段階で不成立(成立7形) ③最終形(hangerは平滑化+保持セル足し戻しの近似)を元の粗格子へ再占有化した再解析は、製品SF ±0.5%・M5b raw・M5b目標が保存値とほぼ同じ ④平滑化後の最小幅: 半格子のm立方openingで、hanger200N(要求10mm・実現11.98mm)が10mm立方で3.25%・11.98mmで39.9%の体積を失い、2000N(実現10.99mm)は1.2〜1.9%、小型は6mm立方で0.5%未満 ⑤平滑化メッシュ単独は保持セル10〜12個・荷重面の節点1〜6個が欠けてFEMの条件確認が通らず、保持の足し戻しが必須。(09-final-shapes, 2026-09-28)
- **hangerの完成形(最終CAD)は確認されていない。** 足し戻し近似の粗格子再解析を「最終形の安全の裏付け」と書かない。粗格子の合格で完成形の未確認を代用しない。M5b目標・M3は全形で不合格のまま(製品SFゲートだけを満たす形)。局所ピークは固定面から1mm前後(holding)で、格子細分で増加する特異点(setup 側「参照解」節)の影響下にあり独立な安全の裏付けにならない。結論は「採用保留」。(09-final-shapes, 2026-09-28)
- 小型の S1(再配置・M5b_raw受理)と S1c(製品step20・パーセンタイル受理)は受理ゲートが違う。体積差(-3.6%)を新方式の優位の根拠にしない(1条件・1形で、再配置は08で採用候補から外れている)。(09-final-shapes, 2026-09-28)
- 最小幅損失割合のしきい値(0.1%・1%)は解析者が置いた未校正値で、列(`rows`)を一次の根拠にする。openingは角・接合部の丸めでも損失を出すので損失の全部が要求幅違反ではない。(09-final-shapes, 2026-09-28)
- S3の保持のみの cut の体積不整合(取り除き2,704 mm³対、元のボクセルマスクと保持の共通体積17,725 mm³)の原因は未特定。「OCCTの結果が妥当でも体積が合わない」証拠としてのみ使い、原因を断定しない。`F0500_h2.2_w5`・`F1000_h2.2_w5`(旧コーパスで最終形が成立。07の `F0500_thick10mm_h2.2`・`F1000_thick10mm_h2.2` とマスク同一)の最終形再解析は未実施。(09-final-shapes, 2026-09-28)
- 罠: 終了コード0・convertジョブの `status: ok` は最終形の成立を意味しない(finalizeの例外は結果JSONの `final.status=error`。台帳の `final_status` で読む)。平滑化メッシュを同格子へ再占有化しただけではFEMの条件確認(荷重面・固定面の節点被覆)が通らず、保持セルの足し戻し(`smoothed_keep`)が要る。(09-final-shapes, 2026-09-28)
- 台帳 `09_runs.json` は最新の実行だけを持つ(小型2形は条件確認の記録を足すため同一マスクを2回実行、結果は同一)。(09-final-shapes, 2026-09-28)

## 総括(10、縮小範囲: 07〜09と00〜06の保存結果の統合と監査)

- `10_synthesis.json` は `build_10_json.py`(FEMなし)で再生成。手書きの判断は `synthesis_10_static.json`。`derived`(small_matrix・hanger_gates・h2_5000N・hanger_h1・robustness_08・final_shapes_09・discrepancy_checks・cost・multi_metric・reaudit_03a・design_core_groups)は台帳からの再計算。芯の数え上げ `check_10_core_groups.py --case hanger:<要素mm>:<最小幅mm>`(FEMなし・各数秒)、図 `render_10_figures.py` → `docs/topopt_idle_validation/10_figures/`。(10-synthesis, 2026-09-28)
- 結論(推奨): 製品既定(探索・合否)の変更は採用保留。判定方式は**合否を変えず**、局所超過の警告(部位別最悪・M5b raw・保持部最悪の位置)と、全材料が許容超過のときの入口警告を足す案。節点所有権は実装しない。芯が割れた入力は入口で断る(検証では19条件すべて割れず、根拠はコードとテスト)。(10-synthesis, 2026-09-28)
- **H2(5000N)の93.2 MPaは「除去が作った悪化」ではなく設計空間(全材料)自身の値**(除去の影響 M5b +0.19%・M3 -0.06%)で、固定面から約0.5要素にある格子依存のピーク。危険と断定しない(実取付条件・荷重の想定は外部入力不明)。(10-synthesis, 2026-09-28)
- 依存しない: 節点所有権の割合・割れた入力の頻度(未測定)。08の監査件数と07の再現(10は台帳の値を採用しチェックポイントから再集計していない)。全材料の外挿荷重(約1,430N・約1,465N)は実走していない。`step20_full` は `step20` の詳細版でwall時間を足すと二重計上になる。(10-synthesis, 2026-09-28)

## 延期項目(deferred_by_user_deadline)

- 07: methods 残り19・origin 15(16)・init 8。08: 6項目(上記「未測定・未実施」)。09: 最終CADの細分格子FEM再解析・hangerの最終ブーリアン修正と再検証(薄い破片への対処)・`F0500_h2.2_w5`/`F1000_h2.2_w5` の再解析・小型の複数条件/同ゲート追試(例 dilution 250N・step05)・hangerの高荷重(2000N)の最終形評価。05: 合成5題材。06: 凍結パラメータ変更・別seed。03a: dilution level4の等比検定。04a: 原点・他格子の因果検査。固定面近傍のピークを収束する量に置き換える検討、実取付条件の確認。(07-hanger-loads / 08-robustness / 09-final-shapes / 10-synthesis, 2026-09-28)
