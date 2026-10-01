/**
 * 画面の見え方の設定(画角・原点ギズモ・断面)の持ち主。
 *
 * **どれも文書の状態ではない** —— コアに送らず(`session.*` を通らない)、操作ログにも残らず、
 * undo の対象にもならない。画角と原点ギズモはブラウザの localStorage に覚え(`viewSettings.ts`)、
 * 断面は**その場限り**(起動時と文書の差し替えで OFF。issue #6)。
 *
 * **ビューポートへは、値を変えたその場で流す**(`$effect` に任せない)。ビューポートは
 * `onMount` で初めてでき、`$state` ではないので、`$effect` で流すと「できる前に1回走って
 * 何も読まずに終わり、二度と走らない」形を踏む(Svelte 5 runes の罠)。初回の反映は `attach`。
 */
import { evaluateExpression } from './expression'
import type { BasePlaneName } from './session'
import { loadFov, loadOriginVisible, saveFov, saveOriginVisible } from './viewSettings'
import type { SectionState, Viewport } from './viewport'

export interface DisplaySettings {
  /** カメラの画角[度]。`FOV_ORTHO`(0)は完全正投影。 */
  readonly fov: number
  /** 常設の原点ギズモ(原点・3軸・3平面)を出しているか。 */
  readonly originVisible: boolean
  /** 断面表示。位置は平面の法線軸上の座標[mm]。 */
  readonly section: SectionState
  /** できたビューポートを受け取り、今の設定を流す(`onMount` から1回)。 */
  attach(viewport: Viewport): void
  /**
   * 設定からの変更。**その場で効かせて、そのまま覚える**(「適用」を挟まない)。
   *
   * **「戻る先の角度」を覚える仕組みは持たない**(2026-09-04本人決定でUIが2値になった)。
   * `window.__duocad.setFov(70)` のようにUIで選べない値を入れていても、正投影を入切すれば
   * `FOV_DEFAULT` へ落ちる。
   */
  applyFov(deg: number): void
  applyOriginVisible(visible: boolean): void
  toggleSection(on: boolean): void
  setSectionPlane(plane: BasePlaneName): void
  /** 式を評価して位置にする。評価できなければ `fail` へ理由を出して false。 */
  setSectionPosition(text: string): boolean
  setSectionInverted(inverted: boolean): void
  /** 断面を切る(文書が替わったとき)。 */
  closeSection(): void
}

export function createDisplaySettings(deps: { fail: (message: string) => void }): DisplaySettings {
  let viewport: Viewport | null = null
  let fov = $state(loadFov())
  let originVisible = $state(loadOriginVisible())
  let section = $state<SectionState>({ on: false, plane: 'XY', position: 0, inverted: false })

  function setSection(next: SectionState): void {
    section = next
    viewport?.setSection({ ...next })
  }

  /** 内容の外接箱の中心の、その平面の法線軸成分(ON にした瞬間・平面を替えた瞬間の位置)。 */
  function sectionCenter(plane: BasePlaneName): number {
    const center = viewport?.contentCenter() ?? null
    if (center === null) return 0
    return plane === 'XY' ? center[2] : plane === 'XZ' ? center[1] : center[0]
  }

  return {
    get fov() {
      return fov
    },
    get originVisible() {
      return originVisible
    },
    get section() {
      return section
    },
    attach(next) {
      viewport = next
      next.setFov(fov)
      next.setOriginVisible(originVisible)
      next.setSection({ ...section })
      // 断面のハンドルをドラッグしたら位置だけを書き戻す(状態の正は `section`)
      next.onSectionDrag((position) => setSection({ ...section, position }))
    },
    applyFov(deg) {
      if (!Number.isFinite(deg)) return
      fov = deg
      saveFov(deg)
      viewport?.setFov(deg)
    },
    applyOriginVisible(visible) {
      originVisible = visible
      saveOriginVisible(visible)
      viewport?.setOriginVisible(visible)
    },
    toggleSection(on) {
      setSection(
        on ? { ...section, on: true, position: sectionCenter(section.plane) } : { ...section, on: false },
      )
    },
    setSectionPlane(plane) {
      setSection({ ...section, plane, position: sectionCenter(plane) })
    },
    setSectionPosition(text) {
      const evaluated = evaluateExpression(text)
      if (!evaluated.ok) {
        deps.fail(`断面の位置: ${evaluated.reason}`)
        return false
      }
      setSection({ ...section, position: evaluated.value })
      return true
    },
    setSectionInverted(inverted) {
      setSection({ ...section, inverted })
    },
    closeSection() {
      setSection({ ...section, on: false })
    },
  }
}
