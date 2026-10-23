# 移動・鏡映・ブーリアン・押し出し系 verb の契約

pattern/move/mirror/boolean/envelope/extrude/revolve 系の verb と、そのプレビュー(幽霊)・引数収集 UI の契約。
いまの挙動の正はコードとテスト。ここはソースから読めない不変条件・依存禁止・罠だけ。

## ローカル幽霊(確定前のプレビュー)の層

- **入口は `App.svelte` の `showLocalGhost(verb, args)` 1つで、3経路に振る。** (a) `ghostEntity`(これから打つ `add_*` の図形)、(b) `ghostCopies(verb, args, sources)`(`web/src/entityKinds.ts`。既存スケッチ図形の反復・移動・回転後の姿)、(c) `viewport.showBodyGhosts(entries)`(既存ボディの変換後の姿。実体は `web/src/ghostLayer.ts`、型 `BodyGhost`/`BodyGhostRotation`/`bodyGhostMatrix` は `web/src/viewportTypes.ts`)。新しい verb にプレビューを足すときは `CommandDef.preview: 'local'` にしてこの3つのどれかへ乗せる。
- **ローカル方式はサーバへ何も送らない**(Esc で中断しても文書が変わらないことを「送らない」で成立させる)。投機実行へ寄せない。
- **TS の変換規則はコアの写しで、片方だけ変えてはならない**(幽霊と確定結果が別の場所に出る)。`entityKinds.ts` の `translate`/`rotate` ← `duocad/sketch.py` の `_translate_*`/`_rotate_*`、円形反復の刻み角 ← `duocad/sketch.py` の `circular_pattern_step`、点だけ動かすか図形全体か ← `duocad/verbs/move_sketch.py` の `_normalize_targets`/`_moved_params`、`rotatedTargets` ← 同 `_rotation_args`/`_rotated_params`/`_apply`/`_copy`。一致は `tests/test_ghost_agreement.py` と `tests/eval_ghosts.mjs` が突き合わせる。
- **`showBodyGhosts` は本物を隠さない**(`showPreview` は隠す)。幽霊は 1 ボディ = 1 `THREE.Group` で `group.matrixAutoUpdate = false` にして行列を直に置く(`group.position` は効かない)。行列は `T(offset)·T(p)·R·T(-p)`。
- **幽霊が消える場面は `viewport.clearPreview()` に集約**(`update`/`prune`/`showPreview`/`App.clearPreview`/モードが変わったときの `onModeChanged` がすべてここを通る)。別の消し口を作らない。
- **幽霊は「解決済みの数値」でしか描かない。** 中心・軸を省略したときのコアの既定(スケッチ=対象の代表点の平均 / ソリッド=体積重心)はブラウザへ写さない(規則が2箇所に増える。体積重心は三角形分割からの近似にしかならない)。UI で軸・中心を指させるときも画面側が数値へ解決してから渡す(辺の参照をそのまま `showLocalGhost` へ渡さない。実装は `App.svelte` の `resolvePicksForGhost`)。
- **長方形は回しても鏡映しても長方形のまま**(基準の角を動かし、`angle` を回す。`web/src/entityKinds.ts` の `rotate`/`mirror` はコアの `_rotate_rectangle`/`_mirror_rectangle` の写し)。幽霊も同じ長方形を描く。番人は `tests/test_ghost_agreement.py::test_a_rotated_rectangle_ghost_is_still_the_same_rectangle`。
- スケッチ回転の幽霊は `ghostCopies` の `args.mode === 'rotate'` 分岐(引数は `angle`(度)・`center: [u, v]` の数値・任意で `copy`)。`rotateCopies` は **`true`** を渡す(対象そのものを回す。`false` は反復の複製用で向きを変えず位置だけ回す)。

### 依存してはいけないもの

- **`GHOST_COPY_LIMIT`(2000)を仕様と読まない。** 画面が固まらないための表示上限で、コア側の上限は別に `verbs/pattern.py` の `MAX_PATTERN_COPIES`(2500。反復1回で増える図形の総数。再生には掛けない)。2000 を超えて 2500 以下なら Enter は通る(幽霊が出ないだけ)。2500 を超えるとコアが ValueError で断り、画面はトーストに出す。
- **`previewing`(`App.svelte` の `$state`)はサーバ投機実行側だけを表す。** ローカル幽霊が出ていても `false`。
- `showSketchGhosts` は描けた図形だけ数える(折れ線化できない図形は黙って落ちる)。

### 踏んだ罠

- **`move_sketch_entities` は選んだ点だけを動かす**(図形全体ではない)。参照の `point` が `null` なら図形全体、文字列ならその1点。同じ図形に辺と点が両方選ばれたら辺(全体)が勝つ。`copy=true` は常に図形まるごと1つを複製。図形IDに畳んだ `selectedEntities` を幽霊の対象にすると、点1つの選択で図形ごと動く幽霊になる(`GhostSource.points` を使う)。
- **モード切替では `onCancelPreview` が来ない**(`{#key commandRowKey}` が `CommandHost` ごと作り直す)。`App.svelte` の `onModeChanged` の先頭で `clearPreview()` を呼ぶ。**`mode` を差し替える前**でなければならない(`clearPreview` が旧 `sketchId` を読む)。
- **`web/dist` を作り直さないと実機 smoke は古いバンドルを見る**(`duocad/net/http.py` が配る)。`npm run check` が通っても smoke が「幽霊が出ない」で落ちる。画面側を直したら smoke の前に `npm run build`。
- **原点を通る軸で回転行列を検算しても意味が薄い**(`T(p)·R·T(-p)` の平行移動2項が恒等になり、軸位置を落とした実装でも通る)。非原点の軸、Y軸・負角度・非単位ベクトルで検算する。
- **`onBoxSelect` はスケッチの領域も拾う。** 長方形をウィンドウ選択すると領域参照が混ざる。「図形まるごと1つ」の判定は `item.entity !== undefined` で絞る。
- **選択の正はコア**。クリック/ドラッグ直後に `page.evaluate` で選択を読むと往復前の値を見る(必ず `waitFor`)。

## 反復(pattern_rectangular / pattern_circular)

- **複製は実行時に独立した図形として作る**(2026-10-18、本人。DESIGN_DECISIONS「見えるものはすべてさわれる」)。
  返り値もログの行も `created`(作った図形 ID の並び)。元の図形とは繋がらない。
- **旧形式の行(`pattern_id` を持ち `created` が無い)は再生専用。** 振り分けは `created` の有無だけで、旧形式は
  スケッチに `Pattern` を置き、描くときに `region.expanded_entities` が展開する(複製に図形 ID は無い)。
  旧形式を新しい規則で再生すると以後の `d<n>` がずれる。番人は `tests/test_pattern_live_replay.py`。
- 複製が1つもできない指定(`nx*ny < 2`・`count < 2`)と、元と完全に重なる複製ができる指定は ValueError。

## move_body / move_sketch_entities の回転モード

- **両 verb とも `mode`(`translate`/`rotate`。宣言の正は `duocad/verbs/_common.py` の `MOVE_MODES`)で分岐する。モードが取らない引数を渡すと必ず ValueError**(黙って無視も両方適用もしない)。
- **`axis_edge`/`center_vertex`/`center_entity` は選択集合を介さない参照**で受ける(選択集合は「動かす対象」の解決に使われ、軸を載せると対象と区別が付かず軸の辺のボディまで回る。`pattern_circular` の `entities` と同方式)。形は `{"body","index"}`(`kind` は任意、付けるなら `edge`/`vertex` と一致)、`center_entity` は `{"sketch","entity","point"}`(`point` 省略=その種別の最初の点。並びの正は `sketch.entity_point_keys`)。
- **軸の位置の解決順は `center_vertex` → `axis_point` → 辺の位置 → 対象の合成重心。** `axis_point` と `center_vertex`、`axis_edge` と `axis_point`/`axis_dir` は同時に渡せない。`axis_edge` + `center_vertex` は許す(辺が向き・頂点が位置)。
- **`angle` は度。** スケッチは u→v 反時計が正(`pattern_circular` の `total_angle` と同じ)、ソリッドは軸に対し右ねじが正。**360の倍数は ValueError**(UI 側は `commands/moveMode.ts` の `rejectAngle`)。
- **操作ログに載るのは解決済みの数値だけ。** undo は「逆向きに同じ角度」で戻すが、軸に使った辺は対象ボディに属していれば既に回った後にいるので、参照を引き直すと別の軸で戻る。`_rotation_args` が入口で数値へ潰し、`_do_*`/`_undo_*` は数値しか見ない。ただし `move_sketch_entities` の undo は逆向きに動かさず、args の `previous`(動かす前の `params`)を書き戻す。逆向きだと `x + dx - dx != x` で1ulpずれ、同面積の領域の並びが再生した文書と食い違う。`previous` は旧ログに無く、その undo だけ逆向きに動かす。
- **`mode` キーの無いログは並進として読む**(`args.get("mode", MODE_TRANSLATE)`)。2026-09-05 より前の保存文書には `mode` が無く、この既定を消すと開けなくなる。
- **添字は回転でも変わらない**(`copy=False` は `TopoDS_Shape.Moved` で位置だけ差し替え、下層の形状を共有)。`resets_selection` は他の形状変更 verb と同じ既定で、回転が壊すからではない。
- **長方形をまるごと回すと、長方形のまま回る**(基準の角を回し、`angle` に角度を足す。`pattern_circular` の複製も同じ)。`angle` を持たない旧い長方形を回すと `_write_params` が `angle` を新しい名前つきの値として足し、その undo は足した `angle` と名前を外す(`previous` に `angle` が無いことから分かる)。長方形の1点(左下)だけを回すこともできる。
- **`Session` の戻り値(= `Applied`)はモードによらずキーが同じで、使わない側が `null`。** `move_body`: `mode/dx/dy/dz/angle/axis_point/axis_dir/copy/created/changed_bodies`、`move_sketch_entities`: `mode/dx/dy/angle/center/copy/created/sketch_ids`。
- **回転の引数は WebSocket の表(`ws="table"`)に `required=False` で開いている**(`mode` で要る引数が入れ替わるので、どれも任意にして足りないものはコアが ValueError で断る。省略された引数は None のまま verb へ渡す)。画面は `mode` の選択(`commands/moveMode.ts`)を持ち、回転軸を辺クリック・中心を頂点クリックで指す(`PickArgSpec`、共有選択集合には入れない)。詳細は `duocad/catalog.py` の `move_body`/`move_sketch_entities` の note。
- kernel の面: `kernel.rotate_shape`/`kernel.edge_axis(shape, edge_index)`/`kernel.centroid`(体積重心)、`sketch.rotate_point`。

### 依存してはいけないもの

- **`center`/`center_vertex` が3Dの結果を変えると思わない。** 3Dの回転は軸線上のどの点を基準にしても同じ変換。中心が効くのは軸の位置を持たないとき(`axis_dir` だけ → 重心を通す)と、`center_vertex` で軸を寄せるときだけ。
- **`Applied` の `axis_point` を「ユーザーが渡した値」と読まない。** 解決後の数値で、辺で指したときはその辺から取った点が入る。
- **`describe_shape`(MCP)は辺・頂点の ID を返さない**(`query.describe_edges`/`describe_vertices` はあるが MCP ツールにしていない)。AI が軸の辺の添字を知る経路は `set_selection` + `get_selection`(参照に `index` が入る)だけ。

### 既知の穴(判断)

`move_body` の `copy=True` で、対象がその軸まわりの回転対称を持つ角度(立方体を自分の中心軸で90度など)を指すと元と完全に重なる複製ができる。スケッチ側は `params` が座標の許容差(1e-9mm)で一致すれば弾ける(`_common.overlaps_original`。新しく作る複製だけで、redo / 再生は厳密一致のみ。線分の端点の入れ替えや長方形の向きの違いで `params` が変わる重なりは弾けない)が、ソリッドに安くて健全な判定が無い(境界箱の一致は L 字180度回転の正当な場合を誤って断り、最小距離 `BRepExtrema_DistShapeShape` は角を共有する正当な複製を誤って断り、ブーリアン共通部分は重い)。**不健全な判定を入れるより通す**を選んだ(注意書きは `duocad/verbs/move_body.py`)。

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
- **軸(`mirror_sketch_entities`)・回転中心(`move_sketch_entities` の `center_entity`)に別のスケッチの図形を指すときは、対象と (u, v) が同じ平面上の同じ点を指すこと**(`_common.require_same_uv_plane`)。数値の (u, v) を対象の座標へそのまま当てるので、平面が違うと黙って別の場所に映る・回る。向きが同じで原点が法線方向にずれただけのオフセットしたスケッチは通す。
- **長方形は軸が斜めでも長方形のまま鏡映される**(`sketch._mirror_rectangle` が向きを `2φ - angle`、基準の角を元の「height 側の隣の角」の鏡像にして1つの長方形を返す)。
- **円弧の鏡映は始点・終点を入れ替えてから角度を映す**(`new_start = 2*axis_angle - end`、`new_end = 2*axis_angle - start`)。鏡映は向きを反転するので別々に映すと掃引方向が壊れる。掃引量(`end-start`)は保たれる。
- `EntityKindOps.mirror`(7種別とも実装。`mirror(params, ax, ay, ux, uy) -> Dict[str, float]`、rotate と同じく**同じ種別の `params` を1つ返す**)、`kernel.mirror_shape(shape, point, normal)`(`gp_Trsf.SetMirror`。常に独立した複製を返す)。
- **鏡映基準の指し方は既存の `PickArgSpec`(対象とは別チャンネルで図形を1つ直接ピック)に種別を足して実現。** `PickTarget = 'edge'|'vertex'|'face'|'sketchPoint'|'sketchEntity'`。`face` は `PickEvent.hit`、`sketchEntity` は `PickEvent.sketchEntity`(点は落とす)から拾う。種別は `PickEvent` が既に持つものから選ぶ(新しい当たり判定を作らない)。
- **`MIRROR_SKETCH_ENTITIES`**(`commands/sketch.ts`): 対象は選択、`axis` は `sketchEntity` ピック(必須・自動で構える)。`build` は `{axis}` だけを送り、`runsWithoutSketchId` は true。`catalog.py` は `ws="table"`(必須の引数1つなので表の規約に合う。`special` は `_dispatch` に手書き分岐を増やす)。
- **`MIRROR_BODY`**(`commands/features.ts`): `base_plane` の choice は XY/XZ/YZ + 4つめ `'face'`(「面を選ぶ」)。`'face'` はサーバへ行かず、`build` が `plane_face` へ読み替える印(コアの `base_plane` は XY/XZ/YZ のみ)。面ピックは `when: base_plane==='face'` で開く(外すと起動した瞬間に面ピックが開いて対象を選び直せない)。`mirror_body` は `ws="special"` で `plane_face` も WS に開いている(`_dispatch` が `plane_face=message.get("plane_face")` を渡す)。
- **ピックの分岐は `onPick`/`onSketchPick` で選択の編集より前に置く**(`tests/test_ui_command_wiring.py` が `wantsPick` の分岐を固定する)。
- **修飾キー(Shift/Ctrl)付きクリックはピックへ行かない**(`event.mode === 'replace'` だけがピックに食われる)。必須ピックは対象が1つ揃った瞬間に自動で構えるので、素のクリックを全部食うと2つ目の対象が選べない。`move_body.axis_edge`/`center_vertex` にも同じ分岐が効く。
- **面を1つ指させている間は、面より細かい的(辺・頂点・スケッチの点と線)を指せない**(`App.svelte` の `pickKinds` の `FINER_THAN`)。細かい的は面より先に拾われ、鏡映面を狙った素のクリックが辺に取られて対象選択を置き換える。辺を待つ間(回転軸)は頂点を外す。直接ピックを持つ verb を新設するなら `FINER_THAN` に足す。
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

## split_body(ボディの分割。#193)

- 呼ぶ面は `split_body(base_plane | plane_face | sketch_id, keep="both")`。対象は確定選択の面/辺/頂点が属するボディ(`mirror_body` と同じ。複数ボディなら全部を同じ平面で割る)。verb は `duocad/verbs/split_body.py`、幾何は `duocad/kernel_split.py`(`kernel.py` には足していない)。
- **切る平面は3つが排他**(0個・2個以上は ValueError): 基準平面 / 平らな面(法線はその面の外向き。`kernel_sketch.face_frame`)/ スケッチの平面(`sketch_plane_frame`。オフセット平面・面に貼ったスケッチを含む)。**`sketch_id` は図形の参照 `{"sketch", "entity"}` も受ける**(画面は図形を1つ指す。`revolve.axis_line` と同じ別チャンネルのピック。後述の理由)。ログには解決済みの `{plane_point, plane_normal}` だけを載せる(`mirror_body` と同じ)。
- **`keep`**: `both`(既定)/ `positive`(法線の向く側だけ)/ `negative`。画面は `keep` を必ず送る(選択肢に既定を持たせない作法。両側を押す1手が増える)。MCP は省略=both。
- **番号・名前・ID の引き継ぎ(本人ではなく親セッションの決定。2026-10-21)**: 元のボディの ID・番号・名前は、**残す塊のうち体積が最大のもの**が引き継ぐ。法線の向きで引き継ぎ先が変わらない(蓋と本体に割ると大きい本体が元の名前を保つ)。体積が相対 1e-9 以内で同じなら正側、それでも決まらなければ重心の (x, y, z) の小さい方(`kernel_split` が塊をこの順に並べて返し、verb は先頭を取る)。ほかの塊は新しいボディで、**名前は `naming` の通し番号(`ボディN`)**(複製・鏡映と同じ流儀。元の名前から派生させる流儀はコードベースに無い)・表示の属性は `dataclasses.replace` で元から写す。片側が離れた複数の塊になるときは `kernel.split_solids` で塊ごとに別のボディ(U字を横に切る)。
- **全ボディの切断と検査が終わるまで文書にも採番にも触れない**(`_plan` が先、`_apply` が後)。**どれか1つでも平面と交わらなければ全体を断る**(一部だけ成功させない)。面に接しているだけ(片側が空)も「交わらない」。メッシュのボディは `editable_body` が断る。
- **ログの形**: `args = {bodies, plane_point, plane_normal, keep, created: [[新ID…]…](ボディごと), numbers: [[通し番号…]…]}`。再生は同じ計算をして、塊を決定的な順で ID・番号へ当てる(塊の数が記録と違えば `copy_ids` が落ちる)。**由来(`provenance`)は** `{"verb", "bodies": [{"body", "pieces": [{"body", "side", "face_map", "deleted", "new_faces"}]}]}` で、`face_map` の鍵は元の面、値は**その塊の**面の添字、`new_faces` が切り口。
- **undo**: 元のボディの形を `_common.SNAPSHOTS` へ退避し(`fillet` と同じ。`(body_id, seq)` 鍵)、作ったボディは `remove_created_bodies`(足した順の逆)。active は足す直前の値へ戻る(`mirror_body` と同じ)。
- **プレビューは投機実行**(`previewable=True`)。`kernel_split.split_by_plane` の結果は `kernel.remembered` に載るので、確定はプレビューの形をそのまま使う。
- Web は `SPLIT_BODY`(`commands/features.ts`、`#split-body`)。平面の choice は XY/XZ/YZ + `face`(「面を選ぶ」)+ `sketch`(「スケッチを選ぶ」)、後ろの2つは `sends: null` で `build` が `plane_face` / `sketch_id` へ読み替える。**スケッチの平面は「スケッチの線を1本指す」**(`PickArgSpec` の `sketchEntity`。平面そのものを指す的は画面に無く、点は指せない: `FINER_THAN` が点を外す)。そのため**線・円などの無いスケッチの平面は画面からは指せない**(MCP は `sketch_id` で指せる)。`sketch_id` の UI の値(図形の参照)とサーバが配る値(文字列)が違いうるので、AI が文字列で埋めた `sketch_id` は画面のピック欄には戻らない。

## boolean_body

- **ツールボディは引数で渡さない。** 呼ぶ前に選択集合(面/辺/頂点、`min=1`)へ入れ、内部で `selection.require_bodies(document)` が逆引きする。`target_ref` は `{"body","kind":"face","index"}`(面1枚。選択集合には入れない)、`mode` は `union|cut|intersect`。verb 層は `verbs.boolean_body(doc, target_ref, tool_body_ids, mode)`。WS は表(`ws=table`)で `target_ref`/`mode` とも必須。
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

## thread(ねじ)

- 呼ぶ面は `thread(profile, clearance?)`。刻む面は確定選択の全周の円筒面ちょうど1つ(`selection.require_selected(..., exactly_one=True)`)。verb は `duocad/verbs/thread.py`、幾何は `duocad/kernel_thread.py`(依存は kernel_thread → kernel の一方向)。設計と数値の根拠は [docs/THREAD.md](../THREAD.md)・[docs/THREAD_SIM.md](../THREAD_SIM.md) 6.10。
- **仕様(2026-10-23 本人判断)**: 山形は `semicircle` / `trapezoid30` / `trapezoid45` から選ぶ。隙間は入力でき、下限 0・上限(山の深さに比例するピッチの式)で断り、推奨の範囲の外は確定して警告する。既定は穴が推奨の真ん中、軸が 0。ピッチは径から自動で入力欄は無い(AI の判断)。
- **ログには解決後の隙間とピッチを残す**(`{face, profile, clearance, pitch, body_id}`)。再生は記録された値で刻み直すので、既定の表やピッチの式を直しても古い文書の形は変わらない。
- **警告は入口の返り値の `warning` で運ぶ**。`Session._after_logged_verb` が `_suppress_events == 0`(確定)のときだけ `Session.warnings` へ積む。プレビューは同じ入口を通るので、ここで抑えないと幽霊を描くたびにトーストが出る。
- 合否は `IsDone`・`BRepCheck` だけで決めない: ソリッドの数が変わらない・体積の増減が「単独で組んだねじ部の体積 − 埋めた・切った円柱」とねじ部の体積の 1% 以内で合うことまで見る(`BRepCheck` が真のまま芯が消える壊れ方。docs/THREAD.md「形の作り方」)。
- Web は `THREAD`(`commands/features.ts`、`SOLID_FEATURE_COMMANDS`、`#thread`、山形 `#thread-profile-*`、隙間 `#thread-clearance`)。隙間は `NumberArgSpec.optional` で、空欄のまま Enter できる(`build` は空欄なら `clearance` を送らない)。プレビューはサーバの投機実行。

## delete_faces・offset_faces(面の削除・面のオフセット)

- 呼ぶ面は `delete_faces()` と `offset_faces(distance)`。対象の面は確定選択の面から `selection.require_faces` で取る(1ボディ限定)。
  verb は `duocad/verbs/delete_faces.py`・`offset_faces.py`、幾何は `duocad/kernel_face_edit.py`(依存は kernel_face_edit → kernel の一方向)、
  計算は常駐の子プロセス(`duocad/kernel_worker.py`)。方式・実測・本人判断カードは docs/FACE_EDIT.md。
- **合否は OCCT の成功で決めない**: 結果を面ごとに元の面と照らし合わせる関門(1ソリッド・面積0の面なし・削除は残す面が自分の曲面のまま
  元の範囲を保つ・オフセットは旧面と新面が1対1で各面が元の曲面かずらした曲面に乗る)を通ったときだけ採る。断ったら ValueError で、
  文書・ログ・undo を変えない。断り文は原因(英語)と利用者向けの言い換え(日本語)。
- **期限**: 画面・MCP の操作は `kernel_worker.INTERACTIVE_TIMEOUT_S`(置いた値。カード e)、ログの再生は `interactive=False` で期限なし。
  期限切れ・子の異常終了は子を殺して ValueError、次の呼び出しで立て直す。
- **由来**: 削除は `deleted` = 選んだ面、`new_faces` は空。オフセットは旧面と新面が1対1。丸めが付いてくるときは旧丸めが `deleted`、
  丸め直した面が `new_faces`(旧丸めと新丸めを対応させる根拠を持たない)。
- 画面の口はまだ無い(カード a・b・d・e が本人判断待ち)。MCP とセッションのメソッドだけ。

## envelope_regions(スケッチの包絡)

- 呼ぶ面は MCP/セッション `envelope_regions(radius=0, offset=0)`(対象は確定選択の領域。WS は radius・offset 必須)。verb は `duocad/verbs/envelope_regions.py`、幾何は `kernel_sketch.envelope_faces`/`face_boundary_entities`。プレビューは投機実行でなく `preview_envelope_regions`(分裂しても失敗にせず全部の線と個数を返す)。
- **結果は線分・円弧・円・閉じた楕円・3次ベジエ。正確な図形に直せない辺(楕円のずらし曲線・楕円弧・有理 B-スプライン)は誤差 0.001 mm 以下の3次ベジエの列で近似する(`kernel_sketch.edge_entities`。応答の `approximated` が数)。面がちょうど 1 枚でなければ ValueError で何も変えない。** redo/replay はログの図形数値を使う。
- **依存禁止**: 領域の添字(後の編集で指す先が変わる)。負のオフセットを OCCT へ直接頼むこと。
- **罠**: 同心円の面の内向きオフセットは失敗する(箱 − 補集合の拡大で代替)。詳細は docs/CAD_LIBRARY_PITFALLS.md。

## project_to_sketch / project_outline(スケッチの投影)

- 呼ぶ面は MCP/セッション `project_to_sketch(sketch_id)`(対象は確定選択の辺・面)と `project_outline(sketch_id)`(対象は確定選択の面・辺・頂点・ボディが属するボディ)。verb は `duocad/verbs/project_to_sketch.py`、幾何は `kernel_project.edge_entities` / `face_edges` / `outline_entities`。プレビューは投機実行でなく `preview_project_to_sketch` / `preview_project_outline`(`sketch_ghost`。図形が無ければ `pieces=0`)。
- **写した図形は普通の図形で元の形に追従しない。** redo/replay はログの図形数値を使う(面・辺の添字に依存しない)。1回の呼び出しの中の重複(上面と下面の同じ長方形)は1つにするが、スケッチに既にある図形との重複は除かない。
- 点に潰れる辺は図形にしない。全部が潰れたら ValueError で何も変えない。メッシュのボディは断る。
- **写した図形の端点は 0.01 mm 以内なら1点に揃える**(`kernel_project.WELD_TOLERANCE`。近い組から順にまとめ、差し渡し 0.1 mm 以下の図形の両端は1点にしない。円弧の端が集まれば円の上の点 — 違う円の円弧が2つなら2円の交点 — へ揃え、円の上で揃えきれない隙間は短い線分でつなぐ)。STEP の辺の曲線は頂点から形状許容差の分ずれて終わり(本人の PC ケースで最大 0.0097 mm)、揃えないと写した外周が閉じず領域にならない(PC ケースの平らな面 1155 枚のうち、揃えないと 77 枚、揃えると 0 枚)。
- 外形は隠線処理(`HLRBRep_Algo`)の線で平面を割り、法線方向の直線がボディに当たる面の和の境界。`HLRBRep_HLRToShape` の線は射影の局所座標で返り、3D の曲線を持たない辺がある(`BRepLib.BuildCurves3d` で作ってから世界座標へ戻す)。
- Web は `PROJECT_TO_SKETCH`(`#sketch-project`、辺・面の選択)と `PROJECT_OUTLINE`(`#sketch-project-outline`、ボディの選択。面のクリックをボディへ畳む)。起動中はスケッチモードでもソリッドを指す(`App.svelte` の `solidPickInSketch`)。

## 押し出し(extrude)の kernel 分離・web 統合・extrude_face 廃止

- **押し出しの幾何は `duocad/kernel_extrude.py`**(`extrude_regions(faces, distance, mode, normal, target_shape=None)` と `extrude_faces`。mode は join/cut/new)。`verbs/extrude.py` が直接呼ぶ。`kernel.py` に再エクスポートは無く、`kernel.extrude_regions`/`kernel.extrude_face_on_shape` は存在しない(AttributeError)。`mcp_server.py` から `kernel_extrude.*` を直接呼ばない(`tests/test_mcp_verbs.py` が検査)。
- **依存は `kernel_extrude` → `kernel` の一方向。** `kernel.py` から `kernel_extrude` を import しない(循環)。`kernel_extrude` は kernel の private 補助(`_face_map`・`_face_at`・`_face_normal`・`_face_props`・`_checked_volume`・`_require_valid`・`_unify_same_domain`・`_fuse_all`・`_target_successors`・`_face_map_projection`)を import している。これらの名前・シグネチャを `kernel.py` 側で変えるなら `kernel_extrude` も直す。`face_of`/`_face_at` は `query.py`・`kernel_sketch.py`・テストが使うので `kernel` に残す。
- **OCP を import してよいカーネル層は `kernel.py` から切り出した `kernel_*.py` と `mesh_geometry.py` と `topopt/voxelize.py`**(`tests/test_boundaries.py` の `OCP_ALLOWED` が正。押し出しを切り出した当時は 6 本だった)。
- **UI から `extrude_face` を呼ぶ経路は無い。** ツールバーの「押し出す」は 1 つ(`#extrude`)。`features.ts` の `makeExtrude('region'|'face')` が同じ id `extrude` の 2 定義を作り、`EXTRUDE_REGIONS`(`SKETCH_FEATURE_COMMANDS`、選択引数名 `regions`)と `EXTRUDE_FACES`(通常モードの `SOLID_FEATURE_COMMANDS`、`faces`)として出す(どちらも min 1・max null)。距離 `#distance`、テーパー角 `#extrude-taper`、種別 `#extrude-mode-join/cut/new`。サーバへ送るのは両方 `{distance, mode}` と、0 でないときの `taper` だけ(対象はコアの確定選択)。旧 UI id `extrude_face`、`#extrude-regions`、`#regions-distance` は廃止。
- 同じ id `extrude` が 2 つの一覧に居ても、`CATEGORY_OF`(`App.svelte`)は id→category で両方 `feature` なので壊れない。`findCommand(id, list)` の `list`、`CommandHost` の `commands` プロップは必須(既定なし)。
- `App.svelte` の `verb === 'extrude' && !regionsStillExist(...)` は変更不要(面の選択なら領域参照が空集合で true)。通常モードの押し出しは面か1つのスケッチの領域を受け付ける(2026-10-18 本人決定。`EXTRUDE_FACES` の選択引数は `kinds: ['face', 'region']`)。面と領域の混在・別スケッチの領域は画面が先に断る(`makeExtrude` の `build`。コアの `require_extrude_targets` と同じ断り方)。
- **押し出しの公開 verb は `extrude` だけ**(領域は `regions`、面は `faces`)。`extrude_face` はカタログ・`Session`・MCP・WebSocket・`duocad.verbs.__all__` に無い。`verbs/__init__.py` は `extrude_face` モジュールを import して `register_verb` の登録だけを残す(再生専用)。`extrude_face` を新しい操作の記録に使わず、新しい verb の形の手本にしない。
- **古い保存ログの `extrude_face`(args: `body_id`/`face_index`/`distance`)は再生される。** 中身は `kernel_extrude.extrude_faces(body.shape, [face_index], distance, "join" if distance > 0 else "cut", target_shape=body.shape)`、undo は `undo_shape`。再生した `extrude_face` の `provenance` は保存時のまま(キーは `verb`/`face_index`/`after_face_index`)。
- **`tests/fixtures/extrude_face_log.json` は削除前の実装で作った保存ログ(体積 12240・6 面)で、`tests/test_extrude_face_replay.py` が再生して固定する。作り直さない**(「従来と同じ形」の根拠が失われる)。
- 再生専用 verb(`extrude_face`・`set_parameter`)はカタログに無いので、`tests/test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log` は登録済み verb からこの2つを除いて突き合わせる。
- 曲面を掃引すると壊れた形になる実測は docs/CAD_LIBRARY_PITFALLS.md の表が `kernel_extrude._check_coplanar_planes` を指す。

### テーパー(`taper`。issue #182)

- **挙動は Autodesk Fusion の押し出しのテーパーに揃える**(2026-10-18 本人決定。片側の角度1つだけで、両側・対称は持たない)。
  度で `|taper| <= 89`(2026-10-19 本人決定。`MakeDraft` が 89.8° 以上で別の形を成功として返すため)、既定 0。**正で押し出すにつれて太り、負で細る**(Fusion の API 文書 `ExtrudeFeatureInput.taperAngle` と同じ符号)。
- 距離 z の断面は「元の領域を z·tan(taper) だけ外へずらしたもの」で角は尖ったまま。**穴は外周と逆に動く**(正で狭まり、負で広がる。
  Fusion も同じ: Autodesk Community「Extrude Taper option odd behavior!」)。距離が負でも「進むにつれて」で、cut では掘る側の立体に掛かる
  (負なら穴が奥ほど狭まる)。領域・面の両方、join / cut / new のすべてで効く。
- 幾何は `kernel_extrude._tapered_prism`(輪郭ごとに傾けた立体を作り、外周の立体から穴の立体を cut)。輪郭の立体は、
  直線と円弧だけなら柱の側面を `BRepOffsetAPI_DraftAngle` で傾け(面は平面・円錐のまま)、楕円・ベジエ・B スプラインを含むなら
  `BRepOffsetAPI_MakeDraft` で側面を作って底とふたで閉じる(1本の辺で閉じた輪郭は先に4本に分ける)。`MakeDraft` は 0.1 mm の面取り辺を
  持つ多角形と、継ぎ目が曲率のきつい点にある楕円で失敗する(sol レビュー)。傾けた B スプラインの面の体積は既定の積分では合わないので、
  `kernel.volume` / `centroid` は面ごとに積分を選ぶ(docs/CAD_LIBRARY_PITFALLS.md)。
  `LocOpe_DPrism` / `BRepOffsetAPI_DraftAngle` を使わない理由と、潰れを OCCT が見逃す実測は docs/CAD_LIBRARY_PITFALLS.md「テーパー付き押し出しの2経路」。
- **潰れ(壁どうしの衝突・穴が閉じる・辺が消える・円錐が頂点を越える)は ValueError**(文言に `collapses`)。検出は4段:
  曲線の辺の曲率半径とずらす量(`_require_curvature_room`。円の頂点越えは `BRepCheck` も自己交差の検査も通すのでこれが唯一の関門。
  最小の曲率半径は `Geom2dLProp_CurAndInf2d` の曲率の極値・等間隔の標本・標本の極大のまわりの黄金分割で求め、中心が内か外かは
  面の上での辺の向きで決める。標本だけだと継ぎ目をずらした楕円を、黄金分割だけだと区間に極大が複数ある鋭いベジエの山を見逃し、
  点の分類だと半円の円弧の端で太る向きを細る向きと取り違える。どれも sol レビューで再現)、
  傾けた殻の `BRepCheck`、ふたが面1枚で穴の数が底と同じ・立体が1つ、`BRepAlgoAPI_Check` の自己交差。どれが何を捕まえるかは
  `tests/test_extrude_taper.py::test_a_collapsing_section_is_refused` の7例。
- **制御点がすべて弦の上のベジエ・B スプライン**(形は線分。SVG の `C0 0 20 0 20 0` など)は線分に置き換えてから傾ける。
- **端の制御点が重なった曲がったベジエ・B スプライン**は、重なった制御点を次の別の制御点の方へ 1e-7 mm
  ずらしてから傾ける(`_regular_wire`。そのままだと `MakeDraft` が Standard_ConstructionError)。曲がった曲線で端の極が重なると
  その端で曲率半径が0へ向かうので、**細る向きは潰れとして断る**(角で切り落とされて実害が無い場合も断る。慎重側)。
  OCCT の例外は `_tapered_prism` で ValueError に変える。
- 傾けた殻・底・ふたは許容差 1e-6 → 1e-5 → 1e-4 mm の順に縫い、自由辺が残らず正しい立体になった最初の値を使う(`_SEW_TOLS`)。
  楕円を短く押すと 1e-6 では閉じない。
- **テーパー付きの押し出しは `|distance| >= kernel_extrude.MIN_TAPER_DISTANCE`(1e-3 mm)。** `MakeDraft` は 7e-5 mm 以下で
  プロセスごと落ちる(docs/CAD_LIBRARY_PITFALLS.md)。taper 0 の下限は今までどおり `MIN_DISTANCE`。
- **taper 0 は今までの `MakePrism` の経路を通る**(形も面の数も変わらない)。ログの args には 0 でないときだけ `taper` を記録し、
  再生は `args.get("taper", 0.0)`(テーパーの無い古いログはそのまま再生される。形式の版は上げていない)。
  画面(`#extrude-taper`、既定 0)も 0 のときは送らない。
- 実機検査は `web/scripts/smoke-taper.mjs`(欄の既定 0 と、打った角度で角錐台の体積になること)。
- 面の押し出しの `after_face_indices` は、テーパーではふたの重心が底の重心の平行移動にならない(L字など)ので、作った柱のふたの重心で探す。

### 踏んだ罠

- **面の押し出しでは溝の底のような窪みの面は視点から見えず選べない。** 2 枚の同一平面の面を押す smoke の題材は溝を浅く広く(幅 24・深さ 3)した(平面の違う面を混ぜるとトーストに「同一平面上にない」)。ログの `args.faces` は配信で件数の文字列に畳まれる。

## 回転体(revolve)

- **幾何は `kernel_extrude.revolve_regions`**(OCP を触るカーネル層を7本目に増やさないため同居)。領域の立体を束ねて相手へ join / cut する後半は `extrude_regions` と共有(`_combine_region_solids`。統合・由来の射影も押し出しと同じ規則)。verb 側は `verbs/extrude.py` の `_resolve_faces_and_normal` / `_resolve_target` / `_editable_target` を `what="revolve"` で使う。
- **軸は `"x"` / `"y"`(スケッチの原点を通る軸)か、領域と同じスケッチの線分 `{"sketch", "entity"}`。** ソリッドの辺・基準軸はまだ受けない。**公開面(catalog・MCP・WS)では `axis`(x / y)と `axis_line`(線分)の2引数に分けて排他で受け**、入口(`_enter_revolve`)が verb の1つの `axis` へ畳む。1引数(文字列か参照の和)にすると画面の欄(軸の3択・線分のピック)と作業空間の引数が名前で往復できず、再読み込みや AI の `set_command_args` で指した線分が消えた(sol レビューで発覚。`mirror_body` の `base_plane` / `plane_face` と同じ形にした)。ログには**参照のまま**載せ、再生で引き直す(線分の寸法を後から変えると追従する)。鏡映が数値へ解決して載せるのは、軸自身が複製の対象に混ざりうるからで、回転体にはその事情が無い。
- **角度は度で `0 < |angle| <= 360`、負は逆向き**(押し出しの距離の符号と同じ扱い)。正は軸の向き(線分の始点→終点)まわりの右ねじ。`symmetric` は両側へ半分ずつ。360° は角度なしの `BRepPrimAPI_MakeRevol` で作る。
- **軸が領域を横切るのは ValueError**(`_require_one_side_of_axis`)。軸の両側に半平面を置いて共通部分の面積で決める(辺が軸上にあるのは面積0なので通る)。境界を折れ線で拾う判定は、軸をわずかにまたぐ円弧を取りこぼす。
- **360° の回転体のシーム(継ぎ目の辺)をフィレットに渡すと、OCCT が「適した辺が無い」で断り、verb は例外で何も変えない**(`tests/test_revolve.py` が固定)。ピックからは外していない。
- **画面(`REVOLVE`、スケッチモード)は 軸の3択(x軸 / y軸 / 線分を選ぶ)+ 線分のピック(`axis_line`。共有選択に入れない)+ 角度(既定 360)+ 種別。** 「線分を選ぶ」は `sends: null` でサーバへ行かず、`build` が `axis_line` を送る(`MIRROR_BODY` の「面を選ぶ」と同じ形)。**`sends: null` の選択肢は `impliedBy` に配信から自分を戻す引数名を持つ**(「線分を選ぶ」は `axis_line`、「面を選ぶ」は `plane_face`。`CommandHost.syncFromShared` が使い、`tests/test_ui_command_wiring.py` が全選択肢に課す)。無いと再読み込みで選択肢が戻らず、ピックの欄が隠れたまま確定できない。**両側(`symmetric`)は画面から送らない**(選択肢は既定値を持てず、毎回1クリック増えるため)。プレビューは押し出しと同じサーバの投機実行。実機検査は `web/scripts/smoke-revolve.mjs`。

## ロフト(loft)

- **幾何は `kernel_extrude.loft_regions`**(回転体と同じく、OCP を触るカーネル層を7本目に増やさないため同居)。断面ごとに `verbs/extrude.py` の `_resolve_faces_and_normal` で面と法線を引き、`BRepOffsetAPI_ThruSections(isSolid=True, ruled=False)` でつなぎ、後半は `_combine_region_solids`(押し出し・回転体と共有)。
- **向き・始点・辺の数は OCCT の `CheckCompatibility` に任せず自前で揃え、`CheckCompatibility(False)` で渡す。** 任せると、2断面の平面が 90° のとき(XY → YZ、箱の上面 → 側面)平面の軸の取り方しだいで潰れ、楕円の継ぎ目・35° 未満の円の継ぎ目のずれを揃えず、凹んだ形 → 円は生成自体が失敗した(実測は docs/CAD_LIBRARY_PITFALLS.md「ロフト」)。揃え方: 法線を進む向きへ → 線をその法線について反時計回りへ → 前の断面の始点の向きを最小回転で運んで最寄りの点を始点に(辺の数が全断面で同じなら頂点から、違えばその向きのちょうどの点で辺を割る) → 全断面を、どれかの頂点がある極角すべてで割って辺の数を揃える(極角が単調でない凹みがあれば長さの割合で割る)。
- **`shifts`(断面ごとの整数)は自動で揃えた始点を頂点 k 個進める逃げ道**(頂点の無い円・楕円は 90° 単位。断面の数と同じ長さ・省略は全部0)。本人判断(2026-10-17)で、自動合わせだけでなく人が対応をずらせる形にした。
- **壊れた結果は例外**: 体積が断面の最大面積^1.5 の 1e-6 倍以下(潰れ・裏返り)、`BRepCheck`、**`BRepAlgoAPI_Check(solid, True, True)`(第3引数 `bTestSI` が自己干渉。False で呼ぶと潰れ・蝶ネクタイ形・180° のねじれを全部通す。sol レビューで発覚)**。断面3つ以上のなめらかな補間が断面の間で食い込む形もこれで捕まる。はみ出し(自己交差しない膨らみ)は通す。
- **重心から見て裏に回り込む凹み(U字)のある断面は、極角で割れないので候補を2つ試す**: 長さの割合で割る → だめなら OCCT の揃え(`CheckCompatibility(True)`。ここでは `shifts` が効かないことがある)。両方だめなら「凹み」を名指す例外(`shifts` でずらすか、断面を分ける)。U字 → 円は始点をずらすと通り、U字 → 正方形は通らない(`tests/test_loft.py`)。
- **断面は選択の領域を選んだ順**(`selection.require_selected` は追加順を保つ)。1断面1領域、隣り合う断面は別の平面。**穴のある領域は拒む**(外周を join、内側の領域を cut で2回ロフトする。本人判断 2026-10-17)。断面の平面が同じ・点が隣の断面の平面上は ValueError。
- **点への収束は `start_point` / `end_point`(スケッチの図形の点 `{"sketch","entity","point"}`)。** ログには参照のまま載せ、再生で引き直す。断面1つ+点1つでもよい。**画面では回転体の軸と同じく、共有選択に入れない直接ピック**(`PickArgSpec`、target `sketchPoint`、任意。「指す」を押している間だけスケッチの点も当たる)。指せるのは表示中のスケッチの図形の点すべて(点の図形・線分の端・円や円弧の中心・長方形の基準の角)で、画面側では絞らない(断面と同じ平面の点はコアが断る。規則を2か所に持たない)。指した点に印は出さず、プレビューの尖った形と欄の表示で見せる(本人判断 2026-10-19、5件とも AI の推奨どおり)。
- **通常モードのクリックは、コマンドがスケッチの点・図形を直接待っていればそれを欄へ渡す**(`App.svelte` の `onPick`。ロフト専用にしない: 通常モードのコマンドに `sketchPoint` / `sketchEntity` の pick を書いても満たせない空白を閉じる。読み方はスケッチモードの `onSketchPick` と同じで、点は当たった点、図形は点を落として図形1つ)。
- **画面(`LOFT`)は通常モード(`SOLID_FEATURE_COMMANDS`)に置く。** 起動中は全スケッチの領域だけを指せ、収束点の「指す」を押している間だけスケッチの点も当たる(`pickKinds`。スケッチ編集中は開いているスケッチの領域しか当たらないので、そちらには置かない)。選択は断面1つ以上(`build` が「断面2つか、断面1つと収束点」を見る)・種別・収束点2つ(`#loft-start-point` / `#loft-end-point`)・始点のずらし(文字欄 `#loft-shifts`、カンマ区切り、空欄で省略。`TextArgSpec.optional`。断面と対の値なので**次の起動へ持ち越さない** `remember: false` —— 持ち越すと断面の数と合わず確定できない)。**作業空間へは整数の配列で出す**(`TextArgSpec.toShared`。打った文字列のまま出すと、サーバの型(array)違いで `set_command_args` が拒む。sol レビューで発覚)。実機検査は `web/scripts/smoke-loft.mjs`。
- 2断面の間は直線的につながる(`ruled=True` と同体積)。傾いた平面でも曲がった継手にはならない —— 曲げたいなら間に断面を足す。端の接線・案内線(レール)は持たない(`ThruSections` に無い機能)。

