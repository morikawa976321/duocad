# 形状最適化の最終処理・平滑化コーパス・設計要素数の上限の契約

(1) 最終処理（`duocad/topopt/finalize.py`: 保持を足し除外を引く）、(2) 平滑化（marching cubes + Taubin）の検証材料 64 条件（`output/smoothing_corpus/`）とその自己交差の規則、(3) 設計要素数の上限（`MAX_DESIGN_ELEMENTS`）について、ソースを読んでも分からない不変条件・依存禁止・罠。
いまの挙動の正はコードとテスト。判断・実測の正は [../TOPOPT_ACCEPTANCE.md](../TOPOPT_ACCEPTANCE.md)・[../TOPOPT.md](../TOPOPT.md)「要素サイズの下限は設計空間の要素数で決める」。
各項目末尾の括弧は出典のタスクID(先頭8桁が日付)。

## 最終処理（finalize）

`finalize(vertices, faces, preserve, exclude, progress=None)` は **manifold3d によるメッシュ集合演算**（平滑化後の表面 + 保持を足し、除外を引く）で、B-rep（OCCT）のブーリアンを使わない(2026-10-06 から)。現行のスナップショットは `smoothed-mesh-checks`・`cad-meshes`・`mesh-boolean`・`final-mesh-checks`（`docs/TOPOPT_RECORDING.md`）。2026-10-06 より前の実行記録には B-rep の段階が残っている。

- **重複領域は除外が優先する**（`tests/test_finalization.py::test_exclusion_wins_overlap`）。(20260922-0710-finalize-cut-then-subtract-fuse)
- **保持が除外に完全に覆われても、寄与ゼロとして成立する**（`test_preserve_fully_covered_by_exclude_contributes_nothing`）。保持を個別にブーリアンする設計では `P_i - 除外` が空になって「演算結果が空または不正」の例外になる実バグがあった。**処理順を変えるときは「ツール側が対象を全部覆う」ケースを必ず確認する。** 現行の集合演算は結果が空（除外が材料を全部覆う）なら例外にする。(同上)
- `preserve`/`exclude` は CAD ソリッドのリスト（1回限りのイテレータでも可。冒頭で `list()` 化する）。(同上)
- `check_trace.py`・`replay_finalization.py`・`TOPOPT_RECORDING.md` のスナップショット名は実装と一致させる（処理を変えたら同時に直す）。(同上)
- `replay_finalization.py` の読む順は `pre-boolean-mesh-world` → `final-mesh-world` → `finalize-input`。元の run が finalize 失敗でメッシュ側のスナップショットを残していないと `finalize-input` からのフォールバックになり、成功しても `grid` が無いため `final.stl` を保存しない。(同上)

## 平滑化の検証材料（output/smoothing_corpus・64条件）

材料は `C:\Users\tm976\mf\duocad\output\smoothing_corpus\`（gitignore 済み）。条件名は `F<荷重4桁>_h<要素サイズ>_w<セル数>`（荷重 0200/0500/1000/2000・要素サイズ 2.4/2.2/2.0/1.8・セル数 2/3/4/5。例 `F0200_h1.8_w2`）。64条件すべてが `converged` で、解なし（`status: no_solution`）は 0 件。

- **入力にするのは `voxel/<条件名>.npz`**: `mask` bool `(nx,ny,nz)`（平滑化直前のマスク = `build_surface_from_mask` に渡る配列そのもの）・`origin` float64 `(3,)`（要素 (0,0,0) の低い側の角のワールド mm）・`spacing` float スカラー（実際の要素サイズ mm）・`shape` int64 `(3,)`。`voxel/<条件名>.stl` はバイナリ・ワールド mm・外向き巻き順で、材料セルと空セル（格子の外を含む）の境界面だけ（1面2三角形、頂点は面ごとに4つ）。(20260925-1826-topopt-smoothing-corpus-voxel-stl)
- **座標規約**: `optimize_run._mesh_to_world` と同じ。ワールド = `origin + verts * spacing`、verts はセル中心規約（セル `ex` の中心が `ex+0.5`、セル `ex` は `[ex, ex+1]`）。`mesh.build_surface_from_mask(mask)` の頂点（`verts_raw`/`verts_smooth`）は要素添字空間なので、STL にするときはこの式でワールドへ直す。**`build_surface_from_mask` へ実 spacing を渡さない**（二重掛け）。(20260925-1826-topopt-smoothing-corpus-voxel-stl, 20260925-1827-topopt-smoothing-corpus-mesh-stl)
- `manifest.json`: `{generated_at, model, conditions_total: 64, counts, settled, rows[64]}`。行の列は `name, load_n, element_size_requested_mm, element_size_resolved_mm, cells_requested, min_width{requested_mm,spacing_mm,cells,guaranteed_mm,source}, min_width_as_intended, grid{origin,spacing,shape}, design_elements, search_status, termination, evaluations, search_seconds, total_seconds, material_mm3, safety_factor, mask_cells, status("ok"|"no_solution"|"error"), stl, npz, stl_triangles, stl_bytes, peak_working_set_mb, peak_commit_mb, options, commit, dirty_product_code, started_at, finished_at`。`records/<条件名>.json` が1条件の原本で、manifest はそれを並べたもの。**行の並びに依存しない（`name` で引く）**。`voxel/*.tmp`・`records/*.tmp` は書き途中の一時ファイル。`sweep.lock` は OS の排他ロック（ファイルが残っていても走っているとは限らない）。`sweep.log` は OCCT の STEP 出力が混ざる（`grep -a "^\["` でしか読めない）。(20260925-1826-topopt-smoothing-corpus-voxel-stl)
- 不変条件: `mask` は bool、`shape == manifest の grid.shape`、`mask.sum() == manifest の mask_cells`、最小幅は全条件で意図したセル数に解決済み（`min_width.cells == cells_requested`）。マスクは製品の `optimize_run.run`（探索・島除去）が採用したものを `build_surface_from_mask` の直前で捕まえた配列。**同じマスクから製品の marching cubes で作った面はボクセル STL の境界上にある**（頂点の最大距離 4.4e-6 セル。64条件すべて）。Taubin 平滑化後は 0.48〜0.63 セル離れる。STL の巻き順は 64 条件すべてで外向き。(同上)
- 再生成・検査: `topopt_checks/sweep_smoothing_corpus.py`（`--detach` 全条件の再開可・`--one <条件名>`・`--verify <条件名>`・`--manifest` 64条件の確定検査）。荷重・向き・面は `models/hanger_bar_attachment.json` を変えず、開いた `Session` 上で `verbs.optimize_setup.set_loads` で差し替えたもの。`commit` 列は大半が `9120483`、6件が `dba91cb`、`F0200_h2.4_w2` と `F2000_h1.8_w2` は `ecfacfa` + 作業ツリー（`dirty_product_code: true`。`MAX_ELEMENTS` の引き上げだけが未コミットの差）。(同上)
- 材料は探索の最終確定マスクで 5,637〜148,620 セル。最小幅が太い（4〜5 セル）・荷重が軽い条件は探索の終盤が採用1手ずつで数時間かかる（最長 5.9 時間・323 評価）ので、再生成に丸1日かかる。1条件のピークメモリは 1.8mm で 16.7GB なので、並列は空きメモリを見て起動間隔を空ける（スイープが自動でやる）。(同上)
- Windows の Git Bash の `python` は cp932 で JSON を読む。`-X utf8` か `encoding="utf-8"` を付ける。(20260925-1826-topopt-smoothing-corpus-voxel-stl, 20260925-1827-topopt-smoothing-corpus-mesh-stl, 20260925-2101-topopt-smoothing-intersection-rules)

### メッシュ STL（marching_cubes/・smoothed/）

- `marching_cubes/<条件名>.stl` は平滑化前（`verts_raw`）+ `faces`、`smoothed/<条件名>.stl` は平滑化後（`verts_smooth`）+ 同じ `faces`。どちらもバイナリ・ワールド mm・外向き巻き順。**2つの STL は三角形の並びが同一**（三角形添字 `i` は両方で同じ三角形を指す）。STL を読み直すと頂点が三角形ごとに分かれ頂点添字の対応は取れない。頂点単位の比較は npz の `mask` から `mesh.build_surface_from_mask(mask)` を呼び直す（決定的）。(20260925-1827-topopt-smoothing-corpus-mesh-stl)
- `mesh_records/<条件名>.json` が原本（最後に書く）、`mesh_manifest.json` は `voxel/manifest.json` の条件順に並べたもの: `{generated_at, conditions_total: 64, built: 64, missing: [], rows[64]}`。行の列は `name, status, input_npz_sha256, product_commit, grid, voxel_check{...}, marching_cubes_stl, smoothed_stl, triangles, vertices, volume_mm3{raw_mm3,smooth_mm3}, volume_smooth_over_raw, vertex_move{max_cells,p99_cells,max_mm,p99_mm}, topology{raw,smooth}, intersections{raw,smooth}(counts{cross,touch,coplanar,fold,duplicate},pairs,candidates,triangles_involved,centroids[…最大20]), seconds{...}`。`product_commit` は生成時の duocad の HEAD でスクリプトの後の変更を含むので再現の保証に使わない。(同上)
- 再生成・検査: `topopt_checks/build_smoothing_corpus_meshes.py`（引数なし=未作成または入力 npz が変わった条件だけを並列で作る・再開可 / `--one` / `--manifest` / `--summary`（自己交差の集計表）/ `--workers N`）。64条件で約6分。(同上)
- **全64条件の事実**: 2つの STL の三角形数は `triangles` と一致。ボクセル STL と npz は同じマスク（`triangles_off_boundary == 0`・体積の相対誤差 ≤ 1e-7）。**平滑化前後とも接続6項目はすべて 0**（閉じた多様体・向き整合・境界成分 1）。**平滑化前の自己交差は 0**。平滑化後の自己交差は **4条件・計21組（すべて `cross`）**: `F1000_h2.4_w2`(4)・`F2000_h2.0_w2`(4)・`F2000_h2.0_w3`(3)・`F1000_h1.8_w5`(10)。`touch/coplanar/fold/duplicate` は全条件 0。交差した三角形は条件ごとに 4〜8 枚で、重心は1条件につき1か所の近傍に集まる。体積は平滑化後が前の 1.0016〜1.0392 倍（増える理由は測っていない）。頂点移動は最大 0.73〜0.86 セル・p99 は 0.56〜0.60 セル。(同上)
- 交差検査（`triangle_intersections`）と接続検査は**溶接済みの頂点添字**が前提。STL からではなく `build_surface_from_mask` の `verts`/`faces`（ワールドへ直した float64）に対して回してある。STL は float32 なので、STL を読み直して同じ検査を回すと近接頂点の判定が変わりうる。`intersections.*.centroids[].triangle` は生成時の `faces` の添字（別のメッシュ化をすると変わる）。対象の ok 条件は `voxel/manifest.json` の `status == "ok"` から取る。(同上)

### 自己交差の規則（intersection_sites.json）

- 分析は `topopt_checks/analyze_smoothing_intersections.py`（`.venv/Scripts/python.exe -X utf8`。引数なし=入力マスクが記録の後で変わった条件だけ作り直す・並列。`--force`・`--one`・`--report`・`--synthetic`・`--locality`・`--sensitivity`）。文書は `docs/TOPOPT_ACCEPTANCE.md`「平滑化(Taubin)後の自己交差」。`intersection_sites.json`（1件1行）・`intersection_records/<条件名>.json`（原本。`sites[]`・`runs[]`〈管の列の全件〉・`slit_hist`・`mask_sha256`・`counts_match_mesh_manifest`）。(20260925-2101-topopt-smoothing-intersection-rules)
- **規則の要約**: 64条件の交差は4箇所・21組で、4箇所とも四方が閉じた 1×1 の管（板を貫く穴、開-開）の中（長さ3が3箇所・長さ4が1箇所）。管は長さ4以上（開-開）・2以上（止-開）で壁の間隔が 0.1 要素未満に潰れるが、交差したのはそのうち開-開の長さ3〜4（19列中4列）だけで、止-開・長さ5以上・溝は 0。合成（板・棒）では板厚（=管の長さ）4のときだけ確実に交差し（平板20組・棒225/225）、3・5〜12は 0。**実データの長さ3の交差3箇所は合成で再現できておらず、周囲の形の特徴でも分けられていない**（管から4セル以内の形で決まる）。各条件の交差件数は `mesh_manifest.json` と一致（64/64）。(同上)
- 壁の間隔の区切り（0.1/0.25/0.5/0.75）・塊のまとめ半径（3.0）は分析者が置いた値で、実測ではない。(同上)
- **対策（現行）**: 2026-10-07 の本人判断で、探索が確定したマスクの管（2軸以上で挟まれた空セル）をメッシュ化の前に埋める（`duocad/topopt/thin_voids.py` の `fill_tubes`、`optimize_run` の探索後処理）。64条件で交差 21組 → 0、埋めたセルは条件あたり中央値 7・最大 80。**このコーパスの `voxel/*.npz` は埋める前のマスク**で、埋めた後の取り直しは `tube_fill_manifest.json`・`tube_fill_records/`。(出典: `docs/TOPOPT_ACCEPTANCE.md`「平滑化(Taubin)後の自己交差」)
- 座標は要素添字空間（`centre_cell`）で、ワールドは `origin + 添字 * spacing`（`centre_world_mm` は計算済み）。保持・除外との接触は追跡していない（npz にマスクが無い。`keep_or_exclude_contact: "not_tracked"`）。`rows` の並びに依存しない（`condition` で引く）。`runs[].hit` は記録に無く `--report` の読み込み時に付ける。(20260925-2101-topopt-smoothing-intersection-rules)
- 罠: 二値マスクの marching cubes は境界面の中心に頂点を1つずつ置くので頂点と境界面が1対1（壁の間隔はこの対応で測る。`_face_vertex_tables`）。`local_crossings` の「近い」は交差2三角形の頂点の重心で判定（交差点ではない）。交差の有無は三角形の並びの細部で決まり、間隔が 0.02〜0.05 の管の多くは交差しない。管の潰れと交差の因果は測っていない。合成の板厚1の棒 2/225 が交差する原因は未調査。(同上)

## 設計要素数の上限（MAX_DESIGN_ELEMENTS）

- `optimize_run.resolution_limits(doc)` の戻りは `max_design_elements`、`steps[].design_elements_estimate`（**見積もりで実数ではない**）。定数は `optimize_run.MAX_DESIGN_ELEMENTS`（現行値 1,300,000。本人判断 2026-09-26「1.6 mm が通る130万」）。`design_space.offset_hull_volume(corners, margin)` / `obstacle_free_fraction(corners, obstacle)` が見積もりの部品（`_design_element_estimate(volume, spacing)` が組む）。(20260926-1700-topopt-limit-design-elements)
- **実行時の拒否は `derive` 後の `space.element_count` 実数で行う**（`_require_design_elements_within_limit`）。見積もりは実数と 1〜2% ずれうるので、見積もりだけで通ったことを保証と読まない。(同上)
- 梯子の要素数を格子の箱の数として使わない。見積もり/実数の比（0.984〜0.990）は hanger 1件の実測で、他の形では未測定。連続体積だけの見積もりは除外ソリッドを引かないため hanger で実数の1.21倍になり、1.6 mm が拒否される。(同上)
- キー改名の追従漏れは `.mjs`（`web/scripts`, `tests/`）まで grep する。1.6 mm の全行程ピークは未測定（`topopt_checks/measure_limit_run.py` を空きメモリ 24GB 以上で回す。`output/limit_run/` に F2000_h1.6_w2 の途中記録がある）。(同上)
- 上限より粗ければ必ず通るとは限らない（`hanger_bar_attachment.json` は薄い部品 5.00mm があり、粗いと事前検出 `_region_representation` で止まる。通るかどうかは要素サイズに対して単調でない）。(出典: `optimize_run.MAX_DESIGN_ELEMENTS` のコメント)
