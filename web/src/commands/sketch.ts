/**
 * スケッチのコマンド定義 — **手動操作が一級の操作モードになる場所**。
 *
 * 状態機械・数値入力・中断は `CommandHost.svelte` の1つを使い回す(verbごとに独自のUIを
 * 作らない)。骨格には**点(point)引数**があり、これは「スケッチ平面をクリックして (u,v) を
 * 集める」という、選択でも数値でもない3つ目の集め方である。
 *
 * **幽霊はブラウザで描く**(`preview: 'local'`)。押し出しのプレビューはコアに投機実行させる
 * (形がコアにしか分からない)が、スケッチの図形は**形がブラウザ側で分かっている**。
 * **そもそも送らない**ことで「Escで中断しても文書が一切変わらない」が構造的に成立する。
 *
 * **`sketch_id` はここでは付けない**。どのスケッチに描くかは「今どのスケッチモードに居るか」
 * であって、コマンドが集める引数ではない —— `App.svelte` が送信時に足す。
 */
import { isPlaneArgValue, pickedRefOf, type ArgValues, type CommandDef, type Point2 } from './command'
import { MOVE_MODE_OPTIONS, isRotateMode, isTranslateMode, rejectAngle } from './moveMode'
import { arcFromPoints, circleFromPoints, lineFromPoints, rectFromPoints } from './sketchGeometry'

/**
 * 確定前の姿(幽霊)の組み立ては `../entityKinds` の唯一の表へ寄せてある
 * (図形種別ごとの処理を1箇所にまとめる。README.md「モジュールとimport境界」)。
 * ここは元の呼び出し口を保つための再エクスポートだけ。
 */
export { ghostEntity } from '../entityKinds'

/** 寸法として使えない値(0以下)を弾く。**既定値を入れない**のがここの流儀。 */
function positive(label: string): (value: number) => string | null {
  return (value) => (value > 0 ? null : `${label}は0より大きい値`)
}

/** 空のパスだけ弾く。**それ以上の判定はサーバに任せる**(`import_svg` verbのdocstring)。 */
function pathLike(label: string): (value: string) => string | null {
  return (value) => (value.trim() === '' ? `${label}を打つ` : null)
}

/** 打っていない `fromPoints` の寸法は `undefined`(集めた点から導く合図)。 */
function typed(values: ArgValues, name: string): number | undefined {
  const value = values[name]
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

/**
 * 線分。**寸法はFusionの線ツールと同じ「長さ・角度」**(dx/dyではない)。角度は度・
 * 反時計回り・スケッチ平面のX軸からで、`add_arc` の角度と同じ規約に揃えてある。
 *
 * サーバへ送るのは `p0`/`p1` だけ(`add_line` の契約は変えない)。
 */
export const ADD_LINE: CommandDef = {
  id: 'add_line',
  category: 'sketch',
  label: '線分',
  buttonId: 'sketch-line',
  preview: 'local',
  args: [
    { kind: 'point', name: 'p0', label: '始点' },
    { kind: 'point', name: 'p1', label: '終点' },
    {
      kind: 'number',
      name: 'length',
      label: '長さ',
      unit: 'mm',
      inputId: 'sketch-line-length',
      placeholder: 'mm',
      reject: positive('長さ'),
      fromPoints: true,
    },
    {
      kind: 'number',
      name: 'angle_deg',
      label: '角度',
      unit: '度',
      inputId: 'sketch-line-angle',
      placeholder: '度',
      // 0度は正当な向き(X軸方向)なので弾かない。
      reject: () => null,
      fromPoints: true,
    },
  ],
  build(values) {
    const p0 = values.p0 as Point2 | undefined
    const p1 = values.p1 as Point2 | undefined
    if (!p0 || !p1) return { ok: false, reason: '2点が要る' }
    const line = lineFromPoints(p0, p1, typed(values, 'length'), typed(values, 'angle_deg'))
    if (line === null) return { ok: false, reason: '長さは0より大きい値' }
    return { ok: true, args: { p0: line.p0, p1: line.p1 } }
  },
}

export const ADD_CIRCLE: CommandDef = {
  id: 'add_circle',
  category: 'sketch',
  label: '円',
  buttonId: 'sketch-circle',
  preview: 'local',
  args: [
    { kind: 'point', name: 'center', label: '中心' },
    { kind: 'point', name: 'rim', label: '円周の1点' },
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'sketch-radius',
      placeholder: 'mm',
      reject: positive('半径'),
      fromPoints: true,
    },
  ],
  build(values) {
    const center = values.center as Point2 | undefined
    const rim = values.rim as Point2 | undefined
    if (!center || !rim) return { ok: false, reason: '中心と円周の点が要る' }
    const circle = circleFromPoints(center, rim, typed(values, 'radius'))
    if (circle === null) return { ok: false, reason: '半径は0より大きい値' }
    return { ok: true, args: { center: circle.center, radius: circle.radius } }
  },
}

/**
 * 長方形。**角度の欄は無い** —— `add_rectangle` は軸平行(`x`/`y`/`width`/`height`)で、
 * コアもブラウザも回転した長方形を表現できない(`entityKinds.ts` の `rotate` は長方形を
 * 4本の線分に落とす)。
 */
export const ADD_RECTANGLE: CommandDef = {
  id: 'add_rectangle',
  category: 'sketch',
  label: '長方形',
  buttonId: 'sketch-rectangle',
  preview: 'local',
  args: [
    { kind: 'point', name: 'corner', label: '1つ目の角' },
    { kind: 'point', name: 'opposite', label: '対角' },
    {
      kind: 'number',
      name: 'width',
      label: '幅',
      unit: 'mm',
      inputId: 'sketch-width',
      placeholder: 'mm',
      reject: positive('幅'),
      fromPoints: true,
    },
    {
      kind: 'number',
      name: 'height',
      label: '高さ',
      unit: 'mm',
      inputId: 'sketch-height',
      placeholder: 'mm',
      reject: positive('高さ'),
      fromPoints: true,
    },
  ],
  build(values) {
    const corner = values.corner as Point2 | undefined
    const opposite = values.opposite as Point2 | undefined
    if (!corner || !opposite) return { ok: false, reason: '角と対角の2点が要る' }
    const rect = rectFromPoints(corner, opposite, typed(values, 'width'), typed(values, 'height'))
    if (rect === null) return { ok: false, reason: '幅と高さは0より大きい値' }
    return { ok: true, args: { corner: rect.corner, width: rect.width, height: rect.height } }
  },
}

/**
 * 円弧。**3点目は「中心」ではなく「円弧上の点」**で、開始角・終了角は始点と終点の
 * クリックから決まる(角度の欄は持たない)。幾何と掃引の向きは `sketchGeometry.ts` の
 * `arcFromPoints` が閉じている。
 */
export const ADD_ARC: CommandDef = {
  id: 'add_arc',
  category: 'sketch',
  label: '円弧',
  buttonId: 'sketch-arc',
  preview: 'local',
  args: [
    { kind: 'point', name: 'p0', label: '始点' },
    { kind: 'point', name: 'p1', label: '終点' },
    { kind: 'point', name: 'via', label: '円弧上の点' },
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'sketch-arc-radius',
      placeholder: 'mm',
      reject: positive('半径'),
      fromPoints: true,
    },
  ],
  build(values) {
    const p0 = values.p0 as Point2 | undefined
    const p1 = values.p1 as Point2 | undefined
    const via = values.via as Point2 | undefined
    if (!p0 || !p1 || !via) return { ok: false, reason: '始点・終点・円弧上の点が要る' }
    const arc = arcFromPoints(p0, p1, via, typed(values, 'radius'))
    if (arc === null) return { ok: false, reason: '3点が一直線か、半径が弦の半分より小さい' }
    return {
      ok: true,
      args: {
        center: arc.center,
        radius: arc.radius,
        start_deg: arc.start_deg,
        end_deg: arc.end_deg,
      },
    }
  },
}

/**
 * SVGの図形を**いま開いているスケッチへ**足す(CADの「アクティブなスケッチに挿入する」に
 * 相当する形)。
 *
 * **引数はパスだけ**。`sketch_id` はここでは付けない(モジュールdocstringの規律どおり
 * `App.svelte` が送信時に足す)。
 *
 * **倍率(`scale`)はここに出していない** —— 単位の合わないSVGを直すための逃げ道であって、
 * 普段は要らない。常に出すと「毎回1.0と打つ欄」が1つ増える(`catalog` の `ws=False`)。
 */
export const IMPORT_SVG: CommandDef = {
  // **id == サーバのコマンド名 == ログのverb名**。
  id: 'import_svg',
  category: 'sketch',
  label: 'SVG読み込み',
  buttonId: 'sketch-import-svg',
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: 'SVG',
      inputId: 'sketch-svg-path',
      placeholder: 'logo.svg',
      reject: pathLike('SVGのパス'),
    },
  ],
  build(values) {
    const path = values.path
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: 'パスが空' }
    return { ok: true, args: { path: path.trim() } }
  },
}

/**
 * スケッチを作る —— **「スケッチ」を押して、貼る先をクリックする2手**。
 *
 * 分類は `sketch` だが `sketch_id` を取らず、**まだどのスケッチも開いていない状態で走る
 * 唯一のverb**である —— 走る場所は**スケッチモードの貼る先待ち**(`sketch-pending`)で、
 * `sketch_id` を足さずに送る。反復
 * (`pattern_*`)が「分類は feature だが `sketch_id` が要る」のちょうど裏返しで、`App.svelte` の
 * 送信経路はその2箇所を `needsSketchId`(features.ts)と下の `runsOutsideSketch` で捌く。
 *
 * 基準平面のボタンは置かない —— 置くと「ボタンで指す」と「クリックで指す」の2つが並び、面に
 * 貼る経路だけがクリックになる(UI指針1「実物をクリックできるものにボタンを作らない」)。
 *
 * **面は送らない**: 面に貼るときの `build` は **`{}`** を返す。面の参照を引数に入れると
 * 「対象集合の指定方法」が再び2つになる(サーバは `require_confirmed(doc, "face",
 * exactly_one=True)` で確定選択から取る)。2枚選んでいれば `SelectionAmbiguous` が返り、
 * トーストで人に見える。**AIの提案のままでも止まらない** —— 面に貼る
 * (`base_plane` を送らない)経路は対象集合を確定選択から取るので、サーバが走らせる直前に
 * 自動で確定する(`duocad/server.py` の `_commit_before_verb`)。
 */
export const SKETCH_CREATE: CommandDef = {
  id: 'sketch_create',
  category: 'sketch',
  label: 'スケッチ',
  buttonId: 'sketch-create',
  preview: 'none',
  // **引数名は `target`** —— WSに載る名前(`base_plane`)とわざと変えてある。値は
  // 「基準平面名」か「面は選択にある」の2状態で、`base_plane` だと後者が嘘になる。
  args: [{ kind: 'plane', name: 'target', label: 'スケッチを貼る先' }],
  build(values) {
    const plane = values.target
    if (!isPlaneArgValue(plane)) return { ok: false, reason: '貼る先が決まっていない' }
    // **面は送らない**(サーバがコアの確定選択から取る)。基準平面だけが引数になる。
    return { ok: true, args: plane === 'face' ? {} : { base_plane: plane } }
  },
}

/**
 * スケッチモードの**貼る先待ち**(`sketch-pending`)のツールバー。
 *
 * **1つだけ**である —— この状態でできることは「どこに貼るかを指す」しかない。
 * `App.svelte` が `startCommandId` でこれを起動済みにして入るので、モードに入った次の
 * 1クリック(基準平面 or 面)がそのまま `sketch_create` の確定になる(2手のまま)。
 */
export const SKETCH_PENDING_COMMANDS: CommandDef[] = [SKETCH_CREATE]

/**
 * そのverbは**まだどのスケッチも開いていない状態で走る**か(= `sketch_id` を足さずに送るか)。
 *
 * 該当するのは `sketch_create` だけである —— スケッチを作るverbなので、開いていることを
 * 要求すると永久に起動できない。`features.ts` の `needsSketchId` のちょうど裏返しで、
 * **分類だけでは送り方が決まらない2箇所**をこの2つの述語で名指ししている。
 *
 * **列挙にしてある**(「引数に `plane` があるか」等で導出しない)ので、スケッチモードのverbを
 * 新しく足しても既定は変わらない —— 足したverbは今までどおり `sketch_id` が付く。
 */
export function runsOutsideSketch(verb: string): boolean {
  return verb === SKETCH_CREATE.id
}

/**
 * スケッチの点・辺の平行移動 / 回転 / 複製。
 *
 * 対象はコアの確定選択から取る(3Dで点や辺をクリック / 左ドラッグで囲む)。
 * `sketch_id` を取らない唯一のスケッチverbで、どのスケッチかは**選んだ参照が持っている**
 * (下の `runsWithoutSketchId`)。
 *
 * **複製するかどうかも、並進か回転かも人に選ばせる**(既定を置かない)。`move_body` と
 * 同じ理由。**軸は無い** —— スケッチは2Dなので回転軸は常に平面の法線で、自由度が無い
 * (コア側の判断。`duocad/verbs/move_sketch.py`)。中心は省略でき、既定はコアが決める
 * (対象の代表点の平均)。
 */
export const MOVE_SKETCH_ENTITIES: CommandDef = {
  id: 'move_sketch_entities',
  category: 'sketch',
  label: '動かす',
  buttonId: 'sketch-move',
  // **ローカル方式**。`move_body` と同じ発想で、選んだ図形の今の座標を
  // dx/dy だけずらした姿を描く。対象は引数に無い(サーバがコアの確定選択から取る)ので、
  // 幽霊の対象も `App.svelte` が同じ選択集合から導く(`ghostSources`)。
  preview: 'local',
  args: [
    {
      kind: 'selection',
      name: 'target',
      label: '動かす点・辺',
      shapeKind: 'sketch_entity',
      min: 1,
      max: null,
    },
    {
      kind: 'choice',
      name: 'copy',
      label: '複製',
      options: [
        { value: 'move', label: '動かす', buttonId: 'sketch-move-move' },
        { value: 'copy', label: '複製する', buttonId: 'sketch-move-copy' },
      ],
    },
    {
      kind: 'choice',
      name: 'mode',
      label: '種別',
      options: MOVE_MODE_OPTIONS.map((option) => ({
        ...option,
        buttonId: `sketch-move-${option.value}`,
      })),
    },
    {
      kind: 'number',
      name: 'dx',
      label: 'X',
      unit: 'mm',
      inputId: 'sketch-move-dx',
      placeholder: 'mm',
      // 片方の軸だけ動かすのは普通なので0は弾かない(両方0だけを `build` が断る)。
      reject: () => null,
      // 空欄はこの軸を動かさない(=0)扱い。両軸とも空欄(=両方0)は`build`が別途弾く。
      default: 0,
      when: isTranslateMode,
    },
    {
      kind: 'number',
      name: 'dy',
      label: 'Y',
      unit: 'mm',
      inputId: 'sketch-move-dy',
      placeholder: 'mm',
      reject: () => null,
      default: 0,
      when: isTranslateMode,
    },
    {
      kind: 'pick',
      name: 'center_entity',
      label: '回転中心の点',
      target: 'sketchPoint',
      buttonId: 'sketch-move-center',
      // 省略時の中心は**コアが決める**(対象の代表点の平均)。
      optional: true,
      when: isRotateMode,
    },
    {
      kind: 'number',
      name: 'angle',
      label: '角度',
      unit: '度',
      inputId: 'sketch-move-angle',
      placeholder: '度',
      // 符号は u->v の反時計回りが正(`pattern_circular` の `total_angle` と同じ向き)。
      reject: rejectAngle,
      when: isRotateMode,
    },
  ],
  build(values) {
    const targets = (values.target ?? []) as unknown[]
    if (targets.length === 0) return { ok: false, reason: '点も辺も選ばれていない' }
    const copy = values.copy === 'copy'
    if (values.mode === 'rotate') {
      const angle = values.angle
      if (typeof angle !== 'number') return { ok: false, reason: '角度が数値でない' }
      const reason = rejectAngle(angle)
      if (reason !== null) return { ok: false, reason }
      // **中心は参照のまま送る**(`move_body` の軸と同じ規律)。図形の点の座標を出す
      // 規則(`point` 省略ならその種別の最初の点)はコアの `_point_of` が正で、
      // 数値へ潰して送ると同じ規則がもう1つ増える。
      const center = pickedRefOf(values.center_entity)
      return {
        ok: true,
        args: { mode: 'rotate', angle, ...(center === null ? {} : { center_entity: center }), copy },
      }
    }
    const dx = values.dx
    const dy = values.dy
    if (typeof dx !== 'number' || typeof dy !== 'number') {
      return { ok: false, reason: '移動量が数値でない' }
    }
    if (dx === 0 && dy === 0) return { ok: false, reason: '移動量が両方0' }
    // **対象そのものは送らない**(サーバがコアの確定選択から取る)。
    return { ok: true, args: { mode: 'translate', dx, dy, copy } }
  },
}

/**
 * そのverbは**スケッチモードの中で走るのに `sketch_id` を取らない**か。
 *
 * `runsOutsideSketch`(モードの外で走る)のちょうど半分だけ違う位置にいる ——
 * `move_sketch_entities` はモードに入っていることを要求するが、どのスケッチかは
 * 選んだ参照(`{sketch, entity, point}`)が持っているので引数に要らない。
 *
 * **列挙にしてある**(引数の形から導出しない)ので、スケッチverbを新しく足しても既定は
 * 変わらない —— 足したverbは今までどおり `sketch_id` が付く。
 */
export function runsWithoutSketchId(verb: string): boolean {
  return verb === MOVE_SKETCH_ENTITIES.id
}

/**
 * 何も描かないコマンド(スケッチモードの既定)。
 *
 * **これが既定でないと領域をクリックで選べない** —— 描くコマンドが常に点を待っていると、
 * 領域を狙ったクリックが図形の頂点になる。
 *
 * **確定が成功するとここへ戻る**(`CommandHost.svelte` の `commandSucceeded()`)。
 * 連続で同じ図形を描くには、確定のたびにツールバーのボタンを押し直す。
 */
export const SELECT_IN_SKETCH: CommandDef = {
  id: 'select_in_sketch',
  category: 'ui',
  label: '選ぶ',
  buttonId: 'sketch-select',
  // **ボタンは出さない**。領域を直接クリックして選べるので、
  // 人間向けのボタンは重複だった。既定コマンド(`DEFAULT_SKETCH_COMMAND_ID`)としての
  // 役割はそのまま——Escや確定後はここへ戻る(`CommandHost` のdocstring参照)。
  hidden: true,
  preview: 'none',
  args: [],
  build: () => ({ ok: false, reason: '領域をクリックして選ぶ(図形を描くなら上のボタン)' }),
}

/** スケッチモードのツールバー。既定は「選ぶ」。 */
export const SKETCH_COMMANDS: CommandDef[] = [
  SELECT_IN_SKETCH,
  ADD_LINE,
  ADD_CIRCLE,
  ADD_RECTANGLE,
  ADD_ARC,
  MOVE_SKETCH_ENTITIES,
  IMPORT_SVG,
]
export const DEFAULT_SKETCH_COMMAND_ID = SELECT_IN_SKETCH.id
