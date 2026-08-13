/**
 * スケッチのコマンド定義(タスクC・2026-08-13) — **手動操作が一級の操作モードになる場所**。
 *
 * スケッチはM2からコアとMCPにあり、AIからは描けたが**人からは描けなかった**。
 * ここが埋めるのはその片道で、コアには何も足していない(WebSocketに口を開けただけ。
 * `duocad/server.py` の `SKETCH_COMMANDS`)。
 *
 * ## Bの骨格に載せるだけ
 *
 * 状態機械・数値入力・中断は `CommandHost.svelte` の1つを使い回す(実装計画書§8b C
 * 「Bで作ったコマンド層の骨格に載せる。verbごとに独自のUIを作らない」)。Cが骨格へ
 * 足したのは**点(point)引数**だけで、これは「スケッチ平面をクリックして (u,v) を集める」
 * という、選択でも数値でもない3つ目の集め方である。
 *
 * ## 幽霊はブラウザで描く(`preview: 'local'`)
 *
 * 押し出しのプレビューはコアに投機実行させる(形がコアにしか分からないため)が、
 * スケッチの図形は**形がブラウザ側で分かっている**。投機実行にすると、中断しても
 * 文書に一度は入って戻ることになる。ここでは**そもそも送らない**ことで
 * 「Escで中断しても文書が一切変わらない」を構造的に成立させる。
 *
 * ## `sketch_id` はここでは付けない
 *
 * どのスケッチに描くかは「今どのスケッチモードに居るか」であって、コマンドが集める
 * 引数ではない。`App.svelte` が送信時に足す(コマンド定義がセッションの状態を知らない)。
 */
import type { CommandDef, Point2 } from './command'

/** 寸法として使えない値(0以下)を弾く。**既定値を入れない**のはM1からの流儀。 */
function positive(label: string): (value: number) => string | null {
  return (value) => (value > 0 ? null : `${label}は0より大きい値`)
}

export const ADD_LINE: CommandDef = {
  id: 'add_line',
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

/** 実際に図形を作る4つ(「選ぶ」を含まない = サーバが受け取るverbと同じ集合)。 */
export function isSketchVerb(verb: string): boolean {
  return [ADD_LINE, ADD_CIRCLE, ADD_RECTANGLE, ADD_ARC].some((command) => command.id === verb)
}

/**
 * 確定前の姿(幽霊)を、コアの `Entity` と同じ `{kind, params}` へ翻訳する。
 *
 * **形の定義を1箇所に保つ**ため、描くのは `viewport.ts` の `entityPoints`(コアの
 * `Entity.params` のキーをそのまま読む実装)で、ここは verb の引数からそのキーへ
 * 移し替えるだけにしてある。
 */
export function ghostEntity(
  verb: string,
  args: Record<string, unknown>,
): { kind: string; params: Record<string, number> } | null {
  if (verb === 'add_line') {
    const p0 = args.p0 as Point2
    const p1 = args.p1 as Point2
    return { kind: 'line', params: { x0: p0[0], y0: p0[1], x1: p1[0], y1: p1[1] } }
  }
  if (verb === 'add_circle') {
    const center = args.center as Point2
    return { kind: 'circle', params: { cx: center[0], cy: center[1], radius: args.radius as number } }
  }
  if (verb === 'add_rectangle') {
    const corner = args.corner as Point2
    return {
      kind: 'rectangle',
      params: {
        x: corner[0],
        y: corner[1],
        width: args.width as number,
        height: args.height as number,
      },
    }
  }
  if (verb === 'add_arc') {
    const center = args.center as Point2
    return {
      kind: 'arc',
      params: {
        cx: center[0],
        cy: center[1],
        radius: args.radius as number,
        start_deg: args.start_deg as number,
        end_deg: args.end_deg as number,
      },
    }
  }
  return null
}
