# 二値モデル・応力制約探索の契約

二値モデル・応力制約探索(`duocad/topopt/binary_search.py`)の FEM・受入6条件・初期実行可能解・削減・製品配線について、ソースを読んでも分からない不変条件・依存禁止・罠。
**いまの挙動の正はコードとテスト**。現行仕様は [../PLAN_BINARY_STRESS_SEARCH.md](../PLAN_BINARY_STRESS_SEARCH.md)(§1〜§5 が仕様、§11〜§13 が段階3・4の後続の置き換え)。
段階4の peel・停止規則・中空なし既定は [topopt-measurements-stage4.md](topopt-measurements-stage4.md)。

項目末尾の括弧は出典の元タスクID(日付はすべて 2026-09-17)。

## fem.py(FEM の唯一の実装)と material.py

- `duocad/topopt/fem.py` が二値FEMの唯一の実装(`fem.Model` / `PreparedSystem`・CG/direct/MG・残差検算 `_verify_solution`)。二値評価は `active_mask`(bool、True=材料)と、浮いた節点を系から外す `extra_fixed_dofs` を渡す。`rho` は0/1なので `rho**penal` は `penal` に依らず不変。`Model.solve(rho, penal, *, solver=...)` の `solver` 既定は呼び出し側で `'auto'`(`fem.choose_solver` が `cg`/`direct` を選ぶ)。(20260917-0440-binary-search-1-fem-extract)
- 浮いた節点の DOF を作る関数は `anchor.floating_dofs`。(同上)
- 許容応力・ヤング率・印刷低減係数(`PETG_YIELD_MPA` / `PRINT_DERATE` / `DEFAULT_ALLOWABLE_MPA` / `DEFAULT_YOUNGS_MODULUS_MPA`)の唯一の定義は `duocad/topopt/material.py`。`acceptance.py` / `binary_search.py` はここから直接 import し、別の場所で定義し直さない。(20260917-0441-binary-search-2-acceptance)
- **可変グローバル(`fem.PARALLEL_THREADS` / `fem._SOLVE_STATS`)は `fem` を import して `fem.X` を直接触る。** `from .fem import PARALLEL_THREADS` で再輸出した名前は import 時点のコピーで、読み取りは効くが代入は届かない。並列度を一時的に変える検査・計測は `fem.PARALLEL_THREADS = 1` と書く(`check_parallel.py` が該当)。(20260917-0440-binary-search-1-fem-extract)
- `topopt_checks/check_fem.py` が二値の棒・梁4題材(内部空洞・表面切り欠き・複数空洞+スレッド並列)の変位・コンプライアンス・反力・von Mises応力を SHA-256/スカラーで固定している。**`fem.py` を触ったら最初に走らせる**(`.\.venv\Scripts\python.exe topopt_checks\check_fem.py`)。基準値は移設前の実装(コミット `2223908`)から採った。変更後の実装同士の比較で一致を名乗らない。再採取は基準の実装の状態に戻して採る(他者の未コミット変更を壊さない手段で)。(20260917-0440-binary-search-1-fem-extract)
- `rho**penal` と `emin` は `assemble` / `compliance` / `stiffness_apply` に残っている(二値 rho∈{0,1} では penal に依らず無害に縮退する)。(20260917-0445-binary-search-6-legacy-removal)
- 反力ユーティリティは `fem.load_resultant(f)`(荷重合力)と `fem.reaction_forces(model, rho, penal, u)`(拘束自由度**全体**の反力の合力 `R=(Ku-f)|_fixed` の軸別合計)。**荷重釣り合いの確認にだけ使う**。拘束面ごとの内訳・下限制約・感度(随伴)は持たない。二値探索(`acceptance.py` / `binary_search.py`)は使わない。`check_fem.py` の反力SHA基準値と1ビット一致。(同上)
- **`duocad/topopt/trace.py` の `Model` 直列化は、フィールド名リストがハードコードされている。** `fem.Model` のフィールドを増減すると `trace._encode` が `getattr(value, n)` で `AttributeError` になり、`keep_model=True` や `trace.recording()` を使う検査(`check_trace.py`・`tests/test_optimization_trace.py`)が壊れる。フィールドを変えたら `trace.py` のリストも直す。(同上)
- 合成密度場(ガウスぼかしで作る)を使う検査(`check_parallel.py` / `check_stress.py` / `check_mesh.py`)は、ファイルごとにシード・sigma・格子サイズに対して**実測校正した閾値**を持つ。これらを変えると体積一致率や前処理の効きの閾値を割る。変更時は各ファイルのコメントの実測値で再校正する。`check_mesh.py` は marching cubes 直後の体積一致(5%以内)と Taubin 平滑化の体積保存を両立させるため、大きい sigma で太い塊を作ってから二値化している(細い突起があると Taubin で体積が大きく動く)。(同上)

## acceptance.py(6条件の唯一の実装)

- **boolマスクを直接受ける製品APIは `acceptance.inspect(material, before, grid, *, table, fixed_faces, load_faces, forces, preserves, excludes, target_safety_factor, ...)`。** `material` / `before` は格子と同じ形の `dtype==bool`。float の 0/1 でも `ValueError`。`before` は島除去前のboolマスクで、「除去後に材料が増えていないか」だけを見る(密度の閾値変換はしない)。探索の候補評価は `inspect` を直接呼ぶ。現行は `solver='auto'`・`region_overlaps`・`fields`・`member_volume_mm3`・`keep_cells` も取る(全体はソース参照)。(20260917-0441-binary-search-2-acceptance)
- **密度→bool変換は読み取りアダプター `inspect_document(document, rho_clean, rho_before, grid, *, level=0.5, **kwargs)` だけの責務。** 過去記録(密度場)を読むときだけ使い、`rho > level` の二値化と、二値体積とは別枠の密度積算量(`report['volumes']['density_integral_before_mm3'/'density_integral_after_mm3']`)をここでだけ作る。`topopt_checks/check_pre_smoothing.py` はこの関数だけを呼ぶCLIで、6条件のロジックを複製しない。`inspect()` は入力を書き換えず、自分では二値化しない(密度+levelを内部で二値化する形は計画書§5「新規の製品APIはboolマスクを受ける」に反する)。(同上)
- 個別検査も公開: `boundary_check` / `connectivity_check`(幾何、OCP不使用)、`region_check` / `intersection_volume`(CAD、`duocad.kernel*` 越し)、`condition_check`(境界条件)、`safety_check`(FEM、`fem.Model` を直接構築)。(同上)
- **CAD共通体積の生プリミティブは kernel 層に置く**(現行 `duocad/kernel_brep.py` の `boxes_common_volume` / `require_valid_solid` / `grid_cell_common_volumes` / `box_common_volume`。`kernel.py` の `BoundingBoxCache` / `shape_key`)。**Grid / material 配列の概念を持たず**、生の `(min, max)` mm 座標のリストだけを受ける。`acceptance.py` は自分で OCP を import しない(`tests/test_boundaries.py::test_only_kernel_module_imports_ocp` の `OCP_ALLOWED` に載せない)。`topopt/voxelize.py` が OCP を許されているのは「B-repの厳密なbbox(`SetGap` 順序)が要る」という名指しの理由のためで、無関係なOCP呼び出しを便乗させない。(同上)
- bbox計測に `BoundingBoxCache` を使い回せる(`cache=` 引数。`inspect_document` は既定で `document.bbox_cache` を渡す)。`region_check` / `intersection_volume` の体積は `bbox_cache` の有無で一致する(`tests/test_pre_smoothing.py::test_intersection_volume_matches_with_and_without_bbox_cache` / `::test_inspect_result_is_identical_with_and_without_bbox_cache` で固定)。(同上)
- **計算失敗(残差検算・特異行列・非有限値)は例外を握って `status='unknown'` を返す**(`safety_check` / `region_check` とも)。安全率不足・体積不足(不合格)へ変換しない(`tests/test_pre_smoothing.py::test_solver_failure_is_unknown_not_unsafe`、§4.5)。(20260917-0441-binary-search-2-acceptance, 20260917-0442-binary-search-3-initial-feasible)
- `acceptance.py` は `fem.Model` / `fem.SOLVE_RESIDUAL_TOL` を直接使う。許容応力等の既定は `material.py` から import する。(20260917-0441-binary-search-2-acceptance)

## binary_search.py: 初期実行可能解(段階3)

- 入口は `find_initial_feasible(design_space_mask, conditions, *, max_evaluations, min_width_cells=1, is_cancelled=None) -> SearchResult`。`SearchConditions` は `acceptance.inspect` への引数を束ねたもの(`percentile=None` は「acceptance.py の既定へ委ねる」で、既定値をここへ複製しない。`.inspect_kwargs(bbox_cache=...)` で展開)。(20260917-0442-binary-search-3-initial-feasible)
- 段階3は入口検査+全材料マスクの**1回の評価**だけで、合格しなければ形を探さず落ちた検査を返す(計画書§13)。`SearchResult.status` は `'feasible' | 'no_feasible_found' | 'input_contradiction' | 'resolution_insufficient' | 'boundary_mapping_failed'` の5値。`termination` の語彙は `SearchResult` の docstring が正(段階3: `initial_feasible_found` / `full_material_infeasible` / `resolution_insufficient` / `boundary_mapping_failed` / `input_contradiction`、共通: `cancelled` / `evaluation_error`)。`best_feasible` は `status=='feasible'` のときだけ非None。全材料のFEM計算失敗だけなら `evaluation_error`。段階3の `max_evaluations` は合格形状の報告(`report['search']`)に写すだけで、評価は常に1回。(同上)
- **`FeasibleModel.report` は `bbox_cache=None` で最終再検査した `acceptance.inspect` の戻り値そのもの**(§3)。最良合格モデルは必ず「キャッシュ無しの最終再検査」を経てから返し、再検査が `pass` でなければ `feasible` を返さない。`SearchConditions.inspect_kwargs(bbox_cache=None)` は保持・除外の前計算(`region_overlaps`)も一緒に外す。(20260917-0442-binary-search-3-initial-feasible, 20260917-0443-binary-search-4-remove-add-replace)
- **`before` は「島除去の直前の候補」自身であること。** `acceptance.inspect` の `material ⊆ before` 契約(§2)は、`resolve_islands(candidate)` が candidate から `ndimage.label` で生存成分を作る以上 `survivor ⊆ candidate == before` で構造的に成り立つ。祖先・親のsurvivorを `before` に使うと、候補が親の材料の外へ広がった場合に `ValueError('島除去後に材料セルが増えている')` で落ちる。(20260917-0442-binary-search-3-initial-feasible)
- `resolve_islands(candidate, conditions) -> (survivors, diagnostics)`(島の除去は§4.4。全保持セルを含む成分だけを残す)は段階4(`peel` の検算)でも同じ規則で使う。`evaluation_identifier(conditions)` も公開。(同上)

## binary_search.py: 削減(段階4)の入口と終了

- 製品配線が呼ぶ**単一の入口は `find_best_feasible(design_space_mask, conditions, *, max_evaluations=1000, ..., single_cell_patience, target_tolerance, evaluation_error_patience, min_width_cells=1, is_cancelled=None, on_evaluation=None) -> SearchResult`**。内部で `find_initial_feasible`(段階3)→ `_reduce_volume`(段階4)の順に呼ぶ。`max_evaluations` は**段階3・段階4を通した1本の予算**。`status` が `'feasible'` 以外なら段階4は何もせず段階3の結果をそのまま返す(`best_feasible=None`)。(20260917-0443-binary-search-4-remove-add-replace)
- 返却は必ず `best_feasible` から。`status=='feasible'` の `best_feasible` は**常にキャッシュ無し再検査済み**(§3・§4.6)で、製品配線側が再検査し直す必要はない。`best_feasible.material_mm3` は探索が進んでも増えない。同じ入力・同じ予算・同じ設定なら結果は決定的(乱数・dict/set の反復順に依存する分岐は無い)。(同上)
- `is_cancelled` は引数無しで呼べる callable。真を返すとその時点の `best_feasible`(再検査済み)を保ち `termination='cancelled'` で返る。初期合格の前に中断されれば `no_feasible_found` / `cancelled`。段階3は評価の前、段階4は削る候補を作る前ごとに見る。(同上)
- **段階4は合格形状から合格形状へしか動かない**(不合格の候補は捨てて戻る)。体積を真に減らせる候補が無い題材は、初期合格のまま `evaluation_limit` 等で終わりうる。これは§9が許容する結果(初期合格だけで止まって軽量化できない場合は、その事実と終了理由を記載し、体積削減機能の完成と扱わない)。大域最小の保証はしない(§1)。削る操作(peel)は材料マスクの部分集合しか作れないので、最小形状が必要とする「今は材料の無いセル」へは届かない。(同上)

## 製品配線(optimize_run.py ほか)

- `duocad/optimize_run.py` の `_run()` は計画書§3の単一経路: `build_conditions` → `resolution_limits(doc)`(**`rmin_mm` 引数は無い**)→ `design_space.derive`(1回)→ `binary_search.find_best_feasible(space.mask, conditions, is_cancelled=should_cancel, on_evaluation=..., **{"max_evaluations": ...} if opts.max_evaluations)` → `mesh.build_surface_from_mask(best.mask)` → `finalize` → `add_solid_body`。`SearchConditions` は `_build_search_conditions(doc, space, bundle, opts, table=, mapping=)` が組み立てる(`preserves` / `excludes` は `(name, shape)` のタプル、`bbox_cache=doc.bbox_cache`)。(20260917-0444-binary-search-5-product-wiring)
- **探索候補の評価中にメッシュ化・STL出力を一度も呼ばない。** `find_best_feasible` は `acceptance.inspect` しか呼ばず `mesh` に触れない(構造的保証)。後段へ渡るのは `search_result.best_feasible.mask`(再検査済み)だけ。(同上)
- **`mesh.build_surface_from_mask(mask, *, level, spacing=DEFAULT_SPACING, ...)` に実 `spacing` を渡さない。** 要素添字空間のまま渡し、`optimize_run._mesh_to_world`(`grid.origin + verts * grid.spacing`)が実寸変換を一括で行う契約。実 spacing を渡すと二重に掛かりメッシュが格子の外へ飛び(実測: 格子[-21.5,91.5]に対しメッシュ[25,786])、`_check_mesh_placement` が `FaceMappingError` で落ちる。この入口は `apply_density_masks` / `remove_islands` を呼ばない確定マスク専用。回帰テスト `test_no_candidate_meshing_and_the_confirmed_mask_reaches_finalize` が `"spacing" not in kwargs` を直接assertする。(同上)
- **中断(`termination=='cancelled'`)では `best_feasible` が実在しても製品文書へ追加しない**(§4.6)。`status=='feasible'` かつ `termination=='evaluation_limit'` 等の中断ではない終了は通常どおり保存する。2つを混同しない。(同上)
- 密度フィルタ半径 `rmin_mm` と SIMP 由来の `max_iter` / `material_min_mm3` / `material_max_mm3` は `OptimizeOptions` に無い。`session.run_optimization()` / `optimize_resolution()` は旧4引数をキーワードで受け続け、`None` 以外が来たら即 `OptimizeInputError`(黙って無視しない)。`net/ws.py` の `_optimize_start` はこの4引数と `max_evaluations` を転送する。`catalog.py` の `run_optimization` は `rmin_mm` パラメータが無く `max_evaluations` がある(`ws="special"`・`mcp=False` なので文書化のみ)。現行の `OptimizeOptions` は `target_safety_factor` / `search_target_factor` / `element_size_mm` / `min_width_mm` / `max_evaluations`。(同上)
- `run()` の戻り `state` は `done` / `empty_condition_faces` / `unrepresented_regions` / `no_feasible_found` / `input_contradiction` / `resolution_insufficient` / `boundary_mapping_failed` / `fill_infeasible` / `final_unsafe`(`optimize_run.py` の docstring が正)。`"done"` の戻りに `termination`(探索の終了理由文字列)を持ち、`achieved_material_mm3` / `converged` / 詳細 `history` は無い。(同上)
- `reactions`(拘束面ごとの反力)は `acceptance.inspect` が計算しないので、実行結果に無い。UIも参照しない(`web/src/session.ts` に使用箇所0)。反力表示を作るなら FEM 層への新規配線が要る。(同上)
- `check_optimize_wiring.py` は `CHECK_ELEMENT_SIZE_MM = 4.0` を `session.run_optimization(element_size_mm=...)` へ明示して回す。省くと既定は「いちばん粗い」解像度になり、細長い直方体を離して並べた題材(2ボディ 10x40x40 と 10x40x10 を60mm離す)で `voxelize.occupancy` がレイ縮退の `ValueError` を出した(要素サイズ14mmまで成功・16mm以上で失敗)。要素サイズの上限 4 mm(#130)の後は、既定と選べる全解像度で再現しない(14 mm 以上は最小幅の段が無く入口で断られる。2026-10-10 実測、docs/TOPOPT.md「制限」)。(20260917-0444-binary-search-5-product-wiring, 20260917-0445-binary-search-6-legacy-removal)

## 検査 CLI と docs 整理の教訓

- `anchor.py` の `_cli()`(`python -m duocad.topopt.anchor`)は面アンカー表の要約だけ。docs に検査項目を書くときは print 文の番号を実ファイルで確認する。(20260917-0446-binary-search-7-docs)
- `docs/TOPOPT.md` の「現在の呼び出し経路」が現行の `optimize_run.run → _run → design_space.derive → binary_search.find_best_feasible → mesh.build_surface_from_mask → finalize → add_solid_body` の図。呼び出し経路が変わったら真っ先にここを直す。`docs/TOPOPT.md` の節の並びは時系列、現在の値は「現在の正」表を見る。過去の歴史的な節は日付付き・過去形のまま意図的に残してあり、**全文grepで `simp.` / `safety_loop` / `volfrac` 等の旧語がヒットしても、それだけでは不備ではない**(現在時制の節に旧語があるかを見る)。(同上)
- `docs/TOPOPT_SPEED.md` の「1回のFEM評価の内訳」はSIMP時代(2026-09-09)の実測の転用で、現在の二値探索の候補あたり秒数・評価回数の分布を表さない。`docs/TOPOPT_RECORDING.md`「保存する段階」は**個々の候補はディスクへ記録しない**ことを明記している。(同上)
- **同じ古い主張が文書内に複数箇所あるときは、1箇所直して終わりにしない。直す前に古い主張を文書全体でgrepする。** 関数名変更は、契約表のような現在時制の表にだけ確実に反映すればよく、歴史的節の同じ関数への言及まで直す必要はない。(同上)
