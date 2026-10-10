# 形状最適化の最小幅（min_width）・芯の連結・peel の契約

最小幅の保証（規則 R(m)・芯の連結 S(m)・段階4 `peel` への射影・入力検査と画面の段）について、ソースを読んでも分からない不変条件・依存禁止・罠。
いまの挙動の正はコードとテスト、設計の理由と実測の正は [../TOPOPT.md](../TOPOPT.md)「最小幅の保証」と [../TOPOPT_THICKNESS.md](../TOPOPT_THICKNESS.md)。
保持セル（keep）まわりは [topopt-keep-cells.md](topopt-keep-cells.md)、最小幅の入口検査の周辺の目標係数は [topopt-target-factor-warm-start.md](topopt-target-factor-warm-start.md)。
各項目末尾の括弧は出典のタスクID(先頭8桁が日付)。コード中の名前は 2026-10 時点の `duocad/topopt/min_width.py`・`binary_search.py`・`optimize_run.py` で突き合わせ済み。

## 規則と用語（R(m)・S(m)）

- **R(m)**: 全材料セル（`exempt` を除く）が、m 立方の材料だけのブロックのどれかに属する。大域判定は `binary_search._mask_satisfies_min_width`。**S(m)**: 芯 K（セル c を最小の角とする m 立方が全部材料）の6連結成分を、保持セルで結んでなお1塊。判定は `min_width.CoreConnectivity` に集まっている。`_mask_satisfies_min_width` は R(m) のみで S を含まない。合格候補の大域検査は `_mask_satisfies_min_width` と `CoreConnectivity.connected` の2つを見る。(20260921-0055-topopt-min-width-core-connectivity)
- `min_width_mm` 指定（m>1）の探索の採用形状は R(m) **と** S(m) を満たす。`peel` の各手・合格候補の大域検査がこれを守る。保持セルを介した芯の結びは、保持セルが細い橋でも S(m) を満たす（保証しない）。(同上)
- 規則を満たす形は、絞った設計空間（設計空間の m 立方 opening）の部分集合。**絞ることで失われる解は無い**ので、絞った空間での入口検査の拒否は「規則を満たす形が存在しない」と同じ意味。(20260920-1722-topopt-min-width-review-findings)

## 設計空間の絞り込みと初期探索（find_initial_feasible）

- `min_width_cells>1` のとき `find_initial_feasible` / `find_best_feasible` は冒頭で設計空間を `min_width.cube_opening`（m 立方 opening を3軸の1次元 erosion→dilation に分けたもの。`binary_opening` と結果が同じ）で絞る。入口検査（拘束面の被覆・保持・閉じた空洞）は絞った空間に対して行う。本体は `_find_initial_feasible`。診断は `diagnostics['min_width'] = {cells, design_cells, trimmed_cells, design_core_groups, core_adjacency}`（`find_best_feasible` が段階4まで進んだ結果では `diagnostics['initial']['min_width']`）。m=1 は従来と同じでコスト増なし。(20260920-1647-topopt-min-width-measure-and-qa2, 20260920-1722-topopt-min-width-review-findings, 20260921-0055-topopt-min-width-core-connectivity)
- **評価する成分は FEM の前に大域検査（R(m) と S(m)）を通す**（島の除去は規則を保つと限らないため）。落ちた成分は `Trial(outcome='infeasible', checks={'min_width': 'fail'})` を残し、FEM は呼ばない。(20260920-1647-topopt-min-width-measure-and-qa2, 20260920-1722-topopt-min-width-review-findings)
- **直接の m 立方 opening は費用が m³ に比例**（100×60×60 格子・全材料で m=32: 14秒、m=48: 218秒）。軸ごとに分けて m に比例させた（m=48: 0.03秒。小さい窓の m=2・3 では約20マイクロ秒遅い）。(20260920-1722-topopt-min-width-review-findings)
- 設計空間の芯が割れた入力は、入口では断らず（記録だけ。`design_core_groups`）`no_feasible_found` になりうる。何を落とすかは本人判断待ち。hanger 2.4mm の m=2・m=3 は塊が1つなので通る。(20260921-0055-topopt-min-width-core-connectivity)
- 設計空間の opening は `opened | (設計空間 & keep)`（保持セルの扱いは topopt-keep-cells.md）。(20260920-2123-topopt-min-width-exempt-preserve)

## 入力検査と既定（optimize_run）

- **既定**: `OptimizeOptions(min_width_mm=None)` で `_resolve_min_width` が `max(2.0 * spacing, 1.0)` を返す（2026-09-22 本人決定。1.0mm が効くのは spacing<0.5mm のときだけ）。戻り値 dict に `"source"`（`"default"` | `"requested"`）があり、**`requested_mm` は `None` にならない**（既定のときも実際に使った値）。`run()` の結果 `min_width` dict・trace の `resolved-conditions`・画面の結果通知（`web/src/App.svelte` の `handleOptimizeResult`）は同じ dict 経由なので、既定/明示は `_resolve_min_width` 1箇所を見ればよい。(20260922-1516-topopt-min-width-default)
- 既定は常に `cells >= 2` を返す（m=1 は返らない）。`binary_search.find_best_feasible` 自身の既定（`min_width_cells` 省略時は1=制約なし）は**意図的に変えていない**: 製品の入口 `run()` は必ず `_resolve_min_width` を経由して明示で渡し、内部既定は `topopt_checks/` のスクリプトと `binary_search` 単体テストが使う。(同上)
- **`_resolve_min_width(spacing, requested, max_cells=None)`**: `max_cells`（格子の最短辺のセル数。`run()` は `_grid_shape(limits bbox, spacing)` から渡す）を渡すと `max_cells × spacing` を超える値を `OptimizeInputError`（文面に「上限」）で断り、m を `max_cells` で頭打ちにする。上限は「立方が格子に入る最大」= 原理的に不可能な値だけを断る。**太すぎて削れないのは利用者の選択**（「上限は設けない」の方針）。上限ちょうどの値は `値 / spacing` の丸めで m が1つ跳ねうるので、mm で比べて m を頭打ちにする（画面と同じ比較）。既定採用時に上限を超えたときの文面は「最小幅を下げよ」ではなく「要素サイズを下げよ」。(20260920-1722-topopt-min-width-review-findings, 20260922-1516-topopt-min-width-default)
- 下限は `2 × spacing`（8等分した評価格子で4セル分が見える最小の幅）。これ未満の明示値も `OptimizeInputError`。`resolution_limits()["steps"][i]` には `min_width_ceiling_mm`（= 最短辺セル数 × その段の要素サイズ）と `min_width_steps_mm`（m=2..最短辺のセル数の値の配列）が付く。(20260920-1722-topopt-min-width-review-findings, 20260920-1730-topopt-min-width-floor-measure)
- **`min_width_steps_mm(spacing, max_cells)`**: 各値を `_resolve_min_width` に渡すとちょうどその m になる。`m*s/s` が丸めで m を超えるとき（0.1×3/0.1 → 4）は1ulpずつ下げてある。段の値をそのまま `m * spacing` にすると、画面が示した段を入力しても m が1つ飛ぶ。(20260920-1730-topopt-min-width-floor-measure)
- `optimize_run._search_failure_warning(search_result, min_width=None, ...)`: 最小幅が有効（`cells>1`）のとき、入口で止まった（`input_contradiction` / `boundary_mapping_failed`）なら「絞ったセル数を除いた後の空間に対する検査」、予算などで止まったなら「予算より先に最小幅を疑う」を末尾に添える。(20260920-1722-topopt-min-width-review-findings)
- `topopt_checks/measure_min_width_floor.py --cells 1` は測れない（m=1 は既定からも到達不能で `out["error"]` を返すだけ）。係数 `SEARCH_TARGET_FACTOR = 2.4` と規則 R(m)・S(m) は既定変更では変えていない。(20260922-1516-topopt-min-width-default)

## 画面（web/src/minWidth.ts・OptimizePanel）

- `web/src/minWidth.ts`: `judgeMinWidth` / `shownFloorMm` / `shownCeilingMm` / `shownGuaranteedMm` / `roundUpNotice` / `shownStepMm`。下限は切り上げ・上限と保証幅は切り捨てで表示する。**表示した値をそのまま入力して通る向きに丸める**（桁数は下限の大きさから決める。小さい部品で下限・上限が逆転しない）。`judgeMinWidth` の `ok` に `cells` / `roundedUpBy` / `stepMm` / `lowerStepMm` がある。(20260920-1722-topopt-min-width-review-findings, 20260920-1730-topopt-min-width-floor-measure)
- **段のボタン・datalist（`<datalist id="optimize-min-width-list">`）の値は丸めない**（m=2 の段=下限 `2×spacing` は2桁に丸めると下限割れ（切り下げ）か m=3（切り上げ）になる）。表示は丸めてよい。(20260920-1730-topopt-min-width-floor-measure)
- クライアントの m は `ceil(値/要素サイズ)` をサーバーと同じ式で計算し、段の値そのものは配られた配列を使う（段ちょうどの値がクライアントの `ceil` で1つ上へ跳ねないことを `tests/eval_min_width.mjs` が見る）。(同上)
- puppeteer で Svelte の `$state` プロキシを `page.evaluate` の戻り値にすると `{}` になる。`JSON.parse(JSON.stringify(...))` を通す（`web/scripts/browser.mjs` も同じ処置）。(同上)

## 保証されるものの限界

- **保証されるのは探索したマスクの幅で、書き出す solid の幅ではない。** 合成の角材で平滑化（marching cubes + Taubin）後の中央断面は t=2: 1.814（−9%）、t=3: 3.442、t=4: 4.435。ボクセル STL 書き出し（`topopt_checks/measure_min_width_floor.voxel_surface`）は平滑化を通さないのでこの差は見えない。(20260920-1722-topopt-min-width-review-findings)

## 射影つき peel（段階4）

- `peel(mask, order, count, anchor_ids, *, min_width_cells, exempt, protected)`。**手は「セル x + 巻き添え」の一口**。巻き添えは x を抜いた後に m 立方の被覆を失う材料セル（`min_width.casualties` → `lost_coverage`）。一口を抜いた形は、`exempt` が無ければ `opening(mask − 領域)` に等しい。`exempt` があるときは被覆を失った `exempt` セルが残る（巻き添えにならず、材料としては他のセルの立方に数える）。厚みの不変条件は構成上保たれるので、**厚みを理由に手を却下しない**。(20260921-0053-topopt-projected-peel)
- **`min_width.lost_coverage(mask, region, m, exempt=None) -> (origin, lost)`**: 巻き添えはこれで取る（二重実装しない）。`region`（3軸スライスの組）を丸ごと空にしたと仮定して被覆を失う材料セルの真偽配列 `lost`（`region` の周り距離 m−1 の帯）と、その (0,0,0) の `mask` 添字 `origin`。座標は `np.argwhere(lost) + np.asarray(origin)`。`mask` が既に規則を満たす前提。判定は `region` から距離 2×(m−1) の窓だけで足り、大域を再計算しない。`exempt` のセルは `lost` に入らない。(同上)
- `min_width.casualties(mask, cell, m, exempt=None) -> (n,3)` は1セル用の薄い包み。`min_width.cube_opening(mask, m)` は m 立方の opening。`min_width.bite_verdict(padded, bite)` は複数セルの一口の 表面・多様体・連結 の窓判定。戻りは (理由|None, 窓の中で連結と言えたか)。理由は `'surface'`・`'manifold'`・`'connectivity'`。**支えの判定は含まない**（`peel` 側が条件節点の支え数を持つため）。(同上)
- `bite_verdict` の座標: `padded` は外周 +1 なので、内側の (i,j,k) は `padded` の (i+1,j+1,k+1)。窓は一口の外接箱 +1 セルで、頂点は `[1:-1]` の内側だけ見る（窓の外周の頂点は窓の外を空と見るので使えない）。(同上)
- **`PeelStats`**: `skipped_thickness` は**常に 0**（`> 0` を期待するテスト・記録は成り立たない）。`casualties` が増え `as_dict` にも出る。`stats.picked` は「手の数」で、減ったセル数は `picked + casualties + islands`。`count` の打ち切りは巻き添えも数えるので、最後の一口で要求を最大 (2m−1)³−1 セル超える。(同上)
- **支えの数え方**: `peel` は一口の節点IDを重複ありの並びで持ち、却下は `support_flat[n] <= 出現回数`、採用は出現回数ぶん `-= 1`。x 単独の支えの早期却下は残してある（x ⊂ 一口なので x で落ちれば一口も落ちる）。(同上)
- 2セル厚の板では、上面のセルも下面のセルも**同じ貫通柱**を一口にする（重複候補の源。回帰テストはこの形）。自由縁のすぐ内側のセルはリブごと（x 方向に2セル×厚み）抜ける。2×2 の棒の途中は断面ごとで、片側に条件節点が無ければ全体の数え直しで却下される。`_reduce_volume` は不合格の形のハッシュを、手の数に関わらず `rejected` に入れる。(同上)
- **m=1 では `min_width` の関数も `CoreConnectivity` も一切呼ばない・作らない**（`tests/test_binary_search.py` が `min_width` モジュールを差し替えて、呼んだら落ちるようにしてある）。(20260921-0053-topopt-projected-peel, 20260921-0054-topopt-projected-stage3, 20260921-0055-topopt-min-width-core-connectivity)
- **`binary_search` の中で `min_width` は「モジュール名」**。同名のローカル変数（`optimize_run` の `min_width` の dict など）があるスコープではモジュールを別名で import する。(20260921-0053-topopt-projected-peel)

## 芯の連結 S(m)（CoreConnectivity）

- `CoreConnectivity(m, exempt=None)`（`exempt` は保持セルの真偽配列）。`.groups(mask)`: 芯 K の6連結成分を保持セルで結んだ塊の数（K が空なら 0）。`.connected(mask)` = 塊 ≤ 1（大域。`exempt` は `mask` に無くても材料に数える）。`.window(changed_lo, changed_hi, shape)` → `(lo, hi, ext)` と `.proven(before, after, lo, hi) -> bool`: 窓の判定。**真 = 「抜く前に S(m) を満たしていれば、抜いた後も満たす」と窓の中で言えた。偽 = 言えなかっただけで破れるとは限らない**（呼び出し側が `.connected` で大域を数え直す）。`.after_bite(inner, bite, island_cells)`（`peel` 用）は、窓で言えなければ大域で数え直すところまで含む。`min_width.core_cells(material, m)`: m 立方が軸に収まらない格子では全部偽。(20260921-0055-topopt-min-width-core-connectivity)
- 診断: `PeelStats.skipped_core`（`as_dict` に出る。m=1 は常に0）・`diagnostics['min_width']['design_core_groups']`。(同上)
- **`peel` の前提: 入力 `mask` が S(m) を満たす**（窓の判定は「抜く前に満たす」を前提にした十分条件）。`_reduce_volume` は段階3の合格候補（大域検査を通ったもの）から始まるので満たす。(同上)
- 窓判定 `proven` の健全性（真なら大域でも真）は `tests/test_min_width_core.py` が独立な物差し（`tests/topopt_support.py` の `core_groups_oracle`）と突き合わせて固定している。**`window` の範囲を変えるときはそのテストを通すこと。**(同上)
- **`peel` の所要時間**: S(m) の判定は hanger m=2 で `peel` の合計を 38 秒 → 398 秒にした（大半は窓で言えず大域で数え直した手）。所要時間を前提にした見積もり・測定の並べ方をしない。(20260921-0055-topopt-min-width-core-connectivity, 20260922-1516-topopt-min-width-default)
- 乱数のマスクは `peel` の前提（全頂点が正則・1成分）を満たさない。`peel` を繰り返すテストは全材料の箱から始める（`test_peel_refuses_the_moves_that_would_split_the_core_...`）。R と S は別々に差し替える（`_mask_satisfies_min_width` を実行時に差し替えると `peel` の島の除去の確認〈m≥3・保持あり〉まで差し替わり R のみの側の数字が歪む）。(20260921-0055-topopt-min-width-core-connectivity)
- 島の除去は保持成分と辺・頂点でも接しないので、`exempt` が無いか m=2 なら島の除去は厚みの不変条件を壊さない。m≥3 では mask に無い `exempt` セルを挟んで跨ぎうるので、島を消した形を大域で確かめ、崩れる手は却下する（`skipped_connectivity`）。(出典: 現行 `peel` の docstring)

## 測定の基準列と読み方（hanger 2.4mm）

数字の正は [../TOPOPT_THICKNESS.md](../TOPOPT_THICKNESS.md) 節I〜M。測定の生データは git 管理外で、現リポジトリの `output/min_width/` には残っていない。

- **規則が違う列を並べない**: 節K（数えない版: 削られた保持セルを材料に数えない）／ K2 の `m3`・`m2-head`（数える版: `mask ∪ keep`、`705bc1c` 以降）／ 節M（S(m) つき、`11a5512` 以降）。目標 2.0 の基準の列が要る後続は、節M の「S(m) つき」の列を基準にする: m=2 は 3,991 要素・安全率 2.1517・細分 k=1÷k=3 が 1.43 倍、m=3 は 5,222 要素・2.3094・1.33 倍。R のみの列（K2）と混ぜない。(20260921-0054-topopt-refine-measure-m2-m3, 20260921-0055-topopt-min-width-core-connectivity)
- 節K1（R のみ・射影つき peel・m=2・予算60）: 3,546 要素・材料 0.94%・安全率 2.0000104・評価 60（合格45/不合格15）・巻き添え 87,190・`repeated_candidates` 3・芯 71 成分。試作（節J2）の 3,398 要素・安全率 2.00007 とは**完全一致しない**（`exempt` と重複候補の回避で道筋が変わった）。(20260921-0053-topopt-projected-peel)
- **目標 2.0 の列（予算60）は目標に届かず止まった形**: S 付き m=2 は 2.15、m=3 は 2.31。K2 の m=3 は 2.16、m=2 再測定は 2.18。予算を伸ばした形は測っていない。(20260921-0054-topopt-refine-measure-m2-m3, 20260921-0055-topopt-min-width-core-connectivity)
- m=2 の下限測定（予算60・実 spacing 2.387506mm）は、同じ探索を2回回して採用形状・安全率が完全に一致する（決定的）。ただし「形そのものの照合」は採用要素数・安全率・評価回数・`skipped_thickness`・opening で行い、マスクの突き合わせはしていない。(20260920-1647-topopt-min-width-measure-and-qa2, 20260920-1730-topopt-min-width-floor-measure)
- `refine_adopted_mask.py` の k=1 は探索の安全率と一致しない（m=2 K1 2.00001 対 2.0046、再測定 2.1807 対 2.1823、m=3 2.1601 対 2.1577）。スクリプト冒頭コメントの「一致する」は正確でない `(要確認: 未修正のまま)`。細分の合否は `--target`（既定 2.0）に対し、探索に渡した目標ではない。(20260921-0054-topopt-refine-measure-m2-m3, 20260921-0537-topopt-inner-target-hanger-curve)
- `c80e9ff` 以降のコードで K2 の値が再現するかは確かめていない（m=2 は `e74d43e` の展開で全項目〈所要時間を除く〉再現した。m=3 の K2 は現行で未再現）。(20260921-0054-topopt-refine-measure-m2-m3, 20260921-0055-topopt-min-width-core-connectivity)
- 所要時間: 細分 k=1〜3 の解き直しは合計 8〜14 分（k=3 は要素 8万〜14万で 6〜11 分）。探索（予算60）は S 付きで 24〜28 分（R のみの m=2 は 16〜20 分）。射影つきの m=3 の所要時間は当時未測定。(同上)
- 2.4mm の要素サイズ要求は経路で実 spacing が違う（節H2: 2.4063mm、`_snap_to_grid` 経路: 2.387506mm）。格子が違う列を直接比較しない。(20260920-1647-topopt-min-width-measure-and-qa2)
- 本人判断待ちで未決: 拘束・荷重に接する薄いセルを規則の対象外にするか、段階4のブロック単位の削除。(20260920-1722-topopt-min-width-review-findings)

## 測定・運用の罠（共通）

- **他セッションが同じチェックアウトの製品コードを実行中に書き換える**。測定の前後で `git log -3`・`git status --short -- duocad topopt_checks`・`duocad/topopt/*.py` の更新時刻を記録し、どのコード状態で走ったかを結果に書く。作業ツリーでなく `git archive` の展開を `sys.path.insert(0, ...)` で import して回す（`PYTHONPATH` は `.venv` の editable インストールに負ける）。結果 JSON の「コードの状態」は実行後に `git rev-parse HEAD` を取ると動くので、展開時に `HEAD.txt` へ書き出して読む。(20260921-0054-topopt-refine-measure-m2-m3, 20260921-0055-topopt-min-width-core-connectivity)
- Windows の Python（venv 含む）は Git Bash の `/c/...` パスを開けない（黙って無効）。`C:/...` か相対パスで渡す。stdout/`open()` の既定は cp932 なので、cursor の出力 JSON は `encoding='utf-8'` で読み、日本語を print するなら `PYTHONIOENCODING=utf-8`。システムの `python` に numpy は無い（`.venv\Scripts\python.exe`）。(20260920-2123-topopt-min-width-exempt-preserve, 20260921-0053-topopt-projected-peel, 20260921-0537-topopt-inner-target-hanger-curve)
- venv の python を `Start-Process` で切り離すと本体は launcher の子として走る。待つには `timeout N bash -c 'until [ -f 結果 ]; do sleep 10; done'` の前景ループか、PowerShell の `Wait-Process` を使う（Bash の `sleep` 単体はフックで止まる）。空きメモリは `Get-Counter '\Memory\Available MBytes'`（`FreePhysicalMemory` はキャッシュを除くので低く出る）。「空きメモリ 10GB 以上を待つ」ループは自分の子プロセスを「他の重い Python」と数えて数分余計に待つことがある。(20260920-1647-topopt-min-width-measure-and-qa2, 20260921-0055-topopt-min-width-core-connectivity)
- bash の heredoc に `'''`・複雑なクォート・Python の `\` 行継続・`\n` を含む Python を書くと、構文エラーになるか行継続が文字列に消費されて壊れる（実際に `min_width.py` に空白だらけの行が入った）。長い編集は Write/Edit ツールで行う。(20260920-1730-topopt-min-width-floor-measure, 20260921-0054-topopt-projected-stage3, 20260921-0055-topopt-min-width-core-connectivity)
- 行末: `docs/TOPOPT_THICKNESS.md` は CRLF と LF が時期で食い違った（`git ls-files --eol` で確かめる。Python で書き換えるなら `newline=''` で読み書きして行末を保つ）。テスト実行で `output/optimization_runs` に実行記録（各約1MB）が残るので、自分のものだけ（プロセスIDが manifest にある）削除する。(20260920-1809-topopt-warm-start-wiring, 20260921-0053-topopt-projected-peel, 20260921-0537-topopt-inner-target-hanger-curve)
- 数字を引用するときは、その数字がどの条件（列見出し・m・規則の版）の測定かまで確認する。`docs/TOPOPT_THICKNESS.md` 節Mの「38秒」「398秒」は **どちらも m=2**（R のみ → S を足す）の比較で、m=1 vs 既定（m≥2）の比較ではない。`docs/TOPOPT.md`「最小幅の保証」の `peel` コスト増の記述は「旧既定 m=1 からの増分」の実測値ではなく定性的な記述。(20260922-1516-topopt-min-width-default)
- 並行セッションが同じファイルを別件で編集しているとき、パス指定コミットだけでは巻き込む。`git diff` から自分のハンクだけ抜いたパッチを作って `git apply --cached` で部分ステージする。(同上)
