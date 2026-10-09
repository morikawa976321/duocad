# 形状最適化の計測・段階4(peel・停止規則)・中空なしの契約

形状最適化で行った一連の計測(部材の厚み・小さい保持ソリッドの判定・確定表の節点所有権・保持/除外の前計算)と、段階4の削減(`binary_search.peel`)・停止規則・
「中空構造を一切許容しない」既定について、ソースを読んでも分からない不変条件・依存禁止・罠を、元の作業タスクの契約から分野別に蒸留した文書。
**いまの挙動の正はコードとテスト**。実測の表と結論は [../TOPOPT_THICKNESS.md](../TOPOPT_THICKNESS.md)・[../TOPOPT.md](../TOPOPT.md)・[../PLAN_BINARY_STRESS_SEARCH.md](../PLAN_BINARY_STRESS_SEARCH.md)(§12)・
[../TOPOPT_SPEED.md](../TOPOPT_SPEED.md)・[../TOPOPT_STOP_RULE.md](../TOPOPT_STOP_RULE.md) にあり、ここでは繰り返さない。二値探索の各段・製品配線は
[topopt-binary-search.md](topopt-binary-search.md)。

項目末尾の括弧は出典(元タスクID, 日付)。計測スクリプト(`measure_thickness_layers.py` / `measure_small_keep_bodies.py` / `measure_member_thickness.py` / `measure_anchor_node_ownership.py` /
`measure_void_check_cost.py` / `summarize_stage4.py` / `measure_region_overlaps.py`)は、コミット `312aac5`「一度きりの測定スクリプト17本を削除」で消えた(`topopt_checks/` に無い)。
スクリプトの引数の契約は落とし、**測定から得た「後続が判断に使う知見・罠」だけ**を残した。再現が要るときは `312aac5` の親から取り出す。
`(対象コード不在・要確認)` は名前が現行ソースに無いもの。生の測定JSONは `output/`(git追跡外)にあった。

## 部材の厚み・保持小体の計測(`docs/TOPOPT_THICKNESS.md` 節A〜G)

- **測定側の「製品判定の写し」は、製品の判定と必ず突き合わせて使った。** `measure_plate_region` 等の `inside_design` は `verify_against_product` で
  `optimize_run._region_representation`(無改変・実際の `MIN_REGION_ELEMENTS=8`)の判定と照合し、不一致なら例外にしていた(節B: 21ケース、節C・D: 108ケースで不一致0)。
  この照合を外して測定側の値だけを信じない —— 製品の判定が変わったとき測定側だけが古い前提で乖離する。D節は `optimize_run._map_boundary_conditions`(製品の入口関数そのもの)を直接呼んで
  確定表の節点数を読んでおり、独自の節点数え直しを足すと数え方が製品とずれる。(20260918-0239-topopt-measure-thickness-layers, 2026-09-18; 20260918-1330-topopt-measure-small-keep-bodies, 2026-09-18)
  (`measure_thickness_layers.py` / `measure_small_keep_bodies.py` は対象コード不在・要確認: `312aac5` で削除)
- **単一ボディ(板だけ)では「位置合わせ依存」を測れない。** `design_space.grid_for_bbox` は点群の中心を基準に格子を再センタリングするため、条件が板1つだけだと板を平行移動しても格子が一緒に動き
  (厳密な並進不変性)、位置ずらしの効果が出ない。節B・Cは「対象+固定パッド+荷重パッド」の3ボディ構成にした(`Conditions` が拘束面・荷重面を1枚以上要求するため、板単体では条件も組めない)。
  パッドは閾値8要素を大きく超える大きさにして診断は対象の行だけを見る —— **パッドを対象と同程度にすると診断が2行以上出て前提が壊れる**。D節は荷重パッドを置かず「固定パッド+対象(荷重役を兼ねる)」の2ボディ。
  (20260918-0239-topopt-measure-thickness-layers, 20260918-1330-topopt-measure-small-keep-bodies, 2026-09-18)
- **曲げ応力の取りこぼし(節A)の指標は、必ず各 n の理論値で正規化した比で作る。** 生の応力・安全率を異なる n の間で直接比べると、梁の絶対寸法が n で変わり無次元理論値が `~1/n^2` でスケールするため、
  寸法変化に支配されて解像度起因の誤差を表さない(`sf_ratio_1_3` 等の比を作ってから参照解と比べる)。n=1 のとき理論値は厳密に0(唯一の要素の中心が中立軸と一致)でゼロ除算になるが、バグではない。
  結論の表(1層で43.0倍の過大評価〜n=8で1.08倍)は `TOPOPT_THICKNESS.md`。(20260918-0239-topopt-measure-thickness-layers, 2026-09-18)
- **「個数」ゲート(`_region_representation`)と確定表(`_map_boundary_conditions`)は別の物差しで独立に動く。** stop(0要素)と判定されても確定表は成立(pass)しうる(54ケース中13件。cube 9件全部、cylinder 4件)。
  後続がこの2ゲートの関係を前提にするなら要注意。製品コードは無改変の契約だったので直していない。小さい塊の要素数は s/spacing の比に対して**単調でない**(比1.8が前後の1.5・2.0より少ない、量子化の位相依存)。
  「比を上げれば要素数が増える」とコードに書かない。(20260918-1330-topopt-measure-small-keep-bodies, 2026-09-18)
- **節Eの応力比(粗/細)は2ケースで向きが逆(1.52倍・0.71倍)。** 節Aの「層が薄いほど一貫して危険側」という傾向をこの題材へ外挿しない(サンプル2件では傾向を語れない)。節Eは `preserves=()` / `excludes=()` で
  `acceptance.inspect` を呼んでおり(保持・除外の体積検査をスキップ)、結果を「製品の6条件をすべて通った状態」の代表値として扱わない(見ているのは代表応力の値だけ)。位置ずらしはz軸のみで、
  x/y方向の結果が同程度という前提は未検証。(20260918-1330-topopt-measure-small-keep-bodies, 2026-09-18)
- **採用マスクの局所厚さ `distance_transform_edt(material)*2` は下限が定義上2.0要素**(空セルと面で接する材料は必ず距離1×2)。1層の壁も2層の壁も区別できず、「2要素未満0%」は「1層が無い」証拠にならない。
  1層かどうかは opening(2×2×2)で見る。境界規約(格子の外を空/材料のどちらとみなすか)は両方計算して選んだ側を明記する。(20260918-1718-topopt-measure-member-thickness, 2026-09-18; 20260919-0925-topopt-member-thickness-at-2.4-and-2.0, 2026-09-19)
- **応力分位の「薄い部分 vs 全体」比較は母集団構成に依存する。** hanger では全体の99.9%が「薄い」側に入り2つの分位がほぼ同一になった。この比較を検査として再利用する前に、厚み分布に十分な広がりがあるか確かめる。
  (20260918-1718-topopt-measure-member-thickness, 2026-09-18)
- **`DesignSpace.keep` は「保持ボディが最後まで残る保証」ではない。** `document.optimize.preserve_bodies` を指定していないモデルでは、`keep` ロールのボディのボクセルの一部(174要素中57)が二値探索で削られうる。
  「keep由来なら必ず採用マスクに残る」前提を置かない。(20260918-1718-topopt-measure-member-thickness, 2026-09-18)(現行は `SearchConditions.keep_cells` で `peel` が保護セルを削らない。別の経路)
- **過去の記録の数字を「今の挙動」の代わりに使わない。** 同じ spacing(4.8mm)・budget(60)のラベルでも、既存記録は cells=39,313・安全率146.1、当時のHEAD `076698b` の実測は cells=1,104・安全率5.398
  (約35分の1)だった。コード版が違えば別の採用マスクになる。再現するにはそのときのHEADで測り直す。`output/hanger_fix_verification/review-fixes-spacing-4.8-budget-60.json`(39,313セル)が
  現HEADの探索と一致しない原因は未調査。(20260918-1718-topopt-measure-member-thickness, 2026-09-18; 20260919-0809-topopt-precompute-region-overlap, 2026-09-19)
- **節Fの4.8mmと節Gの2.4mmを「解像度だけ違う比較」として読まない**(peel の前後でコード版が違い、材料割合も2.337%対1.137%)。要素単位ではほぼ一定(約2.0〜2.05要素)、mm単位では解像度に反比例して半分になる。
  (measure-member-thickness-at-2.4-and-2.0, 2026-09-19)
- **2.0mm の本番は単独で測れなかった**(起動70秒でプロセスRSS 9.08GB・ピーク11.32GB超、システム空き0.18GBで自己中断)。2.4mm 本番は約60分・RSS 6.95GB。2.0mm は他の実行が無く
  空きメモリ15GB以上で単独に回す。メモリ監視は「システムの空き<閾値で自己中断」を入れ、閾値は3GB以上(1GBにしたら0.5秒の間に0.18GBまで落ち間に合わなかった)。
  **評価予算を下げても所要時間は縮まらない**(2.4mm: 前半8評価+全材料3回で約1,740秒、残り約1,720秒。小さい形でも1評価30秒以上)ので、所要秒を形の大きさに比例させて見積もらない。
  所要秒は他セッションの探索との競合込みで評価1回の実測値として使えない。(20260919-0925-topopt-member-thickness-at-2.4-and-2.0, 2026-09-19)
- 長時間(60分級)の測定の運用: Bash の最大待機は10分なので、プロセスを `Start-Process` で切り離して起動し `Wait-Process -Timeout` の前景で待つ(`Start-Sleep` / `sleep` はハーネスが止める。JSONは終了時にだけ書かれ途中経過は見えない)。
  同じ機械で別セッションが探索を並走させると CPU・メモリ競合が測定を左右するので、起動前に `Get-Process python*` と空きメモリを見る。`verify_hanger_fix.verify()` は既定で tracemalloc を入れて numpy を遅くする
  (所要秒を測るときは `--no-memory-trace`)。(20260919-0925-topopt-member-thickness-at-2.4-and-2.0, 2026-09-19; 20260919-0835-topopt-stage4-peel, 2026-09-19; 20260919-1135-topopt-stage4-stop-rule, 2026-09-19)
- **測定スクリプトの横取り(patch)は「本物を呼んでから結果を横流しするだけ」にする。** `design_space.derive` / `stress.evaluate` の実測値を取るラッパーは計算式を書き換えて代用しない。
  `DesignSpace.keep` や要素ごとの von Mises 場(`report.field.von_mises`)は `verify()` / `acceptance.safety_check` が捨ててしまうので、`verify()` の呼び出し**前**にパッチを当てる。
  パッチは `with` でスコープを閉じる(外で戻すと次の呼び出しに漏れる)。`acceptance.py` は `from . import stress` でモジュール属性参照なので、属性パッチは効く。
  `trace.DEFAULT_ROOT` と `--save-masks` の出力先は `tempfile.TemporaryDirectory` に閉じ、`output/` に触れない。(20260918-1718-topopt-measure-member-thickness, 2026-09-18)
  (対象コード不在・要確認: `measure_member_thickness.py` は削除済み。`verify_hanger_fix.py` と `run_search_capturing_space` の現行の存在・名前は未確認)

## 確定表の節点所有権(`docs/TOPOPT.md`「確定表の節点所有権」)

- **「元の確定表の節点がそのボディ由来か」(所有権測定)と「`allowed_nodes` をそのボディ由来に絞って全面を作り直した表」(制限予行)は別物。** 前者が100%でも後者の節点数・面積は変わりうる
  (hanger 2.4mm の `fixed_face_10` / `fixed_face_7` で15→14節点)。理由: `anchor.build_anchors` を `faces=` で絞らない限り、同じ呼び出し内の他の面の標本も同じ `allowed_nodes` で絞られ、稜線の競合相手が変わる。
  「制限は数値上のno-op・元の表と完全一致」と書くのは誤り(QAで実際に誤記した)。(20260918-1913-topopt-measure-anchor-node-ownership, 2026-09-18)
- **`anchor.build_anchors` を面ごとに `faces=[face]` で絞って呼ばない。** 稜線の競合相手(他の面)が計算から消え、絞らずに作った表と別の節点集合になる。必ず `faces=None`(全面)で呼び `table[face]` だけを取り出す。
  (20260918-1913-topopt-measure-anchor-node-ownership, 2026-09-18)
- 「そのボディ由来」の判定式は `occupancy ∩ design mask → anchor.active_nodes` で、`optimize_run._region_representation` と厳密に同じ式を使う(外接箱だけで判定する等に変えると比較が成立しない)。
  製品コードは呼ぶだけで変更しない。(20260918-1913-topopt-measure-anchor-node-ownership, 2026-09-18)
- 制限予行の `restricted_mapping` は、面ごとの `build_anchors` が**1つでも例外を投げると全体がNone**(all-or-nothing。`len(restricted_anchors) == len(mapping["faces"])` で判定)。
  (20260918-1913-topopt-measure-anchor-node-ownership, 2026-09-18)
- **「hangerでは制限を入れても大丈夫」と一般化しない。** hanger は2.4mm・要素サイズ下限1.874852mm のどちらでも元の表の節点が100%そのボディ由来で、制限は実質no-opだった。制限がノードを大きく削る局面
  (合成題材 cube/cylinder ボスの1.2mm で節点数ベースの所有権32〜39%、面積/重みベースでは92〜98%と2指標が食い違う)は、hanger が1.2mm を要求できない(`OptimizeInputError` で断られる。実測の下限1.874852mm)ため測れていない。
  (20260918-1913-topopt-measure-anchor-node-ownership, 2026-09-18)
- 実行環境の落とし穴: このBash環境ではコンソールがcp932相当で、Pythonが生成した日本語を `print` すると文字化けして見えるが、`Path.write_text(..., encoding="utf-8")` で書いたファイル自体は正しいUTF-8。
  文字化けを見てファイルが壊れていると誤診断しない。(20260918-1913-topopt-measure-anchor-node-ownership, 2026-09-18)
  (`measure_anchor_node_ownership.py` は対象コード不在・要確認: `312aac5` で削除)

## 保持・除外の判定の前計算(`acceptance.RegionOverlaps`。`docs/TOPOPT_SPEED.md`「保持・除外の判定を前計算にした」)

- 呼ぶ面: `acceptance.RegionOverlaps().overlap_volume(material, grid, shape, *, cache=None) -> (体積, 共通体積)` が `intersection_volume` の代わり。`acceptance.region_check(..., overlaps=None)` /
  `acceptance.inspect(..., region_overlaps=None)` に渡すと前計算から引く(既定は直接ブーリアン)。`binary_search.SearchConditions.region_overlaps` の既定は新しい `RegionOverlaps`、`None` で直接ブーリアン。
  `binary_search._recheck_regions_without_precompute` が段階4を出るときに採用形状を直接ブーリアンで取り直し、`diagnostics['reduction']['final_region_recheck']` は
  `'pass'|'not_needed'|'failed:<条件>:<状態>'`。セル単位の共通体積プリミティブは現行 `kernel_brep.grid_cell_common_volumes` / `box_common_volume`(当初は `kernel.` に置いた)。(20260919-0809-topopt-precompute-region-overlap, 2026-09-19)
- **前計算は探索1回のあいだだけ生きる**(格子・ソリッドが固定)。鍵は (ソリッドの同一性〈TShape・Location・Orientation〉, 格子の原点・間隔・形)。探索をまたいで持ち越さない・スレッド間で共有しない。
  `inspect_kwargs(bbox_cache=None)` は前計算も外す(=キャッシュ無し再検査は直接ブーリアン)。段階4の評価だけが `overrides=dict(bbox_cache=None)` で前計算を残す(意図的)。
  6条件の意味・報告の形・`rule` 文言は不変。`inspect_document`(過去記録)は直接ブーリアンのまま。前計算が作れない・途中で失敗したソリッドは、そのソリッドだけ直接ブーリアンへ戻る
  (`RegionOverlaps.fallbacks`)。(20260919-0809-topopt-precompute-region-overlap, 2026-09-19)
- **新旧の体積一致は 1e-9 相対ではなく、平面・単純な曲面は許容差以内、曲面ソリッド(hanger)は相対1e-6で見る。** hanger では最大相対3.7e-8ずれる(旧経路自身の加法性残差、許容差の34〜282倍と同じ桁)。
  `RegionOverlaps._entries` / `_CellOverlaps`(内部)と `TILE_CELLS`(現在8、`acceptance.py`)の値に依存しない。(20260919-0809-topopt-precompute-region-overlap, 2026-09-19)
- **罠: セルごとにブーリアン(`box_common_volume` をセル数だけ)で重なりを取る実装は、継ぎ目近傍で体積を黙って落とす。** 格子の面が形状の継ぎ目に 0.05µm まで近づくと「空」を返す
  (hanger 4.8mm の円板1048mm³で17.4mm³欠落)。**1e-14 の座標の違いで出たり消えたりする**ので、テストで再現を狙うと不安定。
  平面で切って破片を重心でセルへ振り分けるとき、切る平面が形状の端まで届かないと外の残りが1つの破片につながって重心が塊の中に落ちる(853×50×695mm の箱で2,960万mm³を余計に数えた)。
  (20260919-0809-topopt-precompute-region-overlap, 2026-09-19)
- 出力先: `verify_hanger_fix.py` 等は `trace.DEFAULT_ROOT` を一時フォルダへ向けてから走らせる(`output/` を汚す)。(20260919-0809-topopt-precompute-region-overlap, 2026-09-19)

## 段階4: peel(ひずみエネルギー順位で削る。計画書§11〜§12)

- 呼ぶ面: `binary_search.peel(mask, order, count, anchor_ids, *, min_width_cells=1, exempt=None, protected=None) -> (candidate, PeelStats)`(入力マスクは複製して作業するので変えない)。
  当初あった `surface_only` 引数と `DEFAULT_SURFACE_ONLY` は削除済み(2026-09-20 本人指示。`peel` は常に表面セルしか抜かない)。`PeelStats` の欄は
  `picked / casualties / islands / skipped_support / skipped_surface / skipped_manifold / skipped_thickness(常に0) / skipped_connectivity / skipped_neck / skipped_core / already_removed / global_checks /
  core_window_* / core_global / neck_full_grid / seconds / picked_cells`(`as_dict()` に `picked_cells` は出ない)。診断は `diagnostics['reduction']`
  (`peel_calls` / `peel_seconds` / `peel_seconds_max` / `repeated_candidates` / `geometry_rejections`〈候補ごとに `step` `components` `surviving_components` `missing` `removed_islands`〉と `steps[]` の `peel`)。
  **消えたキー: `restored_selections` / `excluded_cells` / `selected_cells`**。段階4の履歴から `no_component` は消えた。幾何の不合格の Trial は `outcome='infeasible'`・`checks={'geometry':'fail'}`。
  (20260919-0835-topopt-stage4-peel, 2026-09-19)
- **不変条件: `peel` は入力が「1成分・不正頂点0・全条件節点に支えあり(・閉じた空洞なし・m>1なら全材料セルが m 立方に属し侵食した芯がつながる)」なら出力も同じ。** 段階4の入力(合格形状)は必ずこれを満たす。
  `resolve_islands` は検算で、`_geometry_rejection` が None でなければ不合格(FEMを走らせない)。**連結性由来の不合格はバグ。** 保持の重なりは `peel` が見ないので幾何の不合格に出うる。
  `skipped_*` は「支え→表面→多様体→連結→くびれ→芯の連結」の順に判定して最初に落ちた理由で数える(欄の並び・数え方に依存しない)。(20260919-0835-topopt-stage4-peel, 2026-09-19)
- 順位から削ったセルが1つ(k=1)で安全率が落ちたとき、**当初は「収束」としていたが現行は `single_cell_patience`(下の停止規則)で外して続ける**。k=1 の不合格のうち計算失敗・保持の重なりは安全率で外すカウントに数えない。
  2つ以上なら m ← m/2。同じ形で不合格と分かった候補が再び出たら解き直さず m を半減(`repeated_candidates`。評価に数えない)。(20260919-0835-topopt-stage4-peel, 20260919-1135-topopt-stage4-stop-rule, 2026-09-19)
- **島として消えるセルも `count` に数える**ので、1回の削減が要求より多くなりうる(hanger 実測: Aの3試行で超過、最大11セル)。安全率の不合格で m を下げても、根元のセルが順位の先頭にあれば同じ島が消える。
  `steps[].peel.seconds` は毎回変わる。**決定性を比べるテストは `seconds` を除いて比べる**。(20260919-0835-topopt-stage4-peel, 20260919-1135-topopt-stage4-stop-rule, 2026-09-19)
- **peel で FEM は減るが最終体積は基準より大きくなった(hanger 4.8mm: A=内部も削る経路で FEM 33回・71,421 mm³・安全率2.433、B=表面だけで FEM 45回・63,256 mm³・2.012。基準は FEM 184回)。**
  k=1 で止める規則だと終盤の1セルの効きで止まる位置が決まり、A は目標2まで21.6%の余裕を残して止まった。これを受けて停止規則を足した(下)。採用1回あたりの削減が 9 → 507〜1,176 に変わったので
  §11.2 の r_max・θ の結論は持ち込めない(再測定は未実施と記録)。以上の数値は1回ずつの実測で、`DEFAULT_SURFACE_ONLY` の値(False→True→削除)を根拠にしない。(20260919-0835-topopt-stage4-peel, 2026-09-19)
  (`--surface-only` / `--no-surface-only` は対象コード不在・要確認: `verify_hanger_fix.py` に現存しない。A/B の測定は削除前のコードでしか再現できない)
- 時間の実測: 「4.8mm の FEM 1回は約15秒」は当てはまらなかった。全材料(47,232セル)の6条件が約22秒、段階4は形が縮むので探索全体の平均は評価1回 3.7〜4.1 秒(A・B 同時実行)。
  A・B とも約3分で終わった。基準の所要秒(1,409秒)は別のコード・並列条件の値で比較に使えない。`verify_hanger_fix.py` は `optimize_run.run` を通らないので `trace.DEFAULT_ROOT` の記録は出ず
  `output/optimization_runs` は増えない。(20260919-0835-topopt-stage4-peel, 2026-09-19)
- `docs/TOPOPT.md`「現在の正」の表は、太字が数値の行だけを `tests/test_doc_code_agreement.py` が機械照合する。真偽値(`DEFAULT_SURFACE_ONLY` 等)の行は照合されない。(20260919-0835-topopt-stage4-peel, 2026-09-19)
- 診断ツール: `topopt_checks/verify_hanger_fix.py`(`--no-memory-trace` / `--single-cell-patience N|unlimited` / `--target-tolerance` / `--save-masks`)。`summarize_stage4.py`
  (§12.3 の表の値と `max_safety_streak`〈採用を挟まず単セルの安全率で落ちた最長の回数〉を出す)は削除済み。(20260919-0835-topopt-stage4-peel, 20260919-1135-topopt-stage4-stop-rule, 2026-09-19)(`summarize_stage4.py` は対象コード不在・要確認)

## 段階4: 停止規則(`single_cell_patience`。issue #22。`docs/TOPOPT_STOP_RULE.md`)

- 呼ぶ面: `binary_search.find_best_feasible(..., single_cell_patience=binary_search.DEFAULT_SINGLE_CELL_PATIENCE)`(`_reduce_volume` にも同名の引数)。`int >= 1` か `None`(順位が尽きるまで)。
  それ以外(0・負数・小数・文字列・bool)は `ValueError`。**既定は当初16だったが、現行は 8**(2026-09-24 本人判断。計画書§12.13)。(20260919-1135-topopt-stage4-stop-rule, 2026-09-19; 置き換え: 計画書§12.13)
- 診断: `diagnostics['reduction']` の `single_cell_patience` / `safety_excluded_cells`(安全率で外して続けたセルの数。止める原因になった最後の1セルは含まない)/ `adopted_after_exclusion`(安全率で外したセルの後に採用へ至った回数)。
  `stop_reason`: `safety_at_single_cell`(patience 個続けて落ちた)/ `no_removable_cell`(順位が尽きた)/ `within_target_tolerance`(安全率が目標の許容内、§12.12)/ `repeated_evaluation_error`。
  **予算切れは `stop_reason=None` で `SearchResult.termination='evaluation_limit'`**。(20260919-1135-topopt-stage4-stop-rule, 2026-09-19)
- カウントは「削ったセルが1つで安全率が落ちた試行」だけ。k≥2 の不合格・計算失敗(`unknown`)・保持の重なりの不合格は数えない(順位から外すのは k=1 の不合格すべて)。**採用でカウントは0に戻る**。
  外したセル(poison)を採用で解除するか、は当初「解除」だったが現行は「採用をまたいで外したまま、合格形状の安全率が不足分以上に上がったセルだけ戻す」+「不合格の形の内容ハッシュ `rejected` も持ち越す」
  (計画書§12.13)。決定的。有限回で止まる理由は `_reduce_volume` の docstring。(20260919-1135-topopt-stage4-stop-rule, 2026-09-19; 置き換え: 計画書§12.13)
- **`DEFAULT_SINGLE_CELL_PATIENCE` の値そのものに依存しない。** 当時の決め方(測る前に決めた基準: 体積最小→FEM少→patience小)を hanger 4.8mm 1つ・各1回で当てただけで、16 と無制限は予算1000内で同一の試行列
  (採用を挟まず単セルの安全率で落ちた最長が7回)だった。16 が無制限より良い証拠ではない。§12.7 の数値(FEM 999・51,715 mm³・安全率 2.0157)は r_max・θ・題材・予算を変えれば変わる。
  所要秒(3,585秒など)は他セッションの並走込みで比較に使えない。(20260919-1135-topopt-stage4-stop-rule, 2026-09-19)
- 当時の16は hanger 4.8mm で**予算1000を使い切った**(段階4のFEM 999回。基準184・patience=1 の33回に対し5.4倍・30倍)。最終体積は基準 53,130 より 2.7% 小さいが、peel で減らしたFEMを戻している。
  FEM 100回目以降は採用156回で160セルしか削らない(1セル/採用)。終盤の1セルごとの FEM 判定が支配的で、安全率は 2.016 まで進み目標2まで0.8%残して予算切れ。
  「体積を第一にしたので16」という判断の妥当性は本人判断だった。(20260919-1135-topopt-stage4-stop-rule, 2026-09-19)
- `optimize_run.OptimizeOptions` は patience を渡さない(製品経路は既定値・予算1000)。UI・MCP から変える経路は無い。(20260919-1135-topopt-stage4-stop-rule, 2026-09-19)
- 並走の他セッション(hanger 2.0mm/2.4mm)があると CPU 約80%・メモリの大半が使われ、4.8mm の1本(約0.5〜1.3GB)でも所要秒が同じ試行列で 140秒→271秒に伸びた。(20260919-1135-topopt-stage4-stop-rule, 2026-09-19)

## 中空構造を許容しない(`docs/TOPOPT.md`「形状最適化は中空構造(閉じた空洞)を一切許容しない」、計画書§12.9)

- **制約(本人決定 2026-09-19): 「duocadの形状最適化では中空構造を一切許容しない」。** 実測ではなく製品制約で、`DEFAULT_SURFACE_ONLY` の既定変更の根拠はこの制約。実測(§12.3・§12.8)は
  「その制約の下でも探索は基準に並ぶ」ことを示すだけ。細い通路で外へつながる袋は定義上空洞ではない(既知の限界。通路幅の下限は未対応)。(20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)
- 呼ぶ面: `acceptance.void_components(material) -> int`(空の6近傍連結成分の数。格子の外へつながる空を含むので、空洞なしなら 1)/ `acceptance.void_labels(material) -> (ラベル配列, 成分数)`
  (配列は各辺2大きい。ラベル1が外)/ `acceptance.closed_voids(material, *, limit=5) -> [dict(cells, representative_cell)]`(大きい順。`representative_cell` は格子の添字)。
  `acceptance.boundary_check` の報告に `void_components` が加わり、合格は `material.any() and irregular_vertices == 0 and void_components == 1`。報告キー `watertight` と6条件の数え方は不変。
  (20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)
- `find_initial_feasible` は設計空間に閉じた空洞があれば `input_contradiction`(評価0回)で、`diagnostics['closed_voids'] = dict(count, largest=[...])`。画面の一言は
  `optimize_run._search_failure_warning` の同名の分岐。**除外領域が塊に包まれている入力を `input_contradiction` にする扱いは、当初は「本人未確認の既定」だったが 2026-09-20 に本人が承認した**(計画書§12.10)。
  `largest` の並び(大きい順・同数なら平坦添字の小さい順)は診断表示用で契約ではない。(20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)
- **`peel` は閉じた空洞の無い入力から閉じた空洞の無い候補を作る**(抜くセルは常に空の6近傍を持つので、既存の空の成分に属する)。ランダム題材の不変条件テストが `void_components == 1` を固定していた。
  `surface_only=False` では空洞ができることも同じ題材で固定していた(その経路は削除)。段階3の「削除候補は `_touches_empty(mask, region)` が真のブロックだけ」は、段階3の探索削除で無くなった。
  (20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)(`DEFAULT_SURFACE_ONLY` / `_touches_empty` は対象コード不在: 2026-09-20 / 2026-09-21 に削除)
- 追加・置換の段階3候補は空洞を作りうる(評価で `watertight` が落ちて結果には残らないだけ)。削除だけ表面に限っていた。(20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)(段階3の探索削除で該当コードは無い)
- **B(表面だけ・patience 1・θ=0.5・上限0.2)の hanger の試行列は、4.8・2.4・2.0mm とも検査追加前と一致した**(履歴・steps〈`peel.seconds` 除く〉・最終マスクのハッシュまで)。2.4mm は86行・
  `8b1a4b0795d5…`、2.0mm は45行・`1447408d7f43…` で、JSON同士の照合で確かめた(`search.history` の全行の `material_mm3`・`safety_factor`・`checks` と `search.best.mask_hash`)。
  基準は `output/stage4_peel/B-hanger-{4.8,2.4,2.0}mm-target2-theta0.5-cap0.2{,-recheck}.json`(git追跡外)。(20260919-1840-topopt-no-hollow-surface-only-default, 20260919-1905-topopt-no-hollow-recheck-2.4-2.0, 2026-09-19)
- 空洞検査の費用: 1回 数 ms(2.0mm・材料 640,306 セルで最大 8.3 ms、`boundary_check` 全体で約 35 ms)。1評価(数十秒〜)の 0.03% 未満。同じ測定でも1回目と2回目で数 ms 揺れる
  (4.8mm 1回目 0.65 ms・2回目 1.7 ms〈中央値〉。表には2回目)。`measure_void_check_cost.py`(`design_space.derive` までで空洞検査の秒を測る)は削除済み。(20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)
- **A(`--no-surface-only`)で回した過去の値は、現行コードで同じ試行列にならない**(§12.3 のA、§12.7 の patience 1/4/16/無制限、§12.8 のA 3本は検査追加前のコード。A の peel が作る空洞の候補は
  `watertight` で不合格になる)。`DEFAULT_SINGLE_CELL_PATIENCE` の値も A の測定で決めたもので、B では未測定のまま 8 に変えられた。(20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)
- 実行中にタスクファイルが本人指示で更新された(4.8mm → 2.4mm・2.0mm の再確認の追加)のに、着手時に読んだ版のまま進めて気づくのが遅れた。**長い測定に入る前と、QAの前に、タスクファイルを読み直す。**
  測定中に `duocad/**/*.py` を編集しない(後から始まる2本目が編集後のコードを import し、JSON の `code_sha256` が食い違う)。編集は docs だけにする。
  `tests/test_pre_smoothing.py` の `cavity` ケースは仕様どおり不合格になったので、貫通穴だけの `tunnel` と「空洞は不合格」のテストに分けた。(20260919-1840-topopt-no-hollow-surface-only-default, 2026-09-19)
- **ツール経由でファイルへ書く文字列の `\v` は、シェル・Python の heredoc を通ると垂直タブ(0x0B)に化ける**(バックスラッシュが1つ落ち、残りが Python のエスケープとして解釈される)。
  `topopt_checks\verify_hanger_fix.py` が `topopt_checks<0x0B>erify_hanger_fix.py` になっていた(git diff では見えない)。`\t` `\b` `\f` `\a` も同じ機序。Windows パスを docs や
  Python の文字列に書いたら、書いた後に `ord(c)<32` で改行・タブ以外の制御文字を検査する。文字列側は `chr(92)` で組み立てると化けない。(20260919-1905-topopt-no-hollow-recheck-2.4-2.0, 2026-09-19)
- 前のタスクが済ませているかは推測せず、docs の中身と JSON を見て判定する(引継ぎの一文だけでは、空洞検査の費用のような項目があるかを確かめられなかった)。(20260919-1905-topopt-no-hollow-recheck-2.4-2.0, 2026-09-19)
