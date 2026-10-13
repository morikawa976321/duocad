# メッシュ body 種別・最適化の配線と実行 UI の契約

形状最適化の結果を文書に入れる「メッシュボディ」種別、最適化バックエンド(二値探索 `binary_search.find_best_feasible`)を `Session`/WebSocket/画面へつなぐ配線(`duocad/optimize_run.py`)、実行パネル(`OptimizePanel.svelte`)の、ソースから読めない不変条件・依存禁止・罠。
**いまの挙動の正はコードとテスト**。アルゴリズム本体は [../TOPOPT.md](../TOPOPT.md) と `TOPOPT_*.md`、画面は [../UI.md](../UI.md)。デバッグビュー(`?debug=` `?voxel=1`)は ui-selection-viewport.md。
**安定版では入口を出さない**(凍結中): `App.svelte` は WS の `optimize_status` の `available`(`Server._optimize_status`)が false なら「形状最適化」ボタンを、`SettingsDialog.svelte`(`showGpu`)は GPU の欄を出さない。まだ聞いていない間も出さない(安定版で一瞬見えないように)。使えないと聞いたら最適化モード(`setMode` 経由で起動中のコマンドも解く)とステップの表示を畳む。接続が切れても答えは持ち続ける(繋ぎ直しは最初に教わった WebSocket の URL へ行き、設定の読み直しに失敗しても既定の 8765 へ落ちない。`session.ts` の `knownWebSocketUrl`)。最初の設定の取得に失敗したときだけ既定へ落ちる(Vite の開発サーバのため。開発版のサーバは安定版の画面の Origin を断る)。最適化モードの条件の取り直し(`$effect`)も使えると聞くまで頼まない。接続・文書の切り替えのたびのステップの取り直し(`optimize_steps`)も、使えると聞くまで頼まない(`askOptimizeSteps`。断られてエラーのトーストになる)。`/duocad-config.json` には載せない(取得に失敗すると、口を断るサーバと画面の判断が食い違う。sol の指摘)。サーバ側の断り方は [../STABLE.md](../STABLE.md)「安定版で出さない機能」。

---

## メッシュ body 種別(`duocad/document.py` / `meshbody.py` / `verbs/add_mesh.py`)

- **ボディ種別は `document.MESH_KIND`(`"mesh"`)**。`SOLID_KIND` と合わせて `_VALID_KINDS` は2つ。種別の綴りは文字列で書かず定数を import する。形は `Body.mesh`(frozen dataclass `Mesh`: `positions` は x,y,z 平坦、`indices` は**0始まり**で3つずつ。`vertex_count`/`triangle_count`)。**`Body.shape` は常に None**(両方持つ Body は `__post_init__` が弾く)。素の並びから作るのは `document.make_mesh`。
- **メッシュボディを作る唯一の口は verb `add_mesh_body`**(コア `verbs.add_mesh_body(doc, positions, indices, name=None)`、`Session.add_mesh_body`、MCP ツール `add_mesh_body`)。**WebSocket には開けない**(`catalog` の `ws="none"`)。結果は `run_optimization` がサーバ内で入れる。
- ログに載る verb には `catalog` に同じ綴りの宣言が必須(`tests/test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log` が両方向で突き合わせる)。WS(`ws`)や MCP(`mcp`)は開けない宣言にできる。
- `duocad/meshbody.py`: `triangles/bbox/volume/geometry/write_stl/from_tessellation/reject`。**断り文の実体は `reject()` の1箇所**で「次に何ができるか」を必ず含む(`tests/test_mesh_body.py` の `EXPECTED_HINT` が固定)。形状最適化の結果は `from_tessellation` ではなく `duocad.topopt.mesh` の `verts`/`faces` をそのまま平らにして渡す(`verts.ravel().tolist()`/`faces.ravel().tolist()`。巻き順は `topopt.mesh` が外向きに揃えてあるので入れ替えない)。
- **`add_mesh_body` の args に「元の B-rep の ID」を入れない。** 三角形そのものを積む(元を指すと、元ボディを後で編集した文書の再生で別の形が復元される)。
- **`meshbody.py` に OCP / build123d を import しない**(`tests/test_boundaries.py`)。numpy/scipy は関数内 lazy import(現行は `validate_float32` 内)。モジュール直下で import しない。
- 画面への配りものは `meshbody.geometry(mesh)`: `kernel.tessellate_selective` と同じキー構成で `faces` は**要素1つだけ**(`index:1`/`kind:"mesh"`/`detail:"fine"`/`deviation:0.0`)。`web/` は変えずに描画・投影できる。`App.svelte` の `partBodies` は `model?.bodies ?? []` をそのまま使い、濾していない。

### 編集 verb を効かせない(断りの門)

- **断りの門は2つだけ**: `duocad/verbs/_common.py` の `editable_body()` と `duocad/verbs/extrude.py` の `_resolve_target()`(領域の押し出し専用)。**新しい編集 verb は必ず `editable_body()` を通す**。`delete` だけは種別を問わず消せるよう `_common.deletable_body()` を使う(**`delete` の do 側 `_do_delete` にも同じ呼び出しがある**ので2箇所)。
- **選択の粒度はボディ単位だけ。** `selection.select_face/edge/vertex` はメッシュを断り(一括経路 `_coerce_refs` にも同じ門)、メッシュを指すのは `select_body`(`kind="body"`/`index=None`)。AI 側から編集 verb が断りへ届く外からの唯一の経路は `move_body`(`extrude_face`/`fillet`/`chamfer` は面・辺の確定選択を要求し、メッシュは選べない)。
- **「名指ししたら断る／導出した集合からは黙って外す」。** 名指し(`select_face(mesh, 3)`、`predicates` の `body` 明示、面の指紋、貼る先の面)は「このボディには面がある」という誤った主張なので断る。導出(`predicates` の `body` 省略=可視 Body 全部、`export_stl` の対象集合)は主張ではないので外す(断ると最適化結果のメッシュが1つあるだけで述語による選択が丸ごと使えなくなる)。`predicates` では**メッシュを外してから** `on_boundary_of_face` の単一 Body 検査に掛ける(逆順だと可視のメッシュ1つで「複数の Body が見えている」と断られる)。
- **`body.shape` が非 None であることに依存しない。** ボディ全体を舐める新コードは `kernel.face_count`/`query.volume`/`kernel.bounding_box`/`query.describe_shape`/`describe_faces`/`describe_edges` を**無条件に呼ばない**。塞いだ箇所: `view.body_meta/_body_volume/geometry`(メッシュ用の値)、`view.edges/vertices`(断る)、`mcp_server.describe_document/describe_shape`(メッシュ用の値、後者は `triangle_count` を足す)、`io_step._export_candidates`(含める)、`io_step.export_step`(断る)、`selection.select_face/edge/vertex`+`_coerce_refs`(断る)、`predicates._target_bodies`(明示なら断る/省略なら外す)、`optimize.face_fingerprint`(断る)、`sketch.resolve_plane`(面に貼るスケッチ。断る)。`optimize._body_state`/`session._revision_snapshot` 周辺/`set_parameter` の前後比較/`session.preview`/`verbs/*`/`view` のキャッシュ刈り取りは安全と確認済み。
- `body_meta`/`describe_document` の `face_count` は**メッシュでは常に1**。三角形数は `describe_shape` の `triangle_count`(メッシュのときだけ載る)か配りものの `faces[0].indices.length / 3`。
- **`Body` に持たせるフィールドを増やしたら `duocad/speculative.py` の `_snapshot`/`_restore` のタプルにも足す**(`mesh` を含めて6要素)。
- **`body_revision`(`duocad/view.py`)の鍵に `id(body.mesh)` を入れる**(メッシュは `shape` が常に None で `id(None)` が不変なので、入れ忘れると**版が二度と上がらず**差分配信が「変わっていない」と判断し続ける)。`meshbody.geometry` は毎回 positions/indices をリストへ写すので、`view.geometry` に**メッシュの同一性を鍵にしたキャッシュ**がある(消さない。`body_state()` は接続・状態配信のたびに呼ばれる)。
- 罠: `session.document = doc` で文書を差し替えても `session.view` は古い文書を握ったまま。検査で `Session` にメッシュを持たせるときは `Session.add_mesh_body()` 越しに作る(view を作り直す口は `_replace_document` のみ)。`parameters.rebuild()` は `OpLog.replay` とは別の再生経路(`doc.bodies` を空にしてログを頭から流し直す)で、`set_parameter` を1回打つだけで踏む。メッシュボディが復元されることを別検査で固定してある。
- 罠(網羅の取り方): `grep "\.shape"` だけでは `query.describe_edges(body.shape)` のように関数呼び出しの引数の中にあるものを数え落とす。`doc.bodies` を舐める場所(`grep "\.bodies"`)からも辿って**両方向**で潰す。

## 最適化の配線(`duocad/optimize_run.py` / `session.py` / `net/ws.py`)

- **verb は足していない**(結果を文書に入れる口は既存の `add_mesh_body` だけ)。`Session` の4つ: `optimize_conditions()`(4条件がそろって走らせられるか。`{ready, problems, preserve_bodies, exclude_bodies, fixtures, loads, load}`、**`problems` はそのまま人に見せる文**)/ `optimize_resolution()`(要素サイズの上限・下限・既定。軽い)/ `run_optimization(...)`(重い。成功時だけ戻り値に `"applied"` を足す)/ `cancel_optimization()`(旗を立てるだけ。実際に降りるのは次に進捗が出た時点)。コア側は `conditions_report(doc)`/`build_conditions(doc)`/`resolution_limits(doc)`/`run(doc, options, *, progress, should_cancel, debug, debug_kinds)`、摘みは `OptimizeOptions`。
- **`run()` の戻り値は `state` で分岐し、`"done"` 以外では文書は1ビットも変わらない**(`body_id` は None)。`state` は `"done"`(`body_id/name/triangle_count/vertex_count/safety_factor/...`)のほか `"empty_condition_faces"`(条件面に格子節点が付かない。`empty_condition_faces` に診断、`warning` に `anchor.format_condition_face_warning` の1文)／`"unrepresented_regions"`(保持/除外がこの解像度で表現できていない事後検出。`format_region_warning`)／`boundary_mapping_failed`／`no_feasible_found`／`input_contradiction`／`resolution_insufficient`／`fill_infeasible`／`final_unsafe` など(全一覧は `_run` の docstring)。どの状態でも `resolution/element_size_mm/grid/design_elements/allowable/anchor_check/seconds/warning` が入る。**戻り値は素の値だけ**(`json.dumps` が通る。`DesignSpace`/`AnchorTable` を外へ出さない)。
- **進捗は `progress(event)`**。`event` は必ず `{"phase","message","evaluation"}`(phase は `prepare/derive/search/fill/mesh/finalize/final_check/insert`)。例外は文面に「次に何をすればよいか」を含み**握り潰さない**: `OptimizeInputError`(`ValueError`)/`FaceMappingError`(**`RuntimeError`**)/`OptimizeCancelled`(**`RuntimeError`**) ほか。**`OptimizeCancelled` を `ValueError` にしない**(探索内の `acceptance.inspect` の例外処理が入力の不備と分類して**探索が続く**)。
- **元のボディを消さない・形を差し替えない。** 最適化は編集ではなく「条件から新しい形を1つ作る」操作。結果は `optimized`(2回目は `optimized-2`)という名前のメッシュボディとして**足されるだけ**(積み上がる。要らないものは `delete`)。`shape` の同一性(`is`)で固定。
- **面の対応の網を2枚とも外さない。** 面添字がずれても例外は出ず、**荷重が別の面に入ったまま最適化がもっともらしく収束する**。(1) `_verify_face_mapping`: まとめた形(`kernel.make_compound`)側で測り直した指紋がボディ単体の指紋と一致するか。**面積・法線・重心の3つ**(重心を外すと同じ形のボディを2つ並べた文書で取り違えを通す)。(2) `_check_anchor_centroids`: 実際にできた面アンカーの節点の重心が条件面の重心から3要素以内か。
- **結果メッシュの座標変換は1箇所**(`_mesh_to_world`)。`mesh.build_surface` が返す頂点は**要素添字空間**(`spacing=(1,1,1)`・セル中心規約)で、ワールド mm は `grid.origin + verts * grid.spacing`。忘れると原点付近に縮んだメッシュが黙って入るので `_check_mesh_placement` が毎回格子の箱に収まっているかを見る。
- **要素サイズの上限は2つの要求の min**: 固定値 `MAX_ELEMENT_SIZE_MM` と `min(sqrt(条件面の CAD 面積))`(`anchor.empty_face_diagnosis` の `suggested_max_spacing_mm` と同じ式で `optimize_run._face_max_spacing_mm` に1箇所化。近似であって保証ではない)。**既定は上限側(いちばん粗くて安い)**、**時間は制約にしない**。下限は設計空間の要素数の上限 `MAX_DESIGN_ELEMENTS` から割り戻す。
- **事後検出は保持/除外を1つずつ数える**(まとめて数えると大きい保持ソリッド1つで小さいほうが1要素も掴めていないことが隠れる。`anchor._require_nodes` と同じ)。0要素なら最適化本体を呼ばずに戻る。格子の列が1本もその形状を貫かない場合は異常ではなく「この解像度では表現できていない」(要素4mm の格子に1mm 角の保持ソリッドを置くと必ずこうなる)ので、`vx.occupancy(..., allow_empty=True)` で0要素として扱う。`MIN_REGION_ELEMENTS = 8` は根拠のある閾値ではない(確かな線は「0は必ず止める」だけ。0より多く8未満は警告して続ける)。
- **メッシュボディは保持・除外に指定できない。** 断りの門は `verbs/optimize_setup.py` の `_assign_bodies`(新規割り当て)と `optimize_run.conditions_report`(過去のログを再生した文書に入っている場合)。**`_apply_bodies` には門を置かない**(redo/replay も通り、置くと過去に受け付けたログの再生が落ちて文書が開けなくなる)。断り文は `meshbody.reject`。
- **`duocad.topopt` は関数の中で import する。** `session.py` が `optimize_run` をモジュール直下で import するので、崩れると pytest が毎回 skimage と `duocad.topopt` の起動費用を払う(`scipy` の有無では検出できない。`duocad/view.py` が既に読んでいる)。
- 粗い段の中では中断できない(進捗が出るのは探索の反復・候補ごと)。
- 罠(中断の検査): **`Session.cancel_optimization()` を実行前に呼んでも効かない**(旗を畳むのは `begin_optimization`=実行の入口)。中断の検査は「進捗の途中で旗を立てる」形で書く。`should_cancel` を直に渡す `optimize_run.run` なら1件も進捗を出さずに降りる。
- 罠(面添字の変異検査): 面添字を **+1** ずらす変異は「範囲外」で落ちて取り違えを見ていない(最後の面をまたぐ)。**−1 側でずらす**。

## 実行 UI(`OptimizePanel.svelte` / `session.ts` / `net/ws.py`)

- WebSocket に4つ(と、下の「ステップ」の3つ。`catalog` の `category="protocol"`=verb ではない、`ws="special"`/`mcp=False`/`changes_document=False`、実体は `duocad/net/ws.py` の `_dispatch`): `optimize_conditions`(要求した接続にだけ返す)／`optimize_resolution`(`OptimizeInputError` は `type:"error"` にせず `{"error": 文}` に畳む。条件面がまだ無いのは「設定の途中」)／`run_optimization`(**待たずに返り**、`optimize_status`→`optimize_progress`(多数)→`optimize_result` か `optimize_error`→`optimize_status` の順で**全接続へ**配る)／`cancel_optimization`(`optimize_status {running, cancelling}`)。**`optimize_error` には `cancelled: bool` が載り**、中断は失敗ではないので画面はトーストを出さない。`problems` は条件が足りないときだけ入る。`Server` の配信ヘルパは `_emit_event`/`_spawn_broadcast`/`_broadcast`。
- **重い処理は `run_in_executor` で回す。** `_dispatch` の中で待つと `handle()` が1接続を直列処理する以上、同じ接続から届く `cancel_optimization` が読めなくなる。`_ai_start` と同じ形。
- **解像度の目盛りは粗い側へ動かさない。** 粗い側の端は「正しさ」の境界(割ると条件面から節点が消え最小部材幅が表現できず、しかも silent)、細かい側の端は「都合」の境界(遅い・落ちるだけ)。**時間を入力にしない**(時間から解像度を逆算すると最小部材幅と条件面の要求を黙って割る)。段(`resolution_limits()["steps"]`、`optimize_run.RESOLUTION_STEPS`=6の等比、添字0が最粗=既定、最後の段は下限そのもの、上限が下限を下回るときは空)は**サーバが作る**(`_resolution_steps`。要素数の数え方(余白比率0.25と `voxelize.DEFAULT_PAD`)を画面が持つと必ずずれる)。同じ要素数になる段は畳むので `steps.length` が `RESOLUTION_STEPS` と一致することに依存しない。
- 画面の口: `session.ts` の `optimizeConditions/optimizeResolution/runOptimization/cancelOptimization` と hooks 6本(`onOptimizeConditions/Resolution/Status/Progress/Result/Error`)。`OptimizePanel.svelte`(`App.svelte` が状態を持ち、パネルは値と口だけ受け取る。`SettingsDialog.svelte` と同じ規律)。パネルの id: `#optimize-panel/#optimize-conditions/#optimize-resolution/#optimize-resolution-note/#optimize-estimate/#optimize-min-width/#optimize-run/#optimize-cancel/#optimize-progress/#optimize-notice`。`window.__duocad`(`testHooks.svelte.ts`)の読むだけの穴: `optimizeRunning/optimizeCancelling/optimizeConditions/optimizeResolution/optimizeElementSizeMm/optimizeProgress/optimizeNotice`。
- **結果が信用できない意味の通知をトーストだけで流さない。** トーストは自然に消える(`Toast.svelte` の `LIFETIME_MS`)。`#optimize-notice` は**次に走らせるまで残る**。
- **`Session._optimize_cancel` の旗を畳むのは `begin_optimization` だけ**(ワーカースレッドの中で畳まない。投げてから走り出すまでに届いた中断が消える)。ws 側の `_optimize_cancel` は**走っていないときは旗を立てない**(「中断中」が出ると次の実行が中断表示から始まって見える)。
- **メッシュボディのピックはボディ参照へ畳む**(`App.svelte` の `pickedRef`。畳まないと `select_face` が断り、ドラッグの一括選択が丸ごと落ちる。畳むと編集 verb は選択要件を満たせず「押せるのに断られる」が画面から消える。`move_body`/`fillet`/`chamfer`/領域押し出しは `kinds` にボディを持たない)。**スケッチを貼る先だけは畳まずに断る**。**`dedupeRefs` を外さない。** ツールバーを選択に応じて出し分けない(コマンド一覧はモードの持ち物で、選択のたびに作り直すと集めかけの引数が落ちる。`{#key mode.kind}`)。
- **依存禁止**: MCP から最適化を走らせられることに依存しない(`mcp=False`。数分かかる同期呼び出しは進捗も中断も運べずタイムアウトする。開けるなら進捗と中断を MCP で運ぶ形を先に決める)／実行中に MCP・アプリ内 AI からの編集が止まることに依存しない(止めているのは画面の操作だけ)／**最後の接続が切れても最適化は止まらない**(AI の実行は止まる。リロードだけで数分の実行が消える方を避けた。`_on_disconnect` の docstring)／目標安全率の入力欄は画面に無い(口 `target_safety_factor` は開いており、画面は既定 `DEFAULT_TARGET_SAFETY_FACTOR`=2.0 を送る)。
- **罠(浮かせるパネル)**: 画面に帯を1枚足すと、その下のボディの面が指せなくなる。`viewport.project()` は三角形ごとに `clickable(x,y)`(浮いている DOM パネルに覆われていないか)を見るので、覆った帯の下は「指せる画素がゼロ」になり、原因を名指ししない「ボディ X に画面から指せる面が無い」で落ちる。実行パネルをブラウザ右(幅320px)に置いて踏み、**ブラウザと同じ列の下端へ移して解決**。モードの間出しっぱなしのパネルはモデルを覆う位置に置けない(一時的なダイアログは別の帯でよい)。
- **罠(検査)**: 事後検出(`unrepresented_regions`)はボクセル化の時点で分かって短時間で戻るので、`optimizeRunning === true` の立ち上がりをポーリングで待つと**取り逃す**。結果(通知の中身)で待つ。`tests/test_ui_command_wiring.py::test_every_hand_dispatched_command_has_a_branch` が番人(`catalog` に `ws="special"` を足して `_dispatch` に分岐が無いと落ちる)。
- **罠(Svelte)**: runes モードのインスタンススクリプトは `export` を持てない(`OptimizeNotice` 型は `optimizeNotice.ts`)。画面に出す文字列に markdown の `**` を書かない(`.svelte` のテンプレートは markdown を解釈しない。強調は `<strong>`)。
- `web/dist` は `.gitignore` 対象。`web/src` を触ったら `npm run build` が要るが、コミットには現れない(緑の git status は「ビルドした」の証拠にならない)。


## ステップ(探索の合格形を切り替えて見る・選んで仕上げる。`duocad/optimize_steps.py`、2026-10-12 本人指示)

- **正はサーバ。** 1回の実行に `OptimizationSteps` が1つ(`Session.optimization_steps`、その実行の文書とファイル名を控える)。探索の
  `on_accept`(`binary_search.find_best_feasible`)が合格形(`AcceptedShape`、1セル1ビット)を足し、探索が合格形を出して終わると
  `optimize_run` が `finisher`(選んだステップを完成化・完成形検査・さかのぼり・追加まで進める手順。条件・格子はその実行のもの)を置く。
  実行の後も、次に走らせるまで残る。いま開いている文書の実行のものだけを返す(`Session.steps_for_current`。同じオブジェクトか同じファイル名)。
- WebSocket に3つ(`catalog` は `protocol`・`ws="special"`・`mcp=False`): `optimize_steps`(一覧 `{steps:[{index, evaluation, seconds,
  material_mm3, safety_factor, cells}], finishable}`)/ `optimize_step_voxels {index}`(その形の**表面のセルだけ**の通し添字。内側は画面から
  見えないので配らない。形はデバッグ表示の `rho` と同じで、`optimizeDebug.ts` の同じ層で描く)/ `finish_optimization {step?}`。
- **`finish_optimization` は2通り。** 走っている間は `Session.request_finish(step)` の旗だけ(`optimize_status` に `finishing: true`)。探索は
  次の候補を作る前に止まり(`is_cancelled` に中断と並べて渡す)、そのステップを仕上げる(終了理由 `stopped_by_user`)。**中断が先**(両方立って
  いれば何も足さない)。合格形がまだ無ければ仕上げずに `OptimizeCancelled`。旗は探索がどう終わっても読む(予算・枯渇で終わる直前に届いた要求も、
  終了理由はそのままで選んだステップを仕上げる)。実行のスレッドが探索の後に要求を最後に読む(`finish_request(final=True)`)と
  `Session` は受け付けを閉じ、それ以降の要求は `optimize_error`(`already_running: true`。走っている表示は消させない)で断る。
  受け付けと読み取りは `Session._finish_lock` で順に並べる(読んだ後に「受け付けた」と答えて黙って捨てる隙間を作らない)。走っていなければ `Session.finish_optimization_step`
  (`optimize_run.finish_step`。探索はやり直さない。終了理由 `finished_after_run`)を別タスクで回す。結果はどちらも `optimize_result` で、
  `finished_step`(0 から)と `steps` が載る。利用者が選んだステップの結果の名前は `optimized-step<番号>`(番号は画面と同じく 1 から)。
- 一覧・進捗・形の各行に実行の識別子 `run`(作った時刻の16桁の16進。文字列の大小が新旧の順)が載る。画面は新しい実行を始めたら
  前の `run` の返事を捨て、いま見ている `run` より古い進捗・一覧も捨てる(別の接続への配信待ちで、前の実行の進捗が後から届くことがある)。
- 進捗(`optimize_progress`)は合格形が増えたとき `step`(上の一覧の1行)を持つ。画面は一覧をこれで伸ばし、走り終わったとき・接続し直したとき・
  文書を切り替えたとき(`resetOptimizeView`)に `optimize_steps` で取り直す。実行の後の仕上げにも `debug` を渡す(`?voxel=1` の表示を新しい結果に合わせる)。新しい実行の開始で `Session.optimization_steps` を空にする(前の実行のステップを見せない)。
- 画面(`OptimizePanel.svelte` の `#optimize-steps`): スライダー・「ボクセルで見る」・「このステップで仕上げる」。走っている間に最後のステップを
  選ぶと「最新を追う」(`step` を省いて送る)。形は選んだときだけ1つ取りに行き、返事を待つ間は次を頼まない(`App.svelte` の `searchStepVoxelsPending`)。
