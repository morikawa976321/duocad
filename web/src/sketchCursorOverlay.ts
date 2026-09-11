/**
 * スケッチの点入力待ちのカーソル強調(Fusion 360 の十字・正方形と同型)。
 *
 * **DOMオーバーレイ**(`viewCubeOverlay` は同一canvasの第2パスだが、こちらは常に画面水平の
 * 線を描くのでHTML/SVG)。`pointer-events: none` でクリックは下のcanvasへ通す。
 */
import { SKETCH_CURSOR_CROSS_COLOR, SKETCH_CURSOR_SNAP_COLOR } from './viewportConstants'

/** スナップ状態に応じた見た目の種別。 */
export type SketchSnapVisual = 'free' | 'line' | 'midpoint' | 'point' | 'grid'

/** 1mm格子への吸着とみなす距離[mm]。`App.svelte` の `snappedToGrid1mm` と同じ刻み。 */
export const GRID_SNAP_TOLERANCE_MM = 0.5

const CROSS_ARM_PX = 8
const CROSS_STROKE_PX = 1.5
const SQUARE_HALF_PX = 5
const SQUARE_STROKE_PX = 1.5
const TICK_HALF_LENGTH_PX = 8
const TICK_STROKE_PX = 1.5

export interface SketchCursorOverlay {
  setActive(active: boolean): void
  /**
   * @param pointer 未スナップ時のカーソル位置(コンテナ内CSS px)。`snap === 'free'` だけ使う。
   * @param snapAt 確定位置の画面座標。吸着時は十字・正方形・ティックをここに置く。
   * @param tickAngleDeg `snap === 'midpoint'` のときだけ使う、線分と直交させるティックの
   *   画面上の角度(度)。呼び出し側(`viewport.ts`)が画面座標へ射影して求める ——
   *   このファイルは three.js を知らないので、(u,v) 空間の向きを自分では画面角度にできない。
   */
  update(
    pointer: { x: number; y: number } | null,
    snap: SketchSnapVisual,
    snapAt: { x: number; y: number } | null,
    tickAngleDeg?: number,
  ): void
  dispose(): void
}

/**
 * 当たり判定の結果から、カーソル強調の種別を決める。
 *
 * 優先順はクリックのスナップ(`snappedPlanePoint`)と同じ: 点(角・交点を含む) → 中点 →
 * 線 → 格子 → 自由。
 *
 * `isCorner` / `isMidpoint` は呼び出し側(`viewport.ts`)が渡す —— `hit.snapAt` /
 * `hit.midpoint` が立っているか(このファイルは three.js も entityKinds も知らない、
 * SVG描画だけの層に閉じてある)。長方形の残り3隅や直線どうしの交点のように、独立した
 * 点ではないが幾何的には点と同じ精度で吸い付く場所を、□ではなく×にしてしまわないため。
 * 中点はそれより1段低い優先度の別種別(□でも×でもなく、線分と直交するティック)。
 */
export function classifySketchSnap(
  hit: { point: string | null } | null,
  rawU: number,
  rawV: number,
  isCorner = false,
  isMidpoint = false,
): SketchSnapVisual {
  if (hit !== null && (hit.point !== null || isCorner)) return 'point'
  if (hit !== null && isMidpoint) return 'midpoint'
  if (hit !== null) return 'line'
  const du = Math.abs(rawU - Math.round(rawU))
  const dv = Math.abs(rawV - Math.round(rawV))
  if (du < GRID_SNAP_TOLERANCE_MM && dv < GRID_SNAP_TOLERANCE_MM) return 'grid'
  return 'free'
}

export function createSketchCursorOverlay(container: HTMLElement): SketchCursorOverlay {
  const root = document.createElement('div')
  root.className = 'sketch-cursor-overlay'
  Object.assign(root.style, {
    position: 'absolute',
    inset: '0',
    pointerEvents: 'none',
    overflow: 'hidden',
    display: 'none',
    zIndex: '2',
  })

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('width', '100%')
  svg.setAttribute('height', '100%')
  root.appendChild(svg)

  const crossGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g')
  const squareGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g')
  const tickGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g')
  svg.appendChild(crossGroup)
  svg.appendChild(squareGroup)
  svg.appendChild(tickGroup)

  const crossH = document.createElementNS('http://www.w3.org/2000/svg', 'line')
  const crossV = document.createElementNS('http://www.w3.org/2000/svg', 'line')
  for (const line of [crossH, crossV]) {
    line.setAttribute('stroke-width', String(CROSS_STROKE_PX))
    line.setAttribute('stroke-linecap', 'square')
    crossGroup.appendChild(line)
  }

  const square = document.createElementNS('http://www.w3.org/2000/svg', 'rect')
  square.setAttribute('fill', 'none')
  square.setAttribute('stroke-width', String(SQUARE_STROKE_PX))
  squareGroup.appendChild(square)

  const tick = document.createElementNS('http://www.w3.org/2000/svg', 'line')
  tick.setAttribute('stroke-width', String(TICK_STROKE_PX))
  tick.setAttribute('stroke-linecap', 'square')
  tick.setAttribute('x1', String(-TICK_HALF_LENGTH_PX))
  tick.setAttribute('y1', '0')
  tick.setAttribute('x2', String(TICK_HALF_LENGTH_PX))
  tick.setAttribute('y2', '0')
  tickGroup.appendChild(tick)

  container.appendChild(root)

  let active = false

  function setCross(x: number, y: number, rotated: boolean, color: string): void {
    crossGroup.setAttribute('transform', `translate(${x},${y}) rotate(${rotated ? 45 : 0})`)
    crossH.setAttribute('x1', String(-CROSS_ARM_PX))
    crossH.setAttribute('y1', '0')
    crossH.setAttribute('x2', String(CROSS_ARM_PX))
    crossH.setAttribute('y2', '0')
    crossV.setAttribute('x1', '0')
    crossV.setAttribute('y1', String(-CROSS_ARM_PX))
    crossV.setAttribute('x2', '0')
    crossV.setAttribute('y2', String(CROSS_ARM_PX))
    crossH.setAttribute('stroke', color)
    crossV.setAttribute('stroke', color)
  }

  function setSquare(x: number, y: number, color: string): void {
    const size = SQUARE_HALF_PX * 2
    square.setAttribute('x', String(-SQUARE_HALF_PX))
    square.setAttribute('y', String(-SQUARE_HALF_PX))
    square.setAttribute('width', String(size))
    square.setAttribute('height', String(size))
    square.setAttribute('stroke', color)
    squareGroup.setAttribute('transform', `translate(${x},${y})`)
  }

  /** 線分と直交する短いティック。`angleDeg` は線分そのものの画面角度(直交させて描く)。 */
  function setTick(x: number, y: number, angleDeg: number, color: string): void {
    tickGroup.setAttribute('transform', `translate(${x},${y}) rotate(${angleDeg + 90})`)
    tick.setAttribute('stroke', color)
  }

  function hideMarkers(): void {
    crossGroup.style.display = 'none'
    squareGroup.style.display = 'none'
    tickGroup.style.display = 'none'
  }

  return {
    setActive(on) {
      active = on
      root.style.display = on ? 'block' : 'none'
      if (!on) hideMarkers()
    },
    update(pointer, snap, snapAt, tickAngleDeg) {
      if (!active) {
        hideMarkers()
        return
      }
      if (snap === 'free') {
        if (pointer === null) {
          hideMarkers()
          return
        }
        hideMarkers()
        crossGroup.style.display = ''
        setCross(pointer.x, pointer.y, false, SKETCH_CURSOR_CROSS_COLOR)
        return
      }
      if (snapAt === null) {
        hideMarkers()
        return
      }
      hideMarkers()
      switch (snap) {
        case 'line':
          crossGroup.style.display = ''
          setCross(snapAt.x, snapAt.y, true, SKETCH_CURSOR_SNAP_COLOR)
          break
        case 'midpoint':
          if (tickAngleDeg === undefined) break // 呼び出し側の契約違反(角度が無い)。何も描かない
          tickGroup.style.display = ''
          setTick(snapAt.x, snapAt.y, tickAngleDeg, SKETCH_CURSOR_SNAP_COLOR)
          break
        case 'point':
          squareGroup.style.display = ''
          setSquare(snapAt.x, snapAt.y, SKETCH_CURSOR_SNAP_COLOR)
          break
        case 'grid':
          crossGroup.style.display = ''
          setCross(snapAt.x, snapAt.y, false, SKETCH_CURSOR_CROSS_COLOR)
          squareGroup.style.display = ''
          setSquare(snapAt.x, snapAt.y, SKETCH_CURSOR_SNAP_COLOR)
          break
      }
    },
    dispose() {
      root.remove()
    },
  }
}
