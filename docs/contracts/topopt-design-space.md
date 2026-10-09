# 形状最適化の設計空間(塊)まわりの契約

形状最適化の「塊(設計空間)」を作る・検査する・表示する層と、その周辺(外接箱 bbox・空要素の除去)の
契約集。**いまの挙動の正はコードとテスト。** ここは、ソースから読めない不変条件・依存禁止・罠を、
元の作業タスクの契約から分野別に蒸留したもの。実測と設計の説明は [../TOPOPT.md](../TOPOPT.md)
(「塊の連結性と境界条件の入口検査」「固定凸包による設計空間」ほか)が持つので、重複は書かない。

出典は項目末尾の括弧 `(タスクID, YYYY-MM-DD)`。`(対象コード不在・要確認)` は、契約が名指す関数・
ファイルが現在のソースに見つからないもの(2026-09-17 の段階6で `simp.py`・`safety_loop.py`・`bench.py`・
`anchor.run_pipeline`・`design_space.run_topopt`・案1の外接直方体導出・案5の粗い段・参照順位による切り出しなどが
コードごと削除され、塊は固定凸包の `design_space.derive` 1本になった)。安全率・体積目標・縮小検証は
[topopt-volume-stress-safety.md](topopt-volume-stress-safety.md)。

## 塊をマスクで受ける入口(voxelize.model_from_design_mask)

- 入口は `voxelize.model_from_design_mask(grid, design, *, anchor_shape, keep=None, obstacle=None, ...)`。
  `design` は `grid.shape` と同じかたちの bool マスクで**複製して持つ**(呼び出し側が後で書き換えても
  `VoxelModel` は変わらない)。かたち違い・全 False は `ValueError`。 (20260903-2216-topopt-split-design-space-from-anchor-soup, 2026-09-03)
- `anchor_shape` は `TopoDS_Shape` / build123d オブジェクト / 既に作った `TriangleSoup` のいずれか。
  **面添字は渡したものの面の並びで決まる**ので、面を選ぶのに使った `TriangleSoup` をそのまま渡すこと
  (作り直すと面添字が食い違いうる)。 (20260903-2216-topopt-split-design-space-from-anchor-soup, 2026-09-03)
- `VoxelModel` の形は `grid` / `design` / `keep` / `obstacle` / `soup` / `stats`。`keep` / `obstacle` が
  `design` の内側に収まる不変条件を持つ。マスク経路の `stats` は `design_source="mask"` が付き、
  **`occupancy` キーは無い**(塊がソリッド由来でないため)。ここに依存しないこと。 (20260903-2216-topopt-split-design-space-from-anchor-soup, 2026-09-03)
- 保持・除外が塊からはみ出したときは **`voxelize.OutsideDesignSpaceError`**(`ValueError` の派生。
  既存の `except ValueError` が効く)。要素数は `e.outside`(`{"keep": n, "obstacle": n}`)で機械から引け、
  保持と除外が両方はみ出したら**両方を1つの文面に**出す。 (20260903-2216-topopt-split-design-space-from-anchor-soup, 2026-09-03)
- 切り詰め用の `on_outside="clip"` は現行の `model_from_design_mask` に無い(常に raise)。旧 `voxelize()` との一致を
  担保していた口なので、clip を前提にした呼び出しを書かないこと。 (20260903-2216-topopt-split-design-space-from-anchor-soup, 2026-09-03) (対象コード不在・要確認: `on_outside` 引数)
- 新しい検査から旧 `anchor.run_pipeline` を呼ぶときは `stl_path` を必ず渡す、という罠があった(既定の
  `output/bracket.stl` は `check_anchor.py` の成果物で、一度上書きして git で戻した)。この共有パスへ書く構造は、
  **同種の「繰り返し呼ぶ実行関数」を書く後続にも当てはまる**ので、実行のたびに出力先を明示すること。 (20260903-2216-topopt-split-design-space-from-anchor-soup, 2026-09-03; 20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04) (対象コード不在・要確認: `anchor.run_pipeline`)

## 塊の導出(design_space.derive)

現行の導出は固定凸包(`derive`、詳細は [../TOPOPT.md](../TOPOPT.md)「固定凸包による設計空間」)。以下は導出方式が
変わっても生きている契約と、方式固有の数字の読み方。

- **`Conditions` に設計空間ソリッドを渡す口は無い。** 検査(旧[5])がフィールド集合そのものを assert していた。
  フィールドを足す・変えるときは検査と一緒に直す(現行は `keep` / `fixed_faces` / `load_faces` / `obstacle` / `anchor_shape` / `bodies`。
  導出時点の旧フィールド `fixed_face` / `load_face` / `load_n` は現行に無い)。 (20260903-2234-topopt-design-space-from-bbox, 2026-09-03; 20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04; 20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- **`DesignSpace.model.obstacle` は常に全 False。** 除外ソリッドは塊から引き切ってあるので、`model_from_design_mask` に
  同じ除外を `obstacle` として渡してはいけない(塊 = 候補 − 除外と定義した以上、渡すと全要素がはみ出す)。
  `on_outside="clip"` へ倒す逃げも取っていない。 (20260903-2234-topopt-design-space-from-bbox, 2026-09-03)
- 塊の組み立て順序は検査に固定されている: 条件の点群(保持ボディ・条件面を含むボディ。**除外は入れない**)→
  余白(最長辺 × 比率)→ 格子 → 候補 − 除外の占有 → 格子外ガード → `model_from_design_mask`
  (`keep` を渡し `obstacle` は渡さない)。順序を入れ替えると検査が落ちる。 (20260903-2234-topopt-design-space-from-bbox, 2026-09-03)
- **面アンカーの表は全面・既定の半径で作る**(`an.build_anchors(soup, grid)`)。`faces=` で対象の面を絞ると
  稜線の規則の競争相手が減り、絞らずに作った表とは**別の節点集合**になる(同じものを見ているつもりで違うものを
  見ることになる)。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- **塊は読み取り専用で、導出は1実行に1回。** 候補材料量ごとに塊を導出し直す(旧案a)経路は削除済み。塊を作り直す
  層を足すなら、`mask_hash` が候補間で一致することを検査が突き合わせている口(`DesignSpace.mask_hash`)を壊さないこと。
  (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04; 20260911-1811-topopt-design-space-width-and-size, 2026-09-11)
- **余白比率の数字の読み方(案1の外接直方体導出での実測。凸包導出にも通じる読み方)**:
  - 壁に接した材料の割合は「塊比」でなく「材料比」で見る。塊比は余白を広げると分母が膨らんで勝手に下がる
    (既定 0.25 でも材料の 8.9% が塊の壁に接し、0.50 でも 3.3% で下げ止まった)。
  - 余白 = 最長辺 × 比率なので、**回転すると最長辺が縮み塊が小さくなる**(45° 回すと最長辺 63 → 55.9mm で、
    30° のほうが 45° より塊が大きい)。「45° が最悪」と決めつけない。
  - 余白比率 0.00 でも当時の検査[5]-[9]は全部緑になる。既定値の根拠は検査ではなく実測。
  (20260903-2234-topopt-design-space-from-bbox, 2026-09-03)
- 「切れている系は SIMP が落としてくれる」に依存しない(連結性の節を参照)。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- 参考: 余白比率の既定は 0.20(`DEFAULT_MARGIN_RATIO`、旧案1の 0.25 から 2026-09-16 の本人判断で下げた)。
  `margin_ratio < 0` は `ValueError` で弾くので、旧検査[9]にあった「負の余白で保持が格子外へ出る梯子」は現行には無い。 (20260903-2234-topopt-design-space-from-bbox, 2026-09-03)

### 旧導出(削除済み): 案1 外接直方体 / 案5 粗い段 / 参照順位切り出し

現在のコードには無い(段階6)。数字の読み方と罠だけを残す。

- 案1は外接直方体 + 余白、入口は `design_space.derive` と `run_topopt`、既定出力 `output/design_space_case1.stl`。案5
  (`derive_from_coarse_run`)は粗い段の結果を膨張して再サンプルした塊で、**案1の箱の中でしか塊を作らない**
  (`mask = (膨張後 | 条件) & fine.mask`)。 (20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04) (対象コード不在・要確認: `derive_from_coarse_run`, `grow_by_mm`, `resample_nearest`, `COARSE_*`, `measure_case5`)
- 案5の不変条件: 4条件の要素(保持・拘束アンカー・荷重アンカー)は無条件で OR する(外すとアンカー要素が9個はみ出した。
  保持だけで「4条件が入る」を見ると素通り)。膨張は**本番の格子**でかける(粗い格子でかけると量子が粗い要素サイズ
  6.75mm になり 0/2/4mm が同じ塊(2,414 要素)になる)。膨張を層数で数える実装では 8mm も 12mm も箱を埋め尽くした。
  (20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04)
- 案5の既定値(粗い段の長辺 14 要素・20 反復・閾値 0.3・膨張 12mm)は検査でなく実測で決めた。膨張 16mm 以上だと案1より
  遅くなる(31.81 秒 対 31.74 秒)。閾値 0.2/0.3/0.5 は既定の膨張では壁接触を 9.0/9.1/9.0% にしか動かさず、
  閾値が効くのは膨張が足りないときだけ。 (20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04)
- 依存してはいけない: 「膨張 0 なら連結性ガードが落ちる」(最近傍の再サンプルは連結性を壊さず 1 成分のまま)、
  「膨張を絞るとコンプライアンスが悪化する」(塊が案1の半分でも C は 3.1788 対 3.1796 で区別が付かない。膨張が
  守っていたのは答えの質でなく「要求した材料が塊に入ること」)、「体積目標が容量の内側なら安全」(膨張 0 の塊は
  ちょうどその材料が入る大きさになり、目標が容量の 92.8% に張り付いて OC が `ZeroDivisionError`)、
  「案5は案1より必ず速い」「格子が縮む」(格子は案1と同じで、速くなるのは空要素が増えて CG の1反復が軽くなるため)。
  (20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04)
- 案5を既定にした時点の実測(8 設計反復の短い測定): 合計 0.93〜1.25 倍で n=30 の4行はすべて案1より遅い(粗い段が
  1.3〜4.0 秒の固定費)。速い/遅いを主張するときは反復数を言うこと。塊は題材で案1の 0.67〜0.91 倍。CG 反復は
  0.90〜1.00 倍でほぼ動かない。体積目標が容量の 98% で OC が落ち 95% で通る(フィルタ後の到達上限 96.8%)ので、
  「境目」は形で決まり定数にしない。 (20260904-1416-topopt-adopt-coarse-run-as-default, 2026-09-04)
- 導出の入口は `derive_default` / `DEFAULT_DERIVATION` / `DERIVATIONS` の1点に寄せ、`server.py` / `perf.py` が導出を
  名指ししないことをソース grep の検査([13-5])で見ていた。`derive_default(derivation="bbox", coarse_...=...)` は
  `TypeError`(黙って捨てると「渡したのに効いていない」になる)。 (20260904-1416-topopt-adopt-coarse-run-as-default, 2026-09-04) (対象コード不在・要確認: `derive_default`)
- 粗い段にも `voxelize.occupancy` のレイ縮退の下限がある(長辺 10 要素で「格子が形状をまったく覆っていない」)。粗い段の
  長辺を 12 に落とすと薄板だけが落ちる。粗い段を 12→22 に細かくしても答えは変わらず秒数だけが増えた(0.65→5.29 秒)
  ので、「粗い段が粗すぎるのでは」と疑う前に測ること。 (20260904-1400-topopt-design-space-from-coarse-run, 2026-09-04; 20260904-1416-topopt-adopt-coarse-run-as-default, 2026-09-04)
- 粗い格子の padding(`voxelize.grid_for_bbox` の `DEFAULT_PAD=1`)は要素サイズが大きいほど短軸を相対的に大きく膨らませる。
  極端なアスペクト比(plate)で粗い段の要素数を下げすぎると、短軸の padding が板厚を何倍にも超えて格子が形状を覆わず
  レイ縮退の `ValueError` になる。ブラケット向けの値(粗い段 12 要素・`dilate_mm` 8.0)はそのまま流用できない。
  (20260904-1437-topopt-safety-factor-outer-loop, 2026-09-04)
- 後継の「参照順位 + 幅つき回廊」切り出し(`build_reference` / `derive_from_reference` / `PICK_RATIO=0.60` /
  `K_MATERIAL=2.0` / `width_corridor_length` / `_assert_rmin_matches_space`)も削除済み。残る知見: 摘み比率 p を振った
  実測(`hanger_bar_attachment` 2.412mm・材料 70,634.5mm³・40 反復)で p=0.08(塊 87,752 要素・保持4つ生存・
  C=121.1)だけが受入を通り、p=0.16(塊 175,503・保持 #1,#4 が alive 0.000・C=164.6)は保持が2つ落ち、p=0.60
  (塊 658,136)は `SolveResidualError`。**幅つき回廊を満たすこと(道を用意すること)は、最適化がその道に材料を置くことの
  十分条件ではない。** `hanger_test` 2.412mm(箱が 3.4 倍小さく病理を構造上再現できない題材)では p=0.60 でも反力ゼロ・
  保持消失は起きず、安全率は p が大きいほど単調に下がり(0.08/0.16/0.60 で 2.605/2.357/2.156)秒/反復はほぼ線形に増えた
  (12.53/14.84/46.03)。大きい塊ほど安全でも速くもない。病理を出せない題材で「直った」と判定しないこと
  ([../TOPOPT.md](../TOPOPT.md)「SIMP時代の記録」)。 (20260911-1811-topopt-design-space-width-and-size, 2026-09-11) (対象コード不在・要確認: `PICK_RATIO`, `derive_from_reference`, `build_reference`)
- その経路の配線: 参照は `build_reference` で1回(31分)だけ作り、探索の各段が `derive_from_reference(reference=...)` で
  使い回す(渡し忘れると毎段 `build_reference` が走り直す)。`rmin` は `build_reference` で要素単位に畳み、
  `build_reference→derive_from_reference` と `derive_from_reference→run_topopt` の2箇所で一致を検算していた。幅つき判定を
  満たせないときは `DesignSpaceDisconnectedError.unreached`(届かなかった条件名のリスト)で機械分岐でき、文面をパース
  しない。 (20260911-1811-topopt-design-space-width-and-size, 2026-09-11) (対象コード不在・要確認: `Reference.cache`, `_model_for_mask`)

## 塊の連結性(connect_seeds / connect_conditions)

説明は [../TOPOPT.md](../TOPOPT.md)「塊の連結性と境界条件の入口検査」。旧入口 `assert_load_path` / `assert_faces_connected`
(拘束↔荷重のラベル共有を見る)は `connect_seeds` / `connect_conditions` に置き換わった。

- **連結性は6近傍。** 26 近傍だと角1点で接する2島を「つながっている」と数える(角で触れる2要素が共有するのは
  節点1つで、回転を止められないピン接合にしかならない)。`NEIGHBOURHOOD = 6`。`element_components(neighbourhood=18/26)` は
  「定義を変えると答えが変わる」を検査で見せる口で、導出の経路からは 6 以外を渡さない。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- **例外は `DesignSpaceDisconnectedError`。`DesignSpaceTooSmallError` の派生にしない**(あちらは「余白を上げろ」の文面で
  分断には効かない)。属性 `components` / `unreached` / `seeds` で機械から引く(旧 `fixed_labels` / `load_labels` / `shared_labels` は
  現行に無い)。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03; 20260911-1811-topopt-design-space-width-and-size, 2026-09-11)
- **見ないもの**: 保持ソリッドの連結性(浮いていても最適化が材料でつなぐのが正しい)、道の細さ(幅1要素の糸でも「つながっている」)、
  最適化後の材料の連結性(`mesh.remove_islands` は出力の後処理で塊の不備は直せない)。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- **条件面のアンカーが空のときは判定せず `{"skipped": "empty_anchor_faces", "empty_faces": [...]}` を返す**(範囲外の別問題)。
  `stats["connectivity"]` が必ず通常の report だと思わないこと。理由: `AnchorTable.element_mask` は `node_ids` 経由で
  `_require_nodes` を通るので、節点が1つも付かない面を渡すと `ValueError` になり、連結性の手前でこれに化けると
  診断の文面が別物になる。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- **「切れている系は SIMP が落としてくれる」に依存しない。** 実測は6配置中4配置が最後まで走って STL を出し(例外も警告も無し)、
  落ちた2配置も文面に設計空間も連結性も出ない `ZeroDivisionError` だった。`emin`(既定 1e-9)が小さいと simp の残差検算は
  黙る(1e-12 まで下げると作動する)。**塊の分断は塊を組み立てる側でしか安く確実に検出できない。** (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- 検査題材の `severed_conditions(angle_deg=0.0, *, thickness=10.0)`(`topopt_checks/_fixtures.py`)の壁は、塊の断面を
  端まで塞ぐ大きさ(200×200mm)にしないと縁を回り込む道が残って「切れている」配置にならない。厚みも要素サイズより
  十分厚くする(要素中心が壁に入らないと1要素も削れない)。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- 連結性ガードの位置は、かつて「格子外ガードより**後**」に実測で固定した(前に置くと、負の余白比率で保持が格子外へ出るより
  先に条件面のアンカー要素が塊から消え、梯子の verdict が「格子外」から「アンカー要素が塊の中に1つも無い」へ化けた)。
  2234 の契約の「前」を意図的に外した点。現行の `derive` は `margin_ratio < 0` を弾くので、この梯子自体が無い。位置を動かすなら
  診断の判断(「除外が保持を食っている配置では保持のはみ出しのほうが原因に近い」)ごと引き継ぐこと。 (20260903-2234-topopt-design-space-from-bbox, 2026-09-03; 20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- 検査[6]の除外(`Box(10,30,30) @ (-30,0,0)`)は塊の断面を塞がないので連結性は通る(1成分・拘束側の要素 63 個が生存)。
  「検査[6]が位置で落ちる」という当初の記述は誤りだった。変異検査で assert を殺せない検査[5]は検出力が未証明のまま。
  (20260903-2234-topopt-design-space-from-bbox, 2026-09-03; 20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)
- 環境の罠: Bash ツール経由の Python で書いたファイルが、書き戻し直後の読み出しでは反映されているのに次のコマンドで
  元に戻っていたことがある(`check_design_space.py` の1箇所。2回再現)。ファイル改変後は必ず実物を読み直す。 (20260903-2235-topopt-design-space-connectivity-guard, 2026-09-03)

## 条件面が空のとき(要求要素サイズより小さい面)

説明は [../TOPOPT.md](../TOPOPT.md)「塊の連結性と境界条件の入口検査」の後半。呼ぶ面は
`AnchorTable.empty_face_diagnosis(face_index)` / `condition_face_diagnoses(fixed_faces, load_faces)` /
`anchor.format_condition_face_warning(diagnoses)`(診断キーはソース参照)。

- **前提の訂正(実測で確認)**: 荷重面が空でも「荷重ゼロで黙って解く」ことは無い。`AnchorTable.node_ids`
  (`fixed_dofs` / `load_vector` / `element_mask` が通る)は `_require_nodes` で、空の面が1つでも混ざれば
  `ValueError` で落ちる(`check_anchor.py` [6] が固定)。**この下層のガードは弱めてはいけない。**「落とさない」という指示は、
  その手前に置く**入口レベルの通知**の話で、下層の既存ガードではない。 (20260904-1556-topopt-warn-on-empty-condition-faces, 2026-09-04)
- 入口は `ValueError` へ落ちる**手前**で空の条件面を診断し、実行を止めて診断を返す層(現行は
  `optimize_run._empty_condition_faces(space)` と `state="empty_condition_faces"`。旧 `anchor.run_pipeline` の早期return は削除)。
  早期return時の戻り値は限られたキーだけで通常成功時のキー(`achieved_material_mm3` 等)は無いので、
  **`empty_condition_faces` が非空かを先に見てから他のキーへ触ること**(さもなくば `KeyError`)。通常どおり完走した戻り値には
  `"empty_condition_faces": []` が常に入る。 (20260904-1556-topopt-warn-on-empty-condition-faces, 2026-09-04)
- `representative_length_mm` / `suggested_max_spacing_mm` は `√cad_area_mm2` の**近似**(正方形とみなした代表寸法。
  細長いスリットや曲面では過大/過小)で、保証ではなく目安。「条件面からの要求要素サイズ」として流用しても精度の根拠には
  ならない。厳密な下限が要るなら、候補の要素サイズで実際に `empty_faces()` を取り直す。`AnchorTable` は `soup` を持たない
  ので、面の bbox から代表寸法を出す案は API が重くなるため採らなかった。 (20260904-1556-topopt-warn-on-empty-condition-faces, 2026-09-04)
- `absorbed_by_faces`(稜線の規則でその面の `raw_node_ids` を持っていった面)が1件以上入ることに依存しない。標本点が1つも
  節点の球に入らない極端に小さい面では空リスト。 (20260904-1556-topopt-warn-on-empty-condition-faces, 2026-09-04)
- **「戻り値の形を変える変更は、呼び出し元を grep し尽くしてから完了と宣言する。」** 当時は「入口を2つ塞いだ」だけで、
  その入口の戻り値を使う `derive_from_coarse_run`(既定の導出)が空条件面の早期returnを受けて `AttributeError` を出す穴を
  QA に指摘された(専用例外 `CoarseRunConditionFacesEmptyError` で修正)。同型の未対応として、`perf.py` の測定関数群・
  本番解像度の最終 solve を呼ぶ安全率ループの評価が `run["result"]` を無条件に読む箇所が当時は残った。実の面選択を
  配線する側は、入口の戻り値・例外の全分岐を自分で確認すること。 (20260904-1556-topopt-warn-on-empty-condition-faces, 2026-09-04) (対象コード不在・要確認: `CoarseRunConditionFacesEmptyError`, `safety_loop._derive_and_run`, `run_topopt`)

## 外接箱 bbox(kernel.bounding_box と voxelize._shape_bbox)

説明と実測は [../TOPOPT.md](../TOPOPT.md)「OCCT の bbox はトリムを見ない」・[../CAD_LIBRARY_PITFALLS.md](../CAD_LIBRARY_PITFALLS.md)。

- **`kernel.bounding_box` と `voxelize._shape_bbox` は `BRepBndLib::AddOptimal_s(shape, box, False, False)`。`Add` に戻さない。**
  `Add` は面のトリムを見ず、曲面部品の寸法が最大9倍に狂う。`_shape_bbox_trimmed` は削除済みで探さない。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- `bounding_box(shape, *, cache=None)` の `cache` はキーワード専用。省くと毎回 OCCT を叩く(正しいが遅い)。繰り返し呼ぶ経路
  (`mcp_server.describe_document` / `describe_shape`・`optimize_run` の条件 bbox 計算)は `cache=document.bbox_cache` を
  必ず渡す。**渡し忘れても答えは正しいので検査では気づけない**(定常状態が 0.5ms から 145ms になるだけ)。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- **`BoundingBoxCache` を bbox 以外へ流用しない。** 鍵(`_ShapeKey`)の同一性 `IsSame` は Orientation を見ないので、法線・面の向き・押し出し方向の
  ような向きに依存する値を載せると静かに壊れる(向きまで見る鍵は別に `_OrientedShapeKey`)。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- **キャッシュをモジュール変数にしない。** 寿命は文書の切り替えまで(2026-09-07 ユーザー決定)。`Document.__init__` が
  `bbox_cache` を持ち、`Session._replace_document` が新しい `Document` を作ることで自動的に捨てられる。`reset()` 相当の
  後始末を `Session` 側に足すと呼び忘れの穴を自分で作る。保存はされない。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- `bounding_box` が返す dict は**毎回作り直す**(呼び出し側が書き換えても波及しない)。最適化でこれを外さない。外へ出る
  のは `extents()` の6要素タプルだけで、キャッシュの中身の型に依存しない。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- `voxelize._shape_bbox` にはキャッシュを載せない(導出1回につき2〜3回しか呼ばれない)。ループから呼ぶ経路を新設するなら
  そのとき考えること。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- **`bounding_box` が速いことに依存しない。** 初回は1形状 18〜60ms、27ボディの `describe_document(include_bbox=True)` は初回 +1.6 秒で、
  キャッシュは初回を1つも減らさない。承知の上の決定(2026-09-07 ユーザー: 29% のボディの寸法が最大9倍狂うほうが害が大きい)
  なので、「遅くなったから戻す」を後から言わない。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- 検査の題材は `models/` の実ファイルでなく `BRepPrimAPI_MakeTorus(100.0, 10.0, π/6)` をその場で生成する(利用者の文書は変わってよい)。
  トリムした環状片でないと `Add` と `AddOptimal` の差が出ない: 部分円柱・部分球は両者が厳密に一致した。**全周のトーラスでも `Add` は外す**
  (R100/r10 の全周で 238.13mm、真値 220.00mm)。差し替え後の実測 `trimmed_torus_bbox_slack_mm` は 9.063142 → 0.010765、
  `untrimmed_bbox_slack_mm` は 0.012431 のまま。 (20260907-2201-bbox-curved-geometry-coverage, 2026-09-07)
- 検査の基準は `AddOptimal` の返り値でなく**三角形分割の節点**(必ず真の面の上にある)。`kernel.tessellate` の戻り値の `bbox`
  を基準にしない(`_bounding_box(faces)` の作り方が変わった日に循環参照になる。`tests/test_kernel_bbox.py` は `faces[].positions` から
  自前で min/max を取る)。許容差は三角形分割の deviation(三角形分割の箱と真値の差は deviation 0.2mm 指定で 0.012mm と
  ずっと小さいが、許容を deviation より小さく取ると機械や OCCT の版で不安定になる)。 (20260907-2201-bbox-curved-geometry-coverage, 2026-09-07)
- 「未達の検査は既知の未達(known gap)として留め、差し替えで落ちるのを正とする」運用があった(`xfail(strict=True)` と
  `check_known_gap`)。差し替え後は留め具を外して通常の検査へ格上げ済みで、現行のソースに留め具は無い。 (20260907-2201-bbox-curved-geometry-coverage, 2026-09-07)
- **コードを直すと嘘になるドキュメントは `grep AddOptimal` では見つからない。** `BRepBndLib` と `_shape_bbox` の両方で `*.md` を掃く
  (当時 `docs/TOPOPT.md` に `Add_s` の実測「実物STEP(231面)で0.003秒」が残っていた)。受入検査の MEASURED は
  トーラス片の題材を除いて1つも動かなかった = 他の題材は `Add` と `AddOptimal` が一致する形しか通っていない。 (20260907-2202-bbox-addoptimal-with-cache, 2026-09-07)
- 検査の実行はパイプに通さない(`| tail -5` の終了コードは `tail` のものになる)。ファイルへリダイレクトして終了コードと
  `[FAIL]` の件数の両方を見る。 (20260907-2201-bbox-curved-geometry-coverage, 2026-09-07)

## 空要素を剛性行列の組み立てから外す(void除去。fem.Model の active_mask)

説明と実測は [../TOPOPT.md](../TOPOPT.md)「空要素を組み立てから外す」。

- 口は `fem.Model(..., active_mask=None)` のみ。`(nelx,nely,nelz)` か `(nele,)` の bool マスクで、True の要素だけで K を組む。
  `Model.active` は `np.flatnonzero(mask.ravel())` か `None`(**常に配列だと思わない**。`None` のときは全要素が組み立てに載る)。
  **`Model.edof` は間引かない**(`Model.compliance` と `stress.py` が全要素で引く)。`_coo_index_arrays(edof)` は各要素 576 項の COO 添字で、
  並びは `ke.ravel()` と対応する契約(`np.kron` 版との完全一致を `check_fem.py` が固定。元は `check_simp.py` [6])。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09)
- **`extra_fixed_dofs` と `active_mask` は同じ `void` から作る。** 片方だけ渡すと、組み立てから外した要素にしか接していない自由度が
  `free` に残り K_ff に全零行ができる(孤立 396 個)。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09)
- **目的値は解いた系のものに揃える。** 組み立てが active だけなので `Model.compliance` の `c` も active だけで足す(`c == F・U`)。`ce` は
  感度用に全要素ぶん返す。組み立ての範囲を変えるときは目的値の総和範囲も一緒に変える。`void_mask` を渡さない系の検査は
  この経路に届かない(「検査が自分の変えた経路を通るか」を確かめる)。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09)
- 自由自由度の集合は void除去で1つも変わらない(`model.free` の SHA-256 が変更前と一致。hanger 5.368mm で `3e4a0b2fb445d708`)。
  「未知数が減る」前提の最適化を後から足さない。`void_mask` を渡さない呼び出しが `active_mask=None` で追加前と同じ経路を通ることの証拠として、
  プリセット側の指紋(旧 `check_absolute_volume.py` [4])は動かしてはならなかった。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09)
- 疎パターン固定・matrix-free(ブランチ `topopt-assembly-comparison` の条件3・4)は main に無い(`PATTERN_BUILD_CHUNK` /
  `_csc_value_positions` / `free_matvec` / `element_moduli` / `ASSEMBLY_STRATEGY` は移していない)。切り替えフラグも無く、切り戻すなら
  `git revert e86f0f8`。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09)
- **必要メモリを箱の要素数(`optimize_run.MAX_ELEMENTS`)から見積もらない。** ピークは塊(組み立てに載る要素数)にほぼ比例し、塊1要素あたり
  約 28kB(実測3点)。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09)
- hanger は 2.4mm では製品の経路(`optimize_run.run`)を通らない: `_check_anchor_centroids` が `FaceMappingError` で落ちる(SIMP の前・2 秒)。
  荷重面が `kind="torus"` で、焼き込まれた `center` と面アンカーの節点重心の距離が**解像度に依らず 12.6〜13.0mm** ある一方、許容は
  `3 × 要素サイズ` なので細かくするほど厳しい。**両方の門を通るのは 4.4mm と 4.8mm だけ**(4.4mm でも余裕 0.15mm)。
  `_region_representation` は要素サイズに対して単調でない(4.2mm で0要素の領域が 4.4mm で1、4.6mm でまた0)ので「N mm 以下なら通る」と
  書けない。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09) (要確認: この門は 2026-09-17 の境界条件確定で作用位置の門に置き換わった。現行の許容は [../TOPOPT.md](../TOPOPT.md)「境界条件の確定」)
- 並行セッションが同じリポジトリを大きく改修中でも、受入検査は使い捨て worktree(`git worktree add --detach`)で隔離して回せる。
  `git commit -- <path>` はそのパスの作業ツリーをそのまま入れるので、相手が触っている文書(`docs/TOPOPT.md` / `README.md`)は
  相手のコミットを待つ。 (20260909-0518-topopt-void-removal-to-main, 2026-09-09)

## 条件の可視化(開発用デモの4色表示)

製品の条件表示・デバッグ表示は [../TOPOPT.md](../TOPOPT.md)「開発用のデバッグ表示」。以下は probe デモ(`server.py` /
`web/index.html`)で作った面で、**現行のソースにデモ本体は無い**(`_get_demo_space` / `probe.setVisible` / `pixelCensus` は
`web/src` にも無い)。画面で条件を色分けして検査する仕組みを作り直すときの罠として残す。

- 「見せている問題」と「解いている問題」を同一オブジェクトにする: 表示用の既定表示と計算用のボタンが同じキャッシュした
  `DesignSpace`(`_get_demo_space()`)を読んだ。表示用と計算用で `derive()` を2回呼ぶ経路を足すと別物に戻る。キャッシュは
  プロセス生存中だけで、`design_space.py` を書き換えても動いている `server.py` を再起動しない限り古い塊のまま応答する。
  (20260903-2304-topopt-show-four-conditions-in-duocad-colors, 2026-09-03) (対象コード不在・要確認: `server.py:_get_demo_space`, `_build_condition_payload`)
- 座標は index 空間(`(mm - grid.origin) / spacing - shape/2`)で送る。拘束面ぶんの三角形は保持の三角形群から
  `setdiff1d` で除いて塗り分け、荷重面は塗り分けを変えず矢印/ラベルだけにした。 (20260903-2304-topopt-show-four-conditions-in-duocad-colors, 2026-09-03)
- **「実行」ボタンは条件応答が届くまで `disabled` で始める。** 外すと、条件読み込み用ソケットと実行用ソケットが同時に走り、後勝ちの
  メッセージが状態を上書きして `done` / `error` に辿り着けなくなる(実機で 15 分タイムアウトを1回踏んだ)。Puppeteer の
  `page.click` は `disabled` なボタンに例外を投げず黙って何もしないので、撮影スクリプトは状態が `conditions` になるまで
  待ってからクリックする(ログには「押した」と出るので原因が分かりにくい)。 (20260903-2304-topopt-show-four-conditions-in-duocad-colors, 2026-09-03)
- **カメラの向きは検査の一部。** 軸整列の拘束面(法線 `-x`)は、カメラが十分 `-x` を向いていないと**同じパッドの隣接面に幾何学的に遮られて**
  ほぼ画面に出ない(バックフェースカリングでなく自己遮蔽なので `side: THREE.DoubleSide` では直らない)。向き `(-0.75, 0.5, 0.6)` を変えると
  画素集計の閾値と検出力の検査が両方壊れる。 (20260903-2304-topopt-show-four-conditions-in-duocad-colors, 2026-09-03)
- **画素集計の閾値は「1色ずつ隠して実際に0近くまで落ちること」で検証する。** 集計値が「そこそこの数」に見えても、半透明の境界線
  (`#5577aa` が背景と混ざった `(48,56,88)` 前後)の色を数えているだけで拘束面を1枚も検出していない閾値だった(表示/非表示で画素数が
  変化しなかった)。閾値はチャンネル比で作り、`b>1.6g` で境界線の混ざり色を弾く。色・カメラ・光源のいずれかを変えたら再確認する。
  (20260903-2304-topopt-show-four-conditions-in-duocad-colors, 2026-09-03)
- 画素集計の `fixed`/`load` フィールド(旧アンカー球表示用)と4色判定(`keep` / `exclude` / `fixture` / `loadColor`)は別物で、同時に描かれない前提で
  両方の式を毎回計算しているだけ。同じ閾値を使い回さない。表示時の画素数は 7000〜10000 程度、解像度(`DEMO_ELEMENTS_LONG_AXIS=22` /
  `DEMO_MAX_ITER=30`)を変えたら閾値を再確認する。`output/server_conditions_demo.stl` はボタンのたびに上書きされるので安定した成果物として扱わない。
  `?debug=space`(塊のボクセルを薄く重ねる)・`?debug=anchors`・`?voxel=1` は共存する(現行は `web/src/debugFlags.ts`)。 (20260903-2304-topopt-show-four-conditions-in-duocad-colors, 2026-09-03)
- 画面を既定の導出に倒しても `shot.mjs` は落ちない(見ているのは `iterations >= 1` と状態・三角形数だけ)ので、倒したことは数字を
  目で見て確かめる。撮影スクリプトのポートは空きを環境変数で渡す。 (20260904-1416-topopt-adopt-coarse-run-as-default, 2026-09-04)

## 文書・コメントの縮小で踏んだ罠

- **見出しを統合・改名・削除したら、削除した見出し文字列を全文 grep して本文中の名指し参照を直す。** `docs/TOPOPT.md` の「変異検査の記録」
  9節を1節に畳んだときに、存在しない見出し名への参照が1件残った。対象には `duocad/topopt/*.py` のコメントも含める(コード側に同種の
  参照を新設した)。 (20260905-0510-topopt-shrink-docs, 2026-09-05; 20260905-0511-topopt-shrink-comments, 2026-09-05)
- **`topopt_checks/check_design_space.py` の旧[12-6]は `docs/TOPOPT.md` の「題材(4つ)と、何を覆うために居るか」節の表の文字列を探す。**
  節を移動・改名するときは検査を先に確認する(文字列一致だけを見ているので気づきにくい)。旧[13-5]は `perf.py` / `safety_loop.py` の
  ソース(コメント行を除外)に導出の名指しが無いことを見る。 (20260905-0510-topopt-shrink-docs, 2026-09-05; 20260905-0511-topopt-shrink-comments, 2026-09-05) (要確認: 4題材・perf.py・safety_loop は削除済みで、現行の検査にこの文字列一致が残っているかはソース参照)
- 変異検査の記録は1つの表(列: 日付 / 対象→検査 / 変異 / 結果)に追記する。新しい検査のたびに新見出しを立てない。単純な行に
  畳めない注記(「等価変異(生存)」「既知の未検出の穴」)は表の直後の短い箇条書きに置く。 (20260905-0510-topopt-shrink-docs, 2026-09-05)
- 件数・行番号・「いま何箇所にある」といった作業指示の記述は書かれた時刻からずれる。現物(grep)で確認してから消す・畳む対象を
  確定する。置き換わった実測を消す前に、その個別数値(例 `292.2307`)が別の節へ引き継がれているかを grep で確かめる。
  (20260905-0510-topopt-shrink-docs, 2026-09-05)
- コメント縮小の線引きは「実測値の再掲か・docs に同じ内容が既にあるか」を**grep で確認してから**消す(「docs にありそう」で消さない)。
  「docs に重複している」と「タスクが名指しで保護している」は独立の判定軸で、片方だけ見て消すと事故る(パッシブ要素は密度フィルタの後で
  上書きする、という注記は docs に一字一句近い形があっても残した)。削減余地は薄く、`simp.py` 1414→1386 行(-2.0%)・`design_space.py` 1238→1206 行
  (-2.6%)にとどまった。 (20260905-0511-topopt-shrink-comments, 2026-09-05)
- コメントだけの変更は「docstring を除いた `ast.dump` が一致する」で確かめられる(`git show HEAD:<path>` で変更前を退避→編集→AST比較。道具の
  `ast_diff.py` は作業領域とともに消えたので必要なら作り直す)。 (20260905-0511-topopt-shrink-comments, 2026-09-05)
- 検査スクリプトは `python -m topopt_checks.check_x` でなく `python topopt_checks/check_x.py` で直接実行する(`_bootstrap` がリポジトリ直下を
  `sys.path` へ足す前提で、`-m` だと `ModuleNotFoundError: No module named '_bootstrap'`)。全検査の通しは Bash ツールの既定 2 分タイムアウトを
  超えるので1ファイルずつ実行するか timeout を長くする。 (20260905-0511-topopt-shrink-comments, 2026-09-05)
