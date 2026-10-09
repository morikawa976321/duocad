# 段階4: 4探索方式の実験用実装

2026-09-28。`docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`「0〜2レビュー後の訂正と実行順序」節の
`20260928-1426-topopt-idle-04-searches`。着手前に `docs/contracts/topopt-idle-validation-setup.md`(出典 `20260928-1426-topopt-idle-03b-metrics-recheck`)
「## 後続への契約」と `03B_METRICS_RECHECK.md` 本体を読み、03bが凍結した評価器(M5b合否ゲート・M3部位別
診断・M7b探索補助)をそのまま引き継いだ。

**本書の役割**: 20%削除・5%削除・適応歩幅＋復帰・再配置の4探索方式を、同じ評価器・同じ幾何検査で
公平に比較できる実験用実装(`topopt_checks/idle_validation/search_methods.py`)を作り、小型モデル
(dilution_small=原因1・path_small=原因2代表)で各機構が実際に発動する証拠を実測し、hangerの代表2条件
(H1=低荷重・太幅・H2=高荷重)で外部確認する。**新方式の性能優位は主張しない**(完了条件)——数値は
すべて実測のまま記録し、評価ゲートが異なる方式間の体積比較は評価シナリオが異なるため直接比較しない。

## 結論(先に要約)

1. **方式1・2(旧20%/5%経路)は製品コード(`binary_search.find_best_feasible`)を無変更で呼ぶだけの
   薄いラッパーで実装した。** dilution_smallで実行し、`00_COMPACT_MODELS.md` §2-2の既知値(20%歩幅:
   材料88,368 mm³・安全率4.836・評価18回)とビット単位で一致することを確認した——比較器がここまで
   正しく動くことの直接証拠。
2. **M5bには2つの版がある**(実測で発見): 03bが凍結した定義(`m5b_raw`。材料の生の許容応力に対する
   超過=「セルが1つでも実際に壊れる境界を割っていないか」)と、製品の探索目標と同じ安全余裕で
   揃えた版(`m5b_target`。`allowable/target_safety_factor` を閾値にする)。**dilution_smallの
   全材料形でさえ `m5b_raw` は合格(5.59)するが `m5b_target` は不合格(192.9 MPa·mm³超過)**——
   両方を常に記録し(`CandidateScore`)、探索の受理ゲートは `AdaptiveConfig.gate` で選べる3択
   (既定 `m5b_raw`。3節)にした。単一の「正しいゲート」を04では確定しない。
3. **適応歩幅方式(方式3)の全機構(安全率余裕・部位別応力信号・一次予測に基づくロールバック・
   最軽量合格形への復帰と別順序での再開・仕上げ探索と停滞判定・旧停止条件との対照)が、
   dilution_smallで実際に発動することを実測で確認した**(4節・`04_mechanism_checks.json`)。
   閾値を極端にして強制発動させた確認と、既定値での通常実行の両方を記録する。
   **既定値のまま2モデルで走らせた主実証(2節)では、dilutionとpathで方向が逆になった**
   ——dilutionでは適応方式が製品20%より軽く(52,472 mm³ vs 88,368)・機構ログ1件と静かに収束した
   一方、pathでは適応方式が製品20%より**重く**(72,520 mm³ vs 57,425)・機構ログ33件と
   rollback/revert_restartが頻発した。同じ実装・同じ既定値でもモデルの応力分布次第で挙動が
   大きく変わるという実測(新方式の性能優位を主張しない理由の一つ)。
4. **再配置方式(方式4)は、path_small(原因2=経路喪失の代表題材。04a未認定)で、削減フェーズが
   捨てた大きな連結成分(実測: 1回のFEM評価で6,810セル)をまるごと復元し、一時的に体積を増やしてから
   削減を再開する一連の動作を実測した。** 追加直後に `poison`/`rejected` キャッシュが無効化される
   ことも確認した(5節)。**path_smallは04aで原因2の認定が済んでいない**ため、この実測は「機構が
   実際に動作する証拠」であって「経路喪失を修復した証明」ではない(04aへ引き継ぐ)。
5. **決定性(同seed一致)・チェックポイント再開(対照実行と完全一致)・キャンセル後の合格形保持を、
   いずれも実際にメカニズムを起動させた状態(revert_restart等を強制発動)で確認した。** 偶然の一致や
   未使用経路での見かけ上の合格ではない(4節末尾)。
6. **hanger外部確認(H1=`F0200_h2.0_w5`・H2=`F5000_h2.4_w2`)を実施した。** H1の「現行」は
   製品の既存保存形`hanger_base`の再スコアで、M5b(raw)超過69.5 MPa·mm³相当という03bが既に報告した
   既知の不合格(引継ぎエントリ2026-09-28「hanger_baseはM5b=69.5」)と一致し、本実装の評価器が
   hanger実形状でも03bと同じ数値を出すことを確認した(6節)。**H2は`hanger_base`をそのまま
   使えなかった**(2.0mm→2.4mmへの投影で連結成分が条件面を失う。実測)ため、H2の「現行」は
   製品コード(20%固定歩幅)をH2条件で新規実行した結果とした——**10評価でconverged、材料
   3,255,099 mm³・安全率4.84に落ち着いたが、M5b(raw)超過29,949 MPa·mm³、部位別最悪値93.2 MPa
   (材料の生の許容応力40MPaの2倍超)という、H1よりずっと大きい実際の危険を抱えていた。**
   **H1・H2いずれも新方式(適応方式、M5b rawゲート、20評価予算)は1件も受理しなかった**——
   「現行」(または全材料形)自体が局所超過を持つ状態からの純粋な除去では、この超過を解消できない
   という実測が両条件で一致した(除去は局所超過を悪化させこそすれ改善しにくいという直感と整合)。
   **percentile安全率(製品ゲート)は両条件とも「合格」相当(H1: 4.82・H2: 4.84)を出す一方、
   M5b(raw、材料の生の許容応力に対する超過)は両条件とも「不合格」——03bが小型モデルとhangerの
   一部条件で示した「パーセンタイル集約は危険を希釈して隠す」という結論が、H2(高荷重)条件でも
   再現された。** hanger本体の新規探索(`find_initial_feasible`からのフル探索)は、H1条件で
   64万要素・全材料1回のFEMに実測約157秒かかることを確認しており(H2の「現行」生成では実際に
   フル探索〈段階3+段階4〉を1回走らせた)、H1の新方式は段階3を経由しない軽量入口
   (`run_adaptive_from_mask`)を使った(7節「制約」)。

## 1. 実装アーキテクチャ

`topopt_checks/idle_validation/search_methods.py`(約910行)。

- **`SearchContext`/`build_context`**: `metric_eval.solve_shape`(タスク3)と同じ条件構築パイプライン
  (`optimize_run.build_conditions`→`design_space.derive`→`_map_boundary_conditions`→
  `_build_search_conditions`)を、新規探索用に設計空間全体(`space.mask`)まで組む。
- **`score_mask`(評価器)**: 候補1つを`_fem_probe.evaluate_full`で1回FEMし、M5b(raw/target 両方)・
  M3(部位別)・M7b(KS)・compliance・要素ひずみエネルギー場を一括で返す(`CandidateScore`)。
  `fem.Model.compliance`を`_fem_probe.evaluate_full`が返す`_model`/`_density`/`_displacement`から
  直接呼び、2回目のFEM解を避ける。
- **除去側の幾何検査**は`binary_search.resolve_islands`/`_geometry_rejection`/`peel`/`ranked_cells`/
  `_min_width_exempt`/`_mask_satisfies_min_width`/`_keep_cells`/`_anchor_node_ids`を**再実装せず**
  直接呼ぶ。
- **追加側の幾何検査**(`_growth_geometry_ok`)は`acceptance.inspect`を呼べない(「材料が`before`を
  超えない」前提があるため)ので、内部の個別チェック(`boundary_check`/`connectivity_check`/
  `region_check`×2/`condition_check`)を直接呼び、さらに`_mask_satisfies_min_width`・
  `acceptance.closed_voids`・`core_group_count`(新設。`min_width.core_cells`の侵食後の連結成分数)
  を追加する——保持・除外・アンカー・単一成分・正則境界・芯の連結・最小幅・閉空洞禁止の全項目。

## 2. 4方式の主実証(`run_04_searches_compact.py`)

`binary_search.find_best_feasible(max_step_ratio=0.2 または 0.05)`を無変更で呼ぶ(方式1・2)。
dilution_small(300N・2.0mm・最小幅4.0mm。設計19,672要素)とpath_small(3000N・2.5mm・最小幅6.0mm。
設計11,372要素。**04a未認定**)の両方で全5構成(20%・5%・適応M5b raw・適応percentile対照・再配置)を
実行した(`output/idle_validation_20260928/04_searches_compact.json`)。

| モデル | 構成 | 材料 mm³ | 安全率(%ile) | M5b(raw)超過 | 評価回数 | 停止理由 | 機構ログ件数 |
|---|---|---:|---:|---:|---:|---|---:|
| dilution | step20 | 88,368 | 4.836 | 0 | 18 | converged | 0 |
| dilution | step05 | 88,488 | 4.844 | 0 | 19 | converged | 0 |
| dilution | adaptive(m5b_raw) | 52,472 | 4.604 | 0 | 120 | evaluation_limit | 1 |
| dilution | adaptive(percentile対照) | 82,680 | 4.800 | 0 | 56 | finish_stagnation | 2 |
| dilution | reallocation | 64,280 | 4.741 | 0 | 323 | grow_cycles_exhausted | 10 |
| path | step20 | 57,425 | 4.836 | 0 | 19 | converged | 0 |
| path | step05 | 57,123 | 4.826 | 0 | 29 | converged | 0 |
| path | adaptive(m5b_raw) | 72,520 | 5.377 | 0 | 43 | finish_stagnation | 33 |
| path | adaptive(percentile対照) | 72,520 | 5.377 | 0 | 43 | finish_stagnation | 33 |
| path | reallocation | 70,755 | 5.338 | 0 | 143 | grow_cycles_exhausted | 111 |

`00_COMPACT_MODELS.md` §2-2の既知値(dilution step20: 材料88,368 mm³・安全率4.836・評価18回)と
**ビット単位で一致**(`run_compact_search.py`が呼ぶ製品の全行程入口ではなく、探索だけを直接呼ぶ経路の
違いはあるが、探索自体の結果は同一)。比較器が製品の探索ロジックを正しく再現できていることの直接証拠。

**全10構成の採用形状は`score_mask`によるM5b(raw)が合格(超過0)**——製品どおりの探索(R=-1相当)が
実際に安全率を満たすという03の既報と整合し、新方式(適応・再配置)も同じ安全下限を守ったまま探索できる。

**`finish_patience`(改善停滞判定)が実際に評価回数を大きく減らす**(QAで「定義のみで未配線」と
指摘され配線した後の実測): dilutionのpercentile対照は120評価予算を使い切らず56評価で
`finish_stagnation`停止(材料はほぼ同じ82,680)、pathの適応方式は120評価から43評価
(機構ログ108件→33件)まで縮んだ。**停滞を検知したら早く止まる**という設計どおりの挙動。

**dilutionとpathで傾向が違う**(実測): dilutionでは適応方式(m5b_raw)が製品の20%(88,368)より
大幅に軽い52,472まで削れ、機構ログは1件(比較的静かな探索)。**pathでは逆に適応方式が製品より
重い72,520で止まり、機構ログが33件**(rollback/revert_restartが頻発)——path_small(経路喪失=原因2
の代表題材)の応力分布では、M5b(raw)ゲートが製品の20%探索よりもずっと保守的に働くことを示す
(gate=percentileと全く同じ軌跡になったのは、この模型では両ゲートが同じ点で不合格を出したため
——安全率余裕とM5b(raw)がpathでは強く相関する、という追加の実測事実)。
**新方式の性能優位は主張しない**(dilutionでは軽い・pathでは重い、と方向すら逆転する)。

## 3. 評価ゲートの選び方(`AdaptiveConfig.gate`)

実装中に発見した罠: `metric_exceedance_integral`(M5b)に渡す`allowable_mpa`は、03b自身の比較
スクリプト(`run_metric_comparison._score_all_metrics`)では材料の生の許容応力(例: 40MPa)をそのまま
使う。これは「セルが1つでも実際に壊れる境界(SF≈1)を割っていないか」を測る診断としては正しいが、
**製品の探索目標(安全率4.8)と同じ意味の合否ゲートではない**。実測: dilution_smallの全材料形
(製品どおりの安全率5.59)でさえ、目標調整版(`m5b_target`=`allowable/4.8`)では不合格(192.9 MPa·mm³
超過)になる。

`AdaptiveConfig.gate`は3択:

- `"m5b_raw"`(既定): 03bが凍結した定義そのもの。SF≈1(生の材料破壊)を割らないことだけを見る、
  最も緩いが最も03bに忠実なゲート。
- `"m5b_target"`: `allowable/target_safety_factor`を閾値にした版。方式1・2と同じ安全余裕で体積を
  比べたいときに使うが、除去だけでは目標安全率まで届かない入口もありうる(dilution_smallの全材料形が
  好例)ため既定にはしない。
- `"percentile"`: 製品の受理ゲート(99.5パーセンタイル安全率)をそのまま使い、同じループを回す対照。

**3つは異なる安全の定義を測っており、跨いだ体積の大小だけで優劣を主張しない**(方式間の比較は
同じゲートのシナリオ内で行う)。

## 4. 方式3: 適応歩幅＋復帰(`run_adaptive`)の機構実証

`search_methods_mechanism_checks.py`が、実際にFEMを解きながら8項目を検証する
(`output/idle_validation_20260928/04_mechanism_checks.json`。全8項目PASS)。強制発動用の閾値
(例: `sudden_jump_relative_drop=1e-6`)は「発動する条件が実在すること」を実測で示すためのもので、
既定の運用値ではない。

| 項目 | 結果 | 実測値 |
|---|---|---|
| ロールバック(予測誤差/急変。安全率・compliance・変位いずれかの急変で発動) | 発動 | `sudden_jump_relative_drop`を極小にした15評価中12回`rollback`発動 |
| 復帰と別順序での再開 | 発動 | stagnation_patience=1の19評価中16回`revert_restart`発動 |
| 旧停止条件との対照 | 対照成立 | `stop_policy="legacy"`は17評価で`within_target_tolerance`(材料88,368mm³)、`"adaptive_finish"`は`finish_budget_exhausted`で30評価・材料82,752mm³ |
| 決定性(同seed一致) | 一致 | revert_restartを11回実際に起こした状態で、同seed2回のtrial列(material_mm3_after列)と最終形状の`mask_hash`が両方完全一致。異seedは分岐 |
| 再開(チェックポイント→resume) | 一致 | 8評価で打切り→再開した結果のmask_hashが、20評価を1本で回した対照実行と完全一致 |
| キャンセル後の合格形保持 | 保持 | 4手でキャンセルしても`best_ever`は既定ゲート(`m5b_raw`)に合格したマスクのまま(material_mm3=128,424) |
| ハッシュの条件非依存(poison/rejectedキャッシュが条件をまたいで漏れない) | 確認 | 異なる荷重条件のctxを直後に走らせても、後続ctx単独の対照実行と評価回数・trial列・FEM回数が完全一致 |
| 安全率・部位別・compliance・変位・実削除体積を使った歩幅/ロールバック | 実装済み | 歩幅=`step_ratio`(安全率余裕)×`multiplier`(急変時の後退)、一次予測は`removed_energy`(実削除セル=巻き添え/島込みの合計エネルギー)。ロールバック判定に`_relative_zone_jump`(M3部位別)・`compliance_relative_increase`・`displacement_relative_increase`を使う |

**踏んだ罠(実装上の罠。§後続への契約④参照)**: 歩幅の下限(`min_step_ratio`)を後退倍率
(`multiplier`)にも床として適用すると、同じ複数セル候補が却下され続けたときに`requested`が
その床で頭打ちのまま変わらなくなり、`peel`が毎回同じ候補を返して無限ループになった(実測)。
`min_step_ratio`は目標歩幅(`gap_ratio`)だけに床を敷き、後退倍率には敷かないよう修正した——
既存`_reduce_volume`と同じ「後退倍率は無制限に半減して`requested→1`に収束する」停止保証を
維持するため。

## 5. 方式4: 再配置(`run_reallocation`)の機構実証

path_small(3000N・2.5mm・最小幅6.0mm。原因2=経路喪失の代表題材、**04a未認定**)で実施。

- **追加候補**は「初期全材料形 ∪ 実行内履歴 ∪ 外部候補(任意)」から現在形を引いた連結成分を、
  現在形へ隣接するものだけ長い順に提案する(`_connected_remnants`)。成分は分割せずまるごと足す
  (セル数上限で機械的に切ると、ravel順の部分集合が部材自身と非連結になり幾何検査でほぼ必ず落ちる
  ——実装中に発見して修正した)。連結成分の候補が尽きれば局所表面(現在形に隣接する設計空間内の
  空セル、超過セルに近い順)へフォールバックする。
- 実測(`04_mechanism_checks.json`の`poison_invalidation_and_reallocation`、2回の追加サイクル):
  2回とも`grow_accepted`、**1回の追加で最大6,810セルを一括復元**(=大きな連結成分=長い部材単位の
  復元)。`temporarily_heavier_observed=true`(仮追加で一時的に重くなる形を実際に経由)。
- **主実証(`04_searches_compact.json`、2節、grow_cycles=3)でも同じ動作を確認**: dilution・path
  いずれも**3回とも全サイクルで`grow_accepted`**、1回あたり6,393〜11,637セルを一括復元し、
  いずれも**ほぼ全材料形(dilution 157,376 mm³・pathの初期全材料に近い171,3xx〜171,4xx mm³)まで
  一時的に戻ってから削減を再開した**(仮追加が「わずかに増やす」ではなく「初期全材料形の連結成分を
  丸ごと再導入する」規模で起きている——追加候補の探索が常に`initial_all_material`由来の成分を
  最長候補として選んだ結果)。
- **`grow_accepted`の直後、必ず`poison_invalidated`が記録される**(全サイクルで直後。`cleared_poison`/
  `cleared_rejected`件数を記録)——再配置で形状が根本的に変わった後、古い安全率マージンに基づく
  poisonエントリや古い形の却下キャッシュを持ち越さない。
- 追加候補は`_growth_geometry_ok`(1節)の全項目を通ったものだけ採用する。

## 6. hanger外部確認

「小型題材を主にする変更」節が要求する最低限の外部確認。H1=`F0200_h2.0_w5`(200N・2.0mm要素・
最小幅5セル=10mm。原因2=低荷重・太幅の代表)、H2=`F5000_h2.4_w2`(5000N・2.4mm要素・最小幅は
製品既定=2×実spacing≈4.8mm。原因1=高荷重の代表)。

**H1とH2で「現行」の作り方が異なる**(実測で発見・修正した理由。`hanger_external_check.py`
モジュールdocstring参照): H1は製品の既存保存形`hanger_base`(`01_stage1_shapes.json`由来、
native 2.0mm)をH1条件(同じ2.0mm)へそのまま投影・再スコアできる(新規探索ではない)。
**H2(2.4mm)へ同じ手順で投影しようとすると、粗い格子への投影で連結成分が条件面を失い
(`resolve_islands`が0件——touches_condition=Trueの17,811セルの成分が丸ごと落ちた)使えなかった**。
`hanger_base`と互換の保存形がH2条件には存在しない(`HANGER_DEFAULTS`は全て200N由来)ため、
**H2の「現行」は製品コード(`run_fixed_step`、20%固定歩幅)をH2条件で新規に1回実行した結果**とした。
新方式(`run_adaptive`、既定`m5b_raw`ゲート)もH2では同じ入口(段階3=`find_initial_feasible`から)で
揃えた。H1は`run_adaptive_from_mask`(段階3を経由せず`hanger_base`から削減を継続)を使う。

実測結果(`output/idle_validation_20260928/04_hanger_external_check.json`):

| 条件 | 設計要素数 | 現行: 材料mm³ | 現行: 安全率(%ile) | 現行: M5b(raw)超過 | 現行: M3最悪値 | 新方式: 材料mm³ | 新方式: 安全率(%ile) | 新方式: 評価回数/理由 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| H1(200N・2.0mm) | 640,306 | 249,716 | 4.824 | 69.5 | 15.9 MPa | 249,716(不変) | 4.824(不変) | 20/evaluation_limit(0件受理) |
| H2(5000N・2.4mm) | 367,390 | 3,255,099 | 4.842 | **29,949** | **93.2 MPa** | 5,118,824(全材料・不変) | 6.525 | 20/evaluation_limit(0件受理) |

**検証**: H1条件での「現行」(`hanger_base`)再スコアはM5b(raw)超過69.5 MPa·mm³相当となり、
2026-09-28の引継ぎエントリ(「現行形hanger_baseはM5b=69.5(不合格)」)と一致した——本実装の評価器が
hanger実形状でも03bと同じ数値を出す。

**H1・H2いずれも新方式(適応方式、M5b rawゲート、20評価予算)は1件も受理しなかった**
(`best_ever`は開始形状のまま不変。H1は`hanger_base`、H2は全材料形。両条件ともrevert_restartが
2回発動した)——「現行」または全材料自体がM5b(raw)で不合格(SF≈1を割る局所超過を持つ)状態からの
純粋な除去では、この超過を解消できなかったという実測(除去は局所超過を悪化させこそすれ改善しに
くいという直感と整合する)。

**H2で特に顕著な発見**: 製品の20%固定歩幅探索は「現行」として材料3,255,099 mm³・安全率4.842
(パーセンタイル、目標4.8を満たす)へ**実際にconverged**したが、この形状はM5b(raw)超過29,949
MPa·mm³・部位別最悪値93.2 MPa(材料の生の許容応力40MPaの**2倍超**)という重大な実際の危険を
抱えたまま「合格」と判定されている。5000Nという高荷重条件では、母数希釈によって隠される実際の
危険がH1(低荷重)よりずっと大きい——03bが確立した「パーセンタイル集約は危険を希釈して隠す」
という結論が、hanger実形状・高荷重条件でも再現された。改善には除去以外の機構(再配置等)が要る
可能性を示唆するが、hanger規模での再配置実行は本タスクでは行っていない(7節「制約」)。

## 7. 完了条件との対応・制約

- **4方式の実行入口**: `run_fixed_step`(20%/5%)・`run_adaptive`(適応)・`run_reallocation`(再配置)。
  済み(1〜2節)。
- **条件・パラメータ・記録形式**: `SearchContext`/`AdaptiveConfig`/`ReallocationConfig`/
  `MethodRecord`/`StepRecord`。`MethodRecord.to_json`が方式・状態・評価回数・FEM回数・所要時間・
  seed・最良形状のマスク(npz)・機構ログ・設定を1形式で出す。済み。
- **幾何/物理不変条件の検査**: 除去側は製品コードを再利用、追加側は本モジュールが持つ全項目。済み。
- **小問題で各機構が発動した証拠**: 4〜5節、`04_mechanism_checks.json`(全8項目 PASS)。済み。
- **長い部材の復元が可能**: 5節(6,810セルの一括復元)。ただし**path_smallは04a未認定**——「復元が
  可能なことの機構実証」であって「経路喪失の修復証明」ではない。
- **新方式の性能優位を主張しない**: 本書全体で数値のみを記録し、評価ゲートが異なる方式間の体積比較を
  直接比較していない。

**制約・未解決**:

1. hanger本体の設計空間からの新規探索(段階3+段階4)はH2条件(`run_fixed_step`・`run_adaptive`)で
   実際に1回ずつ実行した(約34分・約50分)。**H1・H2とも`run_reallocation`(再配置)は
   hanger規模で実行していない**——1FEM解あたり約30〜160秒かかる規模で、再配置は削減フェーズを
   複数回内包するため、比較にならない時間規模(数時間)になりうる。`run_adaptive_from_mask`
   (既存合格形から続きを回す、段階3を経由しない軽量入口)と`run_reallocation`自体は汎用実装
   (どのcontextでも呼べる)なので、05以降で必要になれば同じ入口を使ってhangerでの再配置を
   追加実行できる。
2. path_smallでの再配置実証は04a(原因2小型題材の認定)の前に行った。04aが認定または代替計画を
   出した後、必要ならこの実証を04aの認定済み条件で再確認すること。
3. `m5b_raw`/`m5b_target`のどちらが「正しい」合否ゲートかはこのタスクでは判定しない——03b以降・
   最終タスク(10)の判断に委ねる。

## 8. 再現コマンド・版・成果物

```bash
# 小型モデルでの4方式主実証(dilution_small・path_small)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_04_searches_compact.py

# 各機構が実際に発動する証拠(強制発動条件込み)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\search_methods_mechanism_checks.py

# hanger外部確認(H1・H2)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\hanger_external_check.py --condition both

# 機械可読まとめの再生成(上記3つの出力が揃っていること)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_04_searches_json.py
```

成果物: `output/idle_validation_20260928/04_searches_compact.json`・`04_mechanism_checks.json`・
`04_hanger_external_check.json`・`04_searches.json`(機械可読まとめ)・`04_masks/`(最良形状npz)・
`04_*_checkpoint.json`(チェックポイント。再開に使える)。コードは
`topopt_checks/idle_validation/search_methods.py`(実装本体)・`run_04_searches_compact.py`・
`search_methods_mechanism_checks.py`・`hanger_external_check.py`・`build_04_searches_json.py`。
