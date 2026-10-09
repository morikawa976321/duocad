# 形状最適化のソルバ・最適化手法・性能の契約

形状最適化（`duocad/topopt/`）の線形解法・CPU並列化・性能測定、および削除済みの SIMP 系最適化手法（OC/MMA・反力下限制約・動的アクティブセット・幾何マルチグリッド・合成4題材ベンチ）について、**ソースを読んでも分からない不変条件・依存禁止・罠**を、元の作業タスクの契約から分野別に蒸留した文書。
**いまの挙動の正はコードとテスト**（詳細な実測は [../TOPOPT.md](../TOPOPT.md)・[../TOPOPT_SPEED.md](../TOPOPT_SPEED.md)）。ここは、そこに無い（または散らばっている）契約だけを持つ。
出典は項目末尾の括弧内（タスクID, 日付）。**`(対象コード不在・要確認)` と付いた項目は、対象の関数・ファイルが現在のリポジトリに無い。**

## 現状の要約（読む前に）

- 2026-09-17（`5e3058d`）に SIMP/OC/MMA/反力下限制約/`simp.py`/`safety_loop.py`/`bench.py` と `fem.Model` の `dynamic_active` 機構をコードごと削除した。製品の探索は二値探索（`binary_search.py` / `acceptance.py`）。`simp.py` の線形代数部分は `fem.py` に残っている。
- 2026-09-09（`45e3ab3`）に合成4題材（`design_space.CASES` / `BenchCase`）と `perf.py` を削除。以後の検証は `models/*.json` の実物モデルだけ。
- 2026-09-21（`188e40f`）に幾何マルチグリッド（`solver="mg"`）を削除し、既定を `"cg"` に（その後 `"auto"` 導入。[../TOPOPT.md](../TOPOPT.md)「線形解法とCPU並列化」）。実装は `7762007` 以前の履歴に残る。
- **生き残っているのは**: CPU並列化（`fem.PARALLEL_THREADS` / `_BlockedMatvec` / `parallel_matvec`）、解の計測口（`fem.collect_solve_stats`）、反力の荷重釣り合い確認（`fem.load_resultant` / `fem.reaction_forces`）。以下、生きている面を先に、削除済みを後に並べる。

## 線形解法の計測口（fem.collect_solve_stats）

- **`fem.collect_solve_stats()`（contextmanager）が、その文脈で `Model.solve` が積んだ `solves` / `cg_iterations` / `cg_iterations_each` / `assemble_seconds` / `slice_seconds` / `solve_seconds` / `fallbacks` / `direct_solves` を返す。** 器の鍵は `new_solve_stats()` が1か所に持つ。入れ子にしない（後勝ち）。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- **`_SOLVE_STATS` が None のとき `spla.cg` に callback を渡さない。** 渡すと本番経路の費用が変わる。反復数を数える処理は**解法と同じ場所**（`Model.solve` の中の `counter`）に置き、外から `cg` を差し替えて数える方式にしない（解法を差し替えた瞬間に数え口が外れ、反復数が黙って 0 になる）。（20260904-1413-topopt-benchmark-set-four-cases, 20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- 解法を差し替えたら、反復数を `_SOLVE_STATS` へ積む処理も一緒に移すこと。計測側が「CG反復が1回も数えられていない」で落ちるのは数え忘れの合図であって、実装が壊れたわけではない。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）

## CPU並列化とスレッド数（fem.py）

- **並列化したのは「疎行列×密ベクトル」だけ**（CG 自身の `A@x`）。BLAS のスレッド数（`OMP_NUM_THREADS` 等を 1→14）は有意差なし（疎行列演算が BLAS を経由しない）。将来 BLAS 経由の重い密行列演算が増えたら測り直すこと。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- **RAP（疎×疎）と `splu` は並列化しても速くならない。** 行ブロックに割って `A@P` を投げても `sp.vstack` の再結合費用が利得を食い、**1スレッドでも単発呼びの約2倍遅い**。疎×密と疎×疎で同じ「行ブロック並列」の発想は成立しない。scipy の版が変われば結果が変わりうるので、並列化する前に自分で測り直す。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- **行ブロック分割はビット一致を保証する**（CSR の行 i の内積は行 i の非零要素だけで決まり、他スレッドと独立）。`threads=1/2/3/4/8` で変位が1ビットも変わらない（`topopt_checks/check_parallel.py`）。縮約を並列化する形に変えるなら、この結論は選び直し。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- **`threads<=1` は並列化前と同一のコード経路**（CG に生の `kf` を渡す）。この退避経路が `PARALLEL_THREADS=1` で直列に戻す運用の前提。A/B 実測 1.002倍（誤差内）。壊さないこと。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- **`_MIN_ROWS_PER_THREAD=512`** は検査で使う小さい格子（数百行）でオーバーヘッドだけが乗らないための値で、理論値ではない。変えるなら小さい格子でも遅くならないことを検査で見る。`_effective_threads` が行数に対してスレッド数を自動で絞る。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- **既定スレッド数は `max(1, os.cpu_count()//2)`**（`_default_thread_count`。論理コア14のこのマシンで7）。環境変数 `TOPOPT_THREADS` は import 時に1回だけ読み、壊れた値・0以下は1（半分化の**前**に return するので影響を受けない）。明示指定（`TOPOPT_THREADS` / `threads=N`）は絞らない（`threads=14` が全コアで動くことを実測確認）。比率が契約で、絶対値7はこのマシン固有。**旧仕様（`os.cpu_count()` そのもの）は上書きされた。**（20260904-1558-topopt-cap-threads-at-half-the-cores, 20260904-1415-topopt-parallel-solver, 2026-09-04）
- 既定を変えても数値は動かない（並列化の分割には触らない）。`check_parallel.py` [3-4] は旧既定の値そのものを assert していたので、既定を動かしたときは「検査対象の仕様が変わった」ため期待値を追随させた（「ちょうど半分」という具体値の判定は保つ）。（20260904-1558-topopt-cap-threads-at-half-the-cores, 2026-09-04）
- **「14スレッドで最速」に依存しない。** n=44・4題材の合計で t=8 が 69.9秒、t=14 が 73.5秒（tilted は t=14 で中央値が悪化。発熱・オーバーヘッドが上限を作る）。**半分（7）は t=8 の隣というだけで、最速の実測点ではない。** 「Eコアのせいで半分にした」も測っていない（Arrow Lake の P/E 混在は有力な仮説で未実証）。P コア数を数えるべきかは別途測って決める。（20260904-1415-topopt-parallel-solver, 20260904-1558-topopt-cap-threads-at-half-the-cores, 2026-09-04）
- **「全体が2倍速くなる」に依存しない。** 実測 n=44 で 1.48〜1.65倍（t=8 基準）。RAP+splu が16〜33%を占め並列化されない Amdahl 則で頭打ち。2倍は上限の見積もりで保証値ではない。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- **「薄板は並列化が効かない」ではなく「自由度が小さいと効かない」。** plate n=30（DOF 14,256）は 0.77倍（遅くなる）、n=44（DOF 35,250）は 1.53倍。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- README/文書に倍率を書くときは**測定時のスレッド数**を併記する慣習を続ける。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- 罠: **反復数のベンチを一様乱数密度で取ると桁が狂う。** `rng.uniform(0.05,1.0)` は普通に収束するが、`rho<0.3→1.0 else 0.02` のような二値に近い極端な密度は CG が 1000回超（実測 1216回）。速度比較には前者を使う（後者は並列化の効果ではなく反復数の違いを測る）。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- 罠: **`ThreadPoolExecutor` の cProfile は `lock.acquire` の tottime に惑わされる**（`future.result()` が他スレッドの計算を待つ時間も算入される）。壁時計の before/after で判断し、プロファイラの内訳だけで「オーバーヘッドが支配的」と結論しない。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- 罠: **Barrier ベースの常駐ワーカーは単発 matvec で 1.15倍止まり**（46x28x31 格子・8スレッド）。実装の複雑化（ライフサイクル・例外時のバリア解放）に見合わず不採用。再開するなら再実装が必要（試作スクリプトは残っていない）。（20260904-1415-topopt-parallel-solver, 2026-09-04）
- 罠: `git status` が触っていないファイル（`stress.py`）を modified と出すが `git diff` は空、という現象が並行セッション下で出る。diff が空なら触らず、ステージにも入れない。（20260904-1415-topopt-parallel-solver, 20260904-1558-topopt-cap-threads-at-half-the-cores, 20260904-1557-topopt-filter-radius-in-mm, 2026-09-04）
- 前処理段の並列化の口（`_vcycle(..., blocked=None)` / `_build_blocked_levels`）は、マルチグリッド削除（`188e40f`）と共に消えた。(対象コード不在・要確認: 削除済み)（20260904-1415-topopt-parallel-solver, 2026-09-04）
- `perf.py --threads {1,2,4,8,14}` の伝播経路（`simp.optimize(threads)` → `anchor.run_pipeline` → `design_space.run_topopt`）は SIMP 経路と共に削除。現在は `Model.solve(threads=None)` が `PARALLEL_THREADS` を呼び出し時に読む。(対象コード不在・要確認: `perf.py` / `simp.optimize` は不在)（20260904-1415-topopt-parallel-solver, 2026-09-04）

## 幾何マルチグリッド前処理（削除済み）

2026-09-21 に本人判断で削除（`188e40f`。探索が解く二値マスクの細い形では cg より遅く、メモリも常に多く、格子非依存も出なかった）。復活させるときの落とし穴だけ残す。復元は `7762007` 以前のコミット。(対象コード不在・要確認: `simp.MGOptions` / `build_mg_transfers` / `_mg_setup` / `_vcycle` / `mg_preconditioner` / `check_multigrid.py` はすべて削除済み)

- **前処理は対称正定値でなければ CG が壊れる。** 守る4点: 前後の平滑化回数が同じ・制限は `P^T`・最粗格子は直接解法・`omega < 2/λmax(D^-1 A)`。平滑化重みの倍率 `weight_scale > 1.0` は例外にしてあった（委譲レビューの実測で 5.0 だと `v^T M v` が負になった。検査用の口だからと上限を置かないこと）。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- 軸方向の半粗視化: `min_axis_elements`（既定4）未満の軸は間引かない。外すと薄板で厚みが潰れる。奇数の軸は端だけ幅1の粗セルになり、格子寸法が2の冪になることはない。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- **反復数が格子非依存になったと言わない。** 指数（量∝DOF^p）が平らになったのは plate（0.448→0.020）と bracket（0.426→0.298）だけ。branching は 0.409→0.488 に悪化、tilted は 0.412→0.407 で不変。絶対数は4題材とも 7〜13倍減（295.8→28.6 / 477.0→96.8 / 351.8→25.0 / 687.9→93.4）だが伸び方は別の話。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- **秒は反復数の比に従わない。** 秒/設計反復の倍率は 0.53〜1.37倍で plate n=30 は 1.37倍遅くなった。V-cycle 1回が matvec 6〜8回ぶん＋毎 solve の RAP/splu 組み直しが solve の 16〜46%（20x8x8 40% / 40x16x16 46% / 60x24x24 16%）。scipy の疎積も SuperLU もスレッド並列しないので、反復側だけ並列化しても組み直しが上限（半分なら全体は2倍まで）。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- W-cycle（`cycle_gamma=2`）は段数1のとき数学的に無効（粗格子を厳密に解くので2回目の補正がちょうど 0）。3段以上で効く（片持ち梁4段で 139→99）。段を増やすほど良いとは限らない: 一様密度なら劣化しない（32x16x16 で 1→4段が 14→18反復）のに、実密度（SIMP の 1e9 の剛性差）では 17→69反復。段数の効きは必ず設計反復を回した密度で測る。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- 罠: **一様密度で当たりを取ると全部間違える。** 効き方を決めているのは `emin=1e-9` と `penal=3` が作る剛性差で、設計が二値へ寄ってから出る。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- 罠: Gershgorin の上界だけで減衰係数を決めると平滑化が弱すぎる（真の `λmax(D^-1 A)=3.1` に対し上界 5.36。50x20x20 実密度で 56反復、べき乗法で 48反復）。べき乗法の開始ベクトルは**固定ベクトル**にする（乱数にすると同じ入力で結果が変わり、ρ場の SHA-256 を見る回帰検査が毎回落ちる）。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- 罠: 最粗格子が大きいと `splu` が急に重い（粗15,210自由度で 0.96秒/solve・充填1,680万、粗2,205自由度で 0.04秒。`direct_dofs=6000` が境目）。1段目は自由度数に関わらず必ず作ること（`direct_dofs` で打ち切ると小さい題材が黙って直接解法になり、mg 経路が検査されないまま緑になる）。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- 秒の主張は必ず幅つきで書く（同じ日・同じコードで 0.227 と 0.352 のように振れる）。反復数は何度測っても同じ整数。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）

## 性能測定の流儀（合成4題材ベンチ・削除済み）

`design_space.CASES`（`bracket` / `plate` / `branching` / `tilted`）と `BenchCase`、`perf.py`、`bench.DESIGN_SPACE_FOUR_CASES` は 2026-09-09（`45e3ab3`）に削除。(対象コード不在・要確認: `design_space.CASES` / `BenchCase` / `plate_conditions` / `branching_conditions` / `tilted_conditions` / `_face_on_plane` / `perf.py` / `perf.growth_exponents` / `bench.py` は不在。`design_space.demo_conditions` 等の内部フィクスチャも現在は無い) 測定の考え方は他の性能測定にも再利用できる。

- **物差しの第一は `CG反復/solve`、秒は補助。** 同じセッションで n=30 の4題材を撮り直したとき 8回の solve の反復数が1つ残らず一致しコンプライアンスも下6桁まで一致した一方、秒は同じセッション内でも 1.00〜1.16倍振れた（bracket 1.10x / plate 1.00x / branching 1.09x / tilted 1.16x）。**この幅より小さい秒の差を「速くなった」と言わない。**（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- **秒/設計反復は反復の中央値**（平均・合計でない）。1反復目だけ `x0=0` から解くので極端に遅い（bracket n=44 で 7.26秒 対 中央値 4.93秒）。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- **マルチグリッドは反復数に、並列化は秒に出る。** 別々の欄なので両方を表に出す。新旧は同じセッションで測り、旧基準線を撮り直した JSON だけを比較に使う。（20260904-1413-topopt-benchmark-set-four-cases, 20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- 「速くなった」ではなく**伸びの指数（量∝DOF^p）がいくつからいくつになったか**で書く。（20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）
- **スケーリング則は1題材の直線から外挿しない。** 4題材で秒/設計反復は格子要素数の 1.05乗〜2.03乗（「N^(4/3)」は片持ち梁1題材の数字）。薄板が 2.03乗なのは n=30 の行列（非零99万）がキャッシュに載り n=44（254万）で外れるため（実効帯域 27.4→16.9 GB/s）。CG反復数も4題材とも DOF の 0.41〜0.45乗（線寸法では n^1.10〜1.20）で「N^(1/3)」ではない。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- **条件数は大きさでなく形で決まる。** plate は DOF 14,256 で 477.0反復、bracket は DOF 47,817 で 295.8反復。DOF^(1/3) で割ると plate 19.7 / tilted 17.2 対 bracket 8.2 / branching 9.2 と2倍以上開く。コンプライアンスを題材どうしで比べない（材料量も荷重も塊の大きさも違う）。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- 受入検査用と性能測定用の大きさを1つにまとめない（受入は速さが命、性能は大きさが要る）。解像度 n=50 は動くが（tilted n=50 = 52x44x35 / DOF 257,580 / CG 1215.0 / 23.566秒/反復 / 1題材226秒）、44 にしたのは**測り直しの費用**（4題材で約10分）であって容量ではない（`Model._iK`/`_jK` は int32 で n=50 でも 0.37GB。最初に書いた「メモリ理由」は誤り）。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- 題材設計の罠: 薄板は既定の余白比率（0.25）では薄板にならない（余白 = 条件の点群の最長辺×比率で、長辺182mmに45mm付き厚み12mmの板が厚さ100mmの塊になる。`plate` は 0.05）。薄板は解像度 13 以下で保持パッドの中心を1つも通らず落ちる（受入は 16。`voxelize.occupancy` のレイ縮退は解像度に対して単調でない）。薄板の荷重面は受入解像度16で節点が1つしか付かず実質点荷重。「拘束DOFが多い=多点支持」ではない（面積が広いだけでも増える）ので、検査は塊の上半分の6近傍成分数で書いた。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- 罠: 切り欠きのある面は重心一致（許容 1e-4）では名指しできない。法線方向の位置で面を選ぶ手段（`_face_on_plane`）を足したが、同じ高さの別の面と衝突しうる場合（薄板の荷重面。2つのパッド上面が同じ z）は重心一致のままにした。2つの選び方の使い分けが要る。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- `Conditions` の型（面添字を int で1つずつ・力は1本のベクトル）は「拘束2箇所・荷重2箇所を別面・別向きで持つ」題材を表現できない。下の層（`AnchorTable.fixed_dofs` / `load_vector(out=...)`）は複数に対応済みで、塞いでいるのは型と `run_pipeline(force=...)` の2点だけだった。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- 補足: `tilted` の `radius` を上げても `DesignSpaceDisconnectedError` にはならず（円柱は上下を塞がない）、先に `radius=34` で `DesignSpaceTooSmallError` が出る。既定 26 は箱の 25% を削る。受入検査7本の合計が 140.9秒になった際、縮める候補は `check_anchor.py` [5] の円筒面（約27秒）で、新しい検査ではなかった。（20260904-1413-topopt-benchmark-set-four-cases, 2026-09-04）
- `simp.py` は `bench.py` / `perf.py` を import しない（計測の口は `simp.py` 側、題材の大きさは持たない）。`output/perf_*.stl` / `output/perf-*.json` は追跡しない（数字の正は README/文書）。（20260904-1413-topopt-benchmark-set-four-cases, 20260904-1414-topopt-multigrid-preconditioner, 2026-09-04）

## SIMP の更新則 OC/MMA（削除済み）

2026-09-17（`5e3058d`）に `simp.py` ごと削除。製品は二値探索で、体積制約付き連続最適化は使っていない。復元は `5e3058d` の親コミット。(対象コード不在・要確認: `simp.optimize(update=...)` / `UPDATES` / `_OCUpdate` / `_MMAUpdate` / `DEFAULT_UPDATE` / `Result.update` / `topopt_checks/measure_mma_vs_oc.py` / `docs/TOPOPT_MMA_VS_OC.md` はすべて不在)

- 更新則は `_Update` を継承して `step(*, it, x, x_phys, compliance, dcn, dvn) -> xnew` を実装し `UPDATES` に登録する形だった。`step` が返すのは設計変数だけ（物理密度への写像・収束判定・履歴・`Result` 組み立ては `optimize` 側）。更新則は1回の呼び出しにつき1つ作る（MMA は反復をまたぐ状態＝移動漸近線と直前2点を持つ）。未知の更新則名は既定へ落とさず格子を作る前に `ValueError`。`update` は後に `run_topopt` / `run_pipeline` へも通した（既定 None = 渡さない）。（20260910-1312-topopt-mma-alongside-oc, 20260910-1314-topopt-reaction-constraint, 2026-09-10）
- **MMA は体積が不等式（上限）。** 届かないと例外を上げず目標より軽い解が黙って返る（`padded_case(1)` の 98% 要求で OC は `above_capacity`・不足 24.87mm^3、MMA は 2091.93mm^3＝目標の 98.83% で `stopped_by="max_iter"`）。下限割れ（`below_minimum`）は更新則より手前の `_resolve_volume_target` が見るので両方で出る。超過側は原理的に起きない（実測で達成量は相対 -4.3e-8〜-4.7e-5）。`rho.sum()==target_volume` を前提にした比較は MMA では相対許容が要る（OC は 1e-11）。（20260910-1312-topopt-mma-alongside-oc, 2026-09-10）
- 更新則を足すときの前提: パッシブ要素の感度は 0 ではない（フィルタ越しに隣の自由要素を動かす）。OC は上書きで潰していたが、MMA は部分問題の中で `alpha == beta == x` として凍結していた（体積制約の勘定が狂うため踏襲すること）。`Iteration.change` の意味は更新則で変わる（プリセット 20x8x8 で OC は60反復で止まらず MMA は46反復で `change_tol`）。**反復数を揃えて比べるなら `change_tol=0.0` を明示する。**（20260910-1312-topopt-mma-alongside-oc, 20260910-1313-topopt-mma-vs-oc-measure, 2026-09-10）
- 「x だけ引き継ぐ」ウォームスタートは MMA では根拠が無い（状態を持つ）。（20260910-1312-topopt-mma-alongside-oc, 2026-09-10）
- **OC と MMA の比較結論（hanger 2.4mm・40反復）: 「MMA を使ってよい」。** 自由要素の一致率 0.9858〜0.9956、IoU 0.954〜0.957。最終コンプライアンス相対差 -0.382%（材料10%）/ +0.008%（30%）。MMA の体積は下振れ -1.3e-05〜-2.3e-05。1反復の中央値は MMA が 7〜15% 安い（38.29→35.49秒 / 32.82→28.46秒）。ただし共通目標への到達は OC 9〜20反復 対 MMA 25〜29反復。小題材の観測（プリセット 34.2683 対 34.2933 など）は一般則ではない。（20260910-1313-topopt-mma-vs-oc-measure, 20260910-1312-topopt-mma-alongside-oc, 2026-09-10）
- **MMA 定数（`MMA_*`）は文献の標準値のままで、校正していない。** `MMA_ASYMPTOTE_INIT` は 0.5 より **1.0 のほうが全面的に良かった**（hanger 材料30%で共通目標へ 25→17反復、一致率 0.9858→0.9946、合計 1321→1212秒）。振ったのはこの1本だけ（`move`/`SHRINK`/`GROW`/`ALBEFA` は未）。既定を倒すならこの定数も一緒に決める。「MMA のほうが change 基準で早く止まる」は hanger 2.4mm では逆（最終 change は OC 0.166〜0.173 / MMA 0.200＝移動限界）で、解像度を上げると持ち越せない。（20260910-1313-topopt-mma-vs-oc-measure, 20260910-1312-topopt-mma-alongside-oc, 2026-09-10）
- 比較をやり直すときの規則: ①`change_tol=0.0` ②一致率は**パッシブ要素を除いた自由要素**で読む（hanger は箱 359,656 要素中 37,272 が空で両方 0）③OC と MMA は**同じプロセスで連続して**回す。題材は `anchor.run_pipeline` から横取りして作り（自分で `fixed_dofs`/`load`/マスクを組み直すと製品とは別の問題を測る）、`--mma-const` は `simp` のモジュール属性を直接書き換えるので段ごとに別プロセスにする。（20260910-1313-topopt-mma-vs-oc-measure, 2026-09-10）
- **小さい題材の壁時計は信用しない。** demo30（`threads=1`・1反復1秒）で、ビット単位に同じ計算の1反復中央値が2時間空けた再測定で -16% / -16% / +2% 動いた。hanger 規模（`threads=7`・1反復32秒）では再現する（合計 0.4% 差）。秒の倍率を語ってよいのは hanger 規模だけ。更新則が7秒安いのに1反復の差は 2.8〜4.4秒しかない（MMA のほうが CG に時間が掛かる。密度場が違えば CG 反復数も違う）。（20260910-1313-topopt-mma-vs-oc-measure, 2026-09-10）
- 罠: **`TaskStop` で止めたバックグラウンドの bash が子スクリプトを生かしたまま残り**、67分後に2本目の実測を暴発させてログを切り詰め、70秒間 CPU を奪った。止めた後に `Get-CimInstance Win32_Process` の CreationDate で孤児を確認する。待ち合わせはラッパーに書かず、先行が終わってから自分で次を起動する。（20260910-1313-topopt-mma-vs-oc-measure, 2026-09-10）
- 罠: **Windows で python の標準出力をファイルへリダイレクトすると cp932 になる。** 完了マーカーを日本語で grep する待ち合わせは永久に成立しない。マーカーは ASCII にする。（20260910-1313-topopt-mma-vs-oc-measure, 2026-09-10）
- 罠: 計測スクリプトは走行の最後に JSON を書く形だと、途中で止めると生データが1バイトも残らず、**先に書いた文書だけが実在しないパスを指したまま残る**。`--merge` で段ごとに積み、長い段は分けて回す。（20260910-1314-topopt-reaction-constraint, 2026-09-10）
- 罠: 1ファイルが肥大した際の切れ目の知見: 更新則（`_Update`/`_OCUpdate`/`_MMAUpdate`）は共有状態を持たず切りやすいが、`_OCUpdate` が `VolumeTargetError` を上げるので**例外の置き場を先に決めないと循環 import になる**。反力の型と計算（約230行）は `Model` を型注釈でしか使わず別モジュールへ出せるが、それだけでは 1,500 行の目盛りを下回らなかった。（20260910-1312-topopt-mma-alongside-oc, 20260910-1314-topopt-reaction-constraint, 2026-09-10）
- 罠: `anchor.py` は `from . import simp` を関数の中で行うので、`an.simp` を差し替えようとすると `AttributeError`（`simp.optimize` をモジュール属性ごと差し替えれば届く）。(対象コード不在・要確認)（20260910-1312-topopt-mma-alongside-oc, 2026-09-10）

## 反力（荷重釣り合い確認は生存・面ごとの内訳と下限制約は削除済み）

- **生存**: `fem.load_resultant(f) -> (3,)`（荷重ベクトル→合力）と `fem.reaction_forces(model, rho, penal, u) -> (3,)`（拘束自由度全体の反力の合力 `R=(K u - f)|_fixed` の軸ごとの合計）。**荷重釣り合い（合計が荷重合力の符号反転）の確認にだけ使う。** 拘束面ごとの内訳・下限制約・感度は持たない。（20260910-1314-topopt-reaction-constraint, 2026-09-10 ※戻り値の形は削除時に `(n,3)` から `(3,)` へ変わった）
- 削除済みの面（要旨）: `ReactionGroup` / `Reaction` / `reaction_forces -> (n,3)` / `reaction_sensitivity` / `ReactionTargetError` / `simp.REACTION_TOLERANCE=0.02` / `reaction_min`（MMA 必須、OC に渡すと格子を作る前に `ValueError`）/ 応答の `"reactions"` / `docs/TOPOPT_REACTION.md`。(対象コード不在・要確認: `simp.py` ごと削除。`optimize_run` の応答は反力を常に空リスト)（20260910-1314-topopt-reaction-constraint, 2026-09-10）
- 反力は無次元（荷重総量 1.0）で、読むのは `fraction`（`magnitude/|荷重の合力|`）。**`fraction` の合計は 1 にならない**（ベクトル和は荷重合力の符号反転に 8.6e-14 で一致するが、大きさの和は取付点どうしが引き合うぶんだけ大きい。hanger 3枚で 3.85〜3.88）。「合計が 1」を前提に読まない・断言しない。随伴が「グループ数-1本」で済む前提も置けない（一定なのは合力ベクトルの和で、大きさの和ではない。n 本より多い 3(n-1) 本の随伴が要る）。（20260910-1314-topopt-reaction-constraint, 2026-09-10）
- 設計知見（ε＝各拘束面の反力下限。推奨 0.10、既定では無効だった）: ①「保持ソリッドが残る最小の ε」は存在しない（ε が決めるのは受け持ちの量）②代償は ε に強く効く（合成題材でコンプライアンス +234%(ε=0.02)〜+35746%(ε=0.70)。**下限は繋げるためで、荷重を配り直すためではない**）③hanger（`hanger_test`/`hanger_bar_attachment`、5.3/4.0/2.412mm のどれでも）は制約なしで3枚とも 0.84〜1.93 を受け持ち、制約が1度も効かない（ε=0.10 は健全設計の最小の 1/8。「粗いから病理が出ないだけ」は 2026-09-11 に本番解像度で反証）。ε の下限を測る場は合成題材（遠い壁）にしかなく、そのコンプライアンス悪化率は他題材へ持ち越せない（向きだけ持ち越せる）。④**効かない場でも費用は 2.36倍**（`hanger_test` 2.412mm・材料30%・40反復・`threads=7`で 1331→3143秒。答えは同じ。線形解 40→160・CG反復合計 2.88倍・峰メモリ 7.8GB 不変）。「念のため常に掛ける」ではなく反力内訳を見て 0 に近い取付点があるときだけ掛ける。（20260910-1314-topopt-reaction-constraint, 2026-09-10）
- 超過の評価は `fraction/ε − 1`（割る側の量）で行い、絶対差 `fraction − ε` と混ぜない（桁が1つずれる）。静定な支持配置では原理的に満たせない。判定は最後に解いた密度場の実際の反力で行い（MMA の近似した制約値ではない）、反力を1度も測っていない終端（`max_iter=0` や `reactions=[]`）は「満たした」と読ませず例外にする。途中の反復で制約を破るのは許容（Svanberg の人工変数 `MMA_CONSTRAINT_PENALTY=1000`）。(対象コード不在・要確認)（20260910-1314-topopt-reaction-constraint, 2026-09-10）
- 罠: `|R_i|=0` の点で `|·|` は微分できず、随伴の右辺が零になって感度 0。反復の中で `Model.prepare` が持つ K_ff と各段は大きいので、使い終わったら名前を手放す（持ち越すと峰メモリが重なる）。中間 STL（2.4mm を2段で 6.5MB）は JSON に数字があるのでコミットしない。(対象コード不在・要確認)（20260910-1314-topopt-reaction-constraint, 2026-09-10）

## 動的アクティブセット（dynamic_active・削除済み）

2026-09-17（`5e3058d`）に `fem.Model` の `dynamic_active` 機構一式を削除（本人判断 2026-09-13 では一度維持と決めたが、段階6で計画書の指示により削除）。(対象コード不在・要確認: `Model.dead_rho_threshold` / `reset_active` / `_refresh_active` / `design_space.PICK_RATIO` / `"active_refresh_seconds"` / `topopt_checks/measure_active_set.py` / `docs/TOPOPT_ACTIVE_SET.md` はすべて不在)

- 閾値は `emin ** (1/penal)`（定数でなく `penal` に追随）。上げると「落とした要素が戻れるのは数値的に死んでいた要素だけ」という論証が崩れる（捨てる感度の割合: 閾値 1e-3 で 2.56e-12、1e-2 で 3.996e-05＝1.56e7 倍）。（20260912-1715-topopt-dynamic-active-set, 2026-09-12）
- `free` が反復ごとに変わるとき見直す箇所: 移送作用素キャッシュは捨てる・ウォームスタート `u0` は全長ベクトルを `model.free` で縮める・`compliance`/`stiffness_apply`/`reaction_forces` は同じ集合に揃える・`PreparedSystem` は**1つの ρ につき1つを使い切る**（別の ρ で `prepare` すると `model.free` が変わり先に返した `kf` と噛み合わない）。`fixed` は `_split_dofs` を通ると昇順に揃うので集合で比べる。`update="mma"`・`reaction_min` との併用は入口で `ValueError`。（20260912-1715-topopt-dynamic-active-set, 2026-09-12）
- **`np.union1d` / `np.setdiff1d` を自由度に使わない。** ndof 3.6M で合わせて 8.0秒/反復（setdiff1d 6.32 + union1d 1.66）。真偽マスク（`_split_dofs`）で 0.013秒。これを直す前は口を入れても -2.6% しか速くならず採否の見え方が変わる大きさだった。（20260912-1715-topopt-dynamic-active-set, 2026-09-12）
- **崖は反復1の求解にあり、この仕組みは最適化を始められるようにするものではない。** 一様初期値（充填率 0.0077）は閾値 1e-3 を1要素も下回らず、`PICK_RATIO=0.60` でも効果ゼロ（どちらも `SolveResidualError` 残差 2.963e-06・max|u| 1.739e+06・338.6秒 対 334.6秒。秒は同条件で 15% 振れる）。崖の出所は `emin` ではなく**保持ソリッド(ρ=1)と背景(ρ=充填率)のコントラスト**（0.77% で 2.2e6 倍、5.8% で 5.1e3 倍）で、`spla.cg` は再帰残差で「収束」と返す一方で真の残差は約3000倍ある。次に手を付けるなら真の残差での再スタート・対角スケーリング・初期場を一様にしない。（20260912-1715-topopt-dynamic-active-set, 2026-09-12）
- 既定（`PICK_RATIO=0.08`）での効果: 答えは変わらない（コンプライアンス相対差 4.74e-06・反力3枚6桁一致・保持ソリッド4つの生死一致）。40反復で自由自由度 299,625→90,105（設計空間の 75% が閾値未満）、1反復 49.8→37.6秒（-24.5%）。ただし CG反復数 +36.7%（`free` が細い構造になり幾何マルチグリッドの効きが落ちる）。（20260912-1715-topopt-dynamic-active-set, 2026-09-12）
- 罠: `docs/TOPOPT.md` の 2026-09-12 基準値（C=121.14 / 反力 0.643/0.643/1.248 / 保持4つ生存）は再現できなかった（口なしで C=127.02 / 0.608/0.608/1.199 / 保持#1と#4が消える）。原因は「塊の切り出し元の参照 run の違い（2026-09-09 の pickle）」か「スレッド数 7 対 1」で未切り分け。数字を引くときはどの参照・何スレッドで測ったかごと引く。（20260912-1715-topopt-dynamic-active-set, 2026-09-12）
