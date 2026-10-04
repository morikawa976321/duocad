# 段階5: 合成題材の比較・機構別検証

2026-09-29。`docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`「0〜2レビュー後の訂正と実行順序」節の
`20260928-1426-topopt-idle-05-synthetic`。着手前に `taskbox/done/20260928-1426-topopt-idle-04a-path-qualification.md`
「## 後続への契約」・`04A_PATH_QUALIFICATION.md`・`REVIEW_00_02_2026-09-28.md` を読んだ。

## 結論(先に要約)

1. **4方式(20%削除・5%削除・適応歩幅+復帰・再配置)は、dilution_small・path_small(04a認定条件)を通じて
   明確に区別できた**(「低品質で区別できない」場合の追加方式は不要と判断——3節「全方式が低品質で
   区別できない場合」参照)。**方向は条件ごとに入れ替わる**: dilutionはbaseline(300N/2.0mm/6mm)・
   幅8mm・解像度1.5mmでは適応/再配置が一貫して軽い(最大35%軽量)一方、**200N・250N(荷重を下げた
   条件)では逆に横ばい〜わずかに重く、解像度3.0mmでは旧方式の約2.7倍重い**形状で止まる。pathでも
   同様に条件依存で、3300N・解像度1.5mmでは約1.5〜1.8倍重い。「新方式の性能優位」は一般には主張できない
   (04_SEARCHES.mdの立場を、より広い条件で再確認した)。
2. **新発見: 適応方式の一次予測係数(`calibrated_k`)が最初に受理された1手だけから較正され、
   以後固定される実装は、粗い格子・小さい候補セル数の条件で予測誤差ロールバックを暴走させる。**
   dilution 3.0mm・path 1.5mmでは、序盤の大きな1手で校正した`k`が後続の小さい候補すべてを
   `prediction_error`で拒否し続け(実際のジャンプは0.0004%〜1%未満の些少な値なのに、予測値がそれより
   さらに小さいため比率だけが閾値を超える)、`revert_restart`を経ても同じ点に留まり続けて
   `finish_stagnation`/`evaluation_limit`まで動けなくなる(4節)。これは**hangerへ適応方式を持ち込む前に
   直すべき実装上の弱点**である(9節)。
3. **`AdaptiveConfig.gate="m5b_raw"`(既定)だけでは製品の目標安全率(パーセンタイル4.8)を守れない。**
   dilution 250N・path各warm-start条件で、m5b_rawゲートに合格したまま安全率が3.85〜4.56まで沈む形状が
   複数実測された(5・8節)。**hangerで使うなら`m5b_raw`単独ではなく`m5b_target`または`percentile`との
   併用が要る**(9節)。
4. **アブレーションで「復帰」の効果はモデル依存**: dilution(母数希釈代表)ではrollback/revert_restartが
   既定パラメータで一度も発動せず、「適応歩幅のみ」と「復帰あり」が完全に同じ結果になった。
   path(経路喪失代表)では復帰の有無で材料が49,224→67,144(+36.4%)・安全率が4.15→4.74まで変わり、
   復帰が実際に安全側へ効いた(5節)。
5. **再配置(方式4)の「追加」は、全main_matrix・全ablation条件を通じてほぼ例外なく初期全材料形へ
   一時的に戻ってから削減を再開する**(51件の`grow_accepted`イベント全てで、追加候補は
   `initial_all_material`由来の1連結成分=設計空間のほぼ全体だった)。これは04_SEARCHES.mdが
   dilution/path各1条件で示した挙動が、荷重・幅・解像度を変えた全条件で一貫することを確認した——
   `_connected_remnants`の実装(部材単位を分割しない)が、単純な箱型形状では常に「設計空間の残り
   ほぼ全体」を最長候補として選んでしまうための構造的な傾向であり、**現在の再配置は「局所的な部材の
   復元」ではなく実質的に「全材料形からの再削減」に近い**(6節)。
6. **格子原点の半セル移動だけで材料が最大50.8%変動し(dilution +0.5z/-0.5z)、8条件全てで位相
   (mask_hash)が基準から変わった。** 原点は製品の第一級パラメータではないため、hangerでも同じ規模の
   ばらつきが起こりうる(7節)。
7. **パラメータ感度**: dilution(300N/2.0mm)は3パラメータいずれにも70評価予算の範囲で無感度
   (rollback/revert未発動のため)。path(3000N/2.0mm)は`prediction_error_multiplier`に感度あり
   (相対体積スプレッド14.4%、本タスクが置いた10%閾値を超過)。既定値を疑う実測上の破綻は出なかったため、
   **選定値は既定値のまま据え置く**(6節)。
8. **warm-start(初期形状)は経路喪失からの回復を保証しない**: 04aの優越形(percentileシナリオで認定
   済み・SF4.800)から`run_adaptive_from_mask`(m5b_rawゲート)を60評価続けると、SF4.562まで悪化した
   形状に動く(8節)——③の「m5b_rawだけでは目標安全率を守れない」が、既に安全な形状から出発しても
   再現する。

## スコープの決定(先に述べる。本タスクの実験計画全体を規定する)

タスクファイル(`taskbox/running/20260928-1426-topopt-idle-05-synthetic.md`)自身が、本文の
「## 小型題材への変更」節の見出しに「**今回の追記が下記の旧行列指示に優先**」と明記している。
「## 指示・詳細」節は文字どおりには「第1タスクの全合成題材・荷重ケース」(=`01_PROTOCOL.md` §5が
定義したS-T/S-B/S-TB/S-L/S-MSの5題材)での4方式比較を要求しているが、この優先指定により
「小型題材への変更」節の内容が実際の実行計画を決める。

共通指示書(`TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`)「小型題材を主にする変更」節は
「**先行工程0で認定したモデルを主題材とし、広い条件行列・パラメータ調整は小型で行う**」とする。
「認定したモデル」とは `docs/topopt_idle_validation/00_COMPACT_MODELS.md` が作った
`topopt_idle_dilution_small.json`(原因1・母数希釈)・`topopt_idle_path_small.json`(原因2・経路喪失。
04a追記により**2.0mm格子・3000N・percentile評価シナリオに限り認定**)であり、01_PROTOCOL §5の
S-T等5題材ではない。実際、S-T/S-Bはtask2/3(`analytic_checks.py`の`run_tension`/`run_bending`)で
**解析解比較のパッチテスト専用**に使われており、設計空間を持つtopopt探索モデルとしては
(01_PROTOCOL §5執筆時点でも本タスク時点でも)構築されていない。S-TB/S-L/S-MSは寸法・荷重すら
未確定(01_PROTOCOL §11「S-TB/S-L/S-MSの具体的な寸法・荷重は本タスクでは決定していない」)。

**したがって本タスクは、dilution_small・path_small(04a認定条件)を主題材として、荷重・最小幅・
解像度・初期形状を振った広い条件行列とアブレーション・パラメータ感度・機構履歴分析・格子/原点
ロバスト性を行う。S-T/S-B/S-TB/S-L/S-MSは本タスクでは新規構築しない。** この判断が要求を狭めすぎて
いないかは、以下の理由で妥当と判断した:
1. タスクファイル自身が明示的に優先順位を宣言している(推測ではなく本文の指示)。
2. 5題材の新規構築(設計空間・境界条件・保持/接続/一般部ゾーンの実装・荷重の実測選定)は、
   dilution_small/path_smallと同等以上の準備工程を要し、「小型題材を主にする変更」の精神(小型モデルの
   反復開発・パラメータ調整を主軸にする)から外れる。
3. hanger実行はtask6/7が担当する(task4のH1/H2で外部確認済み)。

**この判断の妥当性はtask10のcoverage監査に引き継ぐ**(10節「制約」に明記)。task10が「5題材の実装が
結論を左右する未解決」と判定した場合、必要な範囲で追加実装することになる。

## 実験の全体構成

全実験は `topopt_checks/idle_validation/search_methods.py`(タスク4の実装、03bが凍結した評価器
M5b/M3/M7bをそのまま使う)を再利用する。製品コード(`duocad/topopt/*`)は変更しない。新設した
実験専用コードは `topopt_checks/idle_validation/synthetic_matrix_support.py`(条件表・アブレーション用
config・原点シフト/フィルタ幅mm固定のprocess内一時パッチ・best-so-far曲線抽出)。

| # | 実験 | スクリプト | 出力 |
|---|---|---|---|
| A | 主要行列(4方式×荷重/幅/解像度) | `run_05_main_matrix.py` | `05_main_matrix.json`(15条件×4方式) |
| B | アブレーション(適応歩幅のみ/復帰/再配置、停止条件×ゲート2x2) | `run_05_ablation.py` | `05_ablation.json` |
| C | パラメータ感度(調整用→未使用検証) | `run_05_sensitivity.py` | `05_sensitivity.json` |
| D | 機構履歴・失敗モード分析 | `analyze_05_mechanism_history.py` | `05_mechanism_history.json` |
| E | 格子/原点ロバスト性・フィルタ幅mm固定 | `run_05_grid_origin.py` | `05_grid_origin.json` |
| F | 初期形状(warm-start) | `run_05_initshape.py` | `05_initshape.json` |

主題材の条件: **dilution_small**(`models/topopt_idle_dilution_small.json`、300N基準・2.0mm基準・
最小幅4.0mm基準)、**path_small**(`models/topopt_idle_path_small.json`、04a認定条件=3000N・2.0mm・
最小幅6.0mm、**percentile評価シナリオに限る**——m5b_targetでは現行・優越形とも不合格のまま未決着。
04a §後続への契約参照)。

## 1. 主要行列: dilution_small(原因1・母数希釈)

7条件(baseline幅6mm・荷重2条件・幅1条件・解像度3条件)×4方式。荷重baseline=300N・解像度baseline=2.0mm。
**下表のbaseline行(幅6mm)は新規に実行した結果であり、task4のデータを転記したものではない**(条件表・
コード上は`dilution_base_w6`)。task4の`04_searches_compact.json`にある300N/2.0mm/**4.0mm**幅の結果
(既に実測済み)は引用のみで再実行していない——本行列の主軸は「baseline幅6mmを基準にした荷重・幅・
解像度の1軸ずつのスイープ」であり、4mm幅の条件はアブレーション(5節)側のbaselineとして別途使う
(5節はtask4と同じ4mm幅、本節は新規の6mm幅——**幅が違う2つの「baseline」が並存する**ので混同しない
こと)。評価予算は4方式で揃えていない(adaptiveは100評価、reallocation内側は60評価×grow_cycles=3
——再配置は削減フェーズを複数回内包するため総FEM回数は自然に大きくなる。比較は材料量・安全率・
機構ログを対にして読むこと、評価回数だけを公平性の基準にしない)。

| 条件 | 要素数 | step20 | step05 | adaptive(m5b_raw) | reallocation |
|---|---:|---|---|---|---|
| baseline(300N/2.0mm/6mm) | 19,672 | 88,376 mm³・SF4.837 | 88,520・4.846 | **57,768・4.713**(-34.6%) | **70,952・4.768**(-19.7%) |
| 200N | 19,672 | 16,128・4.845 | 16,056・4.838 | 16,760・4.925(+3.9%) | 16,816・4.889(+4.3%) |
| 250N | 19,672 | 20,456・4.844 | 19,664・4.824 | **15,504・3.850**(-24.2%、**目標未達**) | 18,032・4.660(-11.9%) |
| 幅8mm | 19,672 | 88,392・4.836 | 88,408・4.836 | 57,024・4.675(-35.5%) | 70,392・4.762(-20.4%) |
| 解像度1.5mm | 46,269 | 78,956・4.841 | 78,839・4.838 | 52,669・4.623(-33.3%) | 64,664・4.732(-18.1%) |
| 解像度2.5mm | 9,936 | 79,270・4.829 | 79,238・4.830 | 78,397・4.824(-1.1%) | 76,287・4.819(-3.8%) |
| **解像度3.0mm** | 6,132 | 23,440・4.837 | 24,854・4.812 | **63,013・5.716**(**+168.8%**) | **62,882・5.716**(**+168.3%**) |

(カッコ内%はstep20比。太字は「新方式優位」の前提が崩れる条件。)

**方向の反転(幅・解像度軸)**: baseline(300N/2.0mm/6mm)・解像度1.5mm・幅8mmでは適応/再配置がstep20より
一貫して軽い(-18〜-36%)。**解像度2.5mmではほぼ差が無くなり、3.0mmでは適応/再配置がstep20の約2.7倍重い
形状で止まる**——4節で原因(`calibrated_k`の単点校正が粗い格子で暴走する)を実測で特定した。

**方向の反転(荷重軸)**: baselineと同じ2.0mm解像度でも、荷重を下げた200N・250Nでは方向が変わる。
200Nでは適応/再配置がstep20よりわずかに**重い**(+3.9%/+4.3%)。**250Nの安全率未達**: `adaptive`
(m5b_rawゲート)がSF3.850まで削り込み、製品の探索目標(4.8)を大きく割った。`m5b_raw`(超過0)は
満たしているが、**目標安全率を守る保証にはならない**(結論3参照)。「2.0mm解像度」だけでは方向を
予測できず、荷重・幅・解像度いずれの軸を動かしたかで符号が変わる。

## 2. 主要行列: path_small(原因2・経路喪失、04a認定条件)

**baseline(3000N/2.0mm/6mm)は04aが認定した条件そのもの**(task4が使った2.5mm/6mm/3000Nとは別条件——
04a後続への契約①に従い、05では2.0mmを使う)。8条件×4方式。

| 条件 | 要素数 | step20 | step05 | adaptive(m5b_raw) | reallocation |
|---|---:|---|---|---|---|
| baseline(3000N/2.0mm/6mm) | 21,152 | 71,928・4.842 | 72,016・4.843 | 67,168・4.744(-6.6%) | 67,264・4.746(-6.5%) |
| 2700N | 21,152 | 53,664・4.845 | 53,432・4.843 | 60,384・4.997(+12.5%) | 60,568・5.001(+12.9%) |
| **3300N** | 21,152 | 108,592・4.846 | 108,592・4.846 | **160,080・5.134**(**+47.4%**) | **160,120・5.134**(**+47.4%**) |
| 幅4mm | 21,152 | 71,856・4.839 | 71,840・4.839 | 63,320・4.604(-11.9%) | 63,456・4.606(-11.7%) |
| 幅8mm | 21,152 | 72,608・4.835 | 73,360・4.848 | 64,640・4.651(-11.0%) | 64,736・4.657(-10.9%) |
| **解像度1.5mm** | 50,758 | 83,130・4.844 | 82,998・4.835 | **152,385・5.915**(**+83.3%**) | **152,081・5.903**(**+82.9%**) |
| 解像度2.5mm | 11,372 | 57,425・4.836 | 57,123・4.826 | 72,655・5.381(+26.5%) | 70,755・5.338(+23.2%) |
| 解像度3.0mm | 6,461 | 85,941・4.839 | 85,779・4.843 | 78,570・4.615(-8.6%) | 78,813・4.617(-8.3%) |

**baseline・幅4/8mm・3.0mmでは適応/再配置が軽い方向(-6.6〜-11.9%)である一方、2700Nは逆に+12.5〜+12.9%重く、
3300N・1.5mmでは大幅に重い(+47〜+83%)**——04_SEARCHES.mdが単一条件(3000N/2.5mm)で見た「dilutionは
軽い・pathは重い」の単純な二分法は、より広い条件では成立しない(方向は条件ごとに変わる)。

## 3. 「全方式が低品質で区別できない場合」の判定

上記1・2節で、同一条件内の4方式間に最大168.8%(dilution 3.0mm)・83.3%(path 1.5mm)の材料差、
最大1.08ポイント(path 解像度1.5mm、次点は約0.99ポイントのdilution 250N)の安全率差が実測された。**方式間は明確に区別できており、
追加方式(応力感度/BESO型等)を要する「全方式が低品質で区別できない」状況には該当しない。** 二値探索
(方式1・2)自体の限界(3.0mm等で局所解に留まる)は既に段階0〜4で確認済みの現象であり、本タスクの
主要行列でも同じ傾向(格子依存の局所解)が再現された(4節)。

## 4. 機構履歴・失敗モード分析

`analyze_05_mechanism_history.py`が全main_matrix(15条件×4方式のうちstep20/step05以外)・
全ablation(2モデル×7設定)のStepRecord列を読み、3つの失敗モードを検査した。

### 4-1. 新発見: `calibrated_k`単点校正の脆弱性(粗い格子・小さい候補数で暴走)

`_run_adaptive_impl`は最初に受理された1手だけから一次予測係数`calibrated_k`を較正し(`calibrated_k is
None`の間だけ更新)、以後固定する。**dilution 3.0mm**では序盤の大きな1手(1,011セル除去、実ジャンプ
0.031%)で校正した後、終盤の候補(2〜4セル、実ジャンプ0.03〜0.1%)がことごとく`prediction_error`で
拒否され続けた(`05_main_matrix.json` `dilution_res3.0_w6.adaptive_full.steps`、末尾10手すべて
`reasons=['prediction_error']`)。**path 1.5mm**でも同型: 最初の1手(5,607セル)で校正した後、2手目
(4,476セル)から`prediction_error`が始まり、7手で`revert_restart`しても同じ点に留まる。両条件とも
`finish_stagnation`/`evaluation_limit`まで動けず、step20の約2.7倍(dilution)・約1.8倍(path)重い形状で終わった(1・2節の
太字条件と一致)。**根本原因**: エネルギー量に対する安全率ジャンプの関係が非線形(序盤の大きな一括除去と
終盤の少数セル除去では「単位エネルギーあたりの安全率変化」が大きく異なりうる)であるのに、単一の線形
係数で全手を予測しているため。**hangerへ持ち込む前の修正候補**(このタスクの範囲外、9節へ申し送り):
直近N手の移動平均で`calibrated_k`を再較正する、または候補セル数の桁が変わったら再較正する。

### 4-2. 急変検知の遅延(late_response)・微小損失の見逃し(fine_grained_creep)

- **late_response: 0件検出。** `gate="m5b_raw"`では受理される手が定義上m5b_raw超過0を満たすため
  (`_run_adaptive_impl`の`gate_ok and not prediction_error and not sudden_jump`条件)、accepted手の
  系列にm5b_raw超過が現れることは構造的に無い。**この0件は「検知が常に間に合った」ことの証明ではなく、
  ゲートの定義上この失敗モードが観測されない**ことを意味する(m5b_raw以外のゲート=percentileでの
  観測が要る。本タスクのpercentileゲート実行〈legacy_percentile・adaptive_finish_percentile〉は
  停止までの評価数が17〜56と少なく、この期間内には現れなかった——評価予算を広げた追試が要る)。
- **fine_grained_creep: 42件中0件検出**(本分析が置いた基準: 中央値以下の小さい削除が3手以上連続し、
  かつ単発の急変検知〈actual_relative_jump<0.15〉を経ないままm5b超過へ転じる)。同じ理由(m5b_rawゲート
  下では超過自体が発生しない)により、この基準では検出されなかった。**「起きなかった」のか「この検査
  基準・評価予算では見えない」のかは切り分けられていない**(限界として10節に明記)。

### 4-3. 再配置の「枝が荷重経路になるまで評価されない」問題: 局所復元ではなく全材料形への回帰

`reallocation_lag_events`(51件の`grow_accepted`)を全main_matrix・全ablationで集計すると、**source は
すべて`initial_all_material`**(粗い格子からの投影や実行内履歴からの候補は一度も選ばれなかった)で、
**material_before→material_afterの変化は例外なく「現在形→ほぼ初期全材料形」**だった(例:
dilution_load200 reallocation cycle0: 16,936→157,376 mm³〈+829%〉、dilution_base_w6の3サイクル全てで
70,952〜71,040→157,344〜157,376)。これは04_SEARCHES.mdが1条件で報告した挙動(§5「ほぼ全材料形まで
一時的に戻ってから削減を再開」)が、**全15条件×3サイクルにわたって例外なく再現される**ことを確認した
実測である。**「枝が荷重経路になるまで評価されない」問題の実体**: `_connected_remnants`が現在形へ隣接する
連結成分を長い順に提案する設計だが、単純な箱型形状(dilution/path双方)では「削除済み全セル」自体が
現在形に隣接するほぼ単一の連結成分になるため、**毎回この巨大な成分が最長候補として選ばれ、部材単位の
局所復元が実質発動しない**。「枝」が個別に評価される前に、常に「設計空間の残り全体」が復元候補として
提示されてしまう——これは`grow_fraction_of_removed`/`grow_min_cells`のパラメータ調整では直らない
構造的な傾向であり(候補選定ロジック自体の問題)、9節の未解決の弱点に含める。

### 4-4. エネルギー順位と巻き添え単位評価の差

受理された`reduce`手について、要求セル数(energyランキングの申請)と実削除セル数(連結性維持のための
巻き添え込み)の差を集計した(`05_ablation.json`の`collateral_removal_stats`、`05_mechanism_history.json`の
`collateral_removal_stats`)。**ここでのbaselineは5節アブレーションの条件(dilution 300N/2.0mm/4.0mm・
path 3000N/2.0mm/6.0mm。1節main_matrixのdilution baseline行〈6.0mm〉とは幅が異なる)**。dilution:
100手中43手(43%)で巻き添えが発生、平均0.8セル・最大4セル。path: 48手中21手(44%)、平均1.2セル・
最大7セル。**巻き添え量は小さいが恒常的に発生する**
——単発の大きな巻き添えが急変の原因になっている証拠は今回の条件では見られなかった(最大でも7セル)。

## 5. アブレーション: 適応歩幅のみ/復帰/再配置、停止条件×ゲート

baseline条件(dilution 300N/2.0mm/4mm、path 3000N/2.0mm/6mm)で実施(`05_ablation.json`)。

| 設定 | dilution 材料/SF/評価数/停止理由 | path 材料/SF/評価数/停止理由 |
|---|---|---|
| 適応歩幅のみ(revert無効化) | 58,088 / 4.722 / 100 / evaluation_limit | **49,224 / 4.148**(目標未達) / 100 / evaluation_limit |
| +復帰(既定) | **58,088 / 4.722**(step_onlyと完全一致) / 100 | **67,144 / 4.741**(step_only比+36.4%重い・安全率+0.59) / 100 |
| +再配置 | 58,088 / 4.722(削減段は復帰と一致) / 403 / grow_cycles_exhausted | 67,144 / 4.741(同上) / 392 |
| stop=legacy×gate=percentile | 88,368 / 4.836 / 17 / within_target_tolerance(製品step20と酷似) | 71,928 / 4.842 / 26(同上) |
| stop=legacy×gate=m5b_raw | 88,368 / 4.836 / 17(percentile版と同一) | 71,928 / 4.842 / 26(同一) |
| stop=adaptive_finish×gate=percentile | 82,680 / 4.800 / 56 / finish_stagnation | 70,720 / 4.800 / 46 / finish_stagnation |
| stop=adaptive_finish×gate=m5b_raw(=revert既定) | 58,088 / 4.722 / 100 | 67,144 / 4.741 / 100 |

**dilutionでは「復帰」が実質無効**(既定パラメータでrollback/revert_restartが100評価以内に一度も
発動せず、「適応歩幅のみ」と完全同一の結果)。**pathでは「復帰」が明確に効く**(52回のrollback・7回の
revert_restartが発動し、材料+36.4%(49,224→67,144)と引き換えに安全率を4.148→4.741まで回復させた——2節の`prediction_
error`暴走とは別の、正しく機能したケース)。**stop=legacyは製品のstep20/05をほぼ再現し**(両モデルとも
製品の材料量と1%未満の差)、**gate(percentile/m5b_raw)の違いはstop=legacyでは表れない**(target_
tolerance到達で即座に止まるため、ゲートの違いが試される前に停止する)。gate差が実際に効くのは
`stop_policy="adaptive_finish"`のときで、percentileゲートは製品目標(SF4.8)ちょうどで止まる一方、
m5b_rawゲートはそれを大きく割り込む(path: 4.800→4.741 / dilutionは4.800→4.722)まで削り込む——
**3節「m5b_rawだけでは目標安全率を守れない」の直接証拠**。

## 6. パラメータ感度: 調整用→未使用検証

`05_sensitivity.json`。調整用条件はdilution 300N/2.0mm(baseline)・path 3000N/2.0mm(**04aが調整に
使った条件と同一。「未使用検証例」から除外**)。各3パラメータ(`stagnation_patience`・
`prediction_error_multiplier`・`sudden_jump_relative_drop`)を3値、70評価予算で振った。

| モデル | パラメータ | 相対体積スプレッド | 感度(閾値10%、本タスクが置いた基準) |
|---|---|---:|---|
| dilution | stagnation_patience | 0.0% | 無感度(rollback/revert未発動のため全値で同一結果) |
| dilution | prediction_error_multiplier | 0.0% | 同上 |
| dilution | sudden_jump_relative_drop | 0.0% | 同上 |
| path | stagnation_patience | 0.2% | 無感度 |
| path | **prediction_error_multiplier** | **14.4%** | **感度あり**(1.5→64,240mm³ / 2.5→67,272 / 4.0→57,600) |
| path | sudden_jump_relative_drop | 0.0% | 無感度 |

**選定**: 感度が見つかった`prediction_error_multiplier`について、既定値(2.5)を疑う実測上の破綻
(発散・全滅・目標未達等)は出なかったため、**選定値は既定値のまま据え置いた**(「感度があるからといって
未検証の値へ差し替える根拠にはしない」という本タスクの判断、詳細は05_sensitivity.jsonの
`chosen_params_rationale`)。未使用検証(dilution 200N・path 2700N・3300N)は既定値=選定値のため
`result_with_default`と`result_with_chosen`が数値として同一になる——**これは「異なる値でも汎化する」
ことの検証ではなく、既定値の決定性・条件間の一貫した挙動を確認したにとどまる**(限界として10節に明記)。

## 7. 格子/原点ロバスト性・フィルタ幅mm固定

`05_grid_origin.json`。

### 7-1. 原点シフト(01_PROTOCOL.md 6-1節の8条件)

`synthetic_matrix_support.build_context_with_origin_shift`(`metric_eval`の原点シフトパッチと同じ
process内一時パッチを新規探索にも適用)。step20(製品コード)で8条件を実行。

| モデル | 位相(mask_hash)変化 | 材料の相対変化幅 |
|---|---|---|
| dilution | 基準以外の**7条件全てで位相が変化** | **-50.8%〜0%**(+0.5z/-0.5zが最大変化) |
| path | 基準以外の**7条件全てで位相が変化** | **-20.6%〜+7.4%** |

半セル移動は「意図した0.5セルちょうど」にならない場合がある注記どおり、達成シフト量は
`achieved_origin_mm`にケースごと記録した(`05_grid_origin.json`)。適応方式でも基準・全軸+0.5の2点を
追加確認し、dilutionは78,648→102,032 mm³(+29.7%)、pathは67,312→57,008 mm³(-15.3%)と、**方式に
よらず原点シフトが材料に大きく影響する**ことを確認した。

### 7-2. フィルタ幅mm固定 vs セル数固定(`ENERGY_FILTER_SIZE`)

`patched_filter_size`(process内一時パッチ)で、baseline解像度(2.0mm)の物理幅3セル=6.0mmを他解像度でも
mm固定した版と、既定のセル数固定(3セル、物理幅は解像度ごとに変わる)を比較した(step20)。**最小幅は
dilution・path共通で6.0mmに揃えた**(1節のdilution主要行列baseline行`dilution_base_w6`と同じ6.0mm。
dilutionの荷重スイープ行〈200N/250N〉や5節アブレーションのbaseline〈4.0mm、task4と同条件〉とは別条件
——ここは「フィルタ幅」だけを比較変数にするため、最小幅は両モデルで揃う6.0mmに固定した)。

- **2.0mm(baseline)では両者が定義上一致**(3セル=6.0mmちょうど)。
- **1.5mm/2.5mm/3.0mmでは、材料量の差は小さい(<2%)が、mask_hashは4解像度中3つで異なった**
  (dilutionの2.0mmのみ一致、path も2.0mmのみ一致)——フィルタ幅の定義(mm固定かセル数固定か)は
  **僅かな体積差の裏で異なる形状を選んでいる**ことが多く、材料量だけを見て「同じ結論」と判断しない
  よう記録した(共通指示書「実験の品質と十分性」8「実験用の局所体積やピーク除外に物理的根拠がなければ
  未校正と表示する」と同じ注意)。

## 8. 初期形状(warm-start)

`05_initshape.json`。

| 条件 | 投入形(投影元の格子自身のorigin/spacingで投影) | 評価予算60後 | 元の状態との比較 |
|---|---|---|---|
| dilution: 3.0mm解像度からの投影 | 22,248 mm³(投影+侵食後) | **22,064 mm³・SF3.899**(32評価でfinish_stagnation、目標未達) | さらに削り込んで目標を割った |
| path: **04a優越形から**(既にpercentileシナリオで認定済み、SF4.800) | 67,800 mm³(投影不要) | **60,624 mm³・SF4.562**(60評価、目標未達) | **安全な形状から出発しても、m5b_rawゲートの適応方式が続けて安全率を目標未満まで削る** |
| path: 3.0mm解像度からの投影 | 109,176 mm³(投影+侵食後) | 58,896 mm³・SF4.479(60評価、目標未達) | 同上 |

**3件とも評価予算60以内(dilutionは32評価でfinish_stagnation、他2件は評価上限)に製品目標(SF4.8)を
割り込んだ**。「粗い格子や既存の優越形から始めればウォームスタートが原因2(局所解)を避けられるか」
(01_PROTOCOL 6-2節)という問いへの答えは、**m5b_rawゲートの適応方式単体では否**——安全な初期形状を
与えても、ゲートが目標安全率を守らない限り削り過ぎる(3節・8節共通の教訓)。再配置(方式4)のwarm-start版は
本タスクでは実施していない(10節「制約」4)。**投影は投影元自身の格子(origin/spacing)を使う必要があり、
実装時に目的格子(2.0mm)のoriginを誤って使う罠を踏んだ**(実測: 2.0mm origin(-16,-16,-16) vs 3.0mm
origin(-16.97,-17.70,-17.76)。修正後の数値が上表)。

## 9. hangerへの凍結パラメータ案・未解決の弱点

**凍結パラメータ案**(本タスクの実測に基づく推奨。製品への配線は本タスクの範囲外):

- `AdaptiveConfig`の既定値(`stagnation_patience=6`・`prediction_error_multiplier=2.5`・
  `sudden_jump_relative_drop=0.15`等)は、6節の感度分析で変更の実測根拠が無いため**そのまま維持**。
- **`gate`は`m5b_raw`単独で使わない。** hangerで使うなら`m5b_target`または`percentile`との併用
  (両方が合格するまで受理しない)が要る——3・5・8節で共通して確認した弱点。
- **粗い格子(目安: baseline格子の1.5倍以上の要素サイズ)・少数セル候補が続く終盤では、適応方式の
  `prediction_error`ロールバックが暴走しうる**(4-1節)。hangerの粗い解像度条件(H1: 2.0mm、H2: 2.4mm)
  では、要素数がdilution/path baselineの30倍以上(64万要素)あるため序盤の1手が相対的に小さく、
  この暴走が起きにくい可能性はあるが**未検証**(実測していない、10節)。
- **再配置(方式4)は現状の実装のまま使うと「部材単位の局所復元」ではなく「全材料形への回帰」になる**
  (4-3節)。hangerのような大規模モデルでは、この挙動のまま使うと1サイクルあたりのFEM再評価コストが
  非常に大きくなる(全材料形に近い形状を評価するため)。04_SEARCHES.md §制約1が既に指摘した「hanger規模で
  reallocationは未実行(数時間規模になりうる)」という判断は、**この構造的な傾向を踏まえると一層妥当**。

**未解決の弱点(次工程・hangerでの確認が要る)**:

1. `calibrated_k`単点校正の脆弱性(4-1節)——hanger規模での再現性は未検証。
2. `m5b_raw`単独ゲートの安全率未達(3・5・8節)——m5b_target/percentile併用時の挙動は未測定。
3. 再配置の「全材料形への回帰」傾向(4-3節)——連結成分を分割する代替実装は本タスクの範囲外
   (「無関係な全面置換はしない」との指示、4分割は幾何検査でほぼ確実に落ちるという04の既知の罠とも
   関連する)。
4. late_response・fine_grained_creep分析は0件検出だが、**m5b_rawゲートの構造上この失敗モードが
   観測されないだけ**の可能性が高く、percentileゲート×広い評価予算での追試が要る(4-2節)。

## 10. 完了条件との対応・制約

- 「全題材・全方式の実測と機構別比較が揃い」: 1〜8節(15main_matrix条件×4方式・7ablation設定×2モデル・
  6感度スイープ×2モデル・8原点シフト×2モデル・4フィルタ幅解像度×2モデル・3warm-start条件)。
- 「hanger用の凍結パラメータと未解決の弱点がある」: 9節。
- 「優位性がなければその結論でよいが、費用による省略や実装未完成は比較完了にしない」: 3節(方式は
  明確に区別できた、追加方式は不要と判断)。全実験は評価予算・grow_cycles等を明記した上で完走させた
  (途中打ち切りは無い。予算自体は比較指標として明記——共通指示書「実験の品質と十分性」6
  「比較時間は調査の期限ではない」)。

**制約**:

1. S-T/S-B/S-TB/S-L/S-MS(01_PROTOCOL §5)は本タスクでは新規構築していない(スコープの決定節)。
   task10のcoverage監査に判断の妥当性を引き継ぐ。
2. path_smallの04a認定はpercentile評価シナリオに限る(m5b_targetでは現行・優越形とも不合格・未決着)。
   本タスクの評価ゲート既定はm5b_rawのため、path側の受理判定は04aの認定範囲と厳密には別シナリオである
   点に注意(併記される`safety_factor_product`でpercentile側も確認できる)。
3. hanger実行は本タスクの範囲外(task4のH1/H2で外部確認済み、task6/7が引き継ぐ)。9節の凍結パラメータ案・
   未解決の弱点のhanger規模での再現性は未検証。
4. reallocationのwarm-start専用入口は無く(方式4は常に`run_adaptive`の削減→追加を内部で辿る設計)、
   8節の比較は`run_adaptive_from_mask`(方式3)に限る。方式4への拡張は未実施。
5. パラメータ感度スイープ(6節)は評価予算70で打ち切っており、収束の理論的な極限ではない。
6. late_response分析はStepRecordに手単位のイベント直接リンクを持たないため、run単位の粗い近似に
   とどまる(4-2節)。

## 11. 再現コマンド・版・成果物

```bash
# A: 主要行列(15条件×4方式。--only <case_id...> で絞り込み可、--smoke で疎通確認)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_main_matrix.py

# B: アブレーション
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_ablation.py

# C: パラメータ感度
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_sensitivity.py

# E: 格子/原点ロバスト性・フィルタ幅mm固定
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_grid_origin.py

# F: 初期形状(A完走後、res3.0_w6ケースのマスクに依存)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_initshape.py

# D: 機構履歴・失敗モード分析(A・Bの出力に依存、新規FEMなし)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\analyze_05_mechanism_history.py

# 機械可読まとめ(A〜Fの出力が揃っていること)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_05_synthetic_json.py
```

成果物: `output/idle_validation_20260928/05_main_matrix.json`(15 cases)・`05_ablation.json`・
`05_sensitivity.json`・`05_grid_origin.json`・`05_initshape.json`・`05_mechanism_history.json`・
`05_synthetic.json`(機械可読まとめ、schema_version=1、56 cases)・`05_masks/`(最良形状npz)・
`05_*_checkpoint.json`(main_matrixの再開用)。コードは
`topopt_checks/idle_validation/synthetic_matrix_support.py`(共有ヘルパー、実装本体は既存の
`search_methods.py`を無変更で再利用)・`run_05_main_matrix.py`・`run_05_ablation.py`・
`run_05_sensitivity.py`・`run_05_grid_origin.py`・`run_05_initshape.py`・
`analyze_05_mechanism_history.py`・`build_05_synthetic_json.py`。
