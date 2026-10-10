# 形状最適化の計測・段階4(peel・停止規則)・中空なしの契約

部材の厚み・小さい保持ソリッドの判定・確定表の節点所有権・保持/除外の前計算の計測、段階4の削減(`binary_search.peel`)・停止規則・「中空構造を一切許容しない」既定について、ソースを読んでも分からない不変条件・依存禁止・罠。
**いまの挙動の正はコードとテスト**。実測の表と結論は [../TOPOPT_THICKNESS.md](../TOPOPT_THICKNESS.md)・[../TOPOPT.md](../TOPOPT.md)(段階4は「二値探索の方式と契約」の「段階4」)・[../TOPOPT_SPEED.md](../TOPOPT_SPEED.md)・[../TOPOPT_STOP_RULE.md](../TOPOPT_STOP_RULE.md)。二値探索の各段・製品配線は [topopt-binary-search.md](topopt-binary-search.md)。

項目末尾の括弧は出典のタスクID(先頭8桁が日付)。計測は一度きりのスクリプトで行い、コミット `312aac5` で削除した(再現が要るときは `312aac5` の親から取り出す)。生の測定JSONは `output/`(git追跡外)にあった。

## 部材の厚み・保持小体の計測(`docs/TOPOPT_THICKNESS.md` 節A〜G)

- **測定・検証スクリプトに「製品判定の写し」を持たせるときは、製品の入口関数と突き合わせる。** `optimize_run._region_representation`(`MIN_REGION_ELEMENTS=8`)や確定表 `optimize_run._map_boundary_conditions` は製品の関数そのものを呼び、独自に要素数・節点数を数え直さない。(20260918-0239-topopt-measure-thickness-layers, 20260918-1330-topopt-measure-small-keep-bodies)
- **単一ボディ(板だけ)では「位置合わせ依存」を測れない。** `design_space.grid_for_bbox` は点群の中心を基準に格子を再センタリングするため、板を平行移動しても格子が一緒に動く。題材は「対象+固定パッド+荷重パッド」の3ボディにする(`Conditions` が拘束面・荷重面を1枚以上要求する)。(同上)
- **曲げ応力の取りこぼしの指標は、必ず各 n(層数)の理論値で正規化した比で作る。** 生の応力・安全率を異なる n の間で直接比べると、梁の絶対寸法が n で変わり無次元理論値が `~1/n^2` でスケールするため、解像度起因の誤差を表さない。n=1 のとき理論値は厳密に0(唯一の要素の中心が中立軸と一致)でゼロ除算になるが、バグではない。結論の表(1層で43.0倍の過大評価〜n=8で1.08倍)は `TOPOPT_THICKNESS.md`。(20260918-0239-topopt-measure-thickness-layers)
- **「個数」ゲート(`_region_representation`)と確定表(`_map_boundary_conditions`)は別の物差しで独立に動く。** stop(0要素)と判定されても確定表は成立(pass)しうる(54ケース中13件。cube 9件全部、cylinder 4件)。小さい塊の要素数は s/spacing の比に対して**単調でない**(比1.8が前後の1.5・2.0より少ない、量子化の位相依存)。「比を上げれば要素数が増える」とコードに書かない。(20260918-1330-topopt-measure-small-keep-bodies)
- **採用マスクの局所厚さ `distance_transform_edt(material)*2` は下限が定義上2.0要素**(空セルと面で接する材料は必ず距離1×2)。1層の壁も2層の壁も区別できず、「2要素未満0%」は「1層が無い」証拠にならない。1層かどうかは opening(2×2×2)で見る。境界規約(格子の外を空/材料のどちらとみなすか)は両方計算して選んだ側を明記する。(20260918-1718-topopt-measure-member-thickness, 20260919-0925-topopt-member-thickness-at-2.4-and-2.0)
- **過去の記録の数字を「今の挙動」の代わりに使わない。** 同じ spacing(4.8mm)・budget(60)のラベルでも、既存記録は cells=39,313・安全率146.1、当時のHEADの実測は cells=1,104・安全率5.398(約35分の1)だった。コード版が違えば別の採用マスクになる。(20260918-1718-topopt-measure-member-thickness, 20260919-0809-topopt-precompute-region-overlap)
- **hanger 2.0mm の本番探索は単独で回し、並走させない。** 起動70秒でプロセスRSS 9.08GB・ピーク11.32GB超になり、システム空き0.18GBで自己中断した(2.4mm 本番は約60分・RSS 6.95GB)。空きメモリ15GB以上で単独に回す。メモリ監視の自己中断閾値は空き3GB以上(1GBにしたら0.5秒の間に0.18GBまで落ち間に合わなかった)。**評価予算を下げても所要時間は縮まらない**(2.4mm: 前半8評価+全材料3回で約1,740秒、残り約1,720秒。小さい形でも1評価30秒以上)。所要秒は他セッションの探索との競合込みで評価1回の実測として使えない(並走の有無で同じ試行列が 140秒→271秒に伸びた例がある)。(20260919-0925-topopt-member-thickness-at-2.4-and-2.0, 20260919-1135-topopt-stage4-stop-rule)
- 長時間(60分級)の測定の運用: Bash の最大待機は10分なので、プロセスを `Start-Process` で切り離して起動し `Wait-Process -Timeout` の前景で待つ(JSONは終了時にだけ書かれ途中経過は見えない)。起動前に `Get-Process python*` と空きメモリを見る。`verify_hanger_fix.verify()` は既定で tracemalloc を入れて numpy を遅くする(所要秒を測るときは `--no-memory-trace`)。(20260919-0925-topopt-member-thickness-at-2.4-and-2.0, 20260919-0835-topopt-stage4-peel, 20260919-1135-topopt-stage4-stop-rule)
- **測定スクリプトの横取り(patch)は「本物を呼んでから結果を横流しするだけ」にする。** `design_space.derive` 等のラッパーは計算式を書き換えて代用しない。`verify()` / `acceptance.safety_check` が捨ててしまう値(要素ごとの von Mises 場など)は、`verify()` の呼び出し**前**にパッチを当てて取る。パッチは `with` でスコープを閉じる。`acceptance.py` は `from . import stress` でモジュール属性参照なので、属性パッチは効く。(20260918-1718-topopt-measure-member-thickness)

## 確定表の節点所有権(`docs/TOPOPT.md`「確定表の節点所有権」)

- **`anchor.build_anchors` を面ごとに `faces=[face]` で絞って呼ばない。** 稜線の競合相手(他の面)が計算から消え、絞らずに作った表と別の節点集合になる。必ず `faces=None`(全面)で呼び `table[face]` だけを取り出す。同じ理由で、`allowed_nodes` を「そのボディ由来」に絞って表を作り直すと、元の表の節点が100%そのボディ由来でも節点数・面積が変わりうる(hanger 2.4mm の `fixed_face_10` / `fixed_face_7` で15→14節点)。「制限は数値上のno-op・元の表と完全一致」と書くのは誤り。(20260918-1913-topopt-measure-anchor-node-ownership)
- **「hangerでは制限を入れても大丈夫」と一般化しない。** hanger は2.4mm・要素サイズ下限1.874852mm のどちらでも元の表の節点が100%そのボディ由来で、制限は実質no-opだった。制限がノードを大きく削る局面(合成題材 cube/cylinder ボスの1.2mm で節点数ベースの所有権32〜39%、面積/重みベースでは92〜98%と2指標が食い違う)は、hanger が1.2mm を要求できない(`OptimizeInputError` で断られる。実測の下限1.874852mm)ため測れていない。(同上)
- このBash環境ではコンソールがcp932相当で、Pythonが生成した日本語を `print` すると文字化けして見えるが、`Path.write_text(..., encoding="utf-8")` で書いたファイル自体は正しいUTF-8。(同上)

## 保持・除外の判定の前計算(`acceptance.RegionOverlaps`。`docs/TOPOPT_SPEED.md`「保持・除外の判定を前計算にした」)

- 呼ぶ面: `acceptance.RegionOverlaps().overlap_volume(material, grid, shape, *, cache=None) -> (体積, 共通体積)` が `intersection_volume` の代わり。`acceptance.region_check(..., overlaps=None)` / `acceptance.inspect(..., region_overlaps=None)` に渡すと前計算から引く(既定は直接ブーリアン)。`binary_search.SearchConditions.region_overlaps` の既定は新しい `RegionOverlaps`、`None` で直接ブーリアン。`binary_search._recheck_regions_without_precompute` が段階4を出るときに採用形状を直接ブーリアンで取り直し、`diagnostics['reduction']['final_region_recheck']` は `'pass'|'not_needed'|'failed:<条件>:<状態>'`。セル単位の共通体積プリミティブは `kernel_brep.grid_cell_common_volumes` / `box_common_volume`。(20260919-0809-topopt-precompute-region-overlap)
- **前計算は探索1回のあいだだけ生きる**(格子・ソリッドが固定)。鍵は (ソリッドの同一性〈TShape・Location・Orientation〉, 格子の原点・間隔・形)。探索をまたいで持ち越さない・スレッド間で共有しない。`inspect_kwargs(bbox_cache=None)` は前計算も外す(=キャッシュ無し再検査は直接ブーリアン)。段階4の評価だけが `overrides=dict(bbox_cache=None)` で前計算を残す(意図的)。6条件の意味・報告の形・`rule` 文言は不変。`inspect_document`(過去記録)は直接ブーリアンのまま。前計算が作れない・途中で失敗したソリッドは、そのソリッドだけ直接ブーリアンへ戻る(`RegionOverlaps.fallbacks`)。(同上)
- **新旧の体積一致は 1e-9 相対ではなく、平面・単純な曲面は許容差以内、曲面ソリッド(hanger)は相対1e-6で見る。** hanger では最大相対3.7e-8ずれる(旧経路自身の加法性残差、許容差の34〜282倍と同じ桁)。`RegionOverlaps._entries` / `_CellOverlaps`(内部)と `TILE_CELLS`(現在8、`acceptance.py`)の値に依存しない。(同上)
- **罠: セルごとにブーリアン(`box_common_volume` をセル数だけ)で重なりを取る実装は、継ぎ目近傍で体積を黙って落とす。** 格子の面が形状の継ぎ目に 0.05µm まで近づくと「空」を返す(hanger 4.8mm の円板1048mm³で17.4mm³欠落)。**1e-14 の座標の違いで出たり消えたりする**ので、テストで再現を狙うと不安定。平面で切って破片を重心でセルへ振り分けるとき、切る平面が形状の端まで届かないと外の残りが1つの破片につながって重心が塊の中に落ちる(853×50×695mm の箱で2,960万mm³を余計に数えた)。(同上)

## 段階4: peel(ひずみエネルギー順位で削る。[../TOPOPT.md](../TOPOPT.md)「段階4: ひずみエネルギーの順位で削る」)

- 呼ぶ面: `binary_search.peel(mask, order, count, anchor_ids, *, min_width_cells=1, exempt=None, protected=None) -> (candidate, PeelStats)`(入力マスクは複製して作業するので変えない)。`peel` は常に表面セルしか抜かない。`PeelStats` の欄は `picked / casualties / islands / skipped_support / skipped_surface / skipped_manifold / skipped_thickness(常に0) / skipped_connectivity / skipped_neck / skipped_core / already_removed / global_checks / core_window_* / core_global / neck_full_grid / seconds / picked_cells`(`as_dict()` に `picked_cells` は出ない)。診断は `diagnostics['reduction']`(`peel_calls` / `peel_seconds` / `peel_seconds_max` / `repeated_candidates` / `geometry_rejections`〈候補ごとに `step` `components` `surviving_components` `missing` `removed_islands`〉と `steps[]` の `peel`)。幾何の不合格の Trial は `outcome='infeasible'`・`checks={'geometry':'fail'}`。(20260919-0835-topopt-stage4-peel)
- **不変条件: `peel` は入力が「1成分・不正頂点0・全条件節点に支えあり(・閉じた空洞なし・m>1なら全材料セルが m 立方に属し侵食した芯がつながる)」なら出力も同じ。** 段階4の入力(合格形状)は必ずこれを満たす。`resolve_islands` は検算で、`_geometry_rejection` が None でなければ不合格(FEMを走らせない)。**連結性由来の不合格はバグ。** 保持の重なりは `peel` が見ないので幾何の不合格に出うる。`skipped_*` は「支え→表面→多様体→連結→くびれ→芯の連結」の順に判定して最初に落ちた理由で数える(欄の並び・数え方に依存しない)。(同上)
- 順位から削ったセルが1つ(k=1)で安全率が落ちたとき、`single_cell_patience`(下の停止規則)の回数まで順位から外して続ける。k=1 の不合格のうち計算失敗・保持の重なりは安全率で外すカウントに数えない。2つ以上なら m ← m/2。同じ形で不合格と分かった候補が再び出たら解き直さず m を半減(`repeated_candidates`。評価に数えない)。(20260919-0835-topopt-stage4-peel, 20260919-1135-topopt-stage4-stop-rule)
- **島として消えるセルも `count` に数える**ので、1回の削減が要求より多くなりうる(hanger 実測で超過、最大11セル)。安全率の不合格で m を下げても、根元のセルが順位の先頭にあれば同じ島が消える。`steps[].peel.seconds` は毎回変わる。**決定性を比べるテストは `seconds` を除いて比べる**。(同上)
- `verify_hanger_fix.py` は `optimize_run.run` を通らないので `trace.DEFAULT_ROOT` の記録は出ず `output/optimization_runs` は増えない。診断ツール: `topopt_checks/verify_hanger_fix.py`(`--no-memory-trace` / `--single-cell-patience N|unlimited` / `--target-tolerance` / `--save-masks`)。(同上)
- `docs/TOPOPT.md`「現在の正」の表は、太字が数値の行だけを `tests/test_doc_code_agreement.py` が機械照合する。真偽値・文字列の行は照合されない。(20260919-0835-topopt-stage4-peel)

## 段階4: 停止規則(`single_cell_patience`。issue #22。`docs/TOPOPT_STOP_RULE.md`)

- 呼ぶ面: `binary_search.find_best_feasible(..., single_cell_patience=binary_search.DEFAULT_SINGLE_CELL_PATIENCE)`(`_reduce_volume` にも同名の引数)。`int >= 1` か `None`(順位が尽きるまで)。それ以外(0・負数・小数・文字列・bool)は `ValueError`。**既定は 8**(2026-09-24 本人判断。TOPOPT.md「段階4」の「外したセルと不合格の形を採用をまたいで覚える」)。(20260919-1135-topopt-stage4-stop-rule)
- 診断: `diagnostics['reduction']` の `single_cell_patience` / `safety_excluded_cells`(安全率で外して続けたセルの数。止める原因になった最後の1セルは含まない)/ `adopted_after_exclusion`(安全率で外したセルの後に採用へ至った回数)。`stop_reason`: `safety_at_single_cell`(patience 個続けて落ちた)/ `no_removable_cell`(順位が尽きた)/ `within_target_tolerance`(安全率が目標の許容内、§12.12)/ `repeated_evaluation_error`。**予算切れは `stop_reason=None` で `SearchResult.termination='evaluation_limit'`**。(同上)
- カウントは「削ったセルが1つで安全率が落ちた試行」だけ。k≥2 の不合格・計算失敗(`unknown`)・保持の重なりの不合格は数えない(順位から外すのは k=1 の不合格すべて)。**採用でカウントは0に戻る**。外したセル(poison)は、採用をまたいで外したまま、合格形状の安全率が不足分以上に上がったセルだけ順位へ戻す。不合格の形の内容ハッシュ `rejected` も持ち越す(TOPOPT.md「段階4」の同項)。決定的。有限回で止まる理由は `_reduce_volume` の docstring。(同上)
- **`DEFAULT_SINGLE_CELL_PATIENCE` の値そのものに依存しない。** 値は「測る前に決めた基準: 体積最小→FEM少→patience小」を hanger 4.8mm 1つ・各1回で当てて決めたもので、16 と無制限は予算1000内で同一の試行列(採用を挟まず単セルの安全率で落ちた最長が7回)だった。測定は表面だけを削る現行経路(`peel`)ではなく、削除済みの内部も削る経路で行ったため、現行経路では未測定のまま 8 に変えられた。§12.7 の数値(FEM 999・51,715 mm³・安全率 2.0157)は r_max・θ・題材・予算を変えれば変わる。所要秒(3,585秒など)は他セッションの並走込みで比較に使えない。(20260919-1135-topopt-stage4-stop-rule, 20260919-1840-topopt-no-hollow-surface-only-default)
- 当時の測定(patience 16)は hanger 4.8mm で**予算1000を使い切った**(段階4のFEM 999回)。FEM 100回目以降は採用156回で160セルしか削らない(1セル/採用)。終盤の1セルごとの FEM 判定が支配的で、安全率は 2.016 まで進み目標2まで0.8%残して予算切れ。「体積を第一にした」判断の妥当性は本人判断だった。(20260919-1135-topopt-stage4-stop-rule)
- `optimize_run.OptimizeOptions` は patience を渡さない(製品経路は既定値・予算1000)。UI・MCP から変える経路は無い。(同上)

## 中空構造を許容しない(`docs/TOPOPT.md`「形状最適化は中空構造(閉じた空洞)を一切許容しない」)

- **制約(本人決定 2026-09-19): 「duocadの形状最適化では中空構造を一切許容しない」。** 実測ではなく製品制約。実測(§12.3・§12.8)は「その制約の下でも探索は基準に並ぶ」ことを示すだけ。細い通路で外へつながる袋は定義上空洞ではない(既知の限界。通路幅の下限は未対応)。(20260919-1840-topopt-no-hollow-surface-only-default)
- 呼ぶ面: `acceptance.void_components(material) -> int`(空の6近傍連結成分の数。格子の外へつながる空を含むので、空洞なしなら 1)/ `acceptance.void_labels(material) -> (ラベル配列, 成分数)`(配列は各辺2大きい。ラベル1が外)/ `acceptance.closed_voids(material, *, limit=5) -> [dict(cells, representative_cell)]`(大きい順。`representative_cell` は格子の添字)。`acceptance.boundary_check` の報告に `void_components` が加わり、合格は `material.any() and irregular_vertices == 0 and void_components == 1`。報告キー `watertight` と6条件の数え方は不変。(同上)
- `find_initial_feasible` は設計空間に閉じた空洞があれば `input_contradiction`(評価0回)で、`diagnostics['closed_voids'] = dict(count, largest=[...])`。画面の一言は `optimize_run._search_failure_warning` の同名の分岐。除外領域が塊に包まれている入力を `input_contradiction` にする扱いは 2026-09-20 に本人が承認した。`largest` の並び(大きい順・同数なら平坦添字の小さい順)は診断表示用で契約ではない。(同上)
- **`peel` は閉じた空洞の無い入力から閉じた空洞の無い候補を作る**(抜くセルは常に空の6近傍を持つので、既存の空の成分に属する)。ランダム題材の不変条件テストが `void_components == 1` を固定している。(同上)
- 空洞検査の費用: 1回 数 ms(2.0mm・材料 640,306 セルで最大 8.3 ms、`boundary_check` 全体で約 35 ms)。1評価(数十秒〜)の 0.03% 未満。(同上)
- 測定中に `duocad/**/*.py` を編集しない(後から始まる2本目が編集後のコードを import し、JSON の `code_sha256` が食い違う)。(同上)
- **ツール経由でファイルへ書く文字列の `\v` は、シェル・Python の heredoc を通ると垂直タブ(0x0B)に化ける**(`topopt_checks\verify_hanger_fix.py` が `topopt_checks<0x0B>erify_hanger_fix.py` になっていた。git diff では見えない)。`\t` `\b` `\f` `\a` も同じ機序。Windows パスを docs や Python の文字列に書いたら、書いた後に `ord(c)<32` で改行・タブ以外の制御文字を検査する。文字列側は `chr(92)` で組み立てると化けない。(20260919-1905-topopt-no-hollow-recheck-2.4-2.0)
