# 移動・鏡映・ブーリアン・押し出し系 verb の契約

pattern/move/mirror/boolean/envelope/extrude/revolve 系の verb と、そのプレビュー(幽霊)・引数収集 UI の契約。
いまの挙動の正はコードとテスト。ここはソースから読めない不変条件・依存禁止・罠だけ。

## ローカル幽霊(確定前のプレビュー)の層

- **入口は `App.svelte` の `showLocalGhost(verb, args)` 1つで、3経路に振る。** (a) `ghostEntity`(これから打つ `add_*` の図形)、(b) `ghostCopies(verb, args, sources)`(`web/src/entityKinds.ts`。既存スケッチ図形の反復・移動・回転後の姿)、(c) `viewport.showBodyGhosts(entries)`(既存ボディの変換後の姿。実体は `web/src/ghostLayer.ts`、型 `BodyGhost`/`BodyGhostRotation`/`bodyGhostMatrix` は `web/src/viewportTypes.ts`)。新しい verb にプレビューを足すときは `CommandDef.preview: 'local'` にしてこの3つのどれかへ乗せる。
- **ローカル方式はサーバへ何も送らない**(Esc で中断しても文書が変わらないことを「送らない」で成立させる)。投機実行へ寄せない。
- **TS の変換規則はコアの写しで、片方だけ変えてはならない**(幽霊と確定結果が別の場所に出る)。`entityKinds.ts` の `translate`/`rotate` ← `duocad/sketch.py` の `_translate_*`/`_rotate_*`、円形反復の刻み角 ← `duocad/region.py` の `expanded_entities`、点だけ動かすか図形全体か ← `duocad/verbs/move_sketch.py` の `_normalize_targets`/`_moved_params`、`rotatedTargets` ← 同 `_rotation_args`/`_rotated_params`/`_apply`/`_copy`。一致は `tests/test_ghost_agreement.py` と `tests/eval_ghosts.mjs` が突き合わせる。
- **`showBodyGhosts` は本物を隠さない**(`showPreview` は隠す)。幽霊は 1 ボディ = 1 `THREE.Group` で `group.matrixAutoUpdate = false` にして行列を直に置く(`group.position` は効かない)。行列は `T(offset)·T(p)·R·T(-p)`。
- **幽霊が消える場面は `viewport.clearPreview()` に集約**(`update`/`prune`/`showPreview`/`App.clearPreview`/`setMode` がすべてここを通る)。別の消し口を作らない。
- **幽霊は「解決済みの数値」でしか描かない。** 中心・軸を省略したときのコアの既定(スケッチ=対象の代表点の平均 / ソリッド=体積重心)はブラウザへ写さない(規則が2箇所に増える。体積重心は三角形分割からの近似にしかならない)。UI で軸・中心を指させるときも画面側が数値へ解決してから渡す(辺の参照をそのまま `showLocalGhost` へ渡さない。実装は `App.svelte` の `resolvePicksForGhost`)。
- **長方形をまるごと回す指定が1つでも混ざったら、幽霊を1つも描かない**(コアは呼び出し全体を ValueError にする)。`pattern_circular` は逆に4本の線分へ落とすのが正しく、幽霊も同じ落とし方をする。番人は `tests/test_ghost_agreement.py::test_a_rotated_rectangle_ghost_is_still_the_same_rectangle`。
- スケッチ回転の幽霊は `ghostCopies` の `args.mode === 'rotate'` 分岐(引数は `angle`(度)・`center: [u, v]` の数値・任意で `copy`)。`rotateCopies` は **`true`** を渡す(対象そのものを回す。`false` は反復の複製用で向きを変えず位置だけ回す)。

### 依存してはいけないもの

- **`GHOST_COPY_LIMIT`(2000)を仕様と読まない。** 画面が固まらないための表示上限でコア側には無い。超えても Enter は通る(幽霊が出ないだけ)。
- **`ghostInfo()`(と `bodies[].matrix`)・`window.__duocad.showLocalGhost` は実機検査用の穴**で画面状態の正ではない。製品の分岐から呼ばない(`web/src/testHooks.svelte.ts`)。
- **`previewing`(`App.svelte` の `$state`)はサーバ投機実行側だけを表す。** ローカル幽霊が出ていても `false`。
- `showSketchGhosts` は描けた図形だけ数える(折れ線化できない図形は黙って落ちる)。

### 踏んだ罠

- **`move_sketch_entities` は選んだ点だけを動かす**(図形全体ではない)。参照の `point` が `null` なら図形全体、文字列ならその1点。同じ図形に辺と点が両方選ばれたら辺(全体)が勝つ。`copy=true` は常に図形まるごと1つを複製。図形IDに畳んだ `selectedEntities` を幽霊の対象にすると、点1つの選択で図形ごと動く幽霊になる(`GhostSource.points` を使う)。
- **モード切替では `onCancelPreview` が来ない**(`{#key mode.kind}` が `CommandHost` ごと作り直す)。`setMode` の先頭で `clearPreview()` を呼ぶ。**`mode = next` より前**でなければならない(`clearPreview` が旧 `sketchId` を読む)。
- **`web/dist` を作り直さないと実機 smoke は古いバンドルを見る**(`duocad/net/http.py` が配る)。`npm run check` が通っても smoke が「幽霊が出ない」で落ちる。画面側を直したら smoke の前に `npm run build`。
- **原点を通る軸で回転行列を検算しても意味が薄い**(`T(p)·R·T(-p)` の平行移動2項が恒等になり、軸位置を落とした実装でも通る)。非原点の軸、Y軸・負角度・非単位ベクトルで検算する。
- **`onBoxSelect` はスケッチの領域も拾う。** 長方形をウィンドウ選択すると領域参照が混ざる。「図形まるごと1つ」の判定は `item.entity !== undefined` で絞る。
- **選択の正はコア**。クリック/ドラッグ直後に `page.evaluate` で選択を読むと往復前の値を見る(必ず `waitFor`)。

## move_body / move_sketch_entities の回転モード

- **両 verb とも `mode`(`translate`/`rotate`。宣言の正は `duocad/verbs/_common.py` の `MOVE_MODES`)で分岐する。モードが取らない引数を渡すと必ず ValueError**(黙って無視も両方適用もしない)。
- **`axis_edge`/`center_vertex`/`center_entity` は `commit_selection` を介さない参照**で受ける(確定選択は「動かす対象」の解決に使われ、軸を載せると対象と区別が付かず軸の辺のボディまで回る。`pattern_circular` の `entities` と同方式)。形は `{"body","index"}`(`kind` は任意、付けるなら `edge`/`vertex` と一致)、`center_entity` は `{"sketch","entity","point"}`(`point` 省略=その種別の最初の点。並びの正は `sketch.entity_point_keys`)。
- **軸の位置の解決順は `center_vertex` → `axis_point` → 辺の位置 → 対象の合成重心。** `axis_point` と `center_vertex`、`axis_edge` と `axis_point`/`axis_dir` は同時に渡せない。`axis_edge` + `center_vertex` は許す(辺が向き・頂点が位置)。
- **`angle` は度。** スケッチは u→v 反時計が正(`pattern_circular` の `total_angle` と同じ)、ソリッドは軸に対し右ねじが正。**360の倍数は ValueError**(UI 側は `commands/moveMode.ts` の `rejectAngle`)。
- **操作ログに載るのは解決済みの数値だけ。** undo は「逆向きに同じ角度」で戻すが、軸に使った辺は対象ボディに属していれば既に回った後にいるので、参照を引き直すと別の軸で戻る。`_rotation_args` が入口で数値へ潰し、`_do_*`/`_undo_*` は数値しか見ない。
- **`mode` キーの無いログは並進として読む**(`args.get("mode", MODE_TRANSLATE)`)。2026-09-05 より前の保存文書には `mode` が無く、この既定を消すと開けなくなる。
- **添字は回転でも変わらない**(`copy=False` は `TopoDS_Shape.Moved` で位置だけ差し替え、下層の形状を共有)。`resets_selection` は他の形状変更 verb と同じ既定で、回転が壊すからではない。
- **長方形をまるごと回すのは ValueError**(仕様)。回すと線分4本に変わり、「undo = 逆向きに同じ角度」も元の図形 ID と `dN` の名前つき寸法の復元も成り立たない。`pattern_circular` は複製を展開時に作るので落とせる。長方形の1点(左下)を回すのはできる。
- **`Session` の戻り値(= `Applied`)はモードによらずキーが同じで、使わない側が `null`。** `move_body`: `mode/dx/dy/dz/angle/axis_point/axis_dir/copy/created/changed_bodies`、`move_sketch_entities`: `mode/dx/dy/angle/center/copy/created/sketch_ids`。
- **回転の引数の WS 露出は `ws="special"`**(省略できる引数があるので `ws="table"` は不可。表は載せた引数を全部必須にするため並進の UI が送れなくなる)。画面は `mode` の選択(`commands/moveMode.ts`)を持ち、回転軸を辺クリック・中心を頂点クリックで指す(`PickArgSpec`、共有選択集合には入れない)。詳細は `duocad/catalog.py` の `move_body`/`move_sketch_entities` の note。
- kernel の面: `kernel.rotate_shape`/`kernel.edge_axis(shape, edge_index)`/`kernel.centroid`(体積重心)、`sketch.rotate_point`。

### 依存してはいけないもの

- **`center`/`center_vertex` が3Dの結果を変えると思わない。** 3Dの回転は軸線上のどの点を基準にしても同じ変換。中心が効くのは軸の位置を持たないとき(`axis_dir` だけ → 重心を通す)と、`center_vertex` で軸を寄せるときだけ。
- **`Applied` の `axis_point` を「ユーザーが渡した値」と読まない。** 解決後の数値で、辺で指したときはその辺から取った点が入る。
- **`describe_shape`(MCP)は辺・頂点の ID を返さない**(`query.describe_edges`/`describe_vertices` はあるが MCP ツールにしていない)。AI が軸の辺の添字を知る経路は `set_selection` + `get_selection`(参照に `index` が入る)だけ。

### 既知の穴(判断)

`move_body` の `copy=True` で、対象がその軸まわりの回転対称を持つ角度(立方体を自分の中心軸で90度など)を指すと元と完全に重なる複製ができる。スケッチ側は `params` 一致で弾けるが、ソリッドに安くて健全な判定が無い(境界箱の一致は L 字180度回転の正当な場合を誤って断り、最小距離 `BRepExtrema_DistShapeShape` は角を共有する正当な複製を誤って断り、ブーリアン共通部分は重い)。**不健全な判定を入れるより通す**を選んだ(注意書きは `duocad/verbs/move_body.py`)。

## 数値引数の既定値(`NumberArgSpec.default`)と空欄軸

- **`NumberArgSpec.default?: number`**(`web/src/commands/command.ts`)。消費者は `CommandHost.svelte` の `values` 導出(`raw === '' ? (spec.default ?? NaN) : …`)だけで、`default` を持つ数値引数は空欄でもその値が `collected[spec.name]` に入り `argSatisfied`/`firstMissingArg` を素通りする。付いているのは移動量(`dx/dy/dz`、`dx/dy`。`commands/moveMode.ts`)の `default: 0` と `envelope_regions` の R・d(`commands/sketch.ts`)。型は `number` のみ。
- **opt-in**: 付けなければ空欄は NaN で `argSatisfied` が false。**半径・距離・個数のように 0 が無効値・未確定を意味する引数に `default: 0` を付けない**(黙って 0 が埋まって壊れる)。
- **全軸空欄の拒否は `default` の責務ではなく各 verb の `build()` が個別に持つ**(移動量が全部 0 → 「移動量が全部0」、コア側 translate モードにも同じ ValueError)。`default` を付ける新 verb は、全部既定値落ちを拒否するチェックを自分で書く。
- **ローカルプレビューに追加のフックは要らない。** `ghostBuilt`/`built` は共通の `values`(`default` 込み)から `command.build()` を呼ぶので、`default` を付ければプレビューも自動で追従する。
- **実機検査の罠**: (1) `copy` の選択(`#move-body-move`/`#sketch-move-move` 等)を押すまで dx/dy/dz の入力欄が `ready` にならない。 (2) `page.evaluate` で `getBoundingClientRect()` を単体で返すと DOMRect のプロパティが own enumerable でなく座標が NaN になり `page.mouse.click` が `Invalid parameters` で落ちる。rect 取得と `project*` 呼び出しを同じ `evaluate` 内で行い、`{x: pos.x + rect.left, …}` まで計算して返す。

## 鏡映(mirror_sketch_entities / mirror_body)と参照ピック UI

- **どちらも常に複製を作る**(in-place 反転も `copy` 引数も無い)。
- **軸・鏡映面は数値へ解決してからログに載せる**(`axis_p0`/`axis_dir`、`plane_point`/`plane_normal`)。対象に軸の図形/面自身が混ざる場合、複製後に参照を引き直すと別物を指すため(`move_body.axis_edge` と同じ)。
- **軸に使った線分自身が対象に混ざっていたら、対象から黙って外す**(`mirror_sketch.py`。直線は自分自身へ映り元と重なる複製にしかならない)。対象が軸だけになったら ValueError。
- **長方形は軸が水平・垂直のときだけ種別を保てる。** 斜めの軸では `sketch._mirror_rectangle` が4本の線分へ分解した結果を返し、`mirror_sketch_entities` 側が検査して ValueError にする(回転と同じ二段構え)。この分解は ValueError へ変換するための中間表現で、戻り値に線分が出ることはない。
- **円弧の鏡映は始点・終点を入れ替えてから角度を映す**(`new_start = 2*axis_angle - end`、`new_end = 2*axis_angle - start`)。鏡映は向きを反転するので別々に映すと掃引方向が壊れる。掃引量(`end-start`)は保たれる。
- `EntityKindOps.mirror`(5種別とも実装。`mirror(params, ax, ay, ux, uy) -> List[dict]`、rotate と同じ「種別が変わりうる」形)、`kernel.mirror_shape(shape, point, normal, *, copy=True)`(`gp_Trsf.SetMirror`)。
- **鏡映基準の指し方は既存の `PickArgSpec`(対象とは別チャンネルで図形を1つ直接ピック)に種別を足して実現。** `PickTarget = 'edge'|'vertex'|'face'|'sketchPoint'|'sketchEntity'`。`face` は `PickEvent.hit`、`sketchEntity` は `PickEvent.sketchEntity`(点は落とす)から拾う。種別は `PickEvent` が既に持つものから選ぶ(新しい当たり判定を作らない)。
- **`MIRROR_SKETCH_ENTITIES`**(`commands/sketch.ts`): 対象は選択、`axis` は `sketchEntity` ピック(必須・自動で構える)。`build` は `{axis}` だけを送り、`runsWithoutSketchId` は true。`catalog.py` は `ws="table"`(必須の引数1つなので表の規約に合う。`special` は `_dispatch` に手書き分岐を増やす)。
- **`MIRROR_BODY`**(`commands/features.ts`): `base_plane` の choice は XY/XZ/YZ + 4つめ `'face'`(「面を選ぶ」)。`'face'` はサーバへ行かず、`build` が `plane_face` へ読み替える印(コアの `base_plane` は XY/XZ/YZ のみ)。面ピックは `when: base_plane==='face'` で開く(外すと起動した瞬間に面ピックが開いて対象を選び直せない)。`mirror_body` は `ws="special"` で `plane_face` も WS に開いている(`_dispatch` が `plane_face=message.get("plane_face")` を渡す)。
- **ピックの分岐は `onPick`/`onSketchPick` で `editSelection` より前に置く**(`tests/test_ui_command_wiring.py` が静的に固定)。
- **修飾キー(Shift/Ctrl)付きクリックはピックへ行かない**(`event.mode === 'replace'` だけがピックに食われる)。必須ピックは対象が1つ揃った瞬間に自動で構えるので、素のクリックを全部食うと2つ目の対象が選べない。`move_body.axis_edge`/`center_vertex` にも同じ分岐が効く。
- **面を1つ指させている間は辺を出さない**(`App.svelte` の `wantsEdges` に `wantedPick !== 'face'`)。辺は面より先に拾われ、出すと鏡映面を狙った素のクリックが辺に取られて対象選択を置き換える。面ピックを持つ verb を新設するなら同じ配慮が要る。
- **軸は図形そのもの**: 端点付近を押しても `point` を落として `{sketch, entity, point: null}` に畳む。
- **`pickedLabel(value, target)` は `target` を渡す。** 同じ `{sketch, entity, point:null}` でも `sketchPoint` は「図形の最初の点」、`sketchEntity` は「図形1つ」。TS 上は省略できるが第1引数だけで呼ぶと表示が誤る。
- **幽霊は無い**(`preview: 'none'`/`previewable=False` を両方維持。`resolvePicksForGhost` 経路には繋がっていない)。Enter で確定する(`needsEnter` が真)。
- 却下した案: 専用の入力モード(`PickArgSpec` の `pendingPick`/`armedPick`/`wantsPick` と二重になる)、軸ピックを `optional` にして自動で構えない案(軸が要ることが伝わらない)、スケッチ側の `base_plane` 相当の3択(平面2Dに基準"軸"の概念が無い)。
- **`face_fingerprint`(面積+法線照合)は鏡映で壊れない**(複製の新ボディは元と別 ID なので指紋が独立)。鏡映複製ソリッドの面法線は反転する(上面 `[0,0,1]` → `[0,0,-1]`)が `face_fingerprint`/`select_face` は壊れない。Body に元ボディ由来の参照を足すなら、この独立性の前提から見直す。

### 踏んだ罠

- **`ws="none"`(MCP 専用)の verb は画面の `Applied` 型に載らない**(`add_mesh_body` 等。ログには載る)。`Applied` は `web/src/generated/protocol.ts` の生成物で、同期の検査は `tests/test_protocol.py`。`mirror_sketch_entities` は `ws="table"` で生成の対象に入れてある。MCP 専用 verb を足すときの扱いは docs/DEV_NOTES.md の「`ws="none"`(MCP 専用)の verb は画面の `Applied` に載らない」項。
- **`#sketch-look` が合わせる視野は「そのとき見えているもの」に依る。** 板だけがあるスケッチへ入り直して押すと原点付近の (0,-20) が画面外になり `projectSketchPoint` が null を返す。スケッチの図形は入った直後に全部描く順序にする。
- **円は直径で持つ。params キーは `diameter`**(`radius` ではない。円弧は `radius` のまま)、中心は `cx`/`cy`。楕円は `cx`/`cy`/`width`/`height`/`angle`(`width` が `angle` の向きの直径)。円も楕円も領域を作る。押し出す領域は面積最大(`regions` の `index/area/centroid`)で選ぶ。
- **`pytest.approx(0.0)` は絶対許容差がほぼ 0。** `kernel.bounding_box` を鏡映(x=0)で検算すると OCCT の境界箱が許容差ぶん膨らんで 1e-7 程度の誤差が乗るので、0 との比較には `abs=1e-4` を明示。
- **`smoke-mirror.mjs` の円を描く手順が 11 回中 1 回だけ落ちた**(原因未確定、再現せず)。描く 4 verb は点を 1 つずつ食うので、クリックが早すぎると図形が出ないのに理由も出ない。1 手ごとに `#command-prompt` の待ち受け(`awaitPrompt`)で確かめ、失敗時にスケッチの図形・選択・トーストを出す。

## boolean_body

- **ツールボディは引数で渡さない。** 呼ぶ前に `commit_selection` で確定選択(面/辺/頂点、`min=1`)へ入れ、内部で `selection.require_bodies(document)` が逆引きする。`target_ref` は `{"body","kind":"face","index"}`(面1枚。確定選択には入れない)、`mode` は `union|cut|intersect`。verb 層は `verbs.boolean_body(doc, target_ref, tool_body_ids, mode)`。WS は表(`ws=table`)で `target_ref`/`mode` とも必須。
- **ターゲットと同じ BodyId がツールに混ざっていたら ValueError**(黙って畳まない)。
- **交差は `target ∩ (tool1 ∪ tool2 ∪ …)`**(`target ∩ tool1 ∩ tool2…` ではない)。ツールは常に先に `BRepAlgoAPI_Fuse` で1つへ融合してからターゲットと演算する(`kernel.boolean_bodies` を素通しで使えばこの意味になる)。
- **1回の undo/redo で「ターゲットの形状」と「消えたツール Body 群」の両方が戻る。** `_common.SNAPSHOTS` に `(target_body_id, seq)` と `("boolean_body:removed", seq)` の2鍵で退避。この2鍵構成を崩すと片方だけ戻る。
- **ソリッド限定チェックは `kernel.is_solid()`(SOLID 0 個なら False)を通す。** `selection.py` と verb 層は `OCP` を直接 import しない(`tests/test_boundaries.py` が強制)。
- **プレビュー再計算は既存の汎用機構が覆う**: `CommandHost.svelte` の `selectionPrint`(`kind:'selection'` の arg を持つコマンドは確定選択の中身をプレビュー再送キーに含める)。選択の増減でプレビューが動く verb を足すときも、まずこれで足りるか確認する。
- Web の `BOOLEAN_BODY` の `args` 順は `[pick target_ref(face), selection tools(face/edge/vertex, min=1), choice mode]`(`firstMissingArg` の質問順)。`SOLID_FEATURE_COMMANDS` にのみ追加。
- **union だけ同一平面の面を統合する**(`_unify_same_domain`。隣り合うボディの継ぎ目を消し、またぐ辺にフィレットをかけられるようにする)。cut / intersect は統合しない。`provenance` は押し出しと同じ形(`face_map` の鍵はターゲットの面、ツール由来の面は `new_faces`)。
- 1つの Body 内に複数 SOLID(compound ソリッド)を持つ場合の個別演算は未保証(`boolean_bodies` は「SOLID が 1 個以上あるか」しか見ない)。
- 新しい verb の追加は `MCP.md` のツール表(`test_doc_code_agreement.py`)と画面の `Applied` 型(`web/src/generated/protocol.ts` の生成物。`npm run check`)の追随が不可避。

## shell_body(シェル・中空化)

- 呼ぶ面は `shell_body(thickness)`。抜く面(開口)は確定選択の面から `selection.require_faces` で取る(選択順・1ボディ限定。複数ボディにまたがれば ValueError)。verb は `duocad/verbs/shell_body.py`、幾何は `duocad/kernel_shell.py` の `shell_solid`(依存は kernel_shell → kernel の一方向)。
- **仕様(2026-10-14 本人判断)**: 抜く面は1枚以上で、**面を選ばない閉じた中空は常に断る**(空の選択は `SelectionEmpty`、ボディだけの選択は `SelectionKindMismatch`)。厚さは外形から内側へ一様(外形寸法を保つ)、内側の角は `GeomAbs_Intersection`(尖ったまま)。後から厚さを変える手段は無い(undo してやり直す)。
- **合否は `IsDone` と `BRepCheck` だけで決めない**: Solid がちょうど1つ・体積が実際に減った・内壁(由来の `new_faces`)と抜かなかった外側の面との最短距離が厚さを割らないことまで見る。OCCT は厚すぎるとき・抜く面がフィレット面に接するとき成功を装って元の形を返し、出っ張りの半径を超える厚さではそこだけ薄い壁で成功を返す(実測は docs/CAD_LIBRARY_PITFALLS.md)。指定より厚い壁は検出しない。断り文は原因の候補と「先にシェル、後で丸める」を名指しする。
- 許容差は `min(1e-3, 厚さ/1000)`(固定 1e-3 では薄い壁が `IsDone=False`)。
- Web は `SHELL_BODY`(`commands/features.ts`、`SOLID_FEATURE_COMMANDS`、`#shell-body`、厚さ `#shell-thickness`)。面の選択引数は `min: 1`。プレビューはサーバの投機実行(`preview: 'server'`)。

## envelope_regions(スケッチの包絡)

- 呼ぶ面は MCP/セッション `envelope_regions(radius=0, offset=0)`(対象は確定選択の領域。WS は radius・offset 必須)。verb は `duocad/verbs/envelope_regions.py`、幾何は `kernel_sketch.envelope_faces`/`face_boundary_entities`。プレビューは投機実行でなく `preview_envelope_regions`(分裂しても失敗にせず全部の線と個数を返す)。
- **結果は線分・円弧・円のみ。面がちょうど 1 枚でなければ ValueError で何も変えない。** redo/replay はログの図形数値を使う。
- **依存禁止**: 領域の添字(後の編集で指す先が変わる)。負のオフセットを OCCT へ直接頼むこと。
- **罠**: 同心円の面の内向きオフセットは失敗する(箱 − 補集合の拡大で代替)。詳細は docs/CAD_LIBRARY_PITFALLS.md。

## 押し出し(extrude)の kernel 分離・web 統合・extrude_face 廃止

- **押し出しの幾何は `duocad/kernel_extrude.py`**(`extrude_regions(faces, distance, mode, normal, target_shape=None)` と `extrude_faces`。mode は join/cut/new)。`verbs/extrude.py` が直接呼ぶ。`kernel.py` に再エクスポートは無く、`kernel.extrude_regions`/`kernel.extrude_face_on_shape` は存在しない(AttributeError)。`mcp_server.py` から `kernel_extrude.*` を直接呼ばない(`tests/test_mcp_verbs.py` が検査)。
- **依存は `kernel_extrude` → `kernel` の一方向。** `kernel.py` から `kernel_extrude` を import しない(循環)。`kernel_extrude` は kernel の private 補助(`_face_map`・`_face_at`・`_face_normal`・`_face_props`・`_checked_volume`・`_require_valid`・`_unify_same_domain`・`_fuse_all`・`_target_successors`・`_face_map_projection`)を import している。これらの名前・シグネチャを `kernel.py` 側で変えるなら `kernel_extrude` も直す。`face_of`/`_face_at` は `query.py`・`kernel_sketch.py`・テストが使うので `kernel` に残す。
- **OCP を import してよいカーネル層は 6 本**(`tests/test_boundaries.py` の `OCP_ALLOWED` と README の表に `kernel_extrude.py`)。
- **UI から `extrude_face` を呼ぶ経路は無い。** ツールバーの「押し出す」は 1 つ(`#extrude`)。`features.ts` の `makeExtrude('region'|'face')` が同じ id `extrude` の 2 定義を作り、`EXTRUDE_REGIONS`(`SKETCH_FEATURE_COMMANDS`、選択引数名 `regions`)と `EXTRUDE_FACES`(通常モードの `SOLID_FEATURE_COMMANDS`、`faces`)として出す(どちらも min 1・max null)。距離 `#distance`、種別 `#extrude-mode-join/cut/new`。サーバへ送るのは両方 `{distance, mode}` だけ(対象はコアの確定選択)。旧 UI id `extrude_face`、`#extrude-regions`、`#regions-distance` は廃止。
- 同じ id `extrude` が 2 つの一覧に居ても、`CATEGORY_OF`(`App.svelte`)は id→category で両方 `feature` なので壊れない。`findCommand(id, list)` の `list`、`CommandHost` の `commands` プロップは必須(既定なし)。
- `App.svelte` の `verb === 'extrude' && !regionsStillExist(...)` は変更不要(面の選択なら領域参照が空集合で true)。通常モードは領域を要求しないので `wantsRegions` は立たず、`viewport.pickRegions` の 3D 領域ピックは通常モードで使われない(コードは残存)。通常モードでは領域を押し出せない(スケッチ編集中に行う)。
- **押し出しの公開 verb は `extrude` だけ**(領域は `regions`、面は `faces`)。`extrude_face` はカタログ・`Session`・MCP・WebSocket・`duocad.verbs.__all__` に無い。`verbs/__init__.py` は `extrude_face` モジュールを import して `register_verb` の登録だけを残す(再生専用)。`extrude_face` を新しい操作の記録に使わず、新しい verb の形の手本にしない。
- **古い保存ログの `extrude_face`(args: `body_id`/`face_index`/`distance`)は再生される。** 中身は `kernel_extrude.extrude_faces(body.shape, [face_index], distance, "join" if distance > 0 else "cut", target_shape=body.shape)`、undo は `undo_shape`。再生した `extrude_face` の `provenance` は保存時のまま(キーは `verb`/`face_index`/`after_face_index`)。
- **`tests/fixtures/extrude_face_log.json` は削除前の実装で作った保存ログ(体積 12240・6 面)で、`tests/test_extrude_face_replay.py` が再生して固定する。作り直さない**(「従来と同じ形」の根拠が失われる)。
- 再生専用 verb(`extrude_face`・`set_parameter`)はカタログに無いので、`tests/test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log` は登録済み verb からこの2つを除いて突き合わせる。
- 曲面を掃引すると壊れた形になる実測は docs/CAD_LIBRARY_PITFALLS.md の表が `kernel_extrude._check_coplanar_planes` を指す。

### 踏んだ罠

- **面の押し出しでは溝の底のような窪みの面は視点から見えず選べない。** 2 枚の同一平面の面を押す smoke の題材は溝を浅く広く(幅 24・深さ 3)した(平面の違う面を混ぜるとトーストに「同一平面上にない」)。ログの `args.faces` は配信で件数の文字列に畳まれる。

## 回転体(revolve)

- **幾何は `kernel_extrude.revolve_regions`**(OCP を触るカーネル層を7本目に増やさないため同居)。領域の立体を束ねて相手へ join / cut する後半は `extrude_regions` と共有(`_combine_region_solids`。統合・由来の射影も押し出しと同じ規則)。verb 側は `verbs/extrude.py` の `_resolve_faces_and_normal` / `_resolve_target` / `_editable_target` を `what="revolve"` で使う。
- **軸は `"x"` / `"y"`(スケッチの原点を通る軸)か、領域と同じスケッチの線分 `{"sketch", "entity"}`。** ソリッドの辺・基準軸はまだ受けない。**公開面(catalog・MCP・WS)では `axis`(x / y)と `axis_line`(線分)の2引数に分けて排他で受け**、入口(`_enter_revolve`)が verb の1つの `axis` へ畳む。1引数(文字列か参照の和)にすると画面の欄(軸の3択・線分のピック)と作業空間の引数が名前で往復できず、再読み込みや AI の `set_command_args` で指した線分が消えた(sol レビューで発覚。`mirror_body` の `base_plane` / `plane_face` と同じ形にした)。ログには**参照のまま**載せ、再生で引き直す(線分の寸法を後から変えると追従する)。鏡映が数値へ解決して載せるのは、軸自身が複製の対象に混ざりうるからで、回転体にはその事情が無い。
- **角度は度で `0 < |angle| <= 360`、負は逆向き**(押し出しの距離の符号と同じ扱い)。正は軸の向き(線分の始点→終点)まわりの右ねじ。`symmetric` は両側へ半分ずつ。360° は角度なしの `BRepPrimAPI_MakeRevol` で作る。
- **軸が領域を横切るのは ValueError**(`_require_one_side_of_axis`)。軸の両側に半平面を置いて共通部分の面積で決める(辺が軸上にあるのは面積0なので通る)。境界を折れ線で拾う判定は、軸をわずかにまたぐ円弧を取りこぼす。
- **360° の回転体のシーム(継ぎ目の辺)をフィレットに渡すと、OCCT が「適した辺が無い」で断り、verb は例外で何も変えない**(`tests/test_revolve.py` が固定)。ピックからは外していない。
- **画面(`REVOLVE`、スケッチモード)は 軸の3択(x軸 / y軸 / 線分を選ぶ)+ 線分のピック(`axis_line`。共有選択に入れない)+ 角度(既定 360)+ 種別。** 「線分を選ぶ」は `sends: null` でサーバへ行かず、`build` が `axis_line` を送る(`MIRROR_BODY` の「面を選ぶ」と同じ形)。**`sends: null` の選択肢は `impliedBy` に配信から自分を戻す引数名を持つ**(「線分を選ぶ」は `axis_line`、「面を選ぶ」は `plane_face`。`CommandHost.syncFromShared` が使い、`tests/test_ui_command_wiring.py` が全選択肢に課す)。無いと再読み込みで選択肢が戻らず、ピックの欄が隠れたまま確定できない。**両側(`symmetric`)は画面から送らない**(選択肢は既定値を持てず、毎回1クリック増えるため)。プレビューは押し出しと同じサーバの投機実行。実機検査は `web/scripts/smoke-revolve.mjs`。
