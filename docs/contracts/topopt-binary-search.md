# 二値モデル・応力制約探索の契約

形状最適化を SIMP から二値モデル・応力制約探索(`duocad/topopt/binary_search.py`)へ移した段階1〜7(FEM抽出・受入6条件・初期実行可能解・
削除追加置換・製品配線・旧経路撤去・docs整理)で、ソースを読んでも分からない不変条件・依存禁止・罠を、元の作業タスクの契約から蒸留した文書。
**いまの挙動の正はコードとテスト**。計画と現行仕様は [../PLAN_BINARY_STRESS_SEARCH.md](../PLAN_BINARY_STRESS_SEARCH.md)(§1〜§5 が仕様、§11〜§13 が段階3・4の後続の置き換え)にあり、ここでは繰り返さない。
段階4の peel・停止規則・中空なし既定は [topopt-measurements-stage4.md](topopt-measurements-stage4.md)。

項目末尾の括弧は出典(元タスクID, 日付)。段階3・4の元の契約は、その後の本人指示(2026-09-20 `surface_only` 削除、2026-09-21 段階3の探索削除)で
大半が置き換わっている。現行と食い違う旧契約は落とし、残した項目は現行コードで確認した。`(対象コード不在・要確認)` はその名前がいまのソースに無いもの。

## fem.py(FEM の唯一の実装)と material.py

- `duocad/topopt/fem.py` が二値FEMの唯一の実装(`fem.Model` / `PreparedSystem`・CG/direct/MG・残差検算 `_verify_solution`)。二値評価は
  `active_mask`(bool、True=材料)と、浮いた節点を系から外す `extra_fixed_dofs` を渡す。`rho` は0/1なので `rho**penal` は `penal` に依らず不変。
  `Model.solve(rho, penal, *, solver=...)` の `solver` 既定は呼び出し側で `'auto'`(`fem.choose_solver` が `cg`/`direct` を選ぶ)。引数の詳細はソース参照。
  (20260917-0440-binary-search-1-fem-extract, 2026-09-17)
- 浮いた節点の DOF を作る関数は、移設時の `fem._floating_dofs` から `anchor.floating_dofs` に移っている(`fem._floating_dofs` は不在。`fem.py` のコメントも
  `anchor.floating_dofs` を指す)。(20260917-0440-binary-search-1-fem-extract, 2026-09-17)(対象コード不在・要確認: `fem._floating_dofs` という名前では呼べない)
- 許容応力・ヤング率・印刷低減係数(`PETG_YIELD_MPA` / `PRINT_DERATE` / `DEFAULT_ALLOWABLE_MPA` / `DEFAULT_YOUNGS_MODULUS_MPA`)の唯一の定義は
  `duocad/topopt/material.py`。`acceptance.py` / `binary_search.py` はここから直接 import し、別の場所で定義し直さない。(20260917-0440-binary-search-1-fem-extract, 20260917-0441-binary-search-2-acceptance, 2026-09-17)
- **可変グローバル(`fem.PARALLEL_THREADS` / `fem._SOLVE_STATS`)は `fem` を import して `fem.X` を直接触る。** `from .fem import PARALLEL_THREADS` で再輸出した
  名前は import 時点のコピーで、読み取りは効くが代入は届かない。並列度を一時的に変える検査・計測は `fem.PARALLEL_THREADS = 1` と書く(`check_parallel.py` が該当)。
  (20260917-0440-binary-search-1-fem-extract, 2026-09-17)
- `fem.Model` / `PreparedSystem` は移設前と計算上完全に同一(要素剛性・DOF番号・組立・CG/direct/MG・残差検算)。`topopt_checks/check_fem.py` が二値の棒・梁4題材
  (内部空洞・表面切り欠き・複数空洞+スレッド並列)の変位・コンプライアンス・反力・von Mises応力を SHA-256/スカラーで固定している。**`fem.py` を触ったら最初にこれを走らせる**
  (`.\.venv\Scripts\python.exe topopt_checks\check_fem.py`)。基準値は移設前の実装(コミット `2223908`)から採った —— 移設後同士の比較で一致を名乗らない。
  再採取が要るときは移設前の状態に戻して採る(他者の未コミット変更を壊さない手段で)。(20260917-0440-binary-search-1-fem-extract, 2026-09-17)
- `fem.Model.__init__` に `dynamic_active` 引数は無い(渡すと `TypeError`)。`dead_rho_threshold` / `reset_active` / `_refresh_active` / `_split_dofs` / `_apply_active` と
  `_SOLVE_STATS["active_refresh_seconds"]` も削除済み。**`rho**penal` と `emin` 自体は `assemble` / `compliance` / `stiffness_apply` に残っている**(二値 rho∈{0,1}
  では penal に依らず無害に縮退する。計画書の「要素剛性・組立を維持する」指示との折衷読み)。(20260917-0445-binary-search-6-legacy-removal, 2026-09-17)
- 反力ユーティリティは `fem.load_resultant(f)`(荷重合力)と `fem.reaction_forces(model, rho, penal, u)`(拘束自由度**全体**の反力の合力 `R=(Ku-f)|_fixed` の軸別合計)。
  **荷重釣り合いの確認にだけ使う**。拘束面ごとの内訳・下限制約・感度(随伴)は持たない(旧SIMPの `ReactionGroup`・反力制約・MMA配線の役目で、削除済み)。
  二値探索(`acceptance.py` / `binary_search.py`)は使わない。`check_fem.py` の反力SHA基準値と1ビット一致(単一グループなら `tobytes()` は形に依らず同じ)。
  (20260917-0445-binary-search-6-legacy-removal, 2026-09-17)
- **`duocad/topopt/trace.py` の `Model` 直列化は、フィールド名リストがハードコードされている。** `fem.Model` のフィールドを増減すると `trace._encode` が
  `getattr(value, n)` で `AttributeError` になり、`keep_model=True` や `trace.recording()` を使う検査(`check_trace.py`・`tests/test_optimization_trace.py`)が壊れる。
  フィールドを変えたら `trace.py` のリストも直す。(20260917-0445-binary-search-6-legacy-removal, 2026-09-17)
- 旧 SIMP モジュール `simp.py` / `safety_loop.py` / `bench.py` は存在しない(`from duocad.topopt import simp` は `ModuleNotFoundError`)。`duocad/topopt/__init__.py` の層構成は
  `fem → voxelize → anchor → design_space → stress → material → acceptance → binary_search → mesh`(後に `finalize` 等が加わった)。
  SIMP の OC/MMA・`penal_at`・`optimize()`・`run_preset()` は無い。(20260917-0440-binary-search-1-fem-extract, 20260917-0445-binary-search-6-legacy-removal, 2026-09-17)
- 合成密度場(SIMPを回さずガウスぼかしで作る)を使う検査(`check_parallel.py` / `check_stress.py` / `check_mesh.py`)は、ファイルごとにシード・sigma・格子サイズに対して
  **実測校正した閾値**を持つ。これらを変えると体積一致率や前処理の効きの閾値を割る。変更時は各ファイルのコメントの実測値で再校正する。
  `check_mesh.py` は marching cubes 直後の体積一致(5%以内)と Taubin 平滑化の体積保存(5%以内)を両立させるため、単一 sigma ではなく「大きい sigma で太い塊を作って
  二値化し、境界だけ sigma=0.6 でぼかし直す」二段階の合成にしてある(細い突起があると Taubin で体積が大きく動く)。(20260917-0445-binary-search-6-legacy-removal, 2026-09-17)
  (`check_multigrid.py` は対象コード不在・要確認: `topopt_checks/` に無い)

## acceptance.py(6条件の唯一の実装)

- **boolマスクを直接受ける製品APIは `acceptance.inspect(material, before, grid, *, table, fixed_faces, load_faces, forces, preserves, excludes, target_safety_factor, ...)`。**
  `material` / `before` は格子と同じ形の `dtype==bool`。float の 0/1 でも `ValueError`(型を厳密に見る)。`before` は島除去前のboolマスクで、「除去後に材料が増えていないか」だけを見る
  (密度の閾値変換はしない)。探索の候補評価は `inspect` を直接呼ぶ(候補は最初からboolなので density/level の概念は要らない)。引数の全体はソース参照
  (現行は `solver='auto'`・`region_overlaps` ・`fields` ・`member_volume_mm3` ・`keep_cells` が加わっている)。(20260917-0441-binary-search-2-acceptance, 2026-09-17)
- **密度→bool変換は読み取りアダプター `inspect_document(document, rho_clean, rho_before, grid, *, level=0.5, **kwargs)` だけの責務。** 過去記録(密度場)を読むときだけ使い、
  `rho > level` の二値化と、二値体積とは別枠の密度積算量(`report['volumes']['density_integral_before_mm3'/'density_integral_after_mm3']`)をここでだけ作る。
  `topopt_checks/check_pre_smoothing.py` はこの関数だけを呼ぶCLIで、6条件のロジックを複製しない。`inspect()` は入力を書き換えず、自分では二値化しない。
  `inspect` が density+level を内部で二値化する形は計画書§5「新規の製品APIはboolマスクを受ける」に反する(最初に旧設計のまま移設しかけた)。(20260917-0441-binary-search-2-acceptance, 2026-09-17)
- 個別検査も公開: `boundary_check` / `connectivity_check` / `active_nodes`(幾何、OCP不使用)、`region_check` / `intersection_volume`(CAD、`duocad.kernel*` 越し)、
  `condition_check`(境界条件)、`safety_check`(FEM、`fem.Model` を直接構築)。(20260917-0441-binary-search-2-acceptance, 2026-09-17)
- **CAD共通体積の生プリミティブは kernel 層に置く**(現行 `duocad/kernel_brep.py` の `boxes_common_volume` / `require_valid_solid` / `grid_cell_common_volumes` /
  `box_common_volume`。`kernel.py` の `BoundingBoxCache` / `shape_key`)。**Grid / material 配列の概念を持たず**、生の `(min, max)` mm 座標のリストだけを受ける。
  `acceptance.py` は自分で OCP を import しない(`tests/test_boundaries.py::test_only_kernel_module_imports_ocp` の `OCP_ALLOWED` に載せない)。
  `topopt/voxelize.py` が OCP を許されているのは「B-repの厳密なbbox(`SetGap` 順序)が要る」という名指しの理由のためで、無関係なOCP呼び出しを便乗させない。
  (20260917-0441-binary-search-2-acceptance, 2026-09-17)
- bbox計測に `BoundingBoxCache` を使い回せる(`cache=` 引数。`inspect_document` は既定で `document.bbox_cache` を渡す)。`region_check` / `intersection_volume` の体積は
  `bbox_cache` の有無で一致する(`tests/test_pre_smoothing.py::test_intersection_volume_matches_with_and_without_bbox_cache` /
  `::test_inspect_result_is_identical_with_and_without_bbox_cache` で固定)。(20260917-0441-binary-search-2-acceptance, 2026-09-17)
- **計算失敗(残差検算・特異行列・非有限値)は例外を握って `status='unknown'` を返す**(`safety_check` / `region_check` とも)。安全率不足・体積不足(不合格)へ変換しない
  (`tests/test_pre_smoothing.py::test_solver_failure_is_unknown_not_unsafe`)。(20260917-0441-binary-search-2-acceptance, 20260917-0442-binary-search-3-initial-feasible, 2026-09-17)
- `acceptance.py` は `fem.Model` / `fem.SOLVE_RESIDUAL_TOL` を直接使う(`simp` を経由しない)。許容応力等の既定は `material.py` から import する。
  `topopt_checks/pre_smoothing_acceptance.py` / `pre_smoothing_geometry.py` は削除済みで、製品側 `acceptance.py` へ全面移設した(import しない)。(20260917-0441-binary-search-2-acceptance, 2026-09-17)

## binary_search.py: 初期実行可能解(段階3)

- 入口は `find_initial_feasible(design_space_mask, conditions, *, max_evaluations, min_width_cells=1, is_cancelled=None) -> SearchResult`。
  `SearchConditions` は `acceptance.inspect` への引数を束ねたもの(`percentile=None` は「acceptance.py の既定へ委ねる」で、既定値をここへ複製しない。
  `.inspect_kwargs(bbox_cache=...)` で展開)。(20260917-0442-binary-search-3-initial-feasible, 2026-09-17)
- **段階3の探索(ブロックの削除・追加・置換、frontier、`repair_candidates`、`block_sizes`)は削除済み**(2026-09-21 本人指示。計画書§13)。段階3は入口検査+全材料マスクの
  **1回の評価**だけで、合格しなければ形を探さず落ちた検査を返す。`SearchResult.status` は `'feasible' | 'no_feasible_found' | 'input_contradiction' | 'resolution_insufficient' |
  'boundary_mapping_failed'` の5値(初期の4値に `boundary_mapping_failed` が加わった)。`termination` の語彙は `SearchResult` の docstring が正
  (段階3: `initial_feasible_found` / `full_material_infeasible` / `resolution_insufficient` / `boundary_mapping_failed` / `input_contradiction`、共通: `cancelled` / `evaluation_error`)。
  `best_feasible` は `status=='feasible'` のときだけ非None。(20260917-0442-binary-search-3-initial-feasible, 2026-09-17; 置き換え: 計画書§13)
- **`FeasibleModel.report` は `bbox_cache=None` で最終再検査した `acceptance.inspect` の戻り値そのもの**(§3)。最良合格モデルは必ず「キャッシュ無しの最終再検査」を経てから返す。
  この再検査が `pass` でなければ `feasible` を返さない。`SearchConditions.inspect_kwargs(bbox_cache=None)` は保持・除外の前計算(`region_overlaps`)も一緒に外す。
  (20260917-0442-binary-search-3-initial-feasible, 20260917-0443-binary-search-4-remove-add-replace, 2026-09-17)
- **`before` は「島除去の直前の候補」自身であること。** `acceptance.inspect` の `material ⊆ before` 契約(§2)は、`resolve_islands(candidate)` が candidate から `ndimage.label` で
  生存成分を作る以上 `survivor ⊆ candidate == before` で構造的に成り立つ。祖先・親のsurvivorを `before` に使うと、候補が親の材料の外へ広がった場合に
  `ValueError('島除去後に材料セルが増えている')` で落ちる(QAで「survivorに1セル穴、design_spaceは満杯、addで穴埋め」が実例)。回帰テスト
  `test_add_and_replace_repairs_never_violate_the_before_superset_contract` が `acceptance.inspect` を spy して契約を確認していた。
  (20260917-0442-binary-search-3-initial-feasible, 2026-09-17)(対象コード不在・要確認: add/replace 生成は削除済みで、このテスト `test_add_and_replace_repairs_never_violate_the_before_superset_contract` も現行 `tests/` に無い。原則だけ有効)
- `resolve_islands(candidate, conditions) -> (survivors, diagnostics)`(島の除去は§4.4。全保持セルを含む成分だけを残す)は段階4(`peel` の検算)でも同じ規則で使う。
  `evaluation_identifier(conditions)` も公開。`visited_candidates`(生の候補)と `visited_survivors`(島除去後)を別集合にする原則は、frontier が無くなった
  いまは該当する集合が無い。(20260917-0442-binary-search-3-initial-feasible, 2026-09-17)(frontier 3要素・`visited_*`・`repair_candidates` の接続条件・`solver_retry`・生候補の予算の契約は、2026-09-21 の探索削除で落とした)
- 計算失敗は不合格の証拠にしない(§4.5)。`unknown` の候補から幾何修復を生成しない、という旧契約は探索削除で消えた。現行は「全材料のFEM計算失敗だけなら `evaluation_error`」。
  (20260917-0442-binary-search-3-initial-feasible, 2026-09-17; 置き換え: 計画書§13)
- 段階3の `max_evaluations` は合格形状の報告(`report['search']`)に写すだけで、段階3の評価は常に1回。(置き換え後の現行。20260917-0442-binary-search-3-initial-feasible, 2026-09-17)
- `topopt_checks/` に段階3専用の検査CLIは無い(§6.1の `check_safety_loop.py` 置換は段階6が担当した)。(20260917-0442-binary-search-3-initial-feasible, 2026-09-17)

## binary_search.py: 削減(段階4)の入口と終了

- 製品配線が呼ぶ**単一の入口は `find_best_feasible(design_space_mask, conditions, *, max_evaluations=1000, ..., single_cell_patience, target_tolerance, evaluation_error_patience,
  min_width_cells=1, is_cancelled=None, on_evaluation=None) -> SearchResult`**。内部で `find_initial_feasible`(段階3)→ `_reduce_volume`(段階4)をこの順に呼ぶ。
  `max_evaluations` は**段階3・段階4を通した1本の予算**(別々にしない)。`status` が `'feasible'` 以外なら段階4は何もせず段階3の結果をそのまま返す(`best_feasible=None`)。
  (20260917-0443-binary-search-4-remove-add-replace, 2026-09-17)
- 返却は必ず `best_feasible` から。`status=='feasible'` の `best_feasible` は**常にキャッシュ無し再検査済み**(§3・§4.6)で、製品配線側が再検査し直す必要はない。
  `best_feasible.material_mm3` は探索が進んでも増えない。同じ入力・同じ予算・同じ設定なら結果は決定的(乱数・dict/set の反復順に依存する分岐は無い)。
  (20260917-0443-binary-search-4-remove-add-replace, 2026-09-17)
- `is_cancelled` は引数無しで呼べる callable。真を返すとその時点の `best_feasible`(再検査済み)を保ち `termination='cancelled'` で返る。初期合格の前に中断されれば
  `no_feasible_found` / `cancelled`。段階3は評価の前、段階4は削る候補を作る前ごとに見る。(20260917-0443-binary-search-4-remove-add-replace, 2026-09-17; 現行 docstring に合わせ更新)
  (旧契約「中断は削減段階にしか効かず段階3は反応しない」は、段階3が1回評価だけになったため解消した)
- **段階4は合格形状から合格形状へしか動かない**(不合格の候補は捨てて戻る)。体積を真に減らせる候補が無い題材は、初期合格のまま `evaluation_limit` 等で終わりうる
  — これは§9が許容する結果(「初期合格だけで止まって軽量化できない場合は、その事実と終了理由を記載し、体積削減機能の完成と扱わない」)。大域最小の保証はしない(§1)。
  段階5〜7で実機題材(hanger)の既定予算・停止規則での収束を確かめる必要があった。(20260917-0443-binary-search-4-remove-add-replace, 2026-09-17)
- 旧段階4(削除・追加・置換の巡回、`deque(maxlen=frontier_capacity)` の FIFO frontier、`max_consecutive_adds`、体積昇順の優先度付け)は削除済み。以下の2点は旧実装で得た知見で、
  現行の peel(ひずみエネルギー順位で削る)でも「削除だけでは届かない形」の限界として残る:
  **削除だけでは体積を減らせない題材が実在する**(削除は材料マスクの部分集合しか作れず、最小形状が必要とする「今は材料の無いセル」へ届かない)。一方で
  frontier を体積昇順の安価な事前指標で優先付けすると、一時的に体積が増える `add` 系が常に後回しになり**FIFOより悪化**した(取り出し順でも予約枠が消費される設計が要る)。
  (20260917-0443-binary-search-4-remove-add-replace, 2026-09-17)(`frontier_capacity` / `max_consecutive_adds` / `_is_smaller_feasible` は対象コード不在・要確認)

## 製品配線(optimize_run.py ほか)

- `duocad/optimize_run.py` の `_run()` は計画書§3の単一経路: `build_conditions` → `resolution_limits(doc)`(**`rmin_mm` 引数は無い**)→ `design_space.derive`(1回)→
  `binary_search.find_best_feasible(space.mask, conditions, is_cancelled=should_cancel, on_evaluation=..., **{"max_evaluations": ...} if opts.max_evaluations)` →
  `mesh.build_surface_from_mask(best.mask)` → `finalize` → `add_solid_body`。`SearchConditions` は `_build_search_conditions(doc, space, bundle, opts, table=, mapping=)` が組み立てる
  (`preserves` / `excludes` は `(name, shape)` のタプル、`bbox_cache=doc.bbox_cache`)。(20260917-0444-binary-search-5-product-wiring, 2026-09-17)
- **探索候補の評価中にメッシュ化・STL出力を一度も呼ばない。** `find_best_feasible` は `acceptance.inspect` しか呼ばず `mesh` に触れない(構造的保証)。後段へ渡るのは
  `search_result.best_feasible.mask`(再検査済み)だけ。(20260917-0444-binary-search-5-product-wiring, 2026-09-17)
- **`mesh.build_surface_from_mask(mask, *, level, spacing=DEFAULT_SPACING, ...)` に実 `spacing` を渡さない。** 要素添字空間のまま渡し、`optimize_run._mesh_to_world`
  (`grid.origin + verts * grid.spacing`)が実寸変換を一括で行う契約。実 spacing を渡すと二重に掛かりメッシュが格子の外へ飛ぶ(実測: 格子[-21.5,91.5]に対しメッシュ[25,786])で
  `_check_mesh_placement` が `FaceMappingError` で落ちる。この入口は `apply_density_masks` / `remove_islands` を呼ばない確定マスク専用。回帰テスト
  `test_no_candidate_meshing_and_the_confirmed_mask_reaches_finalize` が `"spacing" not in kwargs` を直接assertする。旧 `anchor.run_pipeline` の `mesh.build_surface` 呼び出しも `spacing` を渡さない。
  (20260917-0444-binary-search-5-product-wiring, 2026-09-17)
- **中断(`termination=='cancelled'`)では `best_feasible` が実在しても製品文書へ追加しない**(§4.6)。`status=='feasible'` かつ `termination=='evaluation_limit'` 等の
  中断ではない終了は通常どおり保存する。2つを混同しない(退行検出の `test_search_cancellation_discards_a_found_best_feasible_too` は現行 `tests/` に無い。対象コード不在・要確認)。(20260917-0444-binary-search-5-product-wiring, 2026-09-17)
- `OptimizeOptions` から `rmin_mm` / `max_iter` / `material_min_mm3` / `material_max_mm3` を削除。`session.run_optimization()` / `optimize_resolution()` は旧4引数をキーワードで受け続け、
  `None` 以外が来たら即 `OptimizeInputError`(黙って無視しない)。`net/ws.py` の `_optimize_start` はこの4引数と `max_evaluations` を転送する。`catalog.py` の `run_optimization` は
  `rmin_mm` パラメータが無く `max_evaluations` がある(`ws="special"`・`mcp=False` なので文書化のみ)。現行の `OptimizeOptions` は `target_safety_factor` / `search_target_factor` /
  `element_size_mm` / `min_width_mm` / `max_evaluations`(当初の4フィールドに後から加わった)。(20260917-0444-binary-search-5-product-wiring, 2026-09-17)
- `run()` の戻り `state` は当初の6値(`done` / `empty_condition_faces` / `unrepresented_regions` / `no_feasible_found` / `input_contradiction` / `resolution_insufficient`)から、
  現行は `boundary_mapping_failed` / `fill_infeasible` / `final_unsafe` も出る(`optimize_run.py` の docstring が正)。`"done"` の戻りに `termination`(探索の終了理由文字列)を持ち、
  `achieved_material_mm3` / `converged` / 詳細 `history` は無い。(20260917-0444-binary-search-5-product-wiring, 2026-09-17; 現行コードで更新)
- `reactions`(拘束面ごとの反力)は `acceptance.inspect` が計算しないので、旧 `anchor.run_pipeline` の経路は無い。UIも参照しない(`web/src/session.ts` に使用箇所0、2026-09-17 実測)。
  反力表示を作るなら FEM 層への新規配線が要る。(20260917-0444-binary-search-5-product-wiring, 2026-09-17)(`optimize_run.py` に `reactions` は残っていない)
- **所要時間の事前見積もり(`estimate_seconds`、`resolution_limits()` の `estimate`、各ステップの `seconds_typical` / `seconds_worst`)は廃止**。実測ベースの見積もりへの置き換えは
  していない。フロントの `OptimizePanel.svelte` から最小部材幅 rmin の入力欄と所要時間表示、`App.svelte` の `optimizeRminText` / `optimizeRminMm` も削除した
  (後に `min_width_mm` の入力が別の意味で加わっている)。(20260917-0444-binary-search-5-product-wiring, 2026-09-17)
- 進捗コールバックの粒度: 当初は `find_best_feasible` に評価ごとの callback が無く、UIは `phase` の変化だけを見た。**現行は `on_evaluation` が有り評価ごとに `pump.emit`** する
  (旧契約は上書き)。(20260917-0444-binary-search-5-product-wiring, 2026-09-17; 置き換え: `find_best_feasible` の `on_evaluation`)
- **rmin を廃止した結果、ユーザーが解像度を指定しないと「いちばん粗い」解像度が使われ、細長い直方体を離して並べた題材で `voxelize.occupancy` が「レイのずらしを試してもすべて
  縮まない」ValueError を出しうる**(実測: 2ボディ 10x40x40 と 10x40x10 を60mm離す題材で、要素サイズ14mmまで成功・16mm以上で失敗。`resolution_limits(doc)` の
  `max_element_size_mm` ≈19.6mm)。原因は `voxelize.occupancy` のレイキャスト頑健性で、段階5の担当外。以前は rmin が暗黙の安全マージンになっており粗い解像度を derive するテストが無かった。
  テストは `element_size_mm` を明示して避けている。(20260917-0444-binary-search-5-product-wiring, 20260917-0445-binary-search-6-legacy-removal, 2026-09-17)(要確認: 現行 `voxelize.py` は「レイのずらしを N 通り試したがすべて縮退した」で例外を出す。
  `_run()` が `OptimizeInputError` に翻訳しているかは未確認)
- `check_optimize_wiring.py` は題材(離れた2パッド、10x40x40 と 10x40x10・60mm離す)が上記の既知バグの実例そのもので、`element_size_mm` を明示しないと落ちる。
  段階6で `CHECK_RMIN_MM` を `CHECK_ELEMENT_SIZE_MM = 4.0` に置き換えた(`session.run_optimization(element_size_mm=...)`)。(20260917-0445-binary-search-6-legacy-removal, 2026-09-17)

## 旧経路の撤去(段階6)と docs 整理(段階7)

- `topopt_checks/measure_binary_search.py`(`measure_safety_search.py` の後継。二値探索の評価回数・最良体積の履歴を書く)は**未実機検証のまま作られた**(当時 `models/*.json` が無く
  構文・import確認まで)。(20260917-0445-binary-search-6-legacy-removal, 2026-09-17)(対象コード不在・要確認: `topopt_checks/measure_binary_search.py` は現在存在しない)
- `anchor.py` の `_cli()`(`python -m duocad.topopt.anchor`)は面アンカー表の要約だけ(`--table-only` / `--max-iter` と full-pipeline 実行を削除)。`check_anchor.py` の旧[7]
  「端から端まで」(合成ブラケットでSIMPまで通す回帰)は完全削除。docs に検査項目を書くときは print 文の番号を実ファイルで確認する(番号をそのまま引き継ぐと存在しない項目を書く)。
  (20260917-0445-binary-search-6-legacy-removal, 20260917-0446-binary-search-7-docs, 2026-09-17)
- `duocad/topopt/output/` に残る追跡済みSTL(`check_design_space_case1.stl` / `check_design_space_case5.stl` / `design_space_box.stl` / `design_space_bracket.stl`)と
  `optimized.stl`・`check_mesh.py` が書く成果物は、現行の残した検査が実際に書き出すファイル。削除しない。(20260917-0445-binary-search-6-legacy-removal, 2026-09-17)(要確認: 現行の `output/` の追跡状況は未確認)
- **`check_design_space.py` の `check_case1_end_to_end`(旧§5相当)は、当時 `CASE1_LONG_AXIS=14` のまま `binary_search.find_initial_feasible` を回すと全要素材料の初期候補が watertight にならず
  `no_feasible_found` になった**(`elements_long_axis` を 14/20/24/28 で試すと全て失敗、**18でだけ**成功)ので、その1検査だけ別解像度18で `derive()` をやり直していた。
  題材固有の解像度依存で、解像度を使う検査を新設するときも同じ落とし穴がありうる。(20260917-0445-binary-search-6-legacy-removal, 2026-09-17)(要確認: 現行の `check_case1_end_to_end` は `find_initial_feasible` を呼ばず、
  `derive()` と条件の型確認を行う形に変わっている。この罠自体が現行で再現するかは未確認)
- docs 整理で `docs/TOPOPT_MMA_VS_OC.md` / `TOPOPT_REACTION.md` / `TOPOPT_SIMP_ITER_BREAKDOWN.md` / `TOPOPT_ACTIVE_SET.md` を削除した。価値のある実測の移設先:
  許容応力の導出・荷重の実単位化・反力は荷重釣り合い確認にだけ使う → `docs/TOPOPT.md`「von Mises応力と最小安全率」の3小節。1回のFEM解(assemble / mg_setup / CG)の費用内訳 →
  `docs/TOPOPT_SPEED.md`「1回のFEM評価(assemble + CG)の内訳」。(20260917-0446-binary-search-7-docs, 2026-09-17)
- `docs/TOPOPT.md` の「### 現在の呼び出し経路」が現行の `optimize_run.run → _run → design_space.derive → binary_search.find_best_feasible → mesh.build_surface_from_mask → finalize → add_solid_body` の図。
  リポジトリ最上部の「何が/どこにあるか」表が「現在の呼び出し経路」として**直接リンクしている**唯一の節なので、呼び出し経路が変わったら真っ先にここを直す。
  `docs/TOPOPT.md` の設計(節の並びは時系列、現在の値は「現在の正」表を見る)は維持されていて、2026-09-04〜2026-09-16 の歴史的な節は日付付き・過去形のまま意図的に残してある。
  **全文grepで `simp.` / `safety_loop` / `volfrac` 等の旧語がヒットしても、それだけでは不備ではない**(現在時制の節に旧語があるかを見る)。(20260917-0446-binary-search-7-docs, 2026-09-17)
- `docs/TOPOPT_SPEED.md` の「1回のFEM評価の内訳」はSIMP時代(2026-09-09)の実測の転用で、現在の二値探索の候補あたり秒数・評価回数の分布を表さない(その後、前計算・peel・停止規則の実測が
  別の節に加わっている)。`docs/TOPOPT_RECORDING.md`「保存する段階」は旧「材料量探索」「SIMP全反復」を「削除・追加・置換探索(search-result)」1項目に統合し、**個々の候補は
  ディスクへ記録しない**ことを明記した。(20260917-0446-binary-search-7-docs, 2026-09-17)
- **同じ古い主張が文書内に複数箇所あるときは、1箇所直して終わりにしない。** `estimate_seconds` の削除を書いたつもりでも `docs/TOPOPT.md` 内に「各段に予想時間が付く」が3箇所独立に
  あり、QAが自己矛盾と指摘するまで1箇所しか直せなかった。**直す前に古い主張を文書全体でgrepする**(`grep -n "予想時間\|estimate_seconds"` のように)。
  関数名変更(旧 `simp.py` → `fem.py`)は、契約表のような**現在時制の表**にだけ確実に反映すればよく、歴史的節の同じ関数への言及まで直す必要はない。(20260917-0446-binary-search-7-docs, 2026-09-17)
- `tests/README.md` の `duocad/optimize_run.py` 節は現行API(`resolution_limits(doc)` に `rmin_mm` 無し・旧4引数は `OptimizeInputError` で拒否・`derive` は1回・候補中メッシュ化なし)を書いてある。
  (20260917-0446-binary-search-7-docs, 2026-09-17)
