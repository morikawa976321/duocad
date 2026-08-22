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
 * スケッチを作る —— **「スケッチ」を押して、貼る先をクリックする2手**。
 *
 * 分類は `sketch` だが `sketch_id` を取らず、**スケッチモードの外で走る唯一のverb**である
 * (だから一覧も `SKETCH_COMMANDS` ではなく `App.svelte` のソリッド側に載る)。反復
 * (`pattern_*`)が「分類は feature だが `sketch_id` が要る」のちょうど裏返しで、`App.svelte` の
 * 送信経路はその2箇所を `needsSketchId`(features.ts)と下の `runsOutsideSketch` で捌く。
 *
 * 基準平面のボタンは置かない —— 置くと「ボタンで指す」と「クリックで指す」の2つが並び、面に
 * 貼る経路だけがクリックになる(UI指針1「実物をクリックできるものにボタンを作らない」)。
 *
 * **面は送らない**: 面に貼るときの `build` は **`{}`** を返す。面の参照を引数に入れると
 * 「対象集合の指定方法」が再び2つになる(サーバは `require_confirmed(doc, "face",
 * exactly_one=True)` で確定選択から取る)。2枚選んでいれば `SelectionAmbiguous`、AIの提案の
 * ままなら `SelectionNotCommitted` が返り、トーストで人に見える。
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
 * そのverbは**スケッチモードの外で走る**か(= `sketch_id` を足さずに送るか)。
 *
 * 該当するのは `sketch_create` だけである —— スケッチを作るverbなので、モードに入っている
 * ことを要求すると永久に起動できない。`features.ts` の `needsSketchId` のちょうど裏返しで、
 * **分類だけでは送り方が決まらない2箇所**をこの2つの述語で名指ししている。
 *
 * **列挙にしてある**(「引数に `plane` があるか」等で導出しない)ので、スケッチモードのverbを
 * 新しく足しても既定は変わらない —— 足したverbは今までどおり `sketch_id` が付く。
 */
export function runsOutsideSketch(verb: string): boolean {
  return verb === SKETCH_CREATE.id
}

/**
 * 何も描かないコマンド(スケッチモードの既定)。
 *
 * **これが既定でないと領域をクリックで選べない** —— 描くコマンドが常に点を待っていると、
 * 領域を狙ったクリックが図形の頂点になる。
 *
 * **確定してもここへは戻らない**(実際のCADと同じで、同じ図形を続けて描けるほうが速い)。
 * 選ぶ側へ移るのは**Escを押したとき**である。
 */
export const SELECT_IN_SKETCH: CommandDef = {
  id: 'select_in_sketch',
  category: 'ui',
  label: '選ぶ',
  buttonId: 'sketch-select',
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
]
export const DEFAULT_SKETCH_COMMAND_ID = SELECT_IN_SKETCH.id
