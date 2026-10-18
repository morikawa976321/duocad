# 形状最適化の最小幅（min_width）・芯の連結・peel の契約

最小幅の保証（規則 R(m)・芯の連結 S(m)・段階4 `peel` への射影・入力検査と画面の段）について、ソースを読んでも分からない不変条件・依存禁止・罠。
いまの挙動の正はコードとテスト、設計の理由と実測の正は [../TOPOPT.md](../TOPOPT.md)「最小幅の保証」と [../TOPOPT_THICKNESS.md](../TOPOPT_THICKNESS.md)。
保持セル（keep）まわりは [topopt-keep-cells.md](topopt-keep-cells.md)、探索に渡す目標係数は [../TOPOPT_TARGET_FACTOR.md](../TOPOPT_TARGET_FACTOR.md)。

## 規則と用語（R(m)・S(m)）

- **R(m)**: 全材料セル（`exempt` を除く）が、m 立方の材料だけのブロックのどれかに属する。大域判定は `binary_search._mask_satisfies_min_width`。**S(m)**: 芯 K（セル c を最小の角とする m 立方が全部材料）の連結成分(m が 4 未満は6連結、`CORE_26_MIN_M`=4 以上は26連結)を、保持セルで結んでなお1塊。判定は `min_width.CoreConnectivity` に集まっている。`_mask_satisfies_min_width` は R(m) のみで S を含まない。合格候補の大域検査は `_mask_satisfies_min_width` と `CoreConnectivity.connected` の2つを見る。
- `min_width_mm` 指定（m>1）の探索の採用形状は R(m) **と** S(m) を満たす。`peel` の各手・合格候補の大域検査がこれを守る。保持セルを介した芯の結びは、保持セルが細い橋でも S(m) を満たす（保証しない）。
- 規則を満たす形は、絞った設計空間（設計空間の m 立方 opening）の部分集合。**絞ることで失われる解は無い**ので、絞った空間での入口検査の拒否は「規則を満たす形が存在しない」と同じ意味。

## 設計空間の絞り込みと初期探索（find_initial_feasible）

- `min_width_cells>1` のとき `find_initial_feasible` / `find_best_feasible` は冒頭で設計空間を `min_width.cube_opening`（m 立方 opening を3軸の1次元 erosion→dilation に分けたもの。`binary_opening` と結果が同じ）で絞る。入口検査（拘束面の被覆・保持・閉じた空洞）は絞った空間に対して行う。本体は `_find_initial_feasible`。診断は `diagnostics['min_width'] = {cells, design_cells, trimmed_cells, design_core_groups, core_adjacency}`（`find_best_feasible` が段階4まで進んだ結果では `diagnostics['initial']['min_width']`）。m=1 は従来と同じでコスト増なし。
- **評価する成分は FEM の前に大域検査（R(m) と S(m)）を通す**（島の除去は規則を保つと限らないため）。落ちた成分は `Trial(outcome='infeasible', checks={'min_width': 'fail'})` を残し、FEM は呼ばない。
- **直接の m 立方 opening は費用が m³ に比例**（100×60×60 格子・全材料で m=32: 14秒、m=48: 218秒）。軸ごとに分けて m に比例させた（m=48: 0.03秒。小さい窓の m=2・3 では約20マイクロ秒遅い）。
- 設計空間の芯が割れた入力は、入口では断らず（記録だけ。`design_core_groups`）`no_feasible_found` になりうる。何を落とすかは本人判断待ち。hanger 2.4mm の m=2・m=3 は塊が1つなので通る。
- 設計空間の opening は `opened | (設計空間 & keep)`（保持セルの扱いは topopt-keep-cells.md）。

## 入力検査と既定（optimize_run）

- **既定**: `OptimizeOptions(min_width_mm=None)` で `_resolve_min_width` が**選べるいちばん細い段**(`min_width_cell_range` の下限 m = `ceil(MIN_WIDTH_FLOOR_MM / spacing)` か 2 の大きい方)の `m × spacing` を返す(`MIN_WIDTH_FLOOR_MM` = 2.0 mm。2026-10-09 の変更で `max(2.0 * spacing, 1.0)` から替えた)。戻り値 dict に `"source"`(`"default"` | `"requested"`)があり、**`requested_mm` は `None` にならない**(既定のときも実際に使った値。`None` だと呼び出し側が「未指定=制約なし」と読みうる)。
- 既定は常に `cells >= 2` を返す（m=1 は返らない）。`binary_search.find_best_feasible` 自身の既定（`min_width_cells` 省略時は1=制約なし）は**意図的に変えていない**: 製品の入口 `run()` は必ず `_resolve_min_width` を経由して明示で渡し、内部既定は `topopt_checks/` のスクリプトと `binary_search` 単体テストが使う。
- **`_resolve_min_width(spacing, requested, max_cells=None)`**: `max_cells`(格子の最短辺のセル数。`run()` は `_grid_shape(limits bbox, spacing)` から渡す)を渡すと、上限(上の `MIN_WIDTH_CEILING_RATIO` 倍)を超える値を `OptimizeInputError`(文面に「上限」)で断り、m を上限で頭打ちにする。
- 下限は `MIN_WIDTH_FLOOR_MM`(2.0 mm)以上でいちばん細い m(2 セル未満にはしない)。これ未満の明示値は `OptimizeInputError`。上限は格子の最短辺のセル数の `MIN_WIDTH_CEILING_RATIO`(0.25)倍(切り捨て)で、これを超える値も同じ例外。`resolution_limits()["steps"][i]` には `min_width_ceiling_mm`(= 上限の m × その段の要素サイズ)と `min_width_steps_mm`(下限〜上限の m の値の一覧)が載る。
- **`min_width_steps_mm(spacing, max_cells)`**: 各値を `_resolve_min_width` に渡すとちょうどその m になる。`m*s/s` が丸めで m を超えるとき（0.1×3/0.1 → 4）は1ulpずつ下げてある。段の値をそのまま `m * spacing` にすると、画面が示した段を入力しても m が1つ飛ぶ。
- `optimize_run._search_failure_warning(search_result, min_width=None, ...)`: 最小幅が有効（`cells>1`）のとき、入口で止まった（`input_contradiction` / `boundary_mapping_failed`）なら「絞ったセル数を除いた後の空間に対する検査」、予算などで止まったなら「予算より先に最小幅を疑う」を末尾に添える。
- `topopt_checks/measure_min_width_floor.py --cells 1` は測れない（m=1 は既定からも到達不能で `out["error"]` を返すだけ）。係数 `SEARCH_TARGET_FACTOR = 2.4` と規則 R(m)・S(m) は既定変更では変えていない。

## 画面（web/src/minWidth.ts・OptimizePanel）

- 最小幅は要素サイズごとにサーバーが配る段（`min_width_steps_mm`）から選ぶだけ。表示は「表示した値をそのまま入力して通る向き」に丸める（下限は切り上げ、上限と保証幅は切り捨て。`shownElementSizeRange` / `shownMinWidthMm`）。
- **段の値そのもの（`<option>` の value）は丸めない**。m=2 の段＝下限 `2×spacing` は2桁に丸めると下限割れ（切り下げ）か m=3（切り上げ）になる。クライアントの m は配られた段の値から `stepCells` で取り、段ちょうどの値が `ceil` で1つ上へ跳ねないことを `tests/eval_min_width.mjs` が見る。

## 保証されるものの限界

- **保証されるのは探索したマスクの幅で、書き出す solid の幅ではない。** 合成の角材で平滑化（marching cubes + Taubin）後の中央断面は t=2: 1.814（−9%）、t=3: 3.442、t=4: 4.435。ボクセル STL 書き出し（`topopt_checks/measure_min_width_floor.voxel_surface`）は平滑化を通さないのでこの差は見えない。

## 射影つき peel（段階4）

- `peel(mask, order, count, anchor_ids, *, min_width_cells, exempt, protected)`。**手は「セル x + 巻き添え」の一口**。巻き添えは x を抜いた後に m 立方の被覆を失う材料セル（`min_width.casualties` → `lost_coverage`）。一口を抜いた形は、`exempt` が無ければ `opening(mask − 領域)` に等しい。`exempt` があるときは被覆を失った `exempt` セルが残る（巻き添えにならず、材料としては他のセルの立方に数える）。厚みの不変条件は構成上保たれるので、**厚みを理由に手を却下しない**。
- **`min_width.lost_coverage(mask, region, m, exempt=None) -> (origin, lost)`**: 巻き添えはこれで取る（二重実装しない）。`region`（3軸スライスの組）を丸ごと空にしたと仮定して被覆を失う材料セルの真偽配列 `lost`（`region` の周り距離 m−1 の帯）と、その (0,0,0) の `mask` 添字 `origin`。座標は `np.argwhere(lost) + np.asarray(origin)`。`mask` が既に規則を満たす前提。判定は `region` から距離 2×(m−1) の窓だけで足り、大域を再計算しない。`exempt` のセルは `lost` に入らない。
- `min_width.casualties(mask, cell, m, exempt=None) -> (n,3)` は1セル用の薄い包み。`min_width.cube_opening(mask, m)` は m 立方の opening。`min_width.bite_verdict(padded, bite)` は複数セルの一口の 表面・多様体・連結 の窓判定。戻りは (理由|None, 窓の中で連結と言えたか)。理由は `'surface'`・`'manifold'`・`'connectivity'`。**支えの判定は含まない**（`peel` 側が条件節点の支え数を持つため）。
- `bite_verdict` の座標: `padded` は外周 +1 なので、内側の (i,j,k) は `padded` の (i+1,j+1,k+1)。窓は一口の外接箱 +1 セルで、頂点は `[1:-1]` の内側だけ見る（窓の外周の頂点は窓の外を空と見るので使えない）。
- **`PeelStats`**: `skipped_thickness` は**常に 0**（`> 0` を期待するテスト・記録は成り立たない）。`casualties` が増え `as_dict` にも出る。`stats.picked` は「手の数」で、減ったセル数は `picked + casualties + islands`。`count` の打ち切りは巻き添えも数えるので、最後の一口で要求を最大 (2m−1)³−1 セル超える。
- **支えの数え方**: `peel` は一口の節点IDを重複ありの並びで持ち、却下は `support_flat[n] <= 出現回数`、採用は出現回数ぶん `-= 1`。x 単独の支えの早期却下は残してある（x ⊂ 一口なので x で落ちれば一口も落ちる）。
- 2セル厚の板では、上面のセルも下面のセルも**同じ貫通柱**を一口にする（重複候補の源。回帰テストはこの形）。自由縁のすぐ内側のセルはリブごと（x 方向に2セル×厚み）抜ける。2×2 の棒の途中は断面ごとで、片側に条件節点が無ければ全体の数え直しで却下される。`_reduce_volume` は不合格の形のハッシュを、手の数に関わらず `rejected` に入れる。
- **m=1 では `min_width` の関数も `CoreConnectivity` も一切呼ばない・作らない**（`tests/test_binary_search.py` が `min_width` モジュールを差し替えて、呼んだら落ちるようにしてある）。
- **`binary_search` の中で `min_width` は「モジュール名」**。同名のローカル変数（`optimize_run` の `min_width` の dict など）があるスコープではモジュールを別名で import する。

## 芯の連結 S(m)（CoreConnectivity）

- `CoreConnectivity(m, exempt=None)`（`exempt` は保持セルの真偽配列）。`.groups(mask)`: 芯 K の連結成分（m が4未満は6連結、`CORE_26_MIN_M`＝4 以上は26連結）を保持セルで結んだ塊の数（K が空なら 0）。`.connected(mask)` = 塊 ≤ 1（大域。`exempt` は `mask` に無くても材料に数える）。`.window(changed_lo, changed_hi, shape)` → `(lo, hi, ext)` と `.proven(before, after, lo, hi) -> bool`: 窓の判定。**真 = 「抜く前に S(m) を満たしていれば、抜いた後も満たす」と窓の中で言えた。偽 = 言えなかっただけで破れるとは限らない**（呼び出し側が `.connected` で大域を数え直す）。`.after_bite(inner, bite, island_cells)`（`peel` 用）は、窓で言えなければ大域で数え直すところまで含む。`min_width.core_cells(material, m)`: m 立方が軸に収まらない格子では全部偽。
- 診断: `PeelStats.skipped_core`（`as_dict` に出る。m=1 は常に0）・`diagnostics['min_width']['design_core_groups']`。
- **`peel` の前提: 入力 `mask` が S(m) を満たす**（窓の判定は「抜く前に満たす」を前提にした十分条件）。`_reduce_volume` は段階3の合格候補（大域検査を通ったもの）から始まるので満たす。
- 窓判定 `proven` の健全性（真なら大域でも真）は `tests/test_min_width_core.py` が独立な物差し（`tests/topopt_support.py` の `core_groups_oracle`）と突き合わせて固定している。**`window` の範囲を変えるときはそのテストを通すこと。**
- **`peel` の所要時間**: S(m) の判定は hanger m=2 で `peel` の合計を 38 秒 → 398 秒にした（大半は窓で言えず大域で数え直した手）。所要時間を前提にした見積もり・測定の並べ方をしない。
- 乱数のマスクは `peel` の前提（全頂点が正則・1成分）を満たさない。`peel` を繰り返すテストは全材料の箱から始める（`test_peel_refuses_the_moves_that_would_split_the_core_...`）。R と S は別々に差し替える（`_mask_satisfies_min_width` を実行時に差し替えると `peel` の島の除去の確認〈m≥3・保持あり〉まで差し替わり R のみの側の数字が歪む）。
- 島の除去は保持成分と辺・頂点でも接しないので、`exempt` が無いか m=2 なら島の除去は厚みの不変条件を壊さない。m≥3 では mask に無い `exempt` セルを挟んで跨ぎうるので、島を消した形を大域で確かめ、崩れる手は却下する（`skipped_connectivity`）。

## 測定の基準列と読み方（hanger 2.4mm）

数字の正は [../TOPOPT_THICKNESS.md](../TOPOPT_THICKNESS.md) 節I〜M。測定の生データは git 管理外。

- **規則が違う列を並べない**: 節K（R のみ・削られた保持セルを材料に数えない版）／ K2（`mask ∪ keep` で数える版）／ 節M（S(m) つき）。目標 2.0 の基準列が要る後続は節M の「S(m) つき」の列を基準にし、R のみの列と混ぜない。
- **目標 2.0 の列（予算60）は目標に届かず止まった形**（S 付き m=2 は 2.15、m=3 は 2.31）。予算を伸ばした形は測っていない。
- 2.4mm の要素サイズ要求は経路で実 spacing が違う（節H2: 2.4063mm、`_snap_to_grid` 経路: 2.387506mm）。格子が違う列を直接比較しない。
- 数字を引用するときは、その数字がどの条件（列見出し・m・規則の版）の測定かまで確認する。`TOPOPT_THICKNESS.md` 節Mの「38秒」「398秒」は**どちらも m=2**（R のみ → S を足す）の比較で、m=1 と既定（m≥2）の比較ではない。
