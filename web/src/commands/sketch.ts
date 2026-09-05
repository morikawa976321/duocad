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
import { isPlaneArgValue, type CommandDef, type Point2 } from './command'

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

export const ADD_LINE: CommandDef = {
  id: 'add_line',
  category: 'sketch',
  label: '線分',
  buttonId: 'sketch-line',
  preview: 'local',
  args: [
    { kind: 'point', name: 'p0', label: '始点' },
    { kind: 'point', name: 'p1', label: '終点' },
  ],
  build(values) {
    const p0 = values.p0 as Point2 | undefined
    const p1 = values.p1 as Point2 | undefined
    if (!p0 || !p1) return { ok: false, reason: '2点が要る' }
    return { ok: true, args: { p0, p1 } }
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
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'sketch-radius',
      placeholder: 'mm',
      reject: positive('半径'),
    },
  ],
  build(values) {
    const center = values.center as Point2 | undefined
    const radius = values.radius
    if (!center) return { ok: false, reason: '中心が要る' }
    if (typeof radius !== 'number') return { ok: false, reason: '半径が数値でない' }
    return { ok: true, args: { center, radius } }
  },
}

export const ADD_RECTANGLE: CommandDef = {
  id: 'add_rectangle',
  category: 'sketch',
  label: '長方形',
  buttonId: 'sketch-rectangle',
  preview: 'local',
  args: [
    { kind: 'point', name: 'corner', label: '左下' },
    {
      kind: 'number',
      name: 'width',
      label: '幅',
      unit: 'mm',
      inputId: 'sketch-width',
      placeholder: 'mm',
      reject: positive('幅'),
    },
    {
      kind: 'number',
      name: 'height',
      label: '高さ',
      unit: 'mm',
      inputId: 'sketch-height',
      placeholder: 'mm',
      reject: positive('高さ'),
    },
  ],
  build(values) {
    const corner = values.corner as Point2 | undefined
    const width = values.width
    const height = values.height
    if (!corner) return { ok: false, reason: '左下の点が要る' }
    if (typeof width !== 'number' || typeof height !== 'number') {
      return { ok: false, reason: '幅と高さが数値でない' }
    }
    return { ok: true, args: { corner, width, height } }
  },
}

export const ADD_ARC: CommandDef = {
  id: 'add_arc',
  category: 'sketch',
  label: '円弧',
  buttonId: 'sketch-arc',
  preview: 'local',
  args: [
    { kind: 'point', name: 'center', label: '中心' },
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'sketch-arc-radius',
      placeholder: 'mm',
      reject: positive('半径'),
    },
    {
      kind: 'number',
      name: 'start_deg',
      label: '開始角',
      unit: '度',
      inputId: 'sketch-arc-start',
      placeholder: '度',
      // 0度は正当な開始角なので弾かない(反時計回り・X軸から。`add_arc` の契約)
      reject: () => null,
    },
    {
      kind: 'number',
      name: 'end_deg',
      label: '終了角',
      unit: '度',
      inputId: 'sketch-arc-end',
      placeholder: '度',
      reject: () => null,
    },
  ],
  build(values) {
    const center = values.center as Point2 | undefined
    const radius = values.radius
    const start = values.start_deg
    const end = values.end_deg
    if (!center) return { ok: false, reason: '中心が要る' }
    if (typeof radius !== 'number' || typeof start !== 'number' || typeof end !== 'number') {
      return { ok: false, reason: '半径と角度が数値でない' }
    }
    if (start === end) return { ok: false, reason: '開始角と終了角が同じ' }
    return { ok: true, args: { center, radius, start_deg: start, end_deg: end } }
  },
}

/**
 * SVGの図形を**いま開いているスケッチへ**足す(2026-08-28。「新しいスケッチを1枚作って
 * 全部そこへ入れる」形から、CADの「アクティブなスケッチに挿入する」に相当する形へ変えた)。
 *
 * **引数はパスだけ**。`sketch_id` はここでは付けない(モジュールdocstringの規律どおり
 * `App.svelte` が送信時に足す) —— 貼る先を選ぶ手順(旧「基準平面をクリック」)は消えた。
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
 * 唯一のverb**である(2026-09-03の言い換え。以前は「スケッチモードの外で走る」と書いていたが、
 * モードの入口が上位次元へ上がり、これが走る場所は**スケッチモードの貼る先待ち**
 * (`sketch-pending`)になった。走らせ方は変わっていない —— `sketch_id` を足さずに送る)。反復
 * (`pattern_*`)が「分類は feature だが `sketch_id` が要る」のちょうど裏返しで、`App.svelte` の
 * 送信経路はその2箇所を `needsSketchId`(features.ts)と下の `runsOutsideSketch` で捌く。
 *
 * 基準平面のボタンは置かない —— 置くと「ボタンで指す」と「クリックで指す」の2つが並び、面に
 * 貼る経路だけがクリックになる(UI指針1「実物をクリックできるものにボタンを作らない」)。
 *
 * **面は送らない**: 面に貼るときの `build` は **`{}`** を返す。面の参照を引数に入れると
 * 「対象集合の指定方法」が再び2つになる(サーバは `require_confirmed(doc, "face",
 * exactly_one=True)` で確定選択から取る)。2枚選んでいれば `SelectionAmbiguous` が返り、
 * トーストで人に見える。**AIの提案のままでも止まらない**(2026-08-23) —— 面に貼る
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
 * スケッチの点・辺の平行移動 / 複製(2026-08-30。**パラメータパネルの後継**)。
 *
 * 対象はコアの確定選択から取る(3Dで点や辺をクリック / 左ドラッグで囲む)。
 * `sketch_id` を取らない唯一のスケッチverbで、どのスケッチかは**選んだ参照が持っている**
 * (下の `runsWithoutSketchId`)。
 *
 * **複製するかどうかは人に選ばせる**(既定を置かない)。`move_body` と同じ理由。
 */
export const MOVE_SKETCH_ENTITIES: CommandDef = {
  id: 'move_sketch_entities',
  category: 'sketch',
  label: '動かす',
  buttonId: 'sketch-move',
  // **ローカル方式**(2026-09-05)。`move_body` と同じ発想で、選んだ図形の今の座標を
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
      kind: 'number',
      name: 'dx',
      label: 'X',
      unit: 'mm',
      inputId: 'sketch-move-dx',
      placeholder: 'mm',
      // 片方の軸だけ動かすのは普通なので0は弾かない(両方0だけを `build` が断る)。
      reject: () => null,
    },
    {
      kind: 'number',
      name: 'dy',
      label: 'Y',
      unit: 'mm',
      inputId: 'sketch-move-dy',
      placeholder: 'mm',
      reject: () => null,
    },
  ],
  build(values) {
    const targets = (values.target ?? []) as unknown[]
    if (targets.length === 0) return { ok: false, reason: '点も辺も選ばれていない' }
    const dx = values.dx
    const dy = values.dy
    if (typeof dx !== 'number' || typeof dy !== 'number') {
      return { ok: false, reason: '移動量が数値でない' }
    }
    if (dx === 0 && dy === 0) return { ok: false, reason: '移動量が両方0' }
    // **対象そのものは送らない**(サーバがコアの確定選択から取る)。
    return { ok: true, args: { dx, dy, copy: values.copy === 'copy' } }
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
 * **確定が成功するとここへ戻る**(`CommandHost.svelte` の `commandSucceeded()`。2026-08-31)。
 * 以前は確定してもここへは戻らず、Escを押したときだけ「選ぶ」に移っていた(同じ図形を続けて
 * 描けるほうが速いという判断だった)が、「verbを実行したら中断ボタンを押さなくても実行中
 * 状態が終わってほしい」という要望で、確定成功でも「選ぶ」へ降りるようにした。連続で同じ
 * 図形を描くには、確定のたびにツールバーのボタンを押し直す。
 */
export const SELECT_IN_SKETCH: CommandDef = {
  id: 'select_in_sketch',
  category: 'ui',
  label: '選ぶ',
  buttonId: 'sketch-select',
  // **ボタンは出さない**(2026-09-03本人決定)。領域を直接クリックして選べるので、
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
