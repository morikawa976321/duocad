# 形状最適化の体積目標・応力・安全率・縮小検証の契約

形状最適化の「体積目標(絶対量 mm³)」「von Mises 応力と代表応力」「安全率の外側ループ(材料量の探索)」「受入検査の題材の
縮小」の契約集。**いまの挙動の正はコードとテスト。** ここは、ソースから読めない不変条件・依存禁止・罠を、元の作業タスクの
契約から分野別に蒸留したもの。実測の表と設計の説明は [../TOPOPT.md](../TOPOPT.md)(「von Mises応力と最小安全率」
「SIMP時代の記録」ほか)と [../PLAN_BINARY_STRESS_SEARCH.md](../PLAN_BINARY_STRESS_SEARCH.md)が持つので、重複は書かない。

出典は項目末尾の括弧 `(タスクID, YYYY-MM-DD)`。`(対象コード不在・要確認)` は、契約が名指す関数・ファイルが現在のソースに
見つからないもの。2026-09-17 の段階6で `simp.py`(OC/MMA・密度フィルタ)・`safety_loop.py`(材料量の二分/Illinois 探索)・
`bench.py`・`perf.py`・`anchor.run_pipeline`・`design_space.run_topopt` が削除され、探索は二値マスクの `binary_search.py`
(材料量は探索変数でなく結果)に置き換わった。塊の導出・連結性・bbox は [topopt-design-space.md](topopt-design-space.md)。

## 体積目標の絶対量指定(旧 simp / run_pipeline / run_topopt。削除済み)

体積の口は3層とも `volfrac` と `material_mm3` の**排他**だった。現行の探索は材料量を探索変数にしない
([../TOPOPT.md](../TOPOPT.md)「現在の呼び出し経路」)ので、口そのものは無い。後続が体積を絶対量で扱う設計を作るときの
不変条件と罠として残す。

- **絶対量は「材料の総体積」で、固定(solid)要素のぶんを含む。** 達成できるのは `[solid の体積, (solid + 動かせる要素) の体積]` の
  閉区間だけで、外は例外。「動かせる要素ぶんだけの量」ではないので、安全率から量を作る側はこの定義に合わせる。
  (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04) (対象コード不在・要確認: `simp.optimize(material_mm3=...)`, `simp.VolumeTargetError`, `simp.check_volume_target`)
- **質量(グラム)を内部表現にしない。** 密度を掛けるのは表示の仕事(本人の 2026-09-04 決定)。 (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04)
- 要素体積は上位の層が `model.grid.element_volume` から自分で決める。自分で「mm³ → 要素数」を書かない(格子の作り方が変わったときに
  黙ってずれる)。`derive(spacing=4.0)` を渡しても要素体積は余白比率ごとに変わる(62.99 → 66.43 mm³。格子は箱に合わせて切り直される)。
  「spacing を固定したから要素体積も同じ」と思って計算しない。 (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04)
- 達成不能な指定は**黙って端に張り付かない**(想定は実測で外れた)。ガードを外すと、総体積超過は OC の二分探索が退化して
  `ZeroDivisionError`、下限割れは `K U = F が解けていない` になる。ガードの値打ちは落ちることでなく「どちら側にどれだけ足りないかを
  名指しする」こと。上端ちょうど(容量と等しい絶対量、`volfrac=1.0` も)・下端ちょうども OC が退化して `ZeroDivisionError`(容量の 99.9% は通る)。
  (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04)
- OC の二分探索の退化は後に `VolumeTargetError(side="above_capacity")` で落とすようにした(文面に目標・この反復で到達できる最大・塊の容量・
  その割合)。塊の縁が多い配置では**容量の 98% で落ち 95% で通る**(フィルタ後の到達上限 96.8%)ので、「区間の内側なら通る」に
  依存せず、境目は形で決まるので定数にしない。ガードは健全経路の計算を1ビットも変えない(`l1 + l2 > 0.0` は退化していない限り常に真)。
  (20260904-1416-topopt-adopt-coarse-run-as-default, 2026-09-04)
- 引数の既定値を値にすると「両方渡した」を検出できないので、排他の口は既定を `None` にする(`volfrac: float = 0.3` では判定できない)。
  `Result.volfrac` が入力の写しであることに依存しない(絶対量経路では逆算値)。 (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04)
- 分率の口を消すと既存の受入検査(当時5本)がすべて落ちた。「既定の挙動はビット単位で固定」の証拠として、プリセットとパッシブ要素経路の ρ 場の
  SHA-256 を検査に持ち、数値に触るなら基準値を取り直す前になぜ変わるかを説明できること。 (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04)
- 変異検査の罠: 例外を見る検査は `except Exception` まで受けること。狙った例外だけ捕まえると、変異体がトレースバックで死ぬだけになり
  どの検査が守っているか出力に残らない。「絶対量→実効分率の逆算から solid を落とす」誤りは最終的な材料の量を変えず(目標体積は要素数で同じ)
  初期値と `Result.volfrac` だけを動かすので、**実効分率そのものを assert しないと素通り**する。 (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04)
- 体積目標のうち画面側は当時まだ分率(`DEMO_VOLFRAC=0.3`)で回っていた。画面を絶対量へ倒すと `shot.mjs` の期待値と `screenshots/` の撮り直しが付いてくる。
  (20260904-0142-topopt-volume-target-in-absolute-units, 2026-09-04) (対象コード不在・要確認: `server.py` のデモ)

## von Mises 応力と代表応力(duocad/topopt/stress.py)

実測の表(斜め板の細分3段)・留保・許容応力の導出は [../TOPOPT.md](../TOPOPT.md)「代表応力の規則」。ここは外せない契約。

- **代表応力は素の max でない。** 既定は ρ >= 0.5 の要素の von Mises の 99.5 パーセンタイル(`DEFAULT_RHO_MIN` / `DEFAULT_PERCENTILE`)。
  素の max を使うと解像度を上げるほど安全率が下がり続ける(斜めに切った板を3段細分: 素の max は公称の 12.9 → 39.7 倍、ρ>=0.5 の max も
  1.54 → 2.09 倍、99.5% 点だけが 1.32 / 1.29 / 1.30 で動かない)。この規則を「せっかくだから max で」と戻さない。
  現行の製品の探索は部材域の固定物理体積の上位値(`member_volume_stress`)を使い、99.5% 規則は `member_volume_mm3` を渡さない研究・検査の経路に残る。
  (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04)
- **ρ の閾値が効くのは「採用する代表値」のほうで、max を下げるためではない。** 斜め板では空要素が材料に隣接して素の max が 9.8〜30.5 倍に跳ねるが、
  プリセットの SIMP 出力では素の max は ρ=1 の荷重直下(点荷重の特異点)にあり ρ の閾値では動かなかった(閾値を外すと代表値が 1.346 倍に上がる)。
  「閾値は max を下げるため」と読むと閾値は要らないという誤った結論になる。 (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04)
- 99.5% と ρ>=0.5 は実測で選んだ値で理論値ではない。母集団の要素数が少ないと 99.5% 点は max に近づく(596 要素なら上位3要素)。解像度を跨いで安全率を
  比べるなら `RepresentativeStress.element_count` も一緒に見る。 (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04)
- **応力は E に依存しない。** `sigma_real = sigma_nd * (F/F_nd) / L²`。E が効くのは変位だけ(`u_real = u_nd * (F/F_nd) * (E_nd/E) / L`)なので、
  安全率は E を変えても動かない。「材料を硬いものに変えたら安全率が上がる」を探索の外側で期待しない(`material.DEFAULT_YOUNGS_MODULUS_MPA=2000` は変位表示専用の名目値)。
  無次元側の `E=1.0` / `nu=0.3` / 荷重 1.0 を実材料の値へ変えない(換算は後処理側の責任で、コンプライアンスの基準値が既存の受入検査の回帰基準になっている)。
  (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04; 20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- 換算則 `UnitScale` は解いた `Model` から無次元側の荷重を読んで作る。手で `solved_load` を書くと、荷重ベクトルに `force` を渡した系で応力が黙って
  定数倍ずれる。荷重 0 の系は例外。現行の検査用口 `UnitScale.for_model` の分母は合力でなく `load_magnitude_total`(節点力の大きさの総和。打ち消し合う荷重系でも 0 にならず、
  荷重が1つなら合力と厳密に一致)。製品は荷重を実単位[N]のまま解くので `UnitScale(..., solved_load=1.0)` を直接作る。
  (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04) (要確認: 旧契約は合力 `stress.load_resultant` を分母にしていた。現行は `fem.load_resultant` と `load_magnitude_total`)
- **母集団が空(どの要素も ρ < 閾値)なら例外。** 0 を返すと安全率が無限大になり「材料が1つも無い形」が最も安全になる。母集団に非有限値が混ざった場合も落とす
  (nan は比較が常に偽で、不合格判定にも引っかからない)。 (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04)
- **要素応力は材料そのものの応力**(D は `model.E` そのまま。`rho^penal` を掛けない)。中間密度の要素はひずみが大きく出るぶん安全率を小さい側へ倒す(安全側)。
  (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04)
- **安全率の絶対値を信用しない。** ボクセルFEAで階段の問題は消えない。使ってよいのは同じ解像度での相対比較と「明らかに足りない/足りている」の判定だけ。
  縮めた解像度での実測が本番の数値になるとも限らない(要素サイズが変われば安全率も変わる)。 (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04; 20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- `stress.py` は最適化の中を見ない純粋な後処理(感度も履歴も使わない)。二分探索の途中反復で安全率を覗くと各反復ぶんの solve を足すことになる。変位 `u` は
  最終 ρ で解き直して得る(`Result` に `u` を持たせると ρ を差し替えた瞬間に古くなる)。現行の `evaluate` は `u` が必須引数。 (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04)
  (対象コード不在・要確認: 旧 `simp.optimize(keep_model=True)` / `Result.model`, `evaluate(u=None)` の自動 solve)
- 検査の罠: ① 全自由度を固定した面の近傍では梁理論が成り立たない(根元の要素は M·c/I を 8.5% 上回る)。解析解と比べる断面は**梁せい1つぶん先**に取る。
  ② 単軸の解析解だけでは von Mises の定義を守れない(せん断項の係数 3 → 1 の変異は単軸状態では姿を現さない)ので、純せん断と静水圧の2件を定義の検査として別に置く。
  ③ `rho_min` を 1.0 近くにしても母集団は空にならない(OC が `x` を [0,1] にクリップするので ρ がきっかり 1.0 の要素が残る)。空の場合を検査するなら ρ を全部 0 にした合成場を渡す。
  ④ 要素中心の B 行列は、局所節点順が `_build_edof` の回し方と一致していないと落ちずに「もっともらしい別の値」を返す。格子全体に6成分すべて非零の一様ひずみ場を入れて
  全要素が同じ σ になるかを見る検査を置く(変異で12件落ちる)。 ⑤ 空要素のある斜め板の 72×48×6 は CG を使わず `solver="direct"`(通し 25 秒のうち 15.5 秒)。
  (20260904-0143-topopt-von-mises-and-safety-factor, 2026-09-04)
- 許容応力の定義は `material.py`(`PETG_YIELD_MPA=50.0` × `PRINT_DERATE=0.8` = `DEFAULT_ALLOWABLE_MPA=40.0`)に1か所。数字だけをハードコードしない。
  (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)

## 安全率の外側ループ(材料量の二分探索 → Illinois。safety_loop.py、削除済み)

安全率から材料量を決める外側ループは段階6で二値探索に置き換わり、コードごと無い。後続が「安全率目標 → 材料量」を再び組むときの知見として残す。

- 契約は「目標以上を満たす**最小**の材料量」(ちょうど、ではない)。`rel_tol`(材料量の相対幅)は最終的に 0.08 で、**返る材料量は真の最小より最大 8% 重い**。
  この設定で出た材料量を「最小材料の解」として扱わない。`rel_tol` だけでは収束の質を保証できない場面もあり(plate は材料量 1.5 倍でも安全率が 1% 動かない帯があった)、
  疑わしい題材では `history` の安全率側の変化幅も見る。 (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04; 20260909-0518-topopt-void-removal-to-main, 2026-09-09) (対象コード不在・要確認: `safety_loop.DEFAULT_REL_TOL`, `solve_for_safety_factor`)
- **単調性は理論的な保証ではない。** 4題材×10点で実測して確かめたが、任意の形で単調とは限らない。(a) 候補材料量ごとに塊を導出し直す案と (c) 塊を固定する案は
  完全には一致せず(最大 4.2% の差)、二分探索の途中経過の安全率を他の手法の結果とそのまま比べない。 (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- `material_min_mm3` / `material_max_mm3` は呼び出し側が決める(形ごとの「常識的な材料量」を探索は知らない)。届かないとき `SafetyTargetError`
  (`unreachable_within_max_material`: 上限まで使っても届かない / `invalid_bound`: 上下限そのものが導出できない)。 (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- **探索の途中で STL を共有パスへ書かない。** `run_topopt` / `run_pipeline` は `stl_path` 省略時に既定の共有パス(`design_space_case1.stl` / `bracket.stl`)へ書き、
  他の受入検査が当てにしているファイルを検証のたびに上書きする。繰り返し呼ぶループは scratch の出力先を明示する(採用した材料量の STL が要るなら、その材料量でもう一度呼ぶ)。
  (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- **解像度の既定は本番相当(長辺 40 要素)で重い。** 検証・実験で縮め忘れると 1 回の探索が数分〜(二分探索の回数倍)に伸び、「ハングしたように見える」(例外もメッセージも出ない。
  長辺 40 のままの検査は 2 分でも終わらなかった)。実行前に解像度の引数を確認する癖をつける。極端なアスペクト比の形(plate)では既定の膨張量が効かず、材料量の低い側で
  `DesignSpaceDisconnectedError` を踏んだ。 (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- **Illinois 補間にしても評価回数は減らなかった**(bracket、括り [52,786, 246,337]mm³・長辺 20・20 反復・`rel_tol` 0.08 の4目標で二分 27 → 補間 28 回。目標 sf 3,500/4,500/5,500/5,900 の
  採用材料量は二分 70,932/107,223/197,949/210,046 → 補間 69,869/105,492/192,627/207,487 mm³ で、**採用材料量が常に 1.2〜2.7% 軽くなった**だけ)。時間見積り
  (`TYPICAL_EVALUATIONS=6`)は据え置いた。フォールバックは 8 ケース 55 評価で 0 回、クリップは補間側で6回。 (20260912-1720-topopt-safety-search-illinois, 2026-09-12) (対象コード不在・要確認: `safety_loop.solve_for_safety_factor(searcher=...)`, `_propose_material`, `DEFAULT_SEARCHER`)
- 機序: **安全率は材料量に対して凹**(傾きが 22 倍変わる)。割線は常に曲線の下を通るので、補間は材料の多い側から片側だけ括りを詰める。`log(m)` 軸なら傾きの振れが
  22 倍 → 6.1 倍に縮む(未実装)。「上端 sf が目標の数百〜数千倍だと二分が無駄」という動機は bracket では成立しない(下端の sf が 1,832 あり、目標がそれ以下なら下端の評価で終わる)。
  無駄の指標は `sf(上端)/目標` でなく **`sf(上端)/sf(下端)`**(bracket で 3.3 倍)。クリップ 0.15/0.85 は経験値で根拠が無く、調整するなら測り直す。
  (20260912-1720-topopt-safety-search-illinois, 2026-09-12)
- **安全率を返さない評価(`too_little` / `too_much`)では `f_lo` / `f_hi` を `None` に落とす。** 括りだけ更新して安全率を返さないので、古い値で補間すると提案が括りの外へ飛ぶ。
  分類の名前が同じでも、この配線を外すと静かに壊れる(`too_much` の作られ方を OC のラグランジュ乗数の退化から別の方式へ変えるときが危ない)。`_evaluate` の3分類(`ok` / `too_little` /
  `too_much`)と括りの更新規則は探索器に依らない。補間の契約は `f_lo < 0 <= f_hi`(`f = 安全率 - 目標`)。フォールバック4値は `fallback_reason` に載る。
  (20260912-1720-topopt-safety-search-illinois, 2026-09-12)
- 検査の副作用: 検査を1本走らせると追跡下の `duocad/topopt/output/design_space_reference.stl` が書き換わる(コミットしない)。ログをファイルへ落とすときは
  `PYTHONIOENCODING=utf-8` を付けないと cp932 で化ける。 (20260912-1720-topopt-safety-search-illinois, 2026-09-12)

## 受入検査の題材の縮小(旧 bench.py)

`bench.py` は削除済み(現行の題材の大きさは [../TOPOPT.md](../TOPOPT.md)「受入検査の題材の大きさ」)。縮小で分かった、解像度を触る後続が踏む罠だけ残す。

- **`voxelize.occupancy` のレイ縮退は解像度に対して単調でない。** 旧 `CASE1_LONG_AXIS` は 12 と 16 で落ち、13/14/15/17/18/20/22 は通る。要素サイズは 9.0mm 以上で落ち、
  6.0〜8.5mm は通った。**1点で試して通ったら採る、をやらない**(複数点を測る)。 (20260904-1345-topopt-shrink-verification-loop, 2026-09-04)
- 縮めると検出力が消える4か所があった(メッシュ化の材料・円筒面の標本数 n=32・応力検査の題材・プリセットの反復数 60)。縮小の可否は「その検査が何を検出するか」で決め、
  所要時間だけで決めない。反復数は終盤 10 反復の窓(`TAIL_WINDOW=10`)より小さくしない。片持ち梁プリセットの `nelz` は偶数(奇数だと荷重が2節点へ割れ z 対称性の検査が別のものを見る)。
  (20260904-1345-topopt-shrink-verification-loop, 2026-09-04) (対象コード不在・要確認: `bench.py`, `simp.PRESET`)
- **`simp.py` から `bench.py` を import しない**(`simp.py` を単体でアプリ本体へ移せる状態に保つ境界)。回帰基準の固定値(SHA-256・16 進 float)は題材を変えたら変えた側だけ、2回走らせて
  一致を確認してから取り直す。 (20260904-1345-topopt-shrink-verification-loop, 2026-09-04) (対象コード不在・要確認: `check_absolute_volume.py` の `BASELINES`)
- 秒数に依存しない: 同じコード・同じ機械で 1.7 倍振れる(熱状態)。**新旧の比較は同じセッションで測る**(`perf.py --derivation bbox` → `--baseline` の形)。 (20260904-1345-topopt-shrink-verification-loop, 2026-09-04; 20260904-1416-topopt-adopt-coarse-run-as-default, 2026-09-04)
- 旧ブラケット題材で `LONG_AXIS` を 14 まで落とすと拘束面と荷重面が同じ節点を掴み、モデルが「荷重が自由な自由度に1つも載っていない」で落ちた(18 にした)。
  条件面を要素サイズに近づけると同じ病理(節点の共有・空の面)が出る。 (20260904-1345-topopt-shrink-verification-loop, 2026-09-04)
- 検査内のメモ(`check_anchor._table`)のキーは `id(shape)` なので、形をその場で構築して渡すとキャッシュが効かない(モジュール定数を使う)。 (20260904-1345-topopt-shrink-verification-loop, 2026-09-04)
- 未コミットの `simp.py` の変更が作業中に消えたことがあった(原因未特定)。まとまったら即コミットし、報告の前にもう一度通しを回して数字を目で見る(終了コードだけでは題材の入れ替わりに気づけない)。
  変更前から `check_anchor.py` は `RuntimeWarning: CG が収束しませんでした (info=5240)。直接解法へフォールバックします` を1回出す(合否に無関係・原因未調査)。
  (20260904-1345-topopt-shrink-verification-loop, 2026-09-04)
- `git status` が `stress.py` を modified と出すが `git diff` は空、という表示だけの変更が再現した(触らない・ステージに入れない)。Bash ヒアドキュメントで書いた Python の `"\\n"` が実際の改行に
  なる現象もあったので、パッチ用の文字列に改行のエスケープを書かず、行単位の操作か Write ツールを使う。変異検査のドライバの `subprocess.run(..., text=True)` は Windows で cp932 デコードになり
  日本語出力で `UnicodeDecodeError` を出してスレッドごと死ぬので `encoding="utf-8", errors="replace"` を明示する。
  (20260904-1416-topopt-adopt-coarse-run-as-default, 2026-09-04; 20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04)
- 検査件数(284 → 303 → 426 → 456 → 471 → 502 → 536 件)の進行は作業日ごとの値で、現在値の単一の正は当時 `probe_topopt/readme.md` §3 にあった(件数を文書の複数箇所に書かない)。現行のリポジトリ直下の `README.md` に検査件数の記述は見当たらない(対象コード不在・要確認)。「減らさないこと」が各契約の共通の不変条件。
  (20260904-1345-topopt-shrink-verification-loop, 2026-09-04; 20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04; 20260905-0510-topopt-shrink-docs, 2026-09-05)
