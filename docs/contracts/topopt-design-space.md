# 形状最適化の設計空間(塊)まわりの契約

形状最適化の「塊(設計空間)」を作る・検査する・表示する層と、その周辺(外接箱 bbox・空要素の除去)の契約集。**いまの挙動の正はコードとテスト。** ソースから読めない不変条件・依存禁止・罠だけを置く。実測と設計の説明は [../TOPOPT.md](../TOPOPT.md)(「塊の連結性と境界条件の入口検査」「固定凸包による設計空間」ほか)が持つ。

出典は項目末尾の括弧のタスクID(先頭8桁が日付)。塊は固定凸包の `design_space.derive` 1本。安全率・体積目標・縮小検証は [topopt-volume-stress-safety.md](topopt-volume-stress-safety.md)。

## 塊をマスクで受ける入口(voxelize.model_from_design_mask)

- 入口は `voxelize.model_from_design_mask(grid, design, *, anchor_shape, keep=None, obstacle=None, ...)`。`design` は `grid.shape` と同じかたちの bool マスクで**複製して持つ**。かたち違い・全 False は `ValueError`。(20260903-2216-topopt-split-design-space-from-anchor-soup)
- `anchor_shape` は `TopoDS_Shape` / build123d オブジェクト / 既に作った `TriangleSoup` のいずれか。**面添字は渡したものの面の並びで決まる**ので、面を選ぶのに使った `TriangleSoup` をそのまま渡す(作り直すと面添字が食い違いうる)。(同上)
- `VoxelModel` の形は `grid` / `design` / `keep` / `obstacle` / `soup` / `stats`。`keep` / `obstacle` は `design` の内側に収まる。マスク経路の `stats` は `design_source="mask"` が付き、**`occupancy` キーは無い**。ここに依存しない。(同上)
- 保持・除外が塊からはみ出したときは **`voxelize.OutsideDesignSpaceError`**(`ValueError` の派生)。要素数は `e.outside`(`{"keep": n, "obstacle": n}`)で引け、両方はみ出したら**両方を1つの文面に**出す。(同上)

## 塊の導出(design_space.derive)

現行の導出は固定凸包(詳細は [../TOPOPT.md](../TOPOPT.md)「固定凸包による設計空間」)。

- **`Conditions` に設計空間ソリッドを渡す口は無い。** `check_design_space.py` の `check_case1_end_to_end` がフィールド集合そのものを assert している。足す・変えるときは検査と一緒に直す(現行は `keep` / `fixed_faces` / `load_faces` / `obstacle` / `anchor_shape` / `bodies`)。(20260903-2234-topopt-design-space-from-bbox, 20260904-1400-topopt-design-space-from-coarse-run, 20260904-1437-topopt-safety-factor-outer-loop)
- **`DesignSpace.model.obstacle` は常に全 False。** 除外ソリッドは塊から引き切ってあるので、同じ除外を `obstacle` として `model_from_design_mask` に渡してはいけない(渡すと全要素がはみ出す)。(20260903-2234-topopt-design-space-from-bbox)
- 塊の組み立て順序(現行 `derive`): 条件の点群(保持ボディ・条件面を含むボディ。**除外は入れない**)→ 余白(点群の最長辺 × 比率)→ 格子 → 余白付き凸包の候補 − 除外の占有 → 格子外ガード → 保持の占有 → 面アンカー表 → 連結性 → `model_from_design_mask`(`keep` を渡し `obstacle` は渡さない)。順序を入れ替えると診断が化ける。(同上)
- **面アンカーの表は全面・既定の半径で作る**(`an.build_anchors(soup, grid)`)。`faces=` で絞ると稜線の規則の競争相手が減り、絞らずに作った表とは**別の節点集合**になる。(20260903-2235-topopt-design-space-connectivity-guard)
- **塊は読み取り専用で、導出は1実行に1回。** 塊を作り直す層を足すなら、`mask_hash` が候補間で一致することを検査が突き合わせている口(`DesignSpace.mask_hash`)を壊さない。(20260904-1437-topopt-safety-factor-outer-loop, 20260911-1811-topopt-design-space-width-and-size)
- **余白比率の数字の読み方**(外接直方体導出での実測。凸包導出にも通じる): 壁に接した材料の割合は「塊比」でなく「材料比」で見る(塊比は余白を広げると分母が膨らんで下がる)。余白 = 最長辺 × 比率なので、**回転すると最長辺が縮み塊が小さくなる**。「45° が最悪」と決めつけない。(20260903-2234-topopt-design-space-from-bbox)
- 余白比率の既定は 0.20(`DEFAULT_MARGIN_RATIO`、2026-09-16 の本人判断で 0.25 から下げた)。`margin_ratio < 0` は `ValueError`。(同上)
- **道を用意することは、最適化がその道に材料を置く十分条件ではない。** 病理を構造上再現できない題材(箱が小さい等)で「直った」と判定しない。(20260911-1811-topopt-design-space-width-and-size)

## 塊の連結性(connect_seeds / connect_conditions)

説明は [../TOPOPT.md](../TOPOPT.md)「塊の連結性と境界条件の入口検査」。

- **連結性は6近傍**(`NEIGHBOURHOOD = 6`)。26 近傍だと角1点で接する2島を「つながっている」と数えるが、角で触れる2要素が共有するのは節点1つで、回転を止められないピン接合にしかならない。`element_components(neighbourhood=18/26)` は「定義を変えると答えが変わる」を検査で見せる口で、導出の経路からは 6 以外を渡さない。(20260903-2235-topopt-design-space-connectivity-guard)
- **例外は `DesignSpaceDisconnectedError`。`DesignSpaceTooSmallError` の派生にしない**(あちらは「余白を上げろ」の文面で分断には効かない)。属性 `components` / `unreached` / `seeds` で機械から引く。(20260903-2235-topopt-design-space-connectivity-guard, 20260911-1811-topopt-design-space-width-and-size)
- **見ないもの**: 保持ソリッドの連結性(浮いていても最適化が材料でつなぐのが正しい)、道の細さ(幅1要素の糸でも「つながっている」)、最適化後の材料の連結性(`mesh.remove_islands` は出力の後処理で塊の不備は直せない)。(20260903-2235-topopt-design-space-connectivity-guard)
- **条件面のアンカーが空のときは判定せず `{"skipped": "empty_anchor_faces", "empty_faces": [...]}` を返す**(範囲外の別問題)。`stats["connectivity"]` が必ず通常の report だと思わない。節点が1つも付かない面を渡すと `AnchorTable.element_mask` が `node_ids` 経由の `_require_nodes` で `ValueError` になり、連結性の手前でこれに化けると診断の文面が別物になる。(同上)
- **塊の分断は、下流の解法の失敗に任せず塊を組み立てる側で検出する。** 切れた系は最後まで走って出力することも、設計空間も連結性も文面に出ない別種の例外(`ZeroDivisionError` 等)で落ちることもあった(6配置中4配置が完走、2配置が落ちた)。(同上)
- 検査題材 `severed_conditions(angle_deg=0.0, *, thickness=10.0)`(`topopt_checks/_fixtures.py`)の壁は、塊の断面を端まで塞ぐ大きさ(200×200mm)にしないと縁を回り込む道が残る。厚みも要素サイズより十分厚くする(要素中心が壁に入らないと1要素も削れない)。(同上)
- 連結性ガード(`connect_conditions`)の位置は、格子外ガードより**後**に実測で固定した(前に置くと、保持が格子外へ出ているとき条件面のアンカー要素が塊から消え、診断が「格子外」から「アンカー要素が塊の中に1つも無い」へ化けた)。位置を動かすなら診断の判断(除外が保持を食っている配置では保持のはみ出しのほうが原因に近い)ごと引き継ぐ。(20260903-2234-topopt-design-space-from-bbox, 20260903-2235-topopt-design-space-connectivity-guard)

## 条件面が空のとき(要求要素サイズより小さい面)

説明は [../TOPOPT.md](../TOPOPT.md)「塊の連結性と境界条件の入口検査」の後半。呼ぶ面は `AnchorTable.empty_face_diagnosis(face_index)` / `condition_face_diagnoses(fixed_faces, load_faces)` / `anchor.format_condition_face_warning(diagnoses)`(診断キーはソース参照)。

- 荷重面が空でも「荷重ゼロで黙って解く」ことは無い。`AnchorTable.node_ids`(`fixed_dofs` / `load_vector` / `element_mask` が通る)は `_require_nodes` で、空の面が1つでも混ざれば `ValueError` で落ちる(`check_anchor.py` が固定)。**この下層のガードは弱めない。** 「落とさない」は手前に置く**入口レベルの通知**の話。(20260904-1556-topopt-warn-on-empty-condition-faces)
- 入口は `ValueError` へ落ちる**手前**で空の条件面を診断し、実行を止めて診断を返す層(現行は `optimize_run._empty_condition_faces(space)` と `state="empty_condition_faces"`)。早期return時の戻り値は限られたキーだけで通常成功時のキー(`achieved_material_mm3` 等)は無いので、**`empty_condition_faces` が非空かを先に見てから他のキーへ触る**(さもなくば `KeyError`)。完走した戻り値には `"empty_condition_faces": []` が常に入る。戻り値の形を変えるときは呼び出し元を grep し尽くす(別の呼び出し元が早期returnを受けて `AttributeError` を出す穴があった)。(同上)
- `representative_length_mm` / `suggested_max_spacing_mm` は `√cad_area_mm2` の**近似**(細長いスリットや曲面では過大/過小)で目安。精度の根拠にならない。厳密な下限が要るなら、候補の要素サイズで実際に `empty_faces()` を取り直す。`AnchorTable` は `soup` を持たず、面の bbox から代表寸法を出す案は API が重くなるため採らなかった。(同上)
- `absorbed_by_faces`(稜線の規則でその面の `raw_node_ids` を持っていった面)が1件以上入ることに依存しない。標本点が1つも節点の球に入らない極端に小さい面では空リスト。(同上)

## 外接箱 bbox(kernel.bounding_box と voxelize._shape_bbox)

説明と実測は [../TOPOPT.md](../TOPOPT.md)「OCCT の bbox はトリムを見ない」・[../CAD_LIBRARY_PITFALLS.md](../CAD_LIBRARY_PITFALLS.md)。

- **`kernel.bounding_box` と `voxelize._shape_bbox` は `BRepBndLib::AddOptimal_s(shape, box, False, False)`。`Add` に戻さない。** `Add` は面のトリムを見ず、曲面部品の寸法が最大9倍に狂う。(20260907-2202-bbox-addoptimal-with-cache)
- `bounding_box(shape, *, cache=None)` の `cache` はキーワード専用。繰り返し呼ぶ経路(`mcp_server.describe_document` / `describe_shape`・`optimize_run` の条件 bbox 計算)は `cache=document.bbox_cache` を必ず渡す。**渡し忘れても答えは正しいので検査では気づけない**(定常状態が 0.5ms から 145ms になるだけ)。(同上)
- **`BoundingBoxCache` を bbox 以外へ流用しない。** 鍵(`_ShapeKey`)の同一性 `IsSame` は Orientation を見ないので、法線・面の向き・押し出し方向のような向きに依存する値を載せると静かに壊れる(向きまで見る鍵は別に `_OrientedShapeKey`)。(同上)
- **キャッシュをモジュール変数にしない。** 寿命は文書の切り替えまで(2026-09-07 ユーザー決定)。`Document.__init__` が `bbox_cache` を持ち、`Session._replace_document` が新しい `Document` を作ることで自動的に捨てられる。`reset()` 相当を `Session` 側に足すと呼び忘れの穴を作る。保存はされない。(同上)
- `bounding_box` が返す dict は**毎回作り直す**(呼び出し側が書き換えても波及しない)。外へ出るのは `extents()` の6要素タプルだけで、キャッシュの中身の型に依存しない。(同上)
- `voxelize._shape_bbox` にはキャッシュを載せない(導出1回につき2〜3回しか呼ばれない)。ループから呼ぶ経路を新設するならそのとき考える。(同上)
- **`bounding_box` が速いことに依存しない。** 初回は1形状 18〜60ms、27ボディの `describe_document(include_bbox=True)` は初回 +1.6 秒で、キャッシュは初回を減らさない。承知の上の決定(2026-09-07 ユーザー: 29% のボディの寸法が最大9倍狂うほうが害が大きい)。「遅くなったから戻す」を後から言わない。(同上)
- 検査の題材は `models/` の実ファイルでなく `BRepPrimAPI_MakeTorus(100.0, 10.0, π/6)` をその場で生成する。トリムした環状片でないと `Add` と `AddOptimal` の差が出ない(部分円柱・部分球は両者が厳密に一致)。**全周のトーラスでも `Add` は外す**(R100/r10 の全周で 238.13mm、真値 220.00mm)。差し替え後の `trimmed_torus_bbox_slack_mm` は 9.063142 → 0.010765、`untrimmed_bbox_slack_mm` は 0.012431 のまま。(20260907-2201-bbox-curved-geometry-coverage)
- 検査の基準は `AddOptimal` の返り値でなく**三角形分割の節点**(必ず真の面の上にある)。`kernel.tessellate` の戻り値の `bbox` を基準にしない(循環参照になる。`tests/test_kernel_bbox.py` は `faces[].positions` から自前で min/max を取る)。許容差は三角形分割の deviation(deviation 0.2mm 指定で箱と真値の差は 0.012mm だが、許容を deviation より小さく取ると機械や OCCT の版で不安定になる)。(同上)
- **コードを直すと嘘になるドキュメントは `grep AddOptimal` では見つからない。** `BRepBndLib` と `_shape_bbox` の両方で `*.md` を掃く(当時 `docs/TOPOPT.md` に `Add_s` の実測「実物STEP(231面)で0.003秒」が残っていた)。(20260907-2202-bbox-addoptimal-with-cache)
- 検査の実行はパイプに通さない(`| tail -5` の終了コードは `tail` のもの)。ファイルへリダイレクトして終了コードと `[FAIL]` の件数の両方を見る。(20260907-2201-bbox-curved-geometry-coverage)

## 空要素を剛性行列の組み立てから外す(void除去。fem.Model の active_mask)

説明と実測は [../TOPOPT.md](../TOPOPT.md)「空要素を組み立てから外す」。

- 口は `fem.Model(..., active_mask=None)` のみ。`(nelx,nely,nelz)` か `(nele,)` の bool マスクで、True の要素だけで K を組む。`Model.active` は `np.flatnonzero(mask.ravel())` か `None`(**常に配列だと思わない**。`None` のときは全要素が組み立てに載る)。**`Model.edof` は間引かない**(`Model.compliance` と `stress.py` が全要素で引く)。`_coo_index_arrays(edof)` は各要素 576 項の COO 添字で、並びは `ke.ravel()` と対応する契約(`np.kron` 版との完全一致を `check_fem.py` が固定)。(20260909-0518-topopt-void-removal-to-main)
- **`extra_fixed_dofs` と `active_mask` は同じ `void` から作る。** 片方だけ渡すと、組み立てから外した要素にしか接していない自由度が `free` に残り K_ff に全零行ができる(孤立 396 個)。(同上)
- **目的値は解いた系のものに揃える。** `Model.compliance` の `c` も active だけで足す(`c == F・U`)。`ce` は感度用に全要素ぶん返す。組み立ての範囲を変えるときは目的値の総和範囲も一緒に変える。`active_mask` を渡さない系の検査はこの経路に届かない。(同上)
- 自由自由度の集合は void除去で1つも変わらない(全要素で組んだときと一致することを `check_fem.py` が固定)。「未知数が減る」前提の最適化を後から足さない。(同上)

## 画面の検査で踏んだ一般的な罠(開発用デモの4色表示)

条件の4色表示を作ったデモ本体は現行のソースに無い。製品の条件表示・デバッグ表示は [../TOPOPT.md](../TOPOPT.md)「開発用のデバッグ表示」。以下は画面で色分けを検査する仕組みを作るときに今も当てはまる罠。(20260903-2304-topopt-show-four-conditions-in-duocad-colors)

- **ボタンが `disabled` の間に Puppeteer の `page.click` を呼ぶと、例外を投げず黙って何もしない。** 撮影スクリプトは状態が揃うまで待ってからクリックする。
- **カメラの向きは検査の一部。** 軸整列の拘束面は、カメラが十分その法線を向いていないと**同じパッドの隣接面に幾何学的に遮られて**ほぼ画面に出ない(自己遮蔽なので `side: THREE.DoubleSide` では直らない)。向きを変えると画素集計の閾値と検出力の検査が両方壊れる。
- **画素集計の閾値は「1色ずつ隠して実際に0近くまで落ちること」で検証する。** 集計値が「そこそこの数」に見えても、半透明の境界線の混ざり色を数えているだけで対象を検出していない閾値だったことがある。閾値はチャンネル比で作り、色・カメラ・光源のいずれかを変えたら再確認する。

## 文書・コメントの縮小で踏んだ罠

- **見出しを統合・改名・削除したら、削除した見出し文字列を全文 grep して本文中の名指し参照を直す。** 対象には `duocad/topopt/*.py` のコメントも含める(`docs/TOPOPT.md` の「変異検査の記録」9節を1節に畳んだとき、存在しない見出し名への参照が1件残った)。(20260905-0510-topopt-shrink-docs, 20260905-0511-topopt-shrink-comments)
- 件数・行番号・「いま何箇所にある」といった記述は書かれた時刻からずれる。現物(grep)で確認してから消す・畳む。置き換わった実測を消す前に、その個別数値が別の節へ引き継がれているかを grep で確かめる。(20260905-0510-topopt-shrink-docs)
- コメント縮小の線引きは「実測値の再掲か・docs に同じ内容が既にあるか」を**grep で確認してから**消す(「docs にありそう」で消さない)。「docs に重複している」と「タスクが名指しで保護している」は独立の判定軸。(20260905-0511-topopt-shrink-comments)
- コメントだけの変更は「docstring を除いた `ast.dump` が一致する」で確かめられる(`git show HEAD:<path>` で変更前を退避→編集→AST比較。比較用スクリプトは残っていないので必要なら作り直す)。(同上)
- 検査スクリプトは `python -m topopt_checks.check_x` でなく `python topopt_checks/check_x.py` で直接実行する(`_bootstrap` がリポジトリ直下を `sys.path` へ足す前提で、`-m` だと `ModuleNotFoundError: No module named '_bootstrap'`)。全検査の通しは Bash ツールの既定 2 分タイムアウトを超えるので1ファイルずつ実行するか timeout を長くする。(同上)
