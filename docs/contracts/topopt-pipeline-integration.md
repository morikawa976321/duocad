# 形状最適化の本体取り込み・フィルタ・平滑化・probe 廃止の契約

形状最適化を実験場（`probe_topopt/`）から本体 `duocad/topopt/` へ移した経緯の上に残る不変条件、実験場の廃止後の参照の掃除、密度フィルタ半径（`rmin`/`rmin_mm`）・密度場のガウシアン平滑化・塊の導出（案5の削除）まわりの契約を、ソースを読んでも分からない形で分野別に蒸留した文書。
**いまの挙動の正はコードとテスト**（実測と判断は [../TOPOPT.md](../TOPOPT.md)）。ここは、そこに無い（または散らばっている）不変条件・依存禁止・罠だけを持つ。
出典は項目末尾の括弧内（タスクID, 日付）。**`(対象コード不在・要確認)` と付いた項目は、対象の関数・ファイルが現在のリポジトリに無い。** 現在の探索は二値探索（`binary_search.py` / `acceptance.py`）で、SIMP 系（`simp.py` / `safety_loop.py` / `bench.py`）は 2026-09-17（`5e3058d`）に削除、旧入口 `mesh.build_surface` は 2026-09-21（`9bcdeb5`）に削除済み。ソルバ・最適化手法側の契約は topopt-solver-optimizers.md。

## パッケージ構成と依存の向き（duocad.topopt）

- **呼ぶ側は `from duocad.topopt import design_space as ds` のようにサブモジュールを名指しで import する。** `duocad/topopt/__init__.py` は**サブモジュールを一切 import しない**（`import duocad` は verb 登録で `duocad/__init__.py` から連鎖するので、ここで scipy / skimage / build123d を引き込むと pytest が毎回払う）。`import duocad.topopt` だけでは `duocad.topopt.simp` 等は生えない。`duocad/__init__.py` から `topopt` を import しない。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- この規律の退行を検査するなら見るのは `skimage` と `duocad.topopt` であって scipy ではない（`import duocad.view` で numpy と scipy が来る）。`tests/test_optimize_backend.py` 参照。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04 ※後続の整理で追記された指摘）
- **依存は一方向。** 本体（`duocad/`）から `probe_topopt` への参照は作らない（import も、ソースを読む検査も）。`duocad/topopt/` から検証側（`topopt_checks/` / `web/scripts/`）を import しない（実験場を消せたのはこの一方向性のため）。（20260904-1600-duocad-move-topopt-into-core, 20260905-1701-duocad-remove-probe-topopt, 2026-09-04）
- **`duocad.topopt.OUTPUT_DIR`（= `duocad/topopt/output/`）が成果物（STL・測定JSON）の置き場。** `output/...` と相対パスで書かない（2026-09-04 に `taskbox/queue/output/` へゴミが落ちた）。`duocad/topopt/output/` に成果物が残っていることに依存しない（追跡は一部だけで、残りは `.gitignore` が外す）。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- **モジュールを素のスクリプトで走らせない。** 相対 import なので `python duocad/topopt/design_space.py` は動かない。`python -m duocad.topopt.<module>`（`mesh` / `voxelize` / `anchor` 等）を使う。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- **`tests/test_boundaries.py` の免除は名指しで、ディレクトリ単位ではない**（OCP: `topopt/voxelize.py` / build123d: `topopt/{voxelize,anchor,design_space}.py`）。`duocad/topopt/` に新ファイルを足して OCP か build123d を直接 import すると pytest が落ちる。**免除を増やす前に `duocad.kernel` / `duocad.query` 越しにできないかを考える**（一覧の中身は `test_boundary_scan_covers_every_source_file` が固定しているので、増やすには2箇所直す）。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- 罠: `voxelize._exact_bbox` は `Add_s` の**後**に `SetGap(0.0)` を呼び、`kernel.bounding_box` は**前**に呼ぶので値が 1e-7mm 違い、「辺長が要素サイズの整数倍の箱は体積が厳密一致」の検査（`check_voxelize.py`）が落ちる。移設で挙動を変えない制約が優先なので、`kernel.bounding_box` へ回して解決せず名指し免除にした（検出力は `topopt/simp.py` に `import OCP` を足すと落ちることで確認）。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- **受入検査は `topopt_checks/`（リポジトリ直下。`duocad/` の外）。** `duocad/` の下へ移さない（検査は合成ソリッドを build123d で組むので、中に置くと境界検査に免除が11本ぶん要る。境界の設計意図は「サブパッケージを作って逃げられない」）。各検査は先頭で `import _bootstrap` してリポジトリ直下を `sys.path` へ足す。pytest には入っておらず別途走らせる。件数・本数は時点で動くので契約にしない（検査の項目数を減らさない、という原則だけが契約）。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- 罠: タスク指示の「`check_*.py` は `duocad/topopt/` の隣へ」に文字どおり従うと上の境界にぶつかる。`git mv` でディレクトリを移すと追跡外のファイル（QA の使い捨て `_qa_mut_*.py` / `qa_*.stl`）まで付いてくるので中身を確認してから移す。Bash ツールのヒアドキュメント経由の Python はバックスラッシュが1段落ちるので、Windows パスを含む置換は別ファイルへ書くか `chr(92)` を使う。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- コード内コメントが書く「README」の参照先は `docs/TOPOPT.md` に振り替えてある（59箇所）。`design_space.measure_margin`（余白比率 0.25 を決めるための一度きりの調査）は移設時に削除済み。(対象コード不在・要確認: `measure_rotation` / `measure_case5`（`--measure`/`--compare`）も現在は不在)（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- 罠: `readme.md` の pytest 節の件数は古いまま放置されがち（件数はタスクごとに動く）。件数を契約にしない。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）

## probe_topopt の廃止後の参照

2026-09-05 に `probe_topopt/` を削除（`70eb180`。中身は `03bf794` までの履歴にある）。実験場の `server.py` / `web/index.html` / `shot.mjs` / `shot_anchors.mjs` / `compare_shots.*` / `web/vendor/three/` はすべて消えた。撮る道具は `web/scripts/shoot-topopt.mjs`。

- **`probe_topopt` への言及は全部過去形（「当時は」「旧実験場」「2026-09-05に削除」の修飾付き）にする。** 修飾を外すと「実験場がまだある」と読める。**裸の `server.py` / `web/index.html` を書かない**（`duocad/server.py` が別に存在するので本体のサーバと誤読される。実験場のものは必ず `probe_topopt/` を付ける）。（20260905-1701-duocad-remove-probe-topopt, 2026-09-05）
- 罠: 参照の掃除は `probe_topopt` の grep だけでは足りない。`実験場` / `smooth_compare` / `compare_shots` / 裸の `server.py`・`web/index.html`・`shot.mjs`・`screenshots/` でも取り直す（パスを書かずに実験場を指す箇所が10箇所以上あった）。`docs/TOPOPT.md` 冒頭の「これは何か」は `probe_topopt` を含まないまま隔離フォルダを現在形で説明していて BLOCKER になった。文字列検索で見つからない参照がある。移設先 README の「撮り直し方」が消えたコマンド（`python -m probe_topopt.compare_shots`）を指したままになりやすい。（20260905-1701-duocad-remove-probe-topopt, 2026-09-05）
- **`docs/TOPOPT.md` は時系列の文書で、古い節は隔離フォルダだった時期の記録。節ごとに日付を見る。**（20260905-1701-duocad-remove-probe-topopt, 2026-09-05）
- 比較スクショ16枚と README は `docs/topopt-smooth-compare/` に移したが、**一次資料で撮り直せない**（案5の粗い段が当時の `DEFAULT_RMIN`=1.5 で回っており、ビット単位の再現は不可）。中間物（`smooth_compare/_data/` 9.1MB と `probe_topopt/output/` 84KB）は git 履歴に無く削除で復元不能。(対象コード不在・要確認: `docs/topopt-smooth-compare/` 自体が現在のリポジトリに無い)（20260905-1701-duocad-remove-probe-topopt, 2026-09-05）
- 罠: `web/scripts/shoot-topopt.mjs` の `PAGE` テンプレートリテラル内はバッククォート禁止（コメントのコード引用には `'` を使う）。8775/8776 は `web/scripts/smoke.mjs` の既定のままなので、実験場がなくなっても「空いた」わけではない（安定版のポートでもある）。（20260905-1701-duocad-remove-probe-topopt, 2026-09-05）

## 検査コードが共有成果物を上書きする罠

- **`run_pipeline` / `run_topopt` を呼ぶ検査は、STL を使わない（戻り値だけ見る）呼び出しでも `stl_path` を明示する。** 省略すると共有ファイルへ書く。`check_filter_radius.py` の配線検査が `stl_path` を渡し忘れ、`output/bracket.stl`（`check_anchor.py` の回帰成果物）を小格子の結果で上書きした（47,684→4,684バイト）。`git status` で気づいて再実行で復元した。（20260904-1557-topopt-filter-radius-in-mm, 2026-09-04）
- **`duocad/topopt/output/design_space_reference.stl` は git 追跡下なのに、受入検査を1本走らせるだけで上書きされた**（`build_reference` の既定 STL 出力先。`check_optimize_wiring.py` / `check_safety_loop.py` / `check_design_space.py` で 2,419,684→87,000〜108,000 バイト）。検査を回したら毎回 `git status` を見て、出ていれば `git checkout --` で戻す（自分の成果物ではない）。新しく参照を作る検査は `stl_path=` を必ず渡す。(対象コード不在・要確認: `build_reference` / `design_space_reference.stl` は不在。同種の罠は成果物を `output/` へ既定出力する他の検査にも当てはまる)（20260910-1311-topopt-remove-case5, 20260910-1312-topopt-mma-alongside-oc, 20260910-1314-topopt-reaction-constraint, 2026-09-10）
- 罠: `git commit -- <path>` は索引でなく**作業ツリーの内容**を取るので、並行セッションが同じファイルへ入れた未コミット行が同梱される（`README.md` と `docs/TOPOPT.md` に他タスクの文書地図の1行が乗った例）。並行セッションが同じ docs を編集していることがあるので `git status` を見てから触る。（20260910-1311-topopt-remove-case5, 20260905-1528-topopt-find-filter-parameters, 2026-09-10）

## 密度フィルタ半径（rmin / rmin_mm・削除済み）

SIMP の密度フィルタは 2026-09-17（`5e3058d`）に `simp.py` ごと削除され、二値探索に対応する概念は無い（`catalog.py` / `session.optimize_resolution` / `ws.py` が旧引数 `rmin_mm` を明示的に扱えないと断る）。(対象コード不在・要確認: `simp.FilterRadiusError` / `check_rmin_target` / `MIN_RMIN` / `DEFAULT_RMIN` / `Result.rmin_mm` / `check_filter_radius.py` は不在。`optimize_run.resolution_limits` の `rmin_mm / MIN_RMIN` 式も現行には無い) 復活させる場合の教訓として残す。

- 口は3層（`simp.optimize` / `anchor.run_pipeline` / `design_space.run_topopt`）とも `rmin`（要素単位）と `rmin_mm`（mm）の2語で、**両方渡すと `ValueError`**。mm→要素の換算は `simp.optimize` だけが行う（立方体要素の体積の立方根が要素サイズ。`element_volume_mm3` が要る）。上位層は `model.grid.spacing` から要素サイズを決めて素通しする。形（排他・正）の検査は `check_rmin_target` を上位層がボクセル化の前に呼ぶ（`check_volume_target` と同じ役割分担）。（20260904-1557-topopt-filter-radius-in-mm, 2026-09-04）
- 床（`rmin_mm` 経路が下回ると `FilterRadiusError`）は **1.5要素**。これは「安全側に取った値」であって、フィルタが無効化する厳密な境目（1要素。`_filter_kernel` の `r` が 0→1 に切り替わる点）ではない。検出力の実測は 1.0要素（フィルタ実質無効）対 1.5要素の比較で、1.0〜1.5の間に数学的な特異点は無い（連続的に変わる）。1.5 を動かす理由が出たら実測を取り直す。**ガードは `rmin_mm` 経路だけ**で、生の `rmin=0.5` は弾かない（既存の無検査挙動）。`2.0mm` を既定にしない（製造上の推奨値だが当時のデモ・題材が全部落ちた）。分率・要素単位どちらの口も消さない。（20260904-1557-topopt-filter-radius-in-mm, 2026-09-04）
- `Result.rmin_mm` は要素単位で渡した経路では `rmin × element_size_mm` の逆算値（指定した値の写しではない）。`_filter_kernel` の半径丸めは `int(ceil(rmin))-1`。（20260904-1557-topopt-filter-radius-in-mm, 2026-09-04）
- 罠: `except ... as exc:` の束縛変数はブロックを抜けると暗黙に `del` される（ブロック外で `exc.min_rmin_mm` を読んで `UnboundLocalError`）。持ち出すなら別名へ代入する。（20260904-1557-topopt-filter-radius-in-mm, 2026-09-04）

## 既定のフィルタ半径とチェッカーボード（削除済み）

- **床と既定は別物。** 床 `MIN_RMIN=1.5` は `DEFAULT_RMIN` から名前だけ切り出した値で挙動は不変、**`DEFAULT_RMIN` は 1.5→2.5要素へ動かした**（1.5要素でも `plate`/`tilted` は筋が出る、が出発点）。既定は今後も測り直して動きうるので、床が要るなら `MIN_RMIN` を読み、`DEFAULT_RMIN` の値に依存しない。これは 20260904-1557 の「既定 1.5」を上書きしている。`simp.PRESET["rmin"]` は 1.5 のまま（回帰基準の ρ場 SHA-256 が乗る）。（20260905-1528-topopt-find-filter-parameters, 20260904-1557-topopt-filter-radius-in-mm, 2026-09-05）
- **回帰指紋を採る検査は、既定に追随しうる引数を全部明示する。** パッシブ要素経路が `rmin` 省略で呼んでいて、既定を動かすともう1本の指紋が黙って別物になった（「`PRESET` に乗っているから安全」はプリセット経路にしか当てはまらない）。「既定を変えていない」を assert する検査（`DEFAULT_RMIN == PRESET['rmin']`）は既定を変えるタスクの障害物になるので、守りたい回帰基準（`PRESET`）を直接 assert する形にする。（20260905-1528-topopt-find-filter-parameters, 2026-09-05）
- 筋（1要素幅のチェッカーボード状の筋）の判定: `min_wall_thickness` の最小値そのものでは弁別できない（`rmin=1.5` 筋あり 0.0133要素 / `rmin=2.5` 筋なし 0.0797要素。どちらも 1要素よりはるかに小さい）。使えるのは**頂点ごとの分布**（壁厚<1要素の頂点が全頂点の何%か。しきい値 **2%**。実測23点が 0.00〜0.99% と 6.78〜17.29% に割れる）。`roughness`（離散ラプラシアン）は題材をまたいで比べない（薄板は筋が無くても 0.23〜0.28）。（20260905-1528-topopt-find-filter-parameters, 2026-09-05）
- **「`rmin` を上げれば必ず良くなる」「解像度を上げれば筋が消える」に依存しない。** `plate`（厚み9要素）は `rmin=4.0` で筋の割合が 0.00→0.58% に悪化する（半径が板厚の半分に近づくとフィルタが板を潰す）。効いているのは**要素単位の半径**で物理半径ではない（物理半径を据え置いて長辺 40→56要素にしても実効 2.10要素にしかならず、`tilted` は 6.43% の筋が残った）。（20260905-1528-topopt-find-filter-parameters, 2026-09-05）
- 既定を動かすと、`perf.py` の基準線・案5の比較表・`safety_loop` の単調性の実測はすべて既定に追随して数字が失効する。**案5の粗い段は既定の `rmin` で回る**ので、`rmin` を振る実測は「塊を1回導出してキャッシュし、本番の `rmin` だけを振る」形にしないと塊の変化と交絡する。（20260905-1528-topopt-find-filter-parameters, 2026-09-05）

## メッシュ化の段0: 密度場のガウシアン平滑化と塗り直し（削除済み）・Taubin（生存）

- **生存: Taubin 平滑化の反復数 `mesh.DEFAULT_ITERS=30`**（`DEFAULT_LAMBDA=0.5` / `DEFAULT_MU=-0.53` / `DEFAULT_LEVEL=0.5`）。Taubin 30回は1セル幅の構造を消すフィルタとして働く。中周波のうねり（k≈0.25〜0.5）を消すのは密度場側のガウシアンの役目で、Taubin の反復数を増やしても効率が悪い（伝達関数 `f(k)`: k=0.25 で 0.991 / 0.5 で 0.949 / 1.0 で 0.765。**途中式は python で計算し直して検算すること**。暗算で 0.983 と間違えた）。一時 12 に下げたが 30 へ戻して現在に至る（20260904-1650 は 12 としたが 20260905-1528 が 30 に戻した）。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 20260905-1528-topopt-find-filter-parameters, 2026-09-05）
- 段0（`mesh.smooth_density` / `build_surface(smooth_sigma, keep_mask, zero_mask)`）は 2026-09-05 に既定 σ=1.0→**0.0（無効）**へ戻した（20260904-1650 の「σ=1.0 既定」を上書き）。(対象コード不在・要確認: `smooth_density` / `DEFAULT_SMOOTH_SIGMA` / `build_surface` は 2026-09-21 に旧入口ごと削除済み。現行は `mesh.build_surface_from_mask`) 以下は復活させる場合の注意。（20260905-1528-topopt-find-filter-parameters, 20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 2026-09-05）
- 段0は marching cubes の**前**に密度場（3次元配列）へ掛けるので、`build_surface` が返す頂点の座標系（要素添字空間・`spacing=(1,1,1)`・セル中心規約）は変わらず、`optimize_run._mesh_to_world` に触れない。ガウシアンの**後**に保持=1・除外/設計空間外=0 へ塗り直す（σ=0 でも効く）。マスクを渡さない経路では保持・除外は保護されない。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 20260905-1528-topopt-find-filter-parameters, 2026-09-04）
- **`Result.rho`（SIMP の生出力）は書き換えない**（`smooth_density` は `np.array(rho, copy=True)` のコピーに対して行う）。応力・安全率は平滑化前の `result.rho` で評価しており、**出力する形（平滑化後）と評価した形（平滑化前）は厳密には一致しない**（σ=1要素なら差は小さいはずだが未計測）。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 2026-09-04）
- 塗り直しに使うマスクは `anchor.run_pipeline` が `simp.optimize` に渡した `solid`/`void`（保持+アンカー材料の合算。`space.model.keep` とは範囲が違う）で、`design_space.run_topopt` と `optimize_run.py` に新しい引数を足さず自動で効いた。どちらを使うかは呼び出し側の設計判断。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 2026-09-04）
- **σ=1 が薄肉を守る保証は無い**（未検証）。`plate`/`tilted` の実測で、keep/zero マスクに入っていない薄いリブ・膜は既定 σ=1 でも大きく食われ、穴が空いたり消えたりした。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 2026-09-04）
- 体積一致検査（`check_mesh.py` 検査2）の許容差: σ=1 が既定の間は 5%→12%（実測 3.42%→8.21%）に広げたが、σ=0 に戻した後は **5% へ戻した**（σ=0 の実測が 3.42% なので、ガウシアンが既定に戻る退行はこの検査が捕まえる。戻すなら閾値も一緒に見直す）。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 20260905-1528-topopt-find-filter-parameters, 2026-09-05）
- 罠（検査設計）: 極小の合成密度場（6x5x4 に 2x2x2 のブロック）へ σ=1 を掛けると山が均されて 0.5 を割り込み、`marching_cubes` が `ValueError`（座標系の検査は `smooth_sigma=0` で呼んだ）。保持スパイクが本体ブロックと境界だけで接すると、極端な σ(3.0) で `remove_islands` が浮島として消す（本体の内側から重ねて配置する）。σ=1 での4題材 `max_iter=40` の通しは合計約290秒（`tilted` だけで 139秒）かかり、スクリーンショットの撮り直しは軽くない。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 2026-09-04）

## 塊（設計空間）の導出（案5の削除と連続値保持）

案5（`derive_from_coarse_run`）は 2026-09-10（`7acf35f`）に専用部品ごと削除された。その後 2026-09-16（`3491813`）に「参照方式」も削除され、**現行は固定凸包（`design_space.DERIVATION = "convex-hull-offset"`）を1回だけ導出する方式**。(対象コード不在・要確認: `DERIVATIONS` / `derive_default(derivation=...)` / `derive_from_reference` / `build_reference` / `ReferenceRunConditionFacesEmptyError` / `CoarseRunConditionFacesEmptyError` / `bench.SAFETY_LOOP` / `bench.DESIGN_SPACE_COARSE` / `COARSE_*` はすべて不在)

- 当時の契約: 導出は `{"bbox": derive, "reference": derive_from_reference}` の2つだけ。`derive_default(..., derivation="coarse_run")` は未知の導出で `ValueError`。`derivation="bbox"` は退避経路ではなく製品の経路（`optimize_run.run`）が塊の容量と保持体積を測るのに名指しで使う口で、既定の導出が要求する `material_mm3` を持たないため残した。（20260910-1311-topopt-remove-case5, 2026-09-10）
- **案1（`derive`＝bbox 導出）は消さない。** 連続値保持の土台で、参照の箱を作る。`design_space.derive` は現行にも存在する。（20260910-1311-topopt-remove-case5, 2026-09-10）
- 「`run_topopt`/`anchor.run_pipeline` は条件面が空のとき例外にせず `"result": None` で返し、受ける側（`DesignSpace` を返す層）が `.rho` に触る**前**に `empty_condition_faces` を自分で見て専用例外に変換する」層間契約があった。戻り値の形をこの都合で変えない。変異検査（ガードを外すと `AttributeError` に戻る）で守られていた。(対象コード不在・要確認)（20260910-1311-topopt-remove-case5, 2026-09-10）
- `simp.VolumeTargetError(side="above_capacity")` の文面は「塊を広げる(連続値保持なら k を上げる)か、」に変えた。検査が文面に `"k を上げる"` が入っていることを見ていた（以前は `"dilate_mm"`）。(対象コード不在・要確認)（20260910-1311-topopt-remove-case5, 2026-09-10）
- `docs/TOPOPT.md` の案5の節の数字は現在値として引かない（冒頭に「案5は 2026-09-10 に削除。以下は当時の実測」と注記）。現在値は「現在の正」の表だけを見る。時制の古い記述（`TOPOPT_SPEED.md`「案5は taskbox で削除する」の未来形、`TOPOPT.md` の `COARSE_DILATE_MM` を比喩に使った実在しない記号）が残りうる。（20260910-1311-topopt-remove-case5, 2026-09-10）
- 罠（材料量の勘定）: `build_reference` の材料量は**保持ソリッドを含む総体積**。検査で「容量の30%」と分率で置くと、保持が塊の半分近くを占める題材（`tiny` = 40x24x10 の箱）で保持体積を下回り `VolumeTargetError(below_minimum)` になる。`solid + 割合 × (容量 - solid)` で取る。数値は 1ビットも動かさない削除タスクだったので、`check_absolute_volume.py` [4] の基準（`rho_sha`/`c_last`）を後続が動かしたら後続の変更のせい、と切り分けられた。(対象コード不在・要確認)（20260910-1311-topopt-remove-case5, 2026-09-10）
