# 形状探索(削除・追加・置換)の時間短縮

**この文書の役割:** `binary_search.find_best_feasible`(削除・追加・置換によるbool体積削減
探索)と、その前後の `optimize_run.run()` における**時間の内訳**をまとめたもの。
実測の正は引き続き [TOPOPT.md](TOPOPT.md) とコードの docstring。

**2026-09-17(計画書 `PLAN_BINARY_STRESS_SEARCH.md` の段階6)に、この文書が扱っていた
`safety_loop.solve_for_safety_factor`(安全率目標から材料量[mm³]を二分/Illinois補間で探す
外側ループ)をコードごと削除した。** 材料量の二分探索アルゴリズム比較・単調性の実測・
Regula Falsi/Illiniosの提案式・改善案B0〜B7は、いずれも材料量を直接の探索変数にする
アーキテクチャ固有のものだったため、削除した(旧内容は git 履歴に残る)。
**現在の探索(bool マスクの削除・追加・置換)には対応する速度分析がまだ無い**
——`topopt_checks/measure_binary_search.py`(評価回数・最良体積の履歴を計測する。
2026-09-17時点で未実機検証)を実測に使ってから、この文書を書き足すこと。

以下は削除前から引き継いだ、**探索アルゴリズムに依らず今も有効な事実**(1回のFEM評価の
費用の内訳)である。

---

## 1回のFEM評価(assemble + multigrid CG)の内訳

削除・追加・置換探索は候補ごとに1回、固定した格子上でFEM(`fem.py`)を解く。組立・
multigrid setup・CG/V-cycleの内訳は、SIMP時代に測った下記の実測が**アーキテクチャとして
今も同じ**(fem.pyの組立・CG・multigridの実装自体は段階6でも変えていない)。

**環境:** hanger(`hanger_bar_attachment.json`)・要素サイズ2.4mm・格子78×138×109 =
1,173,276箱要素(点群の外接箱+余白の外枠。固定凸包でも格子の外枠は同じ)・
`solver="mg"`・`cg_rtol=1e-9`。案1の全塊(active 1,096,894要素。2026-09-09実測)での
`collect_solve_stats`によるsolve 1回あたりの内訳:

| 項目 | 秒/solve | 比率(solve内) |
|---|---|---|
| `assemble`(K 組立) | 48.1 | 16% |
| `slice`(自由 DOF 切出) | 3.2 | 1% |
| **`mg_setup`(RAP + 最粗 LU)** | **24.3** | **8%** |
| **CG / V-cycle** | **242.0** | **75%** |
| CG 反復数 | 161〜306(平均 234) | 密度が二値に寄ると増える |

**塊(active要素)を7.6倍にすると1solveの秒数は2.7倍**(線形には伸びない)。理由は2つ:
格子サイズ(組立・フィルタの対象)は材料量と無関係に常に全箱要素で、密度フィルタ・組立は
塊が小さくても格子全体を走査する。加えて自由度の桁も縮まない(塊が小さくても`extra_fixed_dofs`
で拘束に回るだけで、自由DOFの桁は縮まらない)。**`mg_setup`は密度が変わるたびに毎回組み直しが
必要で、並列化できない。**

上記のうち、SIMP時代固有だった「体積制約OCの二分探索が`_physical()`を繰り返す」内訳
(反復あたり数秒〜数十秒)は、**現在の候補評価には無い**(1候補=1FEM解であって、
内側にOC/MMAの反復は無い)。したがって1候補あたりの費用は概ね上表のassemble+mg_setup+CGの
合計に近いはずだが、**未実測**。

---

## 関連コード索引

| ファイル | 内容 |
|---|---|
| `duocad/topopt/fem.py` | `Model.solve`・`collect_solve_stats`・組立・`mg_setup`・CG/V-cycle |
| `duocad/topopt/binary_search.py` | `find_initial_feasible`・`find_best_feasible`・候補生成 |
| `duocad/optimize_run.py` | `run()`・`MAX_ELEMENTS` |
| `topopt_checks/measure_binary_search.py` | 二値探索の評価回数・最良体積の履歴の計測(未実機検証) |
