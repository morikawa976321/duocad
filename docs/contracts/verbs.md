# 移動・鏡映・ブーリアン・押し出し系 verb の契約

pattern/move/mirror/boolean/envelope/extrude 系の verb と、そのプレビュー(幽霊)・引数収集 UI の文書。
いまの挙動の正はコードとテスト。ここは、ソースから読めない不変条件・依存禁止・罠を、元の作業タスクの契約から分野別に蒸留したもの。
項目末尾の括弧は出典(タスクID, 日付)。呼ぶ面のうちソースを見れば分かるもの(引数名・シグネチャ)は省いてある。
ファイルの場所はその後の整理で動いたものがあり、動いた先が分かるものは併記した。

## ローカル幽霊(確定前のプレビュー)の層

- **入口は `App.svelte` の `showLocalGhost(verb, args)` 1つで、3経路に振る。** (a) `ghostEntity`(これから打つ `add_*` の図形)、(b) `ghostCopies(verb, args, sources)`(`web/src/entityKinds.ts`。既存スケッチ図形の反復・移動・回転後の姿)、(c) `viewport.showBodyGhosts(entries)`(既存ボディの変換後の姿。実体は `web/src/ghostLayer.ts`、型 `BodyGhost`/`BodyGhostRotation`/`bodyGhostMatrix` は `web/src/viewportTypes.ts`)。新しい verb にプレビューを足すときは `CommandDef.preview: 'local'` にしてこの3つのどれかへ乗せる。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)
- **ローカル方式はサーバへ何も送らない。** 「Esc で中断しても文書が一切変わらない」を巻き戻しでなく「そもそも送らない」で成立させている。投機実行へ寄せない。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)
- **TS の変換規則はコアの写しで、片方だけ変えてはならない**(幽霊と確定結果が別の場所に出る)。`entityKinds.ts` の `translate`/`rotate` ← `duocad/sketch.py` の `_translate_*`/`_rotate_*`、円形反復の刻み角 ← `duocad/region.py` の `expanded_entities`(旧名 `_expanded_entities`)、点だけ動かすか図形全体か ← `duocad/verbs/move_sketch.py` の `_normalize_targets`/`_moved_params`、`rotatedTargets` ← 同 `_rotation_args`/`_rotated_params`/`_apply`/`_copy`。一致は `tests/test_ghost_agreement.py` と `tests/eval_ghosts.mjs` が突き合わせる。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05; 20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- **`showBodyGhosts` は本物を隠さない**(`showPreview` は隠す)。移動前と移動後が並んで見えることが `move_body` プレビューの中身。幽霊は 1 ボディ = 1 `THREE.Group` で、`group.matrixAutoUpdate = false` にして行列を直に置く(`group.position` を触っても効かない)。行列は `T(offset)·T(p)·R·T(-p)`。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05; 20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- **幽霊が消える場面は `viewport.clearPreview()` に集約**(`update`/`prune`/`showPreview`/`App.clearPreview`/`setMode` がすべてここを通る)。新しい消し口を別に作らない。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)
- **幽霊は「解決済みの数値」でしか描かない。** 中心・軸を省略したときのコアの既定(スケッチ=対象の代表点の平均 / ソリッド=体積重心)はブラウザ側へ写さない。規則が2箇所に増えると幽霊と確定結果が別の場所に出るし、体積重心は三角形分割からの近似にしかならない。描かないほうが正しい。UI で軸・中心を指させるときも、画面側が数値へ解決してから渡す(辺の参照をそのまま `showLocalGhost` へ渡さない。実装は `App.svelte` の `resolvePicksForGhost`)。 (20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- **長方形をまるごと回す指定が1つでも混ざったら、幽霊を1つも描かない。** コアは呼び出し全体を ValueError にする。他の図形だけ回った姿を出すと「プレビューは回る・確定するとエラー」になる。`pattern_circular` は逆に4本の線分へ落とすのが正しい(複製を展開するだけなので種別が変わってよい)。幽霊も同じ落とし方をしないと「プレビューは長方形・確定すると線分」になる。番人は `tests/test_ghost_agreement.py::test_a_rotated_rectangle_ghost_is_still_the_same_rectangle`。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05; 20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- スケッチ回転の幽霊は `ghostCopies` の `args.mode === 'rotate'` 分岐(要る引数は `angle`(度)と `center: [u, v]` の数値、任意で `copy`)。`rotateCopies` は **`true`** を渡す(対象そのものを回す。`false` は反復の複製用で向きを変えず位置だけ回す)。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05; 20260906-0100-duocad-preview-follows-rotation, 2026-09-06)

### 依存してはいけないもの

- **`GHOST_COPY_LIMIT`(2000)を仕様と読まない。** 画面が固まらないための表示上限で、コア側には無い。超えても Enter は通る(幽霊が出ないだけ)。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)
- **`ghostInfo()`(とその `bodies[].matrix`)・`window.__duocad.showLocalGhost` は実機検査用の穴**であって画面状態の正ではない。製品の分岐から呼ばない(定義は `web/src/testHooks.svelte.ts`)。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05; 20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- **`previewing`(`App.svelte` の `$state`)はサーバ投機実行側だけを表す。** ローカル幽霊が出ていても `false`。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)
- `showSketchGhosts` は描けた図形だけ数える(折れ線化できない図形は黙って落ちる)。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)

### 踏んだ罠

- **`move_sketch_entities` は選んだ点だけを動かす**(図形全体ではない)。参照の `point` が `null` なら図形全体、文字列ならその1点。同じ図形に辺と点が両方選ばれたら辺(全体)が勝つ。`copy=true` は常に図形まるごと1つを複製。図形IDに畳んだ `selectedEntities` を幽霊の対象にすると、点を1つ選んだだけで図形ごと動く幽霊になる(`GhostSource.points` を使う)。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)
- **モード切替では `onCancelPreview` が来ない**(`{#key mode.kind}` が `CommandHost` ごと作り直す)。`setMode` の先頭で `clearPreview()` を呼んで塞いだ。**`mode = next` より前**でなければならない(`clearPreview` が旧 `sketchId` を読む)。`extrude` のサーバ投機実行にも元からあった穴(本物を隠したまま幽霊が残る)を同時に塞いでいる。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05)
- **`web/dist` を作り直さないと実機 smoke は古いバンドルを見る**(`duocad/net/http.py` が配る)。`npm run check` が通っても smoke が「幽霊が出ない」で落ちる。画面側を直したら smoke の前に `npm run build`。 (20260905-1814-duocad-add-preview-to-pattern-and-move-verbs, 2026-09-05; 20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- **原点を通る軸で回転行列を検算しても意味が薄い。** `T(p)·R·T(-p)` の平行移動2項が恒等になり、軸の位置を落とした実装でも通る。非原点の軸、Y軸・負角度・非単位ベクトルのケースで検算する。 (20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- **`onBoxSelect` はスケッチの領域も一緒に拾う。** 長方形をウィンドウ選択すると選択に領域参照が混ざる。「図形まるごと1つだけ選べた」の判定は `item.entity !== undefined` で絞る。 (20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- **選択の正はコア**。クリック/ドラッグ直後に `page.evaluate` で選択を読むと往復前の値を見る(必ず `waitFor`)。偽の失敗を出した実例あり。 (20260906-0100-duocad-preview-follows-rotation, 2026-09-06)

## move_body / move_sketch_entities の回転モード

- **両 verb とも `mode`(`translate`/`rotate`。宣言の正は `duocad/verbs/_common.py` の `MOVE_MODES`)で分岐する。モードが取らない引数を渡すと必ず ValueError**(黙って無視も、黙って両方適用もしない)。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **`axis_edge`/`center_vertex`/`center_entity` は `commit_selection` を介さない参照**で受ける。確定選択は「動かす対象」の解決に使われるので、軸を同じチャンネルへ載せると対象と軸の区別が付かず、軸の辺のボディまで回る(`pattern_circular` の `entities` と同じ方式)。形は `{"body","index"}`(`kind` は任意だが付けるなら `edge`/`vertex` と一致)、`center_entity` は `{"sketch","entity","point"}`(`point` 省略=その種別の最初の点。並びは `sketch.entity_point_keys` が正)。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **軸の位置の解決順は `center_vertex` → `axis_point` → 辺の位置 → 対象の合成重心。** `axis_point` と `center_vertex`、`axis_edge` と `axis_point`/`axis_dir` は同時に渡せない。`axis_edge` + `center_vertex` は許す(辺が向き・頂点が位置)。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **`angle` は度。** スケッチは u→v 反時計が正(`pattern_circular` の `total_angle` と同じ)、ソリッドは軸に対し右ねじが正。**360の倍数は ValueError**(0度と同じ理由。UI 側は `commands/moveMode.ts` の `rejectAngle`)。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **操作ログに載るのは解決済みの数値だけ。** undo は「逆向きに同じ角度で回す」で戻すが、そのとき軸に使った辺は既に回った後にいる(軸が対象ボディに属していれば必ず)。参照を引き直すと別の軸で戻る。`_rotation_args` が入口で数値へ潰し、`_do_*`/`_undo_*` は数値しか見ない。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **`mode` キーの無いログは並進として読む**(`args.get("mode", MODE_TRANSLATE)`)。2026-09-05 より前の保存文書には `mode` が無い。この既定を消すと過去の保存文書が開けなくなる。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **添字は回転でも変わらない**(`copy=False` は `TopoDS_Shape.Moved` で位置だけ差し替え、下層の形状を共有)。`resets_selection` は「他の形状変更 verb と同じ既定」であって回転が壊すからではない。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **長方形をまるごと回すのは ValueError**(仕様)。回すと種別が線分4本に変わり、「undo = 逆向きに同じ角度」も元の図形 ID と `dN` の名前つき寸法の復元も成り立たない。`pattern_circular` が落とせるのは複製をエンティティにせず展開時に作るから。長方形の1点(左下)を回すのは従来どおりできる。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **`Session` の戻り値(= `Applied`)はモードによらずキーが同じで、使わない側が `null`。** `move_body`: `mode/dx/dy/dz/angle/axis_point/axis_dir/copy/created/changed_bodies`、`move_sketch_entities`: `mode/dx/dy/angle/center/copy/created/sketch_ids`。戻り値の顔ぶれがモードで変わる設計にはできない(verb ごとに走らせ方が1本しか無い)。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **回転の引数の WS 露出は `ws="special"`**(省略できる引数があるので `ws="table"` は不可 —— 表は載せた引数を全部必須にする規律で、並進の UI が引数不足で送れなくなる)。現状の画面は `mode` の選択(`commands/moveMode.ts`)を持ち、回転軸を3Dで辺クリック、中心を頂点クリックで指す(`PickArgSpec`、共有選択集合には入れない)。詳細は `duocad/catalog.py` の `move_body`/`move_sketch_entities` の note。なお当初の「回転の引数は全部 `ws=False`」「UI から `mode` は飛ばない」「軸・中心の指し方は本人判断待ち」は後続の作業で解消済みなので落とした。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05; 20260906-0100-duocad-preview-follows-rotation, 2026-09-06)
- kernel に足した面: `kernel.rotate_shape`/`kernel.edge_axis(shape, edge_index)`/`kernel.centroid`(体積重心)、`sketch.rotate_point`。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)

### 依存してはいけないもの

- **`center`/`center_vertex` が3Dの結果を変えると思わない。** 3Dの回転は軸線上のどの点を基準にしても同じ変換。中心が結果を変えるのは軸の位置を持たないとき(`axis_dir` だけ → 重心を通す)と、`center_vertex` で軸を寄せるときだけ。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **`Applied` の `axis_point` を「ユーザーが渡した値」と読まない。** 解決後の数値で、辺で指したときはその辺から取った点が入る。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)
- **`describe_shape`(MCP)は辺・頂点の ID を返さない**(`query.describe_edges`/`describe_vertices` はあるが MCP ツールにしていない)。AI が軸の辺の添字を知る経路は `propose_selection` + `get_selection`(参照に `index` が入る)だけ。`describe_edges` の MCP 公開は未着手の別仕事。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)

### 踏んだ罠 / 判断

- **既知の穴を1つ残してある(判断)**: `move_body` の `copy=True` で、対象がその軸まわりの回転対称を持つ角度(立方体を自分の中心軸で90度など)を指すと元と完全に重なる複製ができる。スケッチ側は `params` 一致で弾けるが、ソリッドには同じだけ安くて健全な判定が無い。境界箱の一致は L 字を180度回した「重なっていない」場合を誤って断り、最小距離(`BRepExtrema_DistShapeShape`)は角を共有する正当な複製を誤って断り、ブーリアン共通部分は重い。**不健全な判定を入れるより通す**を選んだ(注意書きは `duocad/verbs/move_body.py` の「既知の穴」)。 (20260905-1900-duocad-add-rotation-to-move-verbs, 2026-09-05)

## 数値引数の既定値(`NumberArgSpec.default`)と空欄軸

- **`NumberArgSpec.default?: number`**(`web/src/commands/command.ts`)。`CommandHost.svelte` の `values` 導出(`raw === '' ? (spec.default ?? NaN) : …`)が唯一の消費者で、`default` を持つ数値引数は入力欄が空欄でもその値が `collected[spec.name]` に入り `argSatisfied`/`firstMissingArg` を素通りする。移動量(`dx/dy/dz`、`dx/dy`)に `default: 0`(現在は `commands/moveMode.ts` が持つ)、`envelope_regions` の R・d(`commands/sketch.ts`)にも付いている。 (20260905-2256-duocad-move-verbs-blank-axis-defaults-to-zero, 2026-09-05)
- **opt-in の不変条件**: `default` は個別の verb が明示的に付けたときだけ効く。付けなければ従来どおり空欄は NaN で `argSatisfied` が false(「まだ決めていない」)。**半径・距離・個数のように 0 が無効値・未確定を意味する引数に安易に `default: 0` を付けない**(黙って 0 が埋まって壊れる)。 (20260905-2256-duocad-move-verbs-blank-axis-defaults-to-zero, 2026-09-05)
- **全軸空欄の拒否は `default` の責務ではなく、各 verb の `build()` が個別に持つ**(移動量が全部 0 →「移動量が全部0」等、さらにコア側の translate モードにも同じ ValueError)。新しい verb に `default` を付けるなら、全部既定値落ちを拒否するチェックをその verb 自身に書く(汎用化されていない)。 (20260905-2256-duocad-move-verbs-blank-axis-defaults-to-zero, 2026-09-05)
- `default` の型は `number` のみ(文字列・関数の既定値は未対応)。 (20260905-2256-duocad-move-verbs-blank-axis-defaults-to-zero, 2026-09-05)
- **ローカルプレビューに追加のフックは要らない。** `ghostBuilt`/`built` は共通の `values`(`default` 込み)から `command.build()` を呼ぶだけなので、`default` を付ければ空欄軸=0 にプレビューも自動で追従する。プレビュー側に個別に既定値を配線する発想は誤り。 (20260905-2256-duocad-move-verbs-blank-axis-defaults-to-zero, 2026-09-05)
- **実機検査の罠**: (1) `copy` の選択(`#move-body-move`/`#sketch-move-move` 等)を押すまで dx/dy/dz の入力欄が active/`ready` にならない(押し忘れると prompt が「複製を選ぶ」のまま止まる)。 (2) `page.evaluate` で `getBoundingClientRect()` を単体で返すと DOMRect のプロパティが own enumerable でなく消え、座標が NaN になって `page.mouse.click` が `Invalid parameters` で落ちる。rect の取得と `project*` の呼び出しを同じ `evaluate` 内で行い、`{x: pos.x + rect.left, …}` まで計算して返す。 (20260905-2256-duocad-move-verbs-blank-axis-defaults-to-zero, 2026-09-05)

## 鏡映(mirror_sketch_entities / mirror_body)と参照ピック UI

- **どちらも常に複製を作る**(in-place 反転は無く `copy` 引数も持たない)。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)
- **軸・鏡映面は数値へ解決してからログに載せる**(ログ: `axis_p0`/`axis_dir`、`plane_point`/`plane_normal`)。対象にその図形/面自身が混ざる場合、複製後に参照を引き直すと別物を指すため(`move_body.axis_edge` と同じ理由)。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)
- **軸に使った線分自身が対象の選択に混ざっていたら、対象から黙って外す**(`mirror_sketch.py`)。直線は軸まわりに自分自身へ映り「元と完全に重なる複製」にしかならないため。エラーにはせず、対象が軸だけになったら ValueError。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)
- **長方形は軸が水平・垂直のときだけ種別を保てる。** 斜めの軸では `sketch._mirror_rectangle` が4本の線分へ分解した結果を返し、`mirror_sketch_entities` 側が検査して ValueError にする(回転と同じ二段構え)。`_mirror_rectangle` の分解を「常に成功する経路」と思わない(ValueError へ変換するための中間表現で、戻り値に線分が化けて出ることはない)。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)
- **円弧の鏡映は始点・終点を入れ替えてから角度を映す**(`new_start = 2*axis_angle - end`、`new_end = 2*axis_angle - start`)。鏡映は向きを反転するので start/end を別々に映すと掃引方向が壊れる。掃引量(`end-start`)は保たれる。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)
- `EntityKindOps.mirror`(5種別とも実装。signature `mirror(params, ax, ay, ux, uy) -> List[dict]`、rotate と同じ「種別が変わりうる」形)、`kernel.mirror_shape(shape, point, normal, *, copy=True)`(`gp_Trsf.SetMirror`)。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)
- **鏡映基準の指し方は、新しい入力モードを作らず既存の `PickArgSpec`(対象とは別チャンネルで図形を1つ直接ピック)に種別を足して実現した。** `PickTarget = 'edge'|'vertex'|'face'|'sketchPoint'|'sketchEntity'`。`face` は `PickEvent.hit`、`sketchEntity` は `PickEvent.sketchEntity`(点は落とす)から拾う。種別を足すときは `PickEvent` が既に持つ種別から選ぶ(新しい当たり判定を作らないことがこの枠組みの安さ)。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **`MIRROR_SKETCH_ENTITIES`**(`commands/sketch.ts`): 対象は選択、`axis` は `sketchEntity` ピック(必須・自動で構える)。`build` は `{axis}` だけを送り、`runsWithoutSketchId` は true(`sketch_id` を足さない)。`catalog.py` は `ws="table"`(軸は必須の引数1つなので表の規約に合う。`special` は要らない所で使うと `_dispatch` に手書き分岐が増える)。当初の `ws="none"`(MCP専用)・「`plane_face` は MCP 専用」は撤回済み。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **`MIRROR_BODY`**(`commands/features.ts`): `base_plane` の choice は XY/XZ/YZ + 4つめ `'face'`(「面を選ぶ」)。`'face'` はサーバへ行かず、`build` が `plane_face` へ読み替える印(コアの `base_plane` は XY/XZ/YZ のみ)。面ピックは `when: base_plane==='face'` で開く —— 外すと起動した瞬間に面ピックが開いて対象を選び直せなくなる。`mirror_body` は `ws="special"` で、`plane_face` も WS に開いている(`_dispatch` が `plane_face=message.get("plane_face")` を渡す)。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06; 20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **ピックの分岐は `onPick`/`onSketchPick` で `editSelection` より前に置く**(`tests/test_ui_command_wiring.py` が静的に順序を固定)。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **修飾キー(Shift/Ctrl)付きクリックはピックへ行かない**(`event.mode === 'replace'` だけがピックに食われる)。必須ピックは対象が1つ揃った瞬間に自動で構えるので、素のクリックを全部食う設計だと2つ目の対象が永久に選べない。`move_body.axis_edge`/`center_vertex` にも同じ分岐が効いている。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **面を1つ指させている間は辺を出さない**(`App.svelte` の `wantsEdges` に `wantedPick !== 'face'`)。辺は面より先に拾われ、出したままだと鏡映面を狙った素のクリックが辺に取られて対象の選択を置き換える。面ピックを持つ verb を新設するなら同じ配慮が要る。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **軸は図形そのもの**: 端点付近を押しても `point` を落として `{sketch, entity, point: null}` に畳む。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **`pickedLabel(value, target)` は `target` を渡す。** 同じ `{sketch, entity, point:null}` でも `sketchPoint` は「図形の最初の点」、`sketchEntity` は「図形1つ」。TS 上は省略できるが第1引数だけで呼ぶと表示が誤る。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **幽霊は無い**(`preview: 'none'`/`previewable=False` を両方維持。move 系の `resolvePicksForGhost` 経路には繋がっていない)。指した瞬間でなく Enter で確定する(対象が何個でも選べる = `needsEnter` が真)。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06; 20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- 却下した案: 専用の入力モード(`PickArgSpec` の `pendingPick`/`armedPick`/`wantsPick` が同じ管理を既に持つので二重になる)、軸ピックを `optional` にして自動で構えない案(軸が要ることが初見で伝わらない)、スケッチ側に `base_plane` 相当の3択(平面2Dに基準"軸"の概念が無い)。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **`face_fingerprint`(面積+法線照合)は鏡映で壊れない**。鏡映複製の新ボディは元と別 ID を持つので指紋が独立しているから。Body に元ボディ由来の参照を足すなら、この独立性の前提から見直す。鏡映複製ソリッドの面法線は反転する(上面 `[0,0,1]` → `[0,0,-1]`)が `face_fingerprint`/`select_face` は壊れない(MCP 経由で実機確認)。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)

### 踏んだ罠

- **`ws="none"`(MCP 専用)の verb は画面の `Applied` 型に載らない**(`add_mesh_body` 等。ログには載る)。`Applied` は `web/src/generated/protocol.ts` の生成物で、同期の検査は `tests/test_protocol.py`。`mirror_sketch_entities` は `ws="table"` にして生成の対象へ入れてある。MCP 専用 verb を足すときの扱いは docs/DEV_NOTES.md の「`ws="none"`(MCP 専用)の verb は画面の `Applied` に載らない」項。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06; 20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **`#sketch-look` が合わせる視野は「そのとき見えているもの」に依る。** 板だけがあるスケッチへ入り直して押すと原点付近の (0,-20) が画面外になり `projectSketchPoint` が null を返した。スケッチの図形は入った直後に全部描く順序にする。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **円の半径の params キーは `radius`**(`r` ではない)、中心は `cx`/`cy`。円も領域を作る。押し出す領域は面積最大(`regions` の `index/area/centroid`)で選ぶ。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)
- **`pytest.approx(0.0)` は絶対許容差がほぼ 0。** `kernel.bounding_box` を鏡映(x=0)で検算すると OCCT の境界箱が許容差ぶん膨らんで 1e-7 程度の誤差が乗るので、0 との比較には `abs=1e-4` を明示。 (20260906-1100-duocad-add-mirror-verb, 2026-09-06)
- **`smoke-mirror.mjs` の円を描く手順が 11 回中 1 回だけ落ちた**(原因未確定、再現せず)。描く 4 verb は点を 1 つずつ食うので、クリックが早すぎる/取りこぼされると図形が出ないのに理由も出ない。1 手ごとに `#command-prompt` の待ち受け(`awaitPrompt`)を確かめる形にし、失敗時にスケッチの図形・選択・トーストを出す。 (20260906-1500-duocad-mirror-reference-picking-ui, 2026-09-06)

## boolean_body

- **ツールボディは引数で渡さない。** 呼ぶ前に `commit_selection` で確定選択(面/辺/頂点、`min=1`)へ入れ、内部で `selection.require_bodies(document)` が逆引きする。`target_ref` は `{"body","kind":"face","index"}`(面1枚。確定選択には入れない)、`mode` は `union|cut|intersect`。verb 層の直接呼び出しは `verbs.boolean_body(doc, target_ref, tool_body_ids, mode)`。WS は表(`ws=table`)で `target_ref`/`mode` とも必須。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- **ターゲットと同じ BodyId がツールに混ざっていたら ValueError**(黙って畳まない)。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- **交差は `target ∩ (tool1 ∪ tool2 ∪ …)`**(`target ∩ tool1 ∩ tool2…` ではない)。ツールは常に先に `BRepAlgoAPI_Fuse` で1つへ融合してからターゲットと演算する。`kernel.boolean_bodies` を素通しで再利用すれば自動的にこの意味になる。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- **1回の呼び出し・1回の undo/redo で「ターゲットの形状」と「消えたツール Body 群」の両方が戻る。** `_common.SNAPSHOTS` に `(target_body_id, seq)` と `("boolean_body:removed", seq)` の2鍵で退避。この2鍵構成を崩すと片方だけ戻る。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- **ソリッド限定チェックは `kernel.is_solid()`(SOLID 0 個なら False)を通す。** `selection.py` と verb 層は `OCP` を直接 import しない(`tests/test_boundaries.py` が静的に強制)。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- **プレビュー再計算は既存の汎用機構が覆う**: `CommandHost.svelte` の `selectionPrint`(`kind:'selection'` の arg を持つコマンドは確定選択の中身をプレビュー再送キーに含める)。選択の増減でプレビューが動く verb を足すときも、まずこの既存経路で足りるか確認してから配線を足す。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- Web の `BOOLEAN_BODY` の `args` 順は `[pick target_ref(face), selection tools(face/edge/vertex, min=1), choice mode]`(`firstMissingArg` の質問順になる)。`SOLID_FEATURE_COMMANDS` にのみ追加。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- 1つの Body 内に複数 SOLID(compound ソリッド)を持つ場合の個別演算は未保証(`boolean_bodies` は「SOLID が 1 個以上あるか」しか見ない)。キーボードショートカットは未割当(全 verb 完成後にまとめて決める方針)。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)
- 新しい verb 追加時は `MCP.md` のツール表(`test_doc_code_agreement.py`)と画面の `Applied` 型(`npm run check`)の追随が不可避。`Applied` は現在 `web/src/generated/protocol.ts` の生成物なので、`MCP.md` と生成の更新が要る。 (20260911-2118-duocad-boolean-body-verb, 2026-09-11)

## envelope_regions(スケッチの包絡)

- 呼ぶ面は MCP/セッション `envelope_regions(radius=0, offset=0)`(対象は確定選択の領域。WS は radius・offset 必須)。verb は `duocad/verbs/envelope_regions.py`、幾何は `kernel_sketch.envelope_faces`/`face_boundary_entities`。プレビューは投機実行でなく `preview_envelope_regions`(分裂しても失敗にせず全部の線と個数を返す)。 (20260926-1648-duocad-sketch-envelope-verb, 2026-09-26)
- **結果は線分・円弧・円のみ。面がちょうど 1 枚でなければ ValueError で何も変えない。** redo/replay はログの図形数値を使う。 (20260926-1648-duocad-sketch-envelope-verb, 2026-09-26)
- **依存禁止**: 領域の添字(後の編集で指す先が変わる)。負のオフセットを OCCT へ直接頼むこと。 (20260926-1648-duocad-sketch-envelope-verb, 2026-09-26)
- **罠**: 同心円の面の内向きオフセットは失敗する(箱 − 補集合の拡大で代替)。詳細は docs/CAD_LIBRARY_PITFALLS.md。 (20260926-1648-duocad-sketch-envelope-verb, 2026-09-26)

## 押し出し(extrude)の kernel 分離・web 統合・extrude_face 廃止

- **押し出しの幾何は `duocad/kernel_extrude.py`**(`extrude_regions(faces, distance, mode, normal, target_shape=None)` と `extrude_faces`。mode は join/cut/new)。`verbs/extrude.py` が直接呼ぶ。`kernel.py` に再エクスポートは無く、`kernel.extrude_regions`/`kernel.extrude_face_on_shape` は存在しない(AttributeError)。`mcp_server.py` から `kernel_extrude.*` を直接呼ばない(MCP 層は幾何を直接叩かない検査が `tests/test_mcp_verbs.py` にある)。 (20260926-2131-duocad-extrude-1-kernel-split, 2026-09-26)
- **依存は `kernel_extrude` → `kernel` の一方向。** `kernel.py` から `kernel_extrude` を import しない(循環)。`kernel_extrude` は kernel の private 補助(`_face_map`・`_face_at`・`_face_normal`・`_face_props`・`_checked_volume`・`_require_valid`・`_unify_same_domain`・`_fuse_all`・`_positions_in`・`_face_map_projection`・`_shape_list`・`_is_removed`)を import している。これらの名前・シグネチャを `kernel.py` 側で変えるなら `kernel_extrude` も直す。 (20260926-2131-duocad-extrude-1-kernel-split, 2026-09-26)
- **OCP を import してよいカーネル層は 6 本**(`tests/test_boundaries.py` の `OCP_ALLOWED` と README の表に `kernel_extrude.py`)。 (20260926-2131-duocad-extrude-1-kernel-split, 2026-09-26)
- 移した関数に固有の補助は `_find_translated_face`・`_through_history`・`_MATCH_TOL`(他に使う所が無いことを grep で確認して一緒に移動)。`face_of`/`_face_at` は `query.py`・`kernel_sketch.py`・テストが使うので `kernel` に残した。 (20260926-2131-duocad-extrude-1-kernel-split, 2026-09-26)
- **UI から `extrude_face` を呼ぶ経路は無い。** ツールバーの「押し出す」は 1 つ(`#extrude`)。`features.ts` の `makeExtrude('region'|'face')` が同じ id `extrude` の 2 定義を作り、`EXTRUDE_REGIONS`(スケッチモード一覧 `SKETCH_FEATURE_COMMANDS`、選択引数名 `regions`)と `EXTRUDE_FACES`(通常モードの `SOLID_FEATURE_COMMANDS`、`faces`)として出す(どちらも min 1・max null)。距離 `#distance`、種別 `#extrude-mode-join/cut/new`。サーバへ送るのは両方 `{distance, mode}` だけ(対象はコアの確定選択)。 (20260926-2133-duocad-extrude-3-web-unify, 2026-09-26)
- 同じ id `extrude` が 2 つの一覧に居ても、`CATEGORY_OF`(`App.svelte`)は id→category で両方 `feature` なので壊れない。`findCommand(id, list)` の `list`、`CommandHost` の `commands` プロップは必須(既定なし。`command.ts` の `COMMANDS` は削除)。 (20260926-2133-duocad-extrude-3-web-unify, 2026-09-26)
- `App.svelte` の `verb === 'extrude' && !regionsStillExist(...)` は変更不要(`regionsStillExist` は選択の領域参照だけを見るので、面の選択なら空集合で true)。通常モードは領域を要求しないので `wantsRegions` は立たず、`viewport.pickRegions` の 3D 領域ピックは通常モードで使われなくなった(コードは残存)。 (20260926-2133-duocad-extrude-3-web-unify, 2026-09-26)
- **押し出しの公開 verb は `extrude` だけ**(領域は `regions`、面は `faces`)。`extrude_face` はカタログ・`Session`・MCP・WebSocket・`duocad.verbs.__all__` から消えた。`verbs/__init__.py` は `extrude_face` モジュールを import して `register_verb` の登録だけを残す(再生専用)。`kernel_extrude.extrude_face_on_shape` は削除済み。 (20260926-2134-duocad-extrude-4-retire-extrude-face, 2026-09-26)
- **古い保存ログの `extrude_face`(args: `body_id`/`face_index`/`distance`)は再生される。** 中身は `kernel_extrude.extrude_faces(body.shape, [face_index], distance, "join" if distance > 0 else "cut", target_shape=body.shape)`、undo は `undo_shape`。再生した `extrude_face` の `provenance` は保存時のまま(キーは `verb`/`face_index`/`after_face_index`)。 (20260926-2134-duocad-extrude-4-retire-extrude-face, 2026-09-26)
- **`tests/fixtures/extrude_face_log.json` は削除前の実装で作った保存ログ(体積 12240・6 面)で、`tests/test_extrude_face_replay.py` が再生して固定する。作り直さない**(「従来と同じ形」の根拠が失われる)。 (20260926-2134-duocad-extrude-4-retire-extrude-face, 2026-09-26)
- **`extrude_face` を新しい操作の記録に使わない**(`Session`・MCP から呼べない)。新しい verb を足すときに形を真似ない。再生専用 verb はカタログに無いので、`tests/test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log` は登録済み verb から `extrude_face` と `set_parameter`(どちらも再生専用)を除いて突き合わせる。 (20260926-2134-duocad-extrude-4-retire-extrude-face, 2026-09-26)
- 曲面を掃引すると壊れた形になる実測は、`extrude_face_on_shape` の docstring と一緒に消えないよう docs/CAD_LIBRARY_PITFALLS.md の表を `kernel_extrude._check_coplanar_planes` へ向け直してある。 (20260926-2134-duocad-extrude-4-retire-extrude-face, 2026-09-26)
- 旧 UI id `extrude_face`、ボタン `#extrude-regions`、入力欄 `#regions-distance` は廃止。現行の id は `#extrude`/`#distance`/`#extrude-mode-*`。 (20260926-2133-duocad-extrude-3-web-unify, 2026-09-26; 20260926-2134-duocad-extrude-4-retire-extrude-face, 2026-09-26)

### 踏んだ罠

- **面の押し出しでは溝の底のような窪みの面は視点から見えず選べない。** 2 枚の同一平面の面を押す smoke の題材は溝を浅く広く(幅 24・深さ 3)した(MCP でコの字を作り柱の上面 2 枚を結合で押す / 平面の違う面を混ぜるとトーストに「同一平面上にない」)。ログの `args.faces` は配信で件数の文字列に畳まれる。 (20260926-2133-duocad-extrude-3-web-unify, 2026-09-26)
- 通常モードでは領域を押し出せない。領域の押し出しはスケッチ編集中に行う。 (20260926-2133-duocad-extrude-3-web-unify, 2026-09-26)
