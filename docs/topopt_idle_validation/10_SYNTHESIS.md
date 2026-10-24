# 不要材料(仕事をしていない材料)の原因検証: 統合報告

2026-10-05(`20260928-1426-topopt-idle-10-synthesis`)。小型2題材と hanger で「仕事をしていない材料」の2原因(母数の希釈・経路喪失)と改善案を検証した段階0〜10 の最終結論と、生きている根拠・再現手順。段階別の詳細は付録A〜G(旧段階文書は git 履歴)。小型モデルの定義は [00_COMPACT_MODELS.md](00_COMPACT_MODELS.md)。
**原因の説明は [TOPOPT_IDLE_MATERIAL.md](../TOPOPT_IDLE_MATERIAL.md)、探索の合否判定の決定は [TOPOPT_STRESS_GATE_DILUTION.md](../TOPOPT_STRESS_GATE_DILUTION.md) にある。**
**範囲は「期限再編(本人の 2026-10-05 指示。付録A-3)で縮小した範囲の統合と監査」で、「元の全検証の完了」でも「製品の安全の保証」でもない。** 新しい最適化・FEM は走らせていない(10 で走らせたのは設計空間だけを対象にした最小幅の芯の数え上げ〈FEMなし・各数秒〉と保存台帳の再集計だけ)。

**この後に製品が変わった点**(本文は 2026-10-05 時点のまま): 「最終ブーリアンが不成立」「hanger の最終形が作れない」は B-rep ブーリアンでの結果で、最終処理は 2026-10-06 にメッシュ集合演算へ替えた([TOPOPT.md](../TOPOPT.md)「最終処理はメッシュ集合演算」、#95)。判断1 の「全材料99.5%点の判定」は 2026-10-09 に部材域の Q_{w³} と完成形の表面の最大応力の検査へ替えた([TOPOPT_STRESS_GATE_DILUTION.md](../TOPOPT_STRESS_GATE_DILUTION.md) 10-4)。

**用語**: 「製品SF」= 製品のパーセンタイル安全率(探索の目標4.8。0.5%以内=4.776以上を達成とみなす)。「M1」= 製品の代表応力(設計空間全体=全材料セルの99.5%点)。「M3」= 部位別(保持・接続・荷重・一般)の99.5%点。「M5b raw」= 許容40 MPa を超えた分の体積積分、「M5b 目標」= 許容/4.8=8.33 MPa に対する同積分。「KS」= M7b。許容40 MPa は PETG 降伏50 MPa × 造形低減0.8([TOPOPT.md](../TOPOPT.md)「許容応力の導出」)。評価式の定義は付録B。数値の出所は `output/idle_validation_20260928/10_synthesis.json`(`derived` が台帳からの再計算)と各段の出力。

## 結論

1. **製品の既定(探索方式・合否判定)を今回の結果で変えることを支持する証拠はない。「現時点では採用しない」が結論。** 改善と見えた結果は、(a)受理ゲートが製品SFより緩いために目標SFを割った軽さ、(b)1条件・1形、(c)hanger では最終形が作れない、のどれかで、現行と同じ基準で優位と確認できたものがない。
2. **本人判断が要る3件への推奨**(詳細は次節): ①判定方式は**合否ゲートは現行のまま、結果に「局所超過の警告」を足す**(置換は推奨しない)。②節点所有権は**実装しない**。③芯が割れた入力は**入口で断る**(最大の塊だけ残す案は採らない)。
3. **評価式(原因1)**: 現行の M1 は局所の危険を隠す(hanger H1 で M1=8.29 合格・M5b=69.5 不合格、母数を20倍に水増しすると 8.271→2.0 MPa)。M5b は反例7件・34ケースで一度も見逃さなかったが、固定面から約0.5要素の位置にある**格子で位置も値も変わるピーク**に反応するため、単独の合否ゲートにはまだできない(付録B)。
4. **探索方式(原因2)**: 小型15条件で、適応・再配置が**製品SFの目標を保ったまま**製品 step20 より軽かったのは dilution 解像度2.5mm の1条件(-1.1%/-3.8%)だけ。ほかの「軽い」結果は全て SF 未達(適応・再配置とも15条件中各8件)で、残りは逆に重い(最大+168.8%)。5%歩幅は小型で-3.9%〜+6.0%、hanger では+14%重く時間は3.0倍。計算時間は適応が中央値で2.2〜2.7倍、再配置が6.2〜8.0倍(最大12.6倍)。
5. **削った長い部材の復元は確認できなかった**。歩幅・復帰では戻らず、再配置の「追加」は59件が全件、初期の全材料への復帰だった。小型 path(3000N・2.0mm)で現行より5.7%軽い優越形を作れたのは手作業の構成(移植・局所成長・仕上げ削減。付録C)だけで、自動方式としては未確立。
6. **完成形の確認は小型の2形だけ**。hanger の2形は旧 B-rep の最終ブーリアンが不成立(旧コーパス64形でも57形が最終形で失敗)。小型の2形は水密・単一ソリッド・自己交差0で、同格子の再解析は保存値とほぼ一致(SF 4.8187/保存4.8190 など)。細格子の応力は未確認(付録G)。
7. **hanger 5000N(H2)**: 製品の判定は合格(SF 4.842)だが、保持部の99.5%点は93.2 MPa(許容40の2.33倍)。**除去が作った悪化ではなく除去前の設計空間そのものの値**(全材料でも93.3 MPa、除去の影響は+0.19%)で、固定面から1.2mm(約0.5要素)にある。製品の判定が見ていないのは「局所の超過」で、その実害(実取付が剛固定でなければ値の意味が変わる)は外部入力不明。
8. **条件を変えると結論は保たれない**: 原点の半セル移動だけで小型の材料が-50.8%〜0%・hanger 単軸が-4%〜+48%、格子は hanger で 1.8/2.0/2.2/2.4mm → +2/0/-9/+27%。初期形3件(小型)はすべて目標SFを割った(付録D・E)。探索方式の差(多くが±10%台)より、格子・原点の差の方が大きい条件がある。

## 本人判断が要る3件

### 1. 製品の現行判定方式(全材料99.5%点の安全率)を変えるべきか

**事実**(台帳から再計算。`10_synthesis.json` の `derived.h2_5000N`・`derived.hanger_gates`):

| 項目 | 値 |
|---|---|
| H2(5000N・2.4mm・製品既定幅)の製品 step20 | 製品SF 4.842(合格)、材料 3,255,099 mm³ |
| 同じ形の保持部99.5%点 | **93.23 MPa = 許容40の2.33倍(降伏50の1.86倍)**、M5b raw 29,949、KS 100.8 MPa |
| 同条件の全材料(適応が20評価で0件受理=出発点のまま) | 製品SF 6.525、保持部 93.29 MPa、M5b raw 29,894(除去の影響は M5b +0.19%、M3 -0.06%) |
| 保存92形(hanger・200〜2000N)の判定 | 製品SF 92/92合格(4.800〜5.234)、M5b raw 63/92、M5b 目標 0/92、M3(目標換算) 0/92 |
| 保存92形でM3最悪値が許容40を超えた数 | 200N 0/20、500N 0/24、1000N 1/24、**2000N 22/24**(許容の2倍超は92形で0。H2 の5000N だけ) |
| 保存92形で保持部以外のゾーン最大が40 MPa を超えた数 | **1/92**(`F0200_h2.0_w5`=最小幅5セルの保存形。荷重パッド端 48.7 MPa。製品SF 4.824 で合格だった) |
| 全材料の保持部最悪値(2.0mm格子) | 500N 13.66・1000N 27.31・2000N 54.62 MPa(**27.3 MPa/kN で荷重に比例**。許容40に届く荷重の線形外挿は約1,465N、M5b raw 外挿は約1,430N〈最大セルが99.5%点より高いので先に超える〉。どちらも実走していない) |
| ピークの位置 | 4格子(1.8〜2.4mm)の step20 で保持部のピークは固定面から 0.90/1.00/1.10/1.20 mm=**要素サイズの0.50倍**。92形のうちピークが保持部にある85形では距離÷要素サイズが78形で0.45〜0.55(中央値0.50。残り7形は0〜2.1)。位置が格子に追従する=特異点の特徴 |
| 格子による値 | 全材料の保持部最悪は 2.0mm格子 27.3 MPa/kN、2.4mm格子(H2)18.7 MPa/kN。粗い格子ほど低く、固定面近傍の値は細分で増え続ける(付録A-2。同一形の細分実測ではない) |

**推奨**

1. **合否ゲートは現行(パーセンタイル安全率)のまま置換しない。** 候補はどれも単独のゲートにできない: M5b 目標(許容/4.8)は92形の保存形で92/92不合格(関門にならない)。M5b raw(tolerance=0)は固定面の特異点に反応し、外挿で約1,430N 以上(実走は2000N のみ)では**全材料自身が不合格**になるため「どの形も不合格」を返し、真の危険か特異点由来かを分けられない。パラメータ(tolerance=0、p=8 等)は未校正、独立参照(別ソルバ・局所細分)は gmsh 等が環境に無く未実装。
2. **合否は変えずに、結果に「局所超過の警告」を足す(推奨)**: (a)部位別(保持・接続・荷重・一般)の最悪値と許容40の比、(b)M5b raw 超過量(0より大きければ警告)、(c)保持部の最悪値の位置の固定面からの距離(1要素前後なら「格子で値が変わる位置」と併記。**部位別最悪の位置と全体ピークの位置は別**: 92形のうち7形は M3 の最悪部位が保持部だが全体ピーク〈最大のセル〉は保持部の外にあり、台帳の `peak_distance_to_fixed_mm` は全体ピークの距離なので、警告には部位別の位置を別に持たせる)。H2 の「合格のまま93 MPa が見えない」状況をなくせる。研究用評価器(`stress_metrics.py`)は応力場だけを入力にするため追加の FEM は要らない見込みだが、**製品の最終確定解析からその応力場を取り出す実装・コストは未測定**。
3. **ゲートへ格上げする条件**: 固定面近傍の扱い(固定面から一定距離を除く、または細分格子・サブモデルで収束する量に置き換える)を決め、最終形の細格子で確認できてから。保持部以外の M5b raw を先にゲートにする案は、保存92形で該当が1形だけで影響が小さい一方、荷重パッド端も特異点の可能性があり、この1件で格上げの根拠にはできない。
4. **入口の警告(推奨)**: 設計空間(全材料)自身が許容超過なら、探索を始める前に報告する。2000N・太い幅では全材料が出発点でゲートを満たさず、適応・再配置・拒否なし適応の3本が全候補を拒否して出発点を返しただけで 18.5〜19.1時間(再配置は4.0時間)を使った。判定は全材料の1回の FEM(約2.5分)で足りる(付録Fの出発点不成立の早期停止と同じ判定)。
5. **この推奨の限界**: 93.2 MPa が実際に危険かは、実取付(固定面が剛固定か、ボルト・接触の剛性があるか)と荷重の想定(500〜2000N・5000N が実機の値か)に依存し、検証では決められない(外部入力不明)。ここで推奨しているのは「見えるようにする」ことで、「危険と判定する」ことではない。

### 2. 確定表の節点所有権([TOPOPT.md](../TOPOPT.md)「確定表の節点所有権」)

**推奨: 実装しない(現状維持)。** 根拠: 本検証の全ケース(hanger 92形+保存形、小型2題材の全条件)で、境界条件の確定表と条件面の入口検査が通過した(`output/idle_validation_20260928/` の台帳 JSON の文字列検索で `boundary_mapping_failed`・`input_contradiction`・`resolution_insufficient` が0件。台帳に残らない失敗が無いことまでは確認していない)。TOPOPT.md の既存実測(2026-09-18)でも、hanger(2.4mm・下限1.875mm)は節点・面積とも100%がそのボディ由来で、制限を入れても `condition_mapping` は変わらない。所有権が下がるのは合成題材を1.2mm まで細かくした域で、そこでも制限後は pass のまま——**実装しても判定が変わる証拠がない**。
**測っていないこと**: 所有権そのもの(ボディ由来の節点の割合)は測っていない。小型2題材での割合は未測定。再検討の契機は、条件面を持つボディの節点が一般材料と共有される形状(薄い保持ボディなど)が出たとき。

### 3. 最小幅の規則で設計空間の芯が2つ以上に割れた入力への対処

**推奨: 入口で断る。最大の塊だけを残す案は採らない。**
**検証の結果**: 本検証の条件で割れは1件も起きていない。`check_10_core_groups.py` で、hanger(要素1.8/2.0/2.2/2.4mm × 最小幅5・10mm、2.4mm×製品既定の組を含む9条件)と小型2題材(10条件)の設計空間を最小幅の m 立方で opening して芯を数えたところ、**19条件すべて芯は1つ**(hanger で絞られたセルは20〜470個=設計空間の0.07%以下)。**割れた入力がどのくらい起きるかは本検証では分からず**、以下はコードとテストからの推奨で検証データが選んだものではない。
**根拠(コード・テスト)**: `binary_search.find_initial_feasible` は割れた設計空間を `no_feasible_found`(`min_width` 検査不合格)で返す(`tests/topopt/test_min_width_core.py` の割れた入力のテスト)ので、現状も黙って形が出ることはない。足りないのは利用者への説明で、`design_core_groups` と割れ目(首)の位置を入口のエラー文に出せば足りる。最大の塊だけ残すと、残りの塊に拘束面・荷重面の節点があれば条件が成り立たなくなり(`boundary_mapping_failed`)、利用者に見えない形で設計空間が変わる。

**本人への依頼**: 上記3件の方針の可否、および外部入力(実取付条件・荷重の想定)の提供。

## 推奨案・条件付き案・棄却案

| 区分 | 案 | 判断と根拠 |
|---|---|---|
| **推奨**(製品へ入れてよい。探索・合否は変えない) | 局所超過の警告表示(部位別最悪・M5b raw・保持部最悪値の固定面距離。全体ピークの距離とは別に持つ) | 現行の合否が見逃す93.2 MPa(H2)を見えるようにする。追加 FEM 不要の見込み(取り出しの実装は未測定) |
| **推奨**(同上) | 全材料が許容超過のときの入口警告 | 19時間の空回しを避ける。判定は全材料1回(約2.5分) |
| 条件付き | M5b raw の非保持部位ゲート | 保存92形で該当1形。荷重パッド端が特異点でないと確認できてから |
| 条件付き | 適応歩幅+予測誤差の下限(floor=0.01) | 過剰拒否は減るが、m5b_raw では製品SF目標を割る(d3 4.039・p15 4.028)。percentile ゲートの同条件では step20 より+6.0%重い(24,854対23,440 mm³)。複数条件・複数seed・hanger の追試なし(付録F) |
| 条件付き | 出発点不成立の早期停止 | 動作確認(FEM1回・2秒)のみ。研究用の安全弁 |
| **棄却** | 再配置(cap付きでも) | 同条件の適応より+11.6%重く(64,496対57,768 mm³)、予測誤差拒否が78件再発。追加は全材料復帰 |
| **棄却** | m5b_raw を単独の受理ゲートに | 50実行中27実行が製品SF目標未達(最小3.85) |
| **棄却** | 全材料開始の適応・再配置の hanger 適用 | 出発点(全材料の1.8〜1.5倍規模)から動かず SF 75〜84 で終了。2000N は出発点を返しただけ |
| **棄却**(現行より優位と確認できない) | 5%歩幅 | 小型で-3.9%〜+6.0%、hanger で+14%重く時間3.0倍(10.3時間対3.4時間) |
| **採用保留** | 手作業の構成(移植+局所成長+仕上げ。付録C) | 3000N・2.0mm・パーセンタイル限定で-5.7%。m5b_target では優劣未決着。自動化・他格子・原点は未検証 |

## 条件別の結果

### 3-1. 小型題材(15条件×4方式、各1実行)

製品 step20 に対する体積の差と製品SF目標(4.776以上)の達成。**適応・再配置の受理ゲートは m5b_raw、step20・step05 はパーセンタイルで、ゲートが違うので体積だけの順位付けはしない**。

| 条件 | step20 mm³ | step05 | 適応 | 再配置 |
|---|---:|---:|---:|---:|
| dilution 200N | 16,128 | -0.4% | +3.9% | +4.3% |
| dilution 300N・2.0mm | 88,376 | +0.2% | -34.6% **SF4.71未達** | -19.7% **SF4.77未達** |
| dilution 250N | 20,456 | -3.9% | -24.2% **SF3.85未達** | -11.8% **SF4.66未達** |
| dilution 幅8mm | 88,392 | 0.0% | -35.5% **SF4.68未達** | -20.4% **SF4.76未達** |
| dilution 1.5mm格子 | 78,956 | -0.1% | -33.3% **SF4.62未達** | -18.1% **SF4.73未達** |
| dilution 2.5mm格子 | 79,270 | 0.0% | **-1.1%(SF4.82達成)** | **-3.8%(SF4.82達成)** |
| dilution 3.0mm格子 | 23,440 | +6.0% | **+168.8%** | **+168.3%** |
| path 3000N・2.0mm | 71,928 | +0.1% | -6.6% **SF4.74未達** | -6.5% **SF4.75未達** |
| path 2700N | 53,664 | -0.4% | +12.5% | +12.9% |
| path 3300N | 108,592 | 0.0% | +47.4% | +47.5% |
| path 幅4mm | 71,856 | 0.0% | -11.9% **SF4.60未達** | -11.7% **SF4.61未達** |
| path 幅8mm | 72,608 | +1.0% | -11.0% **SF4.65未達** | -10.8% **SF4.66未達** |
| path 1.5mm格子 | 83,130 | -0.2% | **+83.3%** | **+82.9%** |
| path 2.5mm格子 | 57,425 | -0.5% | +26.5% | +23.2% |
| path 3.0mm格子 | 85,941 | -0.2% | -8.6% **SF4.62未達** | -8.3% **SF4.62未達** |

- 製品SFを保ったまま軽くなった適応・再配置は dilution 2.5mm格子の1条件だけ(`derived.small_matrix.summary`)。
- 同一条件の適応の体積は評価予算・seedで動く: dilution 基準条件で 57,768(100評価)・58,088(100評価・seed100)・78,648(40評価・seed300)mm³。未収束(`evaluation_limit`)の適応は予算で最大36%動き、表の差の一部は探索の打切りの影響で方式の性質とは言えない。
- 図: [探索中の最良材料体積の履歴](10_figures/10_history_small.png)、[小型の最終形の射影](10_figures/10_shapes_small.png)、[hanger H1 の最終形の射影](10_figures/10_shapes_hanger.png)(step20 と5%歩幅。斜材の回復は見られない)。

### 3-2. 同一条件・同一評価器での全指標(小型。hanger H1 は付録E-1-2)

| 条件 | 方式 | 材料 mm³ | 製品SF | M5b 目標 | M3最悪 MPa | KS MPa | コンプライアンス | 最大変位 mm | 時間 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dilution 300N・2.0mm・4mm | step20 | 88,368 | 4.836 | 208.9 | 9.54 | 10.63 | 327,193 | 0.318 | 25 s |
| 同 | step05 | 88,488 | 4.844 | 209.0 | 9.54 | 10.63 | 327,109 | 0.318 | 27 s |
| 同 | 適応(m5b_raw) | 52,472 | **4.604** | 250.6 | 9.81 | 10.86 | 366,549 | 0.350 | 115 s |
| 同 | 適応(percentile) | 82,680 | 4.800 | 211.6 | 9.57 | 10.65 | 330,223 | 0.318 | 58 s |
| 同 | 再配置 | 64,280 | **4.741** | 232.5 | 9.69 | 10.76 | 346,215 | 0.330 | 337 s |
| path 2.5mm・3000N | step20 | 57,425 | 4.836 | 94.9 | 8.86 | 9.50 | 1,497,232 | 0.112 | 14 s |
| 同 | 適応 | 72,520 | 5.377 | 3.4 | 7.94 | 8.58 | 1,346,817 | 0.100 | 21 s |

- 軽い形は、コンプライアンス・最大変位・部位別最悪がわずかに悪い(dilution: 適応 m5b_raw +12%・+10%)が、同じ受理ゲート内の比較ではない。path 2.5mm では適応の+26%重い形が応力指標をすべて改善している(M5b 目標 94.9→3.4)。**探索の指標(体積)が小さいことと、完成形として優れていることは別**。
- hanger H1 の適応・再配置は全材料(5.1 cm³ 規模)から動かず、step20 の13倍・11倍の材料。拒否は全件が予測誤差(05 で見つけた単点較正の暴走が hanger でも再現)。

### 3-3. hanger の荷重・幅・格子(保存92形の再採点)

| 荷重 | 形数 | 材料 mm³ | 製品SF | M5b raw | M5b 目標 | M3最悪 中央値 MPa |
|---|---:|---|---:|---:|---:|---:|
| 200N | 20 | 72,643〜249,716 | 20/20 | 19/20 | 0/20 | 13.4 |
| 500N | 24 | 117,611〜292,408 | 24/24 | 23/24 | 0/24 | 18.6 |
| 1000N | 24 | 263,877〜656,673 | 24/24 | 21/24 | 0/24 | 26.6 |
| 2000N | 24 | 769,738〜1,063,483 | 24/24 | **0/24** | 0/24 | 47.0 |

- 製品SFと「M5b raw=0」を同時に課す複合ゲートなら92形のうち63形が通る(2000N は0/24)。複合ゲートを探索の受理に使った結果は未検証。
- 製品SF 合格は片側基準(SF≥目標×0.995): 92形のうち64形が目標の+0.5%を超えて高く(最大5.23)、過大設計側も合格に数えている。
- 方式比較(2000N・太い幅・2.0mm相当。ゲートが違うので順位付けしない): step05(percentile)909,886 mm³・38評価・1.13時間。適応・再配置・拒否なし適応は**出発点の全材料(5,098,682 mm³)を返しただけ**(受理0/300等)で 18.53・3.98・19.08時間。2000N 細い幅の適応は約9.7時間走って台帳に書かず終了(exit=-1。原因不明・lost)。
- 保存形の再現: 旧64条件の再採点は64/64でSF・材料体積が一致。旧64条件(セル数指定)と新28条件(実寸mm指定)は別表で混ぜない。

### 3-4. 完成形

小型の2形(S1 dilution 2.5mm・再配置、S1c 同条件 step20)は最終形が成立し、同格子の再解析が保存値とほぼ一致(SF 4.8187/4.8190、4.8357/4.8286。M5b raw 0)。hanger の S2(200N・step20)・S3(2000N・2.2mm・step20)は最終ブーリアンが不成立で、保持を足し戻した近似の再解析でも S2 は M5b 目標・M3 不合格、S3 は M5b raw(118.5)・M5b 目標・M3 のどれも不合格(出発点の全材料自身が不合格)。平滑化後の最小幅は hanger で要求を満たすと言えない(S2 は10.0mm立方で体積の3.25%、11.98mm で39.9%を失う)。詳細と薄い破片の原因は付録G。

### 3-5. 原因別の機構と交互作用

- **評価式の効果**: 現行 M1 は局所を隠す。評価式を換えると合否の意味が変わり(M5b 目標は92/92不合格)、体積は増えるか不合格になる。**評価式の問題は「判定の盲点」で体積節約の源ではない**。受理ゲートを percentile→m5b_raw に変えると適応が dilution で 82,680→52,472 mm³(-36.5%)になるが製品SFは 4.800→4.604: **軽さはゲートの緩さから来る**。
- **探索方式の効果**: 歩幅(5%)・復帰・再配置のいずれも、削った部材を戻していない。hanger の5%歩幅の「-38%」は最小幅5セル(実現9.99mm で要求10mm に0.013mm不足)の保存形同士の比較で、要求を満たす6セルでは step05 が step20 より+14%重い。
- **交互作用**: ゲートを緩めると探索は軽い形を見つけるが局所超過・SF 未達を伴う。製品どおりに戻すと優位は消える(dilution 300N・4mm の適応 percentile は 82,680 mm³・SF 4.800 で step20 の88,368 より-6.4%だが、dilution 3.0mm では+6.0%など混在)。**方式の効果は条件(格子・原点・荷重)で符号が変わる**ため一般則として言えない。

## 計算費用とメモリ

| 項目 | 値 |
|---|---|
| 小型(2.0mm・約2万要素)の step20 | 11〜147秒(多くは30秒前後)。適応 約2.2〜2.7倍、再配置 約6〜8倍 |
| hanger H1(200N・2.0mm・64万要素)の step20 / step05 / 適応(50評価) / 再配置 | 3.4時間(254 FEM) / 10.3時間 / 1.7時間 / 3.5〜8.2時間。1FEM は step20 約48秒・適応約114秒 |
| hanger 新28条件 step20 | 計17.2時間(0.21〜3.04時間)、**ピークのワーキングセット最大16.9 GB**(1.8mm格子。200N 細い幅) |
| 2000N 方式比較(出発点を返しただけ) | 適応18.5時間・拒否なし19.1時間・再配置4.0時間・step05 1.1時間 |
| 全材料の1回の FEM | 約2.5分(500N・1000N) |
| 06 全体 / 07 methods | 約59時間(探索19本) / 約44時間(5本) |

hanger で1回の探索が3.4〜10時間(新方式は全材料開始で19時間級)・16.9 GB 級のメモリ。新方式が速くなる証拠も体積が減る証拠もなく、費用対効果は現行を上回らない。

## 最悪の悪化・ばらつき

- 製品 step20 自身の失敗: hanger H1 を全軸+0.5セルずらすと探索は22,915セル・SF 4.812 まで進んだが、終了時の再検算で共通体積ブーリアンが `unknown` になり全材料(5,127,648 mm³、基準の約37倍)へ戻った(付録E-1-3。原因は原点位置と形の組合せかライブラリ側か未分離)。
- 新方式の最悪: dilution 3.0mm +168.8%、path 1.5mm +83.3%、hanger 全材料開始 +1,010〜1,199%、粗い骨組み開始の SF 4.459(目標未達)。
- ばらつき: 各条件1実行(seed固定)。同一条件の適応が評価予算で 57,768〜78,648 mm³。パラメータ感度は dilution で無感度、path の `prediction_error_multiplier` で14.4%。**別seed・別乱数での再現は未検証**。

## 検証の範囲(工程別)

| 工程 | 状態 | 付録 |
|---|---|---|
| 00 小型題材 / 01 プロトコル / 02・03a 参照解析 | 完了(独立ソルバは環境制約〈gmsh 等なし〉で未実装) | 00_COMPACT_MODELS・A |
| 03・03b 評価式 | 完了(M5b 本体・M3 診断・M7b 補助) | B |
| 04・04a 探索実装・原因2認定 | 完了(04a の範囲限定) | C |
| 05 小型の機構別比較 | **縮小**(小型15条件×4方式。5合成題材は延期) | D |
| 06 hanger 低荷重 | 完了(新規最適化は代表条件) | E-1 |
| 07 hanger 高荷重 | **縮小**(保存台帳の集計+固定形状診断。methods 19・origin 15・init 8 は延期) | E-2 |
| 08 小型の重点検査 | **縮小**(小型8実行) | F |
| 09 完成形 | **縮小**(保存形3形+対照1形。hanger の最終形は不成立) | G |

## 未検証と次の研究案

延期項目: dilution level4(0.5mm、約126万要素 FEM)の等比検定、荷重端 48.72 MPa の形と離散化の寄与の分離(保存形を細分格子で解き直す検証)、04a の原点・他格子・2700N/3300N の因果検査、hanger 規模の再配置、06 の凍結パラメータ変更・別seed・原点フル・6セル形の軌跡、07 の methods 残り19・origin 15・init 8・2000N 細い幅(lost)、08 の percentile floor 対照・再配置 cap+floor・再較正・複合ゲート・早期停止の修正前対照・floor 感度、09 の最終CAD細格子 FEM・hanger 最終ブーリアン修正・`F0500_h2.2_w5`/`F1000_h2.2_w5` の最終形。未検証を合格として扱わない。**自動では queue に積まない。** 採用判断に影響する順の研究案:
(a) 固定面近傍のピークを格子で収束する量に置き換える検討(局所細分・サブモデル・固定面から一定距離の除外)と、それで M5b raw を格上げできるかの再検証。
(b) hanger の最終ブーリアンの薄い破片への対処(2026-10-06 に最終処理をメッシュ集合演算へ替えた。TOPOPT.md)と、最終CADを細格子へ占有化した応力確認。
(c) 同ゲート(percentile)で適応・再配置の効果を小型の複数条件・複数seedで追試。
(d) 実取付条件(剛固定か)と荷重の想定の確認。

## 索引・再現

- 機械可読まとめ: `output/idle_validation_20260928/10_synthesis.json`(`derived`=台帳からの再計算、`audit`・`recommendations`・`coverage`・`limitations`・`deferred_by_user_deadline`)。芯の数え上げ: `output/idle_validation_20260928/10_work/core_groups.json`。`output/` は `.gitignore` で追跡されない(再生成コマンドで再現)。
- 図: [10_figures](10_figures/)(履歴曲線・小型の最終形・hanger H1 の最終形)。再生成は `render_10_figures.py`。
- コード(`topopt_checks/idle_validation/`): `build_10_json.py`・`synthesis_10_static.json`(判断の手書き部分)・`check_10_core_groups.py`・`render_10_figures.py`。製品コード(`duocad/`)は無変更。10 の台帳の `code_version` は 09 のコミット 8920b85 の上に10の追加ファイルを足した版。
- 再現コマンド(リポジトリ直下で):
  - `.venv\Scripts\python.exe topopt_checks\idle_validation\check_10_core_groups.py --case hanger:2.0:10 --case dilution:2.0:4`
  - `.venv\Scripts\python.exe topopt_checks\idle_validation\build_10_json.py`
  - `.venv\Scripts\python.exe topopt_checks\idle_validation\render_10_figures.py`
- 監査の要点: 台帳から再計算して報告と一致を確認した(07の荷重別ゲート件数・04 H2 の SF 4.842/93.23 MPa/29,949・04a の-5.7%・05 の15条件×4方式・08 の d3 -64.6%/p15 -64.3%/SF 4.039/4.028・09 の再解析・03a の荷重スイープと曲げ誤差・06 H1 の所要時間)。再計算していないのは 08 の監査件数(`08_robustness.json` の `audit_summary` を採用)と 07 の再採点の再現(07 の台帳を採用)。QA は別文脈のレビュアー(`composer-2.5`)で4ラウンド、指摘は全て MINOR で修正済み(図の目視・再実行系は未検証のまま)。

---

# 付録A: 検証の土台(プロトコル・参照解析・訂正)

出典: 旧 01_PROTOCOL・02_REFERENCE・03A_REFERENCE_REPAIR・REVIEW_00_02・DEADLINE_PLAN(git 履歴)。

## A-1. 共通プロトコル

- **`acceptance.inspect` の6条件は最小幅を含まない**。最小幅は探索時の peel でだけ強制されるので、別の形から写した形は `binary_search._mask_satisfies_min_width` を別途呼ぶ(hanger の移植トラス 198.4 cm³・SF 8.869 は6条件 pass だが最小幅5セル〈9.99mm〉で不合格。再現: `reproduce_stage1_transplant.py --src F0200_h2.2_w5 --dst F0200_h2.0_w5 --dilate 1`、棚卸しは `inventory_stage1_shapes.py`)。
- **比較の単位**: 旧64条件(`output/smoothing_corpus/`)はセル数指定(`min_width_cells` 一定)で凍結。新規は mm 固定(`min_width_mm`)で `min_width_cells_resolved`・`min_width_guaranteed_mm`・`element_size_resolved_mm` を必ず記録し、両者は混ぜない。複数解像度の比較は最も粗い格子が決める共通実幅(`_resolve_min_width` の下限 `max(2×要素サイズ, 1mm)`)。
- **許容差は製品既定をそのまま使う**(検証用の閾値を作らない): `fem.SOLVE_RESIDUAL_TOL`・`stress.DEFAULT_PERCENTILE`・`binary_search.DEFAULT_MAX_STEP_RATIO`・`ENERGY_FILTER_SIZE`(3セル。物理幅は格子ごとに変わる)。
- **共通基盤** `topopt_checks/idle_validation/_common.py`(git 状態・sha256・アトミック書き込み・ピークメモリ・OS 排他ロック `ExecutionLock`・条件台帳 `upsert_case`/`is_case_done`・`SingleFemRun`)。各スクリプトはコピーせず import する。未実行の条件は `status=missing` のまま pass にしない。
- 半セル原点移動は製品の第一級パラメータでない(`design_space.derive` が原点を計算する)。実験はプロセス内パッチで行い達成シフト量を記録する。5合成題材は構築せず小型2題材の15条件で代替した。

## A-2. 参照解析(固定形状の格子細分・解析解)

再現:
```
.\.venv\Scripts\python.exe topopt_checks\idle_validation\refine_shape.py --shape dilution_r-1 --levels 1 2 3 --loads 200 300
.\.venv\Scripts\python.exe topopt_checks\idle_validation\refine_shape.py --shape path_2.5mm --levels 1 2 3 4 --loads 1500 6000
.\.venv\Scripts\python.exe topopt_checks\idle_validation\refine_shape.py --shape hanger_base --levels 1 --loads 200
.\.venv\Scripts\python.exe topopt_checks\idle_validation\refine_shape.py --shape hanger_r2 --hanger-condition F2000_h2.4_w2 --levels 1 --loads 2000
.\.venv\Scripts\python.exe topopt_checks\idle_validation\analytic_checks.py tension --levels 1 2 3 4
.\.venv\Scripts\python.exe topopt_checks\idle_validation\analytic_checks.py bending --levels 1 2 4 8
.\.venv\Scripts\python.exe topopt_checks\idle_validation\load_sweep_full_material.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_02_reference_json.py   # refine_*/analytic_* を集約。個別再実行後は最後に必ず再実行
```
入力は保存マスク `output/idle_validation_20260928/dilution_mask_r-1.npz`・`path_mask_mesh_2.5mm.npz`(無ければ 00 の再現コマンドで再生成)と hanger の `01_stage1_shapes.json`。

**落とし穴**
- `refine_shape.py --shape hanger_r2` の既定条件は `F0200_h2.4_w2`(200N)。H2 は `--hanger-condition` を明示しないと別条件を静かに実行する。
- `--exact-subdivide` の検証は `--out-dir` で別ディレクトリを指定する(既存の `refine_*.json` を上書きした事故があった)。
- `stress.RepresentativeStress.raw_max` は空セルを含む全要素の最大(材料外の数値産物)、`dense_max` が材料内最大。**raw_max は評価に使わない**。
- 細分で体積が変わる(88,368→88,013 mm³)のは `design_space.derive` が spacing ごとに外枠を再計算し、外枠が level 倍より `2(level-1)` セル小さくなって形の 355 セルがはみ出すため。製品の既定挙動なので実験側で吸収する。
- 曲げパッチテストは FEM が要素中心を読むので理論値も採点位置に合わせる(`σ = P(80−h/2)(10−h/2)/I`)。非等比細分に等比用 Richardson 式を当てない(`_fem_probe.richardson_extrapolate_general`)。

**結果**
- 一様引張は解析解と相対誤差 1e-11〜1e-16 で一致、曲げ(位置補正後)の Richardson 外挿は理論 24.0 MPa の 0.075% 以内(p≈1.03)。FEM 実装は正しい。
- **保持部の点応力は格子細分で増え続け、接続部・一般部は収束する**(dilution 300N の保持部 max 10.45→15.11→18.06 MPa、path の保持部 max は level1→4 で 2.48倍、接続部 1.07倍)。拘束面の階段角の特異点で、一般化 Richardson でも holding max と raw_max は解なし(解なしは発散の証明ではない)。
- 製品の代表応力(99.5%点)は細分で 1〜10% ドリフトする(dilution 300N 8.271→7.997→7.555 MPa)。2.4 係数はこれを吸収するが点maxの発散は吸収しない。
- **固定物理体積の局所平均は点maxが発散する場所でも安定**(dilution の holding ピークまわり半径2〜9mm の平均は3段細分で ±1.7〜6%。点max +73%)。
- Kirsch/Peterson 近似は独立検証として機能しない(kt_fem 19.9〜33.6 vs 理論 2.304)。独立ソルバ(gmsh 等)は環境に無く未実装。
- hanger H1 の **48.72 MPa は荷重パッド接触端**(荷重面から4.99mm)の集中。`_fem_probe.zone_masks` の3ゾーンに荷重ゾーンが無く一般部(29,224セル)へ希釈され、部位別99.5%点(7.31 MPa)に現れなかった。

**訂正済みの値**: 全材料 dilution の荷重スイープは SF×荷重 = 1676.2988934 で厳密に反比例。00 に書かれていた 200N:4.77・250N:5.97 は誤りで正しくは 8.38・6.71。

**ゾーン定義**(`_fem_probe.zone_masks`): 保持部=拘束面 bbox から D 以内、接続部=拘束面重心↔荷重面重心の線分から D 以内、一般部=残り(D の既定は格子幅の3倍)。幾何位置だけで決まるので荷重端のような集中を見逃す。

## A-3. 検証範囲の縮小(2026-10-05 本人指示)

本人の「あと2-3日でおわるように組みなおして」に基づき、以前の費用・時間無制限・全行列の指示より優先して縮小した(納期目標 2026-10-07 18:00・最終 10-08 18:00)。縮小後の規則: 重い FEM は1プロセス系列ずつ、子計算に OS プロセス単位の wall timeout(小型1ケース20分・hanger 固定形状1ケース45分)、hanger の全材料開始の新規最適化・全行列・未完チェーンの再起動は禁止、小型探索の比較は同じ格子・実現幅・荷重・評価ゲート・初期形・停止条件で行い体積だけで順位付けしない、未実施は `deferred_by_user_deadline` で一覧化して pass にしない。工程別の縮小内容は「検証の範囲(工程別)」節。

---

# 付録B: 評価式(応力集約指標)の比較と採否

出典: 旧 03_METRICS・03B_METRICS_RECHECK(git 履歴)。コードは `topopt_checks/idle_validation/stress_metrics.py`(候補式。von Mises 配列+マスクを受ける純粋関数、FEM は呼ばない)・`metric_eval.py`(形状→FEM解→ゾーン付き生の場)。

## B-1. 候補式の定義

全候補は `material_mask`(またはそのゾーン部分集合)だけを母集団にし、`raw_max_mpa` は入力に使わない。許容は `cond.allowable_mpa`=40 MPa。

| ID | 関数 | 定義 |
|---|---|---|
| M1 | `full_population_percentile` | `percentile(v[material], 99.5)`(現行製品の `stress.representative_stress`) |
| M2 | `excluded_population_percentile` | 保持部を除いた母集団の99.5%点 |
| M3 | `zone_percentile` | holding/joint/general 各ゾーンで99.5%点を取り、全ゾーン合格で合格 |
| M4a / M4b | `top_volume_rank` / `top_volume_average` | 応力降順で物理体積 `V_top` に対応する `k=round(V_top/cell³)` 番目の値 / 上位k個の平均(窓 8mm³ と 216mm³) |
| M5a | `exceedance_volume` | `count(v>allowable)·cell³` |
| M5b | `exceedance_integral` | `Σ(v_i−allowable)·cell³`(超過セルのみ。tolerance=0=1セルでも超えたら不合格) |
| M6 | `p_mean` | `mean(v^p)^(1/p)` |
| M7a | `p_norm_unnormalized` | `sum(v^p)^(1/p)`(`vmax` でスケール) |
| M7b | `ks_aggregation` | `vmax + (1/ρ)ln(Σexp(ρ(v_i−vmax)))` |

p=8、ρ=1.0(1/MPa)、tolerance=0 は研究用の初期値で較正していない。

## B-2. 採否(結論)

- **M5b(超過量積分)が合否の本体**: 反例7件・34実形状ケースで一度も危険を見逃さず、母数・窓・ゾーンのいずれの希釈にも強い。**ただし粗格子の合格は安全の証明でない**(path 6000N の固定形状細分で level1〜3 は M5b=0、level4〈0.6176mm〉で拘束近傍の8セルが超過して M5b=5.91)。
- **M3(部位別99.5%点)は診断の内訳に保持**: ゾーンが数百セル以上あると内部で同じ希釈を再現する(許容の1.25倍をどのゾーンの1セルへ注入しても3ゾーンとも合格のまま)。部位別分割は必要条件だが十分条件でない。
- **M7b(KS)は探索時の補助指標**(M5b は超過が無い限り勾配を持たない)。合否には使わない。M7a は「個々には合格だが多数がやや高い」形を過剰に不合格にする。
- **不採用**: M1 は母数希釈と局所危険の見逃しで単独不可。M2 は保持部を除く設計が保持部の危険を隠す。M4 は窓より小さい局所超過を見逃す(80 MPa の1セルを 216mm³ 窓で取ると 8.70〜11.82 MPa)。M5a は深刻さを区別しない。M6 は N で割る構造が希釈と同型(20倍水増しで-31%)。いずれもパラメータで救えない。
- 独立解析による局所評価(別ソルバ・局所サブモデル・conformal メッシュ)は gmsh 等が環境に無く未実装。

## B-3. 主要な実測

- **母数希釈(dilution_r-1・300N、遠方の 2.0 MPa セルを足す)**: 20倍水増し(総母集団231,966)で M1=8.271→2.0(-76%)、M2 も同一、M6 -31%、M7a +0.23%、M7b +9.5%。hanger_base でも M1 -76%、M7a/M7b ±0%。
- **同じ実形状が指標だけで合否逆転**:

| 形状・条件 | 製品SF | M1 | M3 | M5b | M7b |
|---|---:|---|---|---|---|
| hanger H1(200N) | 4.824 | PASS 8.29 | PASS 15.9 | **FAIL 69.5** | FAIL 48.7 |
| hanger H2(2000N) | 2.727 | PASS 14.7 | FAIL 42.5 | FAIL 317.5 | FAIL 48.0 |

  H1 は一般部に荷重端 48.72 MPa のピークがあり M3 の一般部99.5%点(7.31 MPa)に現れない。M5b だけが両方を捕捉した。
- 現実的な材料編集(一般部の低応力20%除去/1セル膨張)では全指標が1〜5%しか動かず合否も反転しない。格子原点 ±0.5セルで dilution は2〜3%、path は M3 +21.6%・SF -12.7%。
- hanger_r2(保持部除外の探索結果)は全母集団 M1 で SF 3.726(探索時の除外母集団評価では 4.84)——**探索目標の達成は評価式次第**。

## B-4. 再現と落とし穴

```
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_metric_comparison.py            # 34ケース×10候補 → metric_comparison.json
.\.venv\Scripts\python.exe topopt_checks\idle_validation\counterexamples_field_only.py       # 反例 a/d/e(場だけ)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\counterexamples_material_edit.py    # 反例 b
.\.venv\Scripts\python.exe topopt_checks\idle_validation\counterexamples_origin_shift.py     # 反例 g
.\.venv\Scripts\python.exe topopt_checks\idle_validation\counterexamples_hanger_extension.py # a/e を hanger 実形状へ
.\.venv\Scripts\python.exe topopt_checks\idle_validation\hanger_common_condition_scoring.py  # 現行/R2/5%/移植の共通条件採点
.\.venv\Scripts\python.exe topopt_checks\idle_validation\level4_exceedance_locality.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\keep_islands_volume_contribution.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_03_metrics_json.py   # 集約 → output\idle_validation_20260928\
```
- 最小幅の判定は `binary_search._min_width_exempt(cond, shape, 5)` 経由で保持セルを規則の対象外にする。`_mask_satisfies_min_width(mask, 5)` を exempt なしで呼ぶと現行形自身が不合格になる。
- `rss_delta_mb` は前後差分でありピークメモリではない(ピークは `tracemalloc` の `traced_peak_mb`)。

---

# 付録C: 4探索方式の実験用実装と原因2の認定

出典: 旧 04_SEARCHES・04A_PATH_QUALIFICATION(git 履歴)。

## C-1. 実装(`topopt_checks/idle_validation/search_methods.py`)

- 入口: `run_fixed_step`(20%/5%。製品 `binary_search.find_best_feasible` を無変更で呼ぶ薄いラッパー)・`run_adaptive`・`run_adaptive_from_mask`(既存形から削減を続ける)・`run_reallocation`。評価器は `score_mask`(1回のFEMで M5b raw/target・M3・M7b・compliance・要素ひずみエネルギーを返す)。除去側の幾何検査は製品の `resolve_islands`/`_geometry_rejection`/`peel`/`ranked_cells`/`_min_width_exempt` を呼ぶ。追加側(`_growth_geometry_ok`)は `acceptance.inspect` が「材料が before を超えない」前提で使えないため個別チェックに最小幅・閉空洞禁止・芯の連結を足す。
- 検算: step20 を dilution_small で走らせると 88,368 mm³・SF 4.836・18評価で製品の既知値とビット一致。機構検査(`search_methods_mechanism_checks.py`)8項目 PASS。
- **受理ゲート `AdaptiveConfig.gate`** は3択で、測る安全の定義が違うので体積だけで優劣を付けない: `m5b_raw`(既定。許容40 MPa=SF≈1 を割らないことだけを見る最も緩いゲート)、`m5b_target`(許容/目標SF=8.33 MPa を閾値にした版)、`percentile`(製品の99.5%点ゲートそのまま)。dilution_small の全材料形(SF 5.59)でさえ `m5b_raw` は合格、`m5b_target` は不合格。
- 再配置の追加候補は「初期全材料 ∪ 実行内履歴 ∪ 外部候補」から現在形を引いた連結成分のうち現在形に隣接するものを長い順に提案する(`_connected_remnants`)。**成分は分割せずまるごと足す**(セル数で切ると部材と非連結になり幾何検査でほぼ必ず落ちる)。`grow_accepted` の直後に必ず `poison_invalidated` を記録する。
- **罠**: 歩幅の下限 `min_step_ratio` を後退倍率にも床として適用すると `peel` が毎回同じ候補を返して無限ループになる。床は目標歩幅(`gap_ratio`)だけに敷く。
- `finish_patience`(改善停滞判定)は評価数を大きく減らす(dilution の percentile 対照 120→56 評価、path の適応 120→43)。

## C-2. hanger 外部確認(H1=`F0200_h2.0_w5`、H2=`F5000_h2.4_w2`、`hanger_external_check.py --condition both`)

| 条件 | 設計要素 | 現行: 材料 mm³ | SF | M5b raw | M3最悪 | 適応(m5b_raw、20評価) |
|---|---:|---:|---:|---:|---:|---|
| H1(200N・2.0mm) | 640,306 | 249,716(保存形 `hanger_base` の再スコア) | 4.824 | 69.5 | 15.9 MPa | 0件受理(不変) |
| H2(5000N・2.4mm) | 367,390 | 3,255,099(製品を新規実行。10評価で converged) | 4.842 | **29,949** | **93.2 MPa** | 0件受理(全材料5,118,824・SF 6.525のまま) |

- 現行が局所超過を持つ状態からの純粋な除去では超過を解消できない(→ 本人判断1)。
- `hanger_base` を H2(2.4mm)へ投影すると連結成分が条件面を失う(`resolve_islands` が17,811セルの成分を落とす)ので、H2 の「現行」は製品を H2 条件で新規実行した結果にした。H1 の全材料1回の FEM は約157秒。

## C-3. 原因2の認定(path_small・2.0mm格子・パーセンタイル評価シナリオに限る)

2.0mm 格子(現行 step20 が経路を失う格子)で「同格子・同評価器・同実現最小幅(6.0mm=3セル)で合格する、現行より軽い形」を手作業の構成で作れた(`qualify_path_small.py`): (A)移植: 2.5mm step20 の採用マスクを 2.0mm へ最近傍で射影し `binary_closing` 2回+`resolve_islands`、(B)局所成長: 材料全体を軸方向へ1セルシフトして重ねる貪欲選択(毎ラウンド6方向を `_growth_geometry_ok` で検査しパーセンタイルSFを最も改善する1方向を採る)、(B')仕上げ削減: `run_adaptive_from_mask(gate="percentile", stop_policy="adaptive_finish", max_step_ratio=0.05)`。

| 荷重 | 現行 step20 | 優越形 | 軽量化 | 成長ラウンド | 仕上げ評価数 |
|---:|---:|---:|---:|---:|---:|
| 2700N | 53,664 mm³(SF 4.845) | 51,000(4.800) | -5.0% | 3 | 91 |
| 3000N | 71,928(4.842) | 67,800(4.800) | -5.7% | 3 | 30 |
| 3300N | 108,592(4.846) | 106,248(4.800) | -2.2% | 13 | 34 |

- 優越形は6条件と最小幅の全てに合格。**m5b_target では現行・優越形とも不合格**(3000N)で、優越形の優位はパーセンタイル評価に限る。
- 因果検査(3000N): 優越形に固有の605セル(corridor)を保護しても製品の `_reduce_volume` は 71,544 mm³ で優越形(67,800)に届かず、最初の1手だけ禁止して再開すると corridor 保持率は8.4%まで下がる。**経路喪失は特定の1手の誤りではなく、ひずみエネルギー順位で削る現行アルゴリズムの構造的な傾向**。歩幅 0.05 でも変わらない。
- 未検証: 原点シフト頑健性、2.0mm 以外の格子、他荷重での因果検査。軸方向シフト成長はこの形状(細い部材が支持点近くで太いパッドへ接続)で有効だった手法で、hanger 等で同じ効率が出る保証はない。
- **不採用の構成法**: 孤立点・表面セル単位の追加と最悪応力点中心の立方体膨張は、最小幅3セルの離散化では `min_width` 検査で落ちるか非効率。仕上げに `stop_policy="legacy"` を使うとオーバーシュートが小さいとき何も削れないので `adaptive_finish` を使う。
- 実装上の契約: `peel` の m>1 では `protected` は `exempt` の部分集合であることが不変条件(`extra_protected` は `exempt` にも OR する。渡すセルは別経路で最小幅確認済みであることを呼び出し側が保証する)。`on_accept(i, mask, score)` で受理ごとの途中マスクを取れる。

再現:
```
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_04_searches_compact.py            # 4方式×2題材の主実証 → 04_searches_compact.json
.\.venv\Scripts\python.exe topopt_checks\idle_validation\search_methods_mechanism_checks.py     # → 04_mechanism_checks.json
.\.venv\Scripts\python.exe topopt_checks\idle_validation\hanger_external_check.py --condition both
.\.venv\Scripts\python.exe topopt_checks\idle_validation\qualify_path_small.py                 # 3000N 主実証+因果検査(約190秒) → 04a_qualification_run.json
.\.venv\Scripts\python.exe topopt_checks\idle_validation\robustness_path_qualification.py       # 2700N・3300N → 04a_robustness_loads.json
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_04a_json.py                       # 前2つを集約 → 04a_path_qualification.json
```
`qualify_path_small.py` は `path_mask_mesh_2.5mm.npz` が前提(無ければ `measure_path_local_optimum.py mesh --sizes 2.5`)。

---

# 付録D: 小型題材での機構別比較(05)

出典: 旧 05_SYNTHETIC(git 履歴)。15条件×4方式の体積・SF の表は 3-1 節。題材は dilution_small(300N・2.0mm・最小幅6mm 基準)と path_small(3000N・2.0mm・6mm)。製品コードは無変更。

## D-1. 結論

- 4方式は明確に区別できる(同一条件内で材料差最大 168.8%)ので追加方式は要らない。**方向は荷重・幅・解像度のどの軸を動かしたかで符号が変わり、新方式の一般的優位は言えない**。
- **適応方式の一次予測係数 `calibrated_k` は最初に受理された1手だけで較正され以後固定されるため、粗い格子・小さい候補セル数の条件で予測誤差ロールバックが暴走する**(dilution 3.0mm は序盤 1,011 セル除去で校正した後、終盤の2〜4セルの候補が全て `prediction_error` で拒否され続ける)。単位エネルギーあたりの安全率変化が序盤と終盤で大きく違うのに単一の線形係数で予測するため(→ 08 で floor を試験)。
- **`m5b_raw` ゲート単独では製品の目標SF(4.8)を守れない**(dilution 250N は SF 3.850)。hanger で使うなら `m5b_target` か `percentile` との併用が要る。
- **再配置の「追加」は実質「全材料形からの再削減」**: 51件の `grow_accepted` すべてで追加候補は `initial_all_material` 由来の1連結成分(例 dilution 200N は 16,936→157,376 mm³)。`grow_fraction_of_removed`/`grow_min_cells` の調整では直らない。
- **原点の半セル移動で材料が大きく動く**: step20 の8条件(基準+各軸±0.5+全軸+0.5)で dilution は基準以外の7条件すべてで位相が変わり材料 -50.8%〜0%、path は -20.6%〜+7.4%。適応でも dilution +29.7%、path -15.3%。
- フィルタ幅の mm 固定とセル数固定(`ENERGY_FILTER_SIZE`)は 2.0mm 以外で材料量の差 2% 未満だが mask_hash は異なる。体積だけで「同じ結論」と判断しない。

## D-2. アブレーション(baseline: dilution 300N/2.0mm/4mm、path 3000N/2.0mm/6mm、評価予算100)

- dilution では復帰(revert)が既定パラメータで一度も発動せず、適応のみ・+復帰・+再配置が同じ 58,088 mm³・SF 4.722。path では 52回の rollback・7回の revert_restart が発動し SF 4.148(目標未達)→4.741 に回復させた(+36.4% 重い)。
- ゲートの違いが効くのは `stop=adaptive_finish` のときだけ(percentile は 4.800 で止まり、m5b_raw はそれを割る)。`stop=legacy` は製品 step20 と酷似(88,368 / 4.836)。
- 巻き添え除去(連結性維持のための要求外セル)は dilution 100手中43手(平均0.8セル)、path 48手中21手(平均1.2)で、急変の原因になっている証拠はない。

## D-3. パラメータ感度(評価予算70)と warm-start

- `stagnation_patience`・`prediction_error_multiplier`・`sudden_jump_relative_drop` を3値ずつ: dilution は全て無感度、path は `prediction_error_multiplier` のみ感度あり(体積スプレッド14.4%)。既定値(6・2.5・0.15)を疑う破綻は出ず**据え置き**。
- warm-start(m5b_raw ゲートの適応): 3.0mm からの投影・04a 優越形(67,800 mm³・SF 4.800)のどれも目標SFを割るまで削った(SF 3.899・4.562・4.479)。**安全な初期形状から始めても m5b_raw ゲートが削り過ぎる**ので、warm-start で経路喪失を避けられるかには「否」。
- **罠**: warm-start の投影は目的格子の origin ではなく**投影元の格子自身の origin/spacing** を使う(2.0mm と 3.0mm の origin を取り違える事故があった)。

## D-4. 未検証

late_response・fine_grained_creep は 0 件(42件中)だったが、`m5b_raw` ゲート下では受理手が構造上超過 0 なので観測されないだけ。`calibrated_k` 暴走が hanger 規模で起きるかは未検証(→ 06 で再現した)。

## D-5. 再現

```
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_main_matrix.py     # 15条件×4方式(--only <case_id...>、--smoke)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_ablation.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_sensitivity.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_grid_origin.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_05_initshape.py       # main_matrix の dilution_res3.0_w6 のマスクに依存
.\.venv\Scripts\python.exe topopt_checks\idle_validation\analyze_05_mechanism_history.py   # 新規FEMなし
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_05_synthetic_json.py
```
出力は `output/idle_validation_20260928/05_*.json`(`05_synthetic.json` が集約)と `05_masks/`。共有ヘルパーは `synthetic_matrix_support.py`。

---

# 付録E: hanger の外部確認

## E-1. 低荷重(06。H1=200N・要素2.0mm・最小幅は実寸10mm 固定)

出典: 旧 06_HANGER_LOW(git 履歴)。「製品」は `binary_search.find_best_feasible`(20%歩幅)。

### E-1-1. 結論

1. **保存形 `hanger_base`(31,337セル・249,716 mm³)は最小幅5セル(実現9.987mm)で今のコードを走らせると1セルも違わず再現する**。要求の実寸10mm 以上(=6セル、実現11.98mm)にすると同じ製品コードが 139,230 mm³(-44%)・253評価の別の形で止まる(保存形との重なりは新形の60%)。**最小幅が約1セル変わっただけで収束先が別物になる**ので、保存形に基づく評価(03/03b/04)は「最小幅が実寸10mm に0.013mm足りない形」の結果である。
2. **48.72 MPa の荷重パッド端ピークは最小幅5セルの保存形にだけある**。最小幅6セルの新規 step20・解像度1.8/2.2/2.4mm の形には出ず(load ゾーン最大 6.1〜21.4 MPa)、新規形のピークは固定面から1〜1.7mm の holding ゾーン。形の寄与と離散化の寄与は分離していない。H2 の 93〜100.8 MPa は固定面近傍のピークで荷重パッドではない。
3. **斜材の回復は4方式のどれでも確認できなかった**。長予算(適応300評価・再配置60評価×3サイクル)でも全材料開始の適応・再配置は step20 に届かず(1.81M/1.56M mm³・SF 84/76)、`prediction_error` 拒否なしでも 557,596 mm³・SF 30.70(step20 の4.0倍)。**長予算の3形は重い代わりに局所応力が低いだけで、「軽くて局所応力も低い」形ではない**。
4. **探索結果が格子の位置・解像度に強く依存する**(E-1-3)。収束した8形はいずれも製品SFは目標4.8を満たしながら M5b target が 1,140〜3,564 超過(原因1の再現)。
5. 05 の弱点2つが hanger で再現した: (a)`prediction_error` ロールバックの暴走(実変化が相対 5.6e-3〜7e-8 でも拒否)、(b)再配置の「追加」は全て `initial_all_material` への回帰。

### E-1-2. 4方式の比較(H1・最小幅6セル)と保存形の再現

| 条件 | 評価 | 停止 | 材料 mm³ | SF | M5b_target | M3 holding | KS | 最大変位 mm | トラス残存 | 時間 h |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| step20 | 253 | converged | 139,230 | 4.842 | 2,212 | 20.78 | 24.96 | 2.763 | 18.4% | 3.4 |
| step05 | 297 | converged | 159,327 | 4.829 | 2,683 | 21.16 | 28.95 | 2.743 | 19.9% | 10.3 |
| 適応(凍結・全材料開始、50評価) | 50 | evaluation_limit | 1,809,261 | 84.50 | 0 | 5.45 | 5.74 | 0.085 | 98.5% | 1.7 |
| 再配置(凍結・30評価×2サイクル) | 92 | grow_cycles_exhausted | 1,545,376 | 75.21 | 0 | 5.37 | 5.74 | 0.090 | 98.0% | 3.5 |
| w5 再現 step20(最小幅5セル) | 21 | converged | 249,716 | 4.824 | 6,842 | 15.86 | 48.72 | 7.714 | - | 0.4 |
| 長予算 適応(凍結) | 82 | finish_stagnation | 1,806,870 | 84.35 | 0 | 5.45 | 5.74 | 0.085 | 98.4% | 2.5 |
| 長予算 再配置(凍結) | 243 | grow_cycles_exhausted | 1,555,520 | 75.65 | 0 | 5.37 | 5.74 | 0.090 | 98.1% | 8.2 |
| 長予算 適応(`prediction_error` 拒否なし・gate=m5b_target) | 200 | finish_budget_exhausted | 557,596 | 30.70 | 0 | 6.73 | 8.33 | 0.204 | 81.2% | 2.5 |

全材料は 5,098,682 mm³。5% 歩幅は 20% より+14% 重く時間は3倍で、**「歩幅を細かくすれば斜材が戻る」は6セル条件で支持されない**。トラス残存率は「2.2mm トラスを当該格子へ投影したマスクのうち5セル現行形に無い部分を最終形がどれだけ含むか」で、重い形ほど高くなる(適応・再配置の98%は体積が大きいためで斜材復元の証拠ではない)。

### E-1-3. 格子・原点依存(step20、最小幅は実寸固定)

| 要素 | 最小幅 | 評価 | 材料 mm³ | SF | M5b_target | KS | トラス残存 | 時間 h |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1.8mm | 6セル(10.79mm) | 193 | 141,431 | 4.847 | 2,674 | 34.81 | 16.2% | 6.2 |
| 2.0mm | 6セル(11.98mm) | 253 | 139,230 | 4.842 | 2,212 | 24.96 | 18.4% | 3.4 |
| 2.2mm | 5セル(10.99mm) | 196 | 126,400 | **5.234** | 1,140 | 17.75 | 57.5% | 3.5 |
| 2.4mm | 5セル(12.03mm) | 18 | 176,465 | 4.821 | 3,564 | 22.89 | 28.3% | 0.2 |

2.0mm 基準で 1.8mm +1.6%、2.2mm -9.2%、2.4mm +26.7%(単調でない)。応力指標は解像度で収束しない(holding ピーク 2.0mm 24.96→1.8mm 34.81 MPa)ので、解像度間の比較は形の差と特異点の格子依存を分離できていない。

原点+0.5セル(=1.0mm)の移動(2.0mm・6セル):

| 条件 | 評価 | 材料 mm³ | 基準比 | SF | M5b_target |
|---|---:|---:|---:|---:|---:|
| 基準 | 253 | 139,230 | - | 4.842 | 2,212 |
| x +0.5 | 138 | 206,454 | +48.3% | 6.328 | 1,338 |
| y +0.5 | 188 | 133,317 | -4.2% | 4.848 | 2,019 |
| z +0.5 | 217 | 173,240 | +24.4% | 4.846 | 3,087 |
| 全軸 +0.5 | 32 | 5,127,648(全材料へ戻された) | 約37倍 | - | - |

**全軸+0.5は、探索が 22,915セル・SF 4.812(`within_target_tolerance`)まで進んだのに、終了時の保持・除外の再検算で除外が `unknown`(`RuntimeError: common-volume boolean did not finish cleanly`)となり、製品が全材料へ戻して `evaluation_error` にした**(`run_06_origin_diag.py --force`。`acceptance.region_check` 99呼び出しのうち pass 以外はこの1件で、疎な形でだけ出た)。根本原因は未分離、捨てられた形のマスクは保存していない。製品 step20 の失敗モードとして記録する。

### E-1-4. 初期形状(適応 m5b_raw、40評価)

移植トラス(2.2mm 由来)開始は 198,413→166,690 mm³・SF 7.968 で step20 より+20%重いが局所超過は小さい(未収束・M5b target 不合格のまま)。粗い骨組み(2.4mm step20 を投影・補修)開始は 173,551 mm³・**SF 4.459(目標未達)**で、**m5b_raw ゲートが目標SFを守れない(05)のが hanger でも再現**。粗い格子で見つけた骨組みは細かい格子の良い開始形にならなかった。全材料開始では gate=m5b_raw と m5b_target が全く同じマスクを返し、**ゲートの差(0%)より `prediction_error` 拒否の有無(-69%)の方が軽量化には効く**が、どちらも目標 SF の6〜18倍で止まる(数十セルの小さな部材が荷重経路を握っている状態に入る。位置は未特定)。

### E-1-5. 斜材の喪失は静かに進む(最小幅5セル製品 step20 の評価ごとの軌跡)

`run_06_w5_trajectory.py`: トラス専有セルの残存は評価5の98%→評価12の14%(**評価6〜12に集中**。SF 55→17、最大変位 0.16→0.53 mm と製品の判定には現れない)。**評価13の1手で SF 16.99→5.21(-69%)、最大変位 0.53→7.64 mm(14.5倍)**に顕在化するが、製品は SF 5.21 > 目標4.8 なので受理する(適応の `sudden_jump` 閾値なら拒否条件に該当)。残存率は単調に減り一度も増えない。**荷重を運ぶ部材がどれかは未特定。最小幅6セルの形で同じ軌跡かは未確認**。

### E-1-6. 4ゾーン診断(`_fem_probe.zone_masks_with_load`。`analyze_06_load_zone.py`)

load ゾーン最大: 保存 hanger_base **48.72**、新規 step20 9.44、1.8mm 6.06、2.2mm 10.47、2.4mm 21.42、H2 3.37 MPa。holding ピークの位置は新規形で固定面から 0.90/1.00/1.10/1.20 mm(1.8/2.0/2.2/2.4mm)=**要素サイズの0.5倍で格子に追従**=特異点の特徴。

### E-1-7. 未検証

凍結パラメータの hanger 固有調整、別乱数・別順位付け(各方式1シード)、原点の負方向・2軸組合せ、斜材の荷重経路としての評価、最小幅6セル形の軌跡。再配置の warm-start 入口は無い。

### E-1-8. 再現(各スクリプトは冪等=`_common.is_case_done`。`--force` で再実行。FEM ロックを共有するので並行しない。台帳に `status: error` が残っていないか必ず確認する〔例外を握りつぶして終了コード0で終わる〕)

```
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_main_matrix.py        # 4方式
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_grid_resolution.py    # 1.8/2.2/2.4mm
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_origin_shift.py        # 全軸・x・y・z の +0.5
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_initshape.py          # 2.4mm ケースに依存
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_adaptive_variants.py  # 拒否なし適応(凍結版と別枠)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_w5_reproduction.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_w5_trajectory.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_origin_diag.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_long_budget.py        # 合計約13時間
.\.venv\Scripts\python.exe topopt_checks\idle_validation\analyze_06_load_zone.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_06_json.py             # 集約 → 06_hanger_low.json
```
出力は `output/idle_validation_20260928/06_*.json`・`06_masks/`。共有ヘルパーは `hanger_low_support.py`。FEM の総時間は約60時間。

## E-2. 高荷重・荷重/幅/格子の行列(07。保存済み結果の集計)

出典: 旧 07_HANGER_LOADS(git 履歴)。期限再編(A-3)で縮小した範囲の報告。出力は `output/idle_validation_20260928/07_corpus_rescore.json`(旧64条件の再採点)・`07_grid.json`(製品 step20 の新28条件=荷重{500,1000,2000}×幅{細い5mm,太い10mm}×格子{1.8,2.0,2.2,2.4}mm の24+200N 細い幅×4格子)・`07_methods.json`・`07_full_material_probe.json`・`07_hanger_loads.json`(集計)・`07_masks/*.npz`。荷重別の92形の判定件数は本人判断1と3-3節。

### E-2-1. 結論

- **再現**: 旧64条件を同じ評価器で再採点すると製品SF・材料体積が 64/64 で完全一致。新しい step20 は解決後の格子が同じ条件で旧コーパスと同一マスク(15/15)。要求2.4mm だけは解決後の格子幅が 2.3875mm(旧)→2.4063mm(今のコード)に変わり同一マスクは 0/7(別条件)。
- **製品SFのゲートは全92形で満たされる(4.80〜5.23)が、局所の応力は荷重とともに悪化する**。KS の平均は 18.9→24.2→31.7→52.1 MPa(200→2000N)。2000N の全24形が 40 MPa を超える。
- **高応力は荷重が上がるほど固定面近傍(holding)に集中する**(目標超過量の holding 割合 55%→97%)。ピーク位置は92形中85形が holding(2000N は全形で固定面から0.5セル以内)。荷重面のピークは200N・最小幅5セルの1形(`hanger_base` と同一)だけ。固定面のうち面23(下側)が反力の約48%・超過量の51〜64%を担う。
- **全材料(設計空間全体 5,102,427 mm³)の局所ピークは荷重に比例**(500N 14.0・1000N 27.9・2000N 55.9 MPa)。製品 step20 形の KS 平均との比は 500N 1.72・2000N 0.93: **削ったことによる局所悪化は低荷重ほど大きく、2000N では全材料と同水準**。M5b raw は 2000N で全材料自身が不合格。
- **2000N の方式比較は成立しない**(ゲートの出発点不成立): 適応・再配置・拒否なし適応は削除候補を全件 `safety_gate` で拒否し、18.53・3.98・19.08時間かけて全材料を返した。**出発点がゲートを満たすかを全材料1回の FEM(約2.5分)で先に確かめる**と空回しを避けられる。同じ percentile ゲートの step05 対 step20 は 2000N で -4.8% / +0.7% と小さく符号も逆。
- **格子と最小幅の効果は分離できない**: 同じ荷重・幅で格子を 1.8〜2.4mm に振ると材料の最大/最小は 1.04〜1.40。実現最小幅(太い幅=6・6・5・5セル)が格子と同時に動く。太い/細いの材料比 1.11〜1.85 は実現幅の大小だけでは説明できない。
- **固定面端のピークは格子依存が強い**(2000N の KS 平均は 1.8mm 60.9・2.4mm 43.7 MPa)。特異性か物理かは未切り分け。
- 荷重10倍(200→2000N)で材料は8.8倍(細い幅 2.0mm: 89,561→788,458 mm³)。低寄与材料(目標許容の0.1倍未満の体積割合)は 200N 17〜52%→2000N 1〜7%。

### E-2-2. 費用・失敗・中断

- 旧コーパス再採点は1条件12〜102秒。製品 step20 の新28件は 0.21〜3.04時間(計17.2時間)、**ピークのワーキングセット最大 16,878 MB(1.8mm 格子)**。適応は1ケース 18.5〜19.1時間(約3.2分/FEM)。
- 2000N 細い幅の適応は約9.7時間走って台帳に書かず終了コード -1 で終了(checkpoint なし、原因未特定)。origin 15件・init 8件・methods 残り19件は未実施(`deferred_by_user_deadline`)。旧 `chain07.ps1`(全行列自動継続)は本人の期限変更で無効化。

### E-2-3. 限界と物理入力不明

荷重500〜2000N が実機の想定か、固定面を完全固定とするモデルが実機の拘束を表すかは外部入力待ちで、結論はこの2点に左右される(固定面端のピークが主因)。1シード・1回、格子と実現幅の同時変化、原点・初期形状は未実施、全材料の算術外挿(約1,430N raw)は実走で確認していない。

### E-2-4. 再現

```
.venv/Scripts/python.exe topopt_checks/idle_validation/analyze_07_corpus_rescore.py
.venv/Scripts/python.exe topopt_checks/idle_validation/run_07_matrix.py --stage grid         # 台帳・マスク保存済みなら冪等でスキップ
.venv/Scripts/python.exe topopt_checks/idle_validation/run_07_full_material_probe.py --case F0500   # 各約2.5分
.venv/Scripts/python.exe topopt_checks/idle_validation/run_07_full_material_probe.py --case F1000
.venv/Scripts/python.exe topopt_checks/idle_validation/build_07_json.py                       # 新規FEMなし
```
共有ヘルパーは `hanger_loads_support.py`。`07_masks/` は 09 の代表形の候補。適応3形は出発点なので完成形の候補ではない。

---

# 付録F: 探索不具合の監査と研究用修正(08)

出典: 旧 08_ROBUSTNESS(git 履歴)。期限再編(A-3)の小型8実行(各20分 wall timeout、計約9分)。修正は `search_methods.py` のみで製品コードは無変更。出力は `output/idle_validation_20260928/08_*.json`(`08_robustness.json` が集約、`08_runs.json` が台帳)。

## F-1. 監査(新規FEMなし。`analyze_08_audit.py` → `08_audit.json`。04〜07の保存台帳から重複除去後57実行)

- **予測誤差の小分母による過剰拒否**: `prediction_error = actual_jump > predicted_jump × 2.5`(`calibrated_k` は最初の受理1手で一度だけ較正)。52実行・6,791手で 2,119手(31%)が `prediction_error` で拒否され、うち 2,100手はゲートも急変検知も通っていた(実ジャンプ中央値 1.6e-4 は受理手と同じ桁)。実ジャンプが微小でも予測値がさらに小さいと比だけが倍率を超える。
- **再配置の追加が全材料へ戻る**: 21実行・追加59件が全件 `initial_all_material` 由来(追加後の材料は初期全材料の99.4%以上)。
- **ゲートの不一致**: 受理ゲート m5b_raw の50実行のうち27実行は製品SFが目標4.8未満(最小3.85)。
- **出発点がゲートを満たさないと探索が動かない**(07): `prediction_error` 0件で上とは別の機構。

## F-2. 研究用修正(既定はすべて旧挙動)

| 設定 | 内容 |
|---|---|
| `AdaptiveConfig.prediction_error_floor`(0.0) | 実ジャンプがこの値以下なら予測誤差で拒否しない(`actual > max(予測×倍率, floor)`)。0.01 は拒否手の分布を見て置いた丸め値で未調整・感度未測定 |
| `ReallocationConfig.grow_max_fraction_of_removed`(None) | 追加1回の上限=削除済みセル数×この割合。超える連結成分は候補から外す |
| `AdaptiveConfig.stop_if_start_fails_gate`(False) | 開始形が受理ゲートを満たさなければ `stop_reason="start_fails_gate"` で戻る(`resume_from` 付きでは見ない) |

修正前の3件は 05 の保存結果と同一のマスクハッシュを再現した。

## F-3. 小型比較(同じ格子・実現最小幅・荷重・ゲート・初期形・予算のペアだけを比べた)

| 条件 | 実行 | ゲート | 材料 mm³ | 製品SF | raw / 目標換算 | 評価 | 停止 |
|---|---|---|---:|---:|---|---:|---|
| dilution 3.0mm(実2.97mm・3セル=8.91mm・300N) | floor 0 | m5b_raw | 63,013 | 5.716 | 合格/合格 | 45 | finish_stagnation |
| 同 | floor 0.01 | m5b_raw | 22,288 | **4.039** | 合格/不合格 | 100 | evaluation_limit |
| path 1.5mm(4セル=6.0mm・3000N) | floor 0 | m5b_raw | 152,385 | 5.915 | 合格/不合格 | 9 | finish_stagnation |
| 同 | floor 0.01 | m5b_raw | 54,466 | **4.028** | 合格/不合格 | 100 | evaluation_limit |
| dilution 3.0mm | 製品 step20 | percentile | 23,440 | 4.837 | 合格/不合格 | 39 | converged |
| 同 | 適応(floor 0.01) | percentile | 24,854(+6.0%) | 4.801 | 合格/不合格 | 53 | finish_stagnation |

- **体積-64%は目標SFの低下(4.04・4.03)と引き換え**で「改善」ではない。percentile に揃えると修正後の適応は step20 より+6.0%重い。percentile ゲートで floor が効くかは未確認。
- 再配置(dilution 300N・2.0mm・6mm・m5b_raw・追加上限10%): 全材料復帰は止まったが最終 64,496 mm³・SF 4.744 は同条件の適応(57,768 mm³)より+11.6%重く、予測誤差拒否が78件再発(floor 未併用)。
- 出発点不成立の早期停止(gate=m5b_target): FEM1回・2秒で `start_fails_gate`。修正前の対照は未実施。
- 最良形7件すべてで最小幅・正則境界・単一成分・閉空洞を満たした(`check_08_final_masks.py`)。

## F-4. 限界・未実施

小型2種の3条件・各1実行(seed固定)。`calibrated_k` の単点較正そのものは直していない(修正後も dilution 3.0mm で予測誤差拒否が54件残る)。未実施: percentile ゲートでの floor 対照・再配置の cap と floor の併用・予測係数の再較正・複合ゲート・早期停止の修正前対照・floor 値の感度。

## F-5. 再現

```
.venv\Scripts\python.exe topopt_checks\idle_validation\analyze_08_audit.py          # 監査(新規FEMなし)
.venv\Scripts\python.exe topopt_checks\idle_validation\run_08_robustness.py --all   # 小型8実行(各20分 wall timeout。--only/--force)
.venv\Scripts\python.exe topopt_checks\idle_validation\check_08_final_masks.py      # 最良形検査(FEMなし)
.venv\Scripts\python.exe topopt_checks\idle_validation\build_08_json.py             # 機械可読まとめ
```
`run_08_robustness.py` は台帳 `08_runs.json` に書き、`LEDGER_NOT_OK` が出たら終了コード2。**終了コード0だけでは合格とせず台帳の `status` を読む**。チェックポイント保存時の Windows `os.replace` が一時的な `PermissionError` を出して台帳に書かず落ちることがある(再起動で通る)。

---

# 付録G: 代表完成形の検証(09)

出典: 旧 09_FINAL_SHAPES(git 履歴)。期限再編(A-3)の範囲(新規最適化なし)。数値は旧 B-rep 経路での記録で、2026-10-06 の最終処理のメッシュ集合演算への切り替え後は `final_shapes_09.py convert` が製品のメッシュ集合演算を使い検査結果を `final.mesh_checks` に保存する。`diagnose_cut_09.py` は旧 B-rep の失敗を再現する専用ツールで、`convert` と `cutdiag` は異なる経路を検証する。

## G-1. 選んだ形(保存済みの探索結果から選定。最終形として保存されたものではない)

| 形 | 役割 | 条件 | マスク |
|---|---|---|---|
| S1_dil25_realloc | 小型の代表改善形(同条件の step20 より軽く製品SF 4.776 以上の新方式の形は dilution 2.5mm の2形だけ。その大きい方) | dilution_small・300N・2.5mm(実2.513mm)・最小幅3セル=7.54mm・再配置・m5b_raw・243評価 | `05_masks/dilution_res2.5_w6_reallocation_best_ever.npz` |
| S1c_dil25_step20(対照) | S1 の同条件・製品 step20(14評価) | 同上 | `05_masks/dilution_res2.5_w6_step20_best_ever.npz` |
| S2_hanger200_step20 | hanger 低荷重の合格候補(06 の H1 step20) | 200N・2.0mm(実1.997mm)・6セル=11.98mm・253評価 | `06_masks/hanger_h1_baseline_step20_best_ever.npz` |
| S3_hanger2000_step20 | hanger 高荷重の代表(07 の 2000N 太い幅 2.2mm。出発点の全材料自身が M5b raw 不合格) | 2000N・2.2mm(実2.199mm)・5セル=10.99mm・13評価 | `07_masks/F2000_thick10mm_h2.2_step20_best_ever.npz` |

4形とも平滑化前のボクセルで最小幅(保持セル免除)・水密・単一成分・閉空洞なし・節点被覆・保持の欠け0が合格。

## G-2. 結論

- **製品の最終形が成立したのは小型の2形(S1・S1c)だけ**(水密・単一ソリッド・自己交差0、約5秒)。hanger の2形は保持ボディを引く最初のブーリアン(`material-cut`)で、保持の表面に沿って残る体積 0.001〜266 mm³ の薄い破片のため単一ソリッドにならず(S2 13個・S3 10個)、S3 は OCCT が結果を不正とした。**S3 の保持のみの cut は取り除いた体積が共通体積の約15%しかなく、「妥当」な結果でも体積が合わない**(原因未特定)。
- 旧コーパス64形も57形が最終形の段階で失敗(`material-cut` 28・三角形分割系18・自己干渉検査未完了6・`preserve-fuse` 3・最終検査不合格2。出典 `output/smoothing_corpus/final_manifest.json`)。**これは不要材料の話とは独立の製品の既知の問題**(最終処理のメッシュ集合演算化は TOPOPT.md)。
- **平滑化後のメッシュを同格子へ再占有化して解いた結果は保存値とほぼ同じ**(S2・S3 は平滑化メッシュ+保持セルの足し戻し=近似):

| 形 | 材料 mm³ | 製品SF(保存値) | M5b raw(保存値) | M5b目標(保存値) | KS | ピーク(固定面から) |
|---|---:|---|---|---|---:|---|
| S1 最終形 | 76,541 | 4.8187(4.8190) | 0(0) | 192.9(192.9) | 10.08 | holding 9.98 MPa(1.26mm) |
| S1c 最終形 | 79,428 | 4.8357(4.8286) | 0(0) | 190.6(190.6) | 10.07 | holding 9.96(1.26mm) |
| S2 平滑化+保持 | 139,501 | 4.8189(4.8418) | 0(0) | 2,200.7(2,212.2) | 24.94 | holding 24.94(1.00mm) |
| S3 平滑化+保持 | 868,360 | 4.8618(4.8383) | 118.5(118.6) | 28,474.8(28,406.6) | 45.94 | holding 45.94(1.10mm) |

  製品SFは保存値から±0.5%以内、材料は+0.09〜+0.33%、IoU 0.987〜0.994。**同じ格子で同じ評価が出る確認であって、細格子でも安全という確認ではない**。S1 対 S1c は探索の受理ゲートが違うので優劣の根拠にしない。
- **平滑化は最小幅をおよそ半セル〜1セル細らせる**: 平滑化後のメッシュを半格子へ占有化して要求幅の立方の opening で失う材料の割合は、S2(実現11.98mm・要求10mm)で 10.0mm 立方 3.25%・11.0mm 11.1%・12.0mm 39.9%、S3 で 9.9mm 1.18%、小型は要求6mm付近で 0.5%未満。**「要求最小幅を平滑化後の形が満たす」とは言えない(hanger で特に)**。ただし opening は角・接合部の丸めでも損失を出すので損失の全部が違反ではない([TOPOPT_MIN_WIDTH.md](../TOPOPT_MIN_WIDTH.md))。製品の合否は平滑化前のボクセルの最小幅で、平滑化後の幅は検査していない。
- **平滑化メッシュ単独(足し戻しなし)は荷重・拘束が成立しない**(同格子へ再占有化すると保持セル・荷重面の節点が材料から外れる)ので保持の足し戻しが必須。hanger の平滑化メッシュは除外ボディに 503 mm³(S2)・1,386 mm³(S3)侵入し、除外の遵守は未確認。

## G-3. 未検証

最終CADの細格子 FEM、hanger の最終ブーリアン(薄い破片)の修正と再検証、`F0500_h2.2_w5`・`F1000_h2.2_w5` の最終形の再解析、小型の複数条件・同ゲートでの追試。小型の最終メッシュは `09_work/*_final_mesh.npz` に保存(細格子再解析の入力にできる)。

## G-4. 再現

```
.venv\Scripts\python.exe topopt_checks\idle_validation\run_09_final_shapes.py --job convert:S2_hanger200_step20       # convert / cutdiag / resolve:<形>:<final|smoothed|smoothed_keep>
.venv\Scripts\python.exe topopt_checks\idle_validation\run_09_final_shapes.py --job cutdiag:S2_hanger200_step20
.venv\Scripts\python.exe topopt_checks\idle_validation\run_09_final_shapes.py --job resolve:S2_hanger200_step20:smoothed_keep
.venv\Scripts\python.exe topopt_checks\idle_validation\build_09_json.py                                               # 機械可読まとめ(FEMなし)
```
各ジョブは新しい OS プロセスで走り wall timeout(小型1200秒・hanger 形状変換1800秒・FEM 2700秒)を超えたら台帳に `timeout` を残す。**`convert` ジョブは `finalize` が不成立でもジョブ自体は `ok`** なので、最終形の成否は台帳の `final_status` で読む。出力は `output/idle_validation_20260928/09_*.json`・`09_runs.json`・`09_work/`。
