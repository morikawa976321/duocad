/**
 * 測定(issue #10)の装飾 —— 測った2点を結ぶ線・両端の点・数値の札。**指せない**
 * (ピックの対象に足さない)。`viewport.showMeasurement` が唯一の書き手。
 *
 * **深度検査をしない**(奥にあっても見える)—— 点から面へ下ろした垂線の足は、面の裏側や
 * ソリッドの内側に落ちうる。隠すと「どこまでの距離か」が読めない。
 */
import * as THREE from 'three'

import { makeTextSprite } from './textSprite'
import { LAYER, MEASURE_COLOR, MEASURE_TEXT_COLOR } from './viewportConstants'
import type { MeasurementVisual } from './viewportTypes'

/** 本編(`viewport.ts`)から借りるもの。 */
export interface MeasureAnnotationDeps {
  /** 内容の外接直方体の対角[mm]。札の大きさをこれへ追随させる。 */
  contentDiagonal(): number
}

export interface MeasureAnnotation {
  /** 装飾を入れるグループ。`scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  show(visual: MeasurementVisual | null): void
  info(): MeasurementVisual | null
  dispose(): void
}

export function createMeasureAnnotation(deps: MeasureAnnotationDeps): MeasureAnnotation {
  const root = new THREE.Group()
  const lineMaterial = new THREE.LineBasicMaterial({
    color: MEASURE_COLOR,
    depthTest: false,
    depthWrite: false,
    transparent: true,
  })
  const endMaterial = new THREE.PointsMaterial({
    color: MEASURE_COLOR,
    size: 8,
    sizeAttenuation: false,
    depthTest: false,
    depthWrite: false,
  })
  let current: MeasurementVisual | null = null
  let disposables: Array<{ dispose(): void }> = []

  function clear(): void {
    for (const child of [...root.children]) root.remove(child)
    for (const item of disposables) item.dispose()
    disposables = []
    current = null
  }

  function show(visual: MeasurementVisual | null): void {
    clear()
    if (visual === null || visual.from.length < 3 || visual.to.length < 3) return
    const from = new THREE.Vector3(visual.from[0], visual.from[1], visual.from[2])
    const to = new THREE.Vector3(visual.to[0], visual.to[1], visual.to[2])

    const lineGeometry = new THREE.BufferGeometry().setFromPoints([from, to])
    const line = new THREE.Line(lineGeometry, lineMaterial)
    line.renderOrder = LAYER.measure
    root.add(line)
    disposables.push(lineGeometry)

    const endGeometry = new THREE.BufferGeometry().setFromPoints([from, to])
    const ends = new THREE.Points(endGeometry, endMaterial)
    ends.renderOrder = LAYER.measure
    root.add(ends)
    disposables.push(endGeometry)

    // 札はモデルの大きさに追随させる(荷重の札と同じ。空の文書は測れないので 0 は来ない)。
    const height = Math.max(deps.contentDiagonal(), 1e-6) * 0.04
    const { sprite, disposables: spriteDisposables } = makeTextSprite(visual.label, height, MEASURE_TEXT_COLOR)
    sprite.position.copy(from).add(to).multiplyScalar(0.5)
    sprite.renderOrder = LAYER.measure
    root.add(sprite)
    disposables.push(...spriteDisposables)

    current = { from: [...visual.from], to: [...visual.to], label: visual.label }
  }

  return {
    object: root,
    show,
    info() {
      return current === null ? null : { from: [...current.from], to: [...current.to], label: current.label }
    },
    dispose() {
      clear()
      lineMaterial.dispose()
      endMaterial.dispose()
    },
  }
}
