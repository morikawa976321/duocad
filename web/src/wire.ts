/**
 * 状態とプレビューの配信を読む: 1枚のバイナリフレームと、省かれて届いたものの埋め戻し(issue #166・#170)。
 * 形と省く規則の正はサーバの `duocad/net/wire.py`(ヘッダの JSON の数の並びが `{"$f32": [バイト位置, 個数]}` /
 * `{"$u32": ...}` に置き換わっている。省かれた面は `{index, serial, detail}`、スケッチは `{sketch_id, unchanged}`、
 * 操作ログは `log` ごと無い)。
 */
import type { BodyGeometry, ServerState, SketchState } from './session'
import type { FaceGeometry } from './viewportTypes'

const decoder = new TextDecoder()

/**
 * フレームを配りものに戻す。数の並びは型付き配列の**写し**にする(フレームの `ArrayBuffer` のビューのままだと、
 * 省かれて使い回される面が1つでも残る限り、そのフレーム全体(初回は 6 MB)を手放せない)。
 */
export function decodeFrame(buffer: ArrayBuffer): unknown {
  const headerLength = new DataView(buffer).getUint32(0, true)
  const base = 4 + headerLength
  const header = decoder.decode(new Uint8Array(buffer, 4, headerLength))
  return JSON.parse(header, (_key, value) => {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return value
    const f32 = (value as { $f32?: [number, number] }).$f32
    if (f32 !== undefined) return new Float32Array(buffer.slice(base + f32[0], base + f32[0] + f32[1] * 4))
    const u32 = (value as { $u32?: [number, number] }).$u32
    if (u32 !== undefined) return new Uint32Array(buffer.slice(base + u32[0], base + u32[0] + u32[1] * 4))
    return value
  })
}

/** 省かれて届いた面(前回このボディで受け取った同じ `serial` の面を使う)。 */
interface OmittedFace {
  index: number
  serial: number
  detail?: 'fine' | 'coarse'
}

/** 省かれて届いたスケッチ(前回受け取ったものを使う)。 */
interface OmittedSketch {
  sketch_id: string
  unchanged: true
}

/** 届いたままの `state`(操作ログが無いことがあり、スケッチが省かれていることがある)。 */
export type WireState = Omit<ServerState, 'log' | 'sketches'> & {
  log?: ServerState['log']
  sketches: Array<SketchState | OmittedSketch>
}

/** 省かれたものを埋め戻せなかった(サーバの記録とこの写しが食い違った)。繋ぎ直せば両側とも忘れて揃う。 */
export class DeliveryMismatch extends Error {}

/**
 * 1つの接続で受け取ったもの(サーバの `wire.Delivered` の写し)。接続ごとに作り直す(サーバも接続ごとに忘れる)。
 * `restore` は `state` と `bodies` を**その場で**埋め戻し、受け取ったものとして覚える。
 */
export function createDelivered(): { restore(state: WireState, bodies: BodyGeometry[]): ServerState } {
  let log: ServerState['log'] | null = null
  let sketches = new Map<string, SketchState>()
  const faces = new Map<string, Map<number, FaceGeometry>>()

  function restore(state: WireState, bodies: BodyGeometry[]): ServerState {
    if (state.log === undefined) {
      if (log === null) throw new DeliveryMismatch('受け取っていない操作ログを省かれた')
      state.log = log
    } else {
      log = state.log
    }
    state.sketches = state.sketches.map((sketch) => {
      if (!('unchanged' in sketch)) return sketch
      const known = sketches.get(sketch.sketch_id)
      if (known === undefined) throw new DeliveryMismatch(`受け取っていないスケッチを省かれた: ${sketch.sketch_id}`)
      return known
    })
    sketches = new Map((state.sketches as SketchState[]).map((sketch) => [sketch.sketch_id, sketch]))
    for (const body of bodies) {
      if (!body.geometry) {
        faces.delete(body.body_id)
        continue
      }
      const known = faces.get(body.body_id)
      const restored = body.geometry.faces.map((face: FaceGeometry | OmittedFace) => {
        if ('positions' in face) return face
        const previous = known?.get(face.index)
        if (previous === undefined || previous.serial !== face.serial) {
          throw new DeliveryMismatch(`受け取っていない面を省かれた: ${body.body_id} の面 ${face.index}`)
        }
        // 同じ三角形(配列もそのまま使い回す。`bodyMesh.ts` が配列ごとに境界線を覚えている)
        return previous.detail === face.detail ? previous : { ...previous, detail: face.detail }
      })
      body.geometry.faces = restored
      faces.set(body.body_id, new Map(restored.map((face) => [face.index, face])))
    }
    const alive = new Set(state.bodies.map((body) => body.body_id))
    for (const bodyId of [...faces.keys()]) if (!alive.has(bodyId)) faces.delete(bodyId)
    return state as ServerState
  }

  return { restore }
}
