/**
 * ウィンドウ選択(左ドラッグ)の帯と、画面座標での包含判定。
 *
 * 矩形に完全包含されたものだけを選ぶ(交差判定はしない・向きも見ない。本人明言)。判定は画面座標で行い、奥行きは関係ない。
 * 何を候補にするか(`collectInBox`)はクリックのゲートと同じでなければならず、ピック統合と一緒に読む必要があるので本編に残す。
 */
import * as THREE from 'three'

export interface BandRect {
  left: number
  top: number
  right: number
  bottom: number
}

export interface SelectionBandDeps {
  container: HTMLElement
  /**
   * 世界座標 → コンテナ内の画素(カメラの裏だけ null。本編の `projectRaw`)。
   * 寸法は呼び出し側が1回の矩形選択の中でだけ読んで渡す(本編 `projectRaw` の注意)。
   */
  projectRaw(point: THREE.Vector3, width: number, height: number): { x: number; y: number } | null
}

export interface SelectionBand {
  rect(fromX: number, fromY: number, x: number, y: number): BandRect
  /** 帯を出す(= 矩形を引いている最中になる)。 */
  show(fromX: number, fromY: number, x: number, y: number): void
  hide(): void
  /** 引いていなければ、離したときの扱いはクリック。 */
  active(): boolean
  polylineWorldInside(worldPoints: readonly THREE.Vector3[], rect: BandRect, width: number, height: number): boolean
  /** 画素が矩形の中か(縁を含む)。 */
  pointInside(point: { x: number; y: number }, rect: BandRect): boolean
  /** そのオブジェクトの全頂点が矩形の中か(`keep` が偽の頂点は数えない。残る頂点が無ければ false)。 */
  objectInside(
    object: THREE.Object3D & { geometry: THREE.BufferGeometry },
    rect: BandRect,
    width: number,
    height: number,
    keep?: ((point: THREE.Vector3) => boolean) | null,
  ): boolean
  /** `positions`(xyz の並び。ボディの1面ぶん)の全頂点が矩形の中か(`objectInside` の頂点配列版)。 */
  positionsInside(
    positions: ArrayLike<number>,
    matrix: THREE.Matrix4,
    rect: BandRect,
    width: number,
    height: number,
    keep?: ((point: THREE.Vector3) => boolean) | null,
  ): boolean
  dispose(): void
}

export function createSelectionBand(deps: SelectionBandDeps): SelectionBand {
  const { container, projectRaw } = deps

  const bandElement = document.createElement('div')
  bandElement.style.position = 'absolute'
  bandElement.style.border = '1px solid #ffb454'
  bandElement.style.background = 'rgba(255, 180, 84, 0.12)'
  // クリックを吸わない: 吸うと `clickable()`(elementFromPoint)が canvas を見失う。
  bandElement.style.pointerEvents = 'none'
  bandElement.style.display = 'none'
  // 親を基準にするためだけの `relative`。`.viewport` の寸法指定は動かさない(styles.css の不変条件)。
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative'
  container.appendChild(bandElement)

  let boxActive = false

  function rect(fromX: number, fromY: number, x: number, y: number): BandRect {
    return {
      left: Math.min(fromX, x),
      right: Math.max(fromX, x),
      top: Math.min(fromY, y),
      bottom: Math.max(fromY, y),
    }
  }

  function show(fromX: number, fromY: number, x: number, y: number): void {
    boxActive = true
    const box = rect(fromX, fromY, x, y)
    bandElement.style.display = 'block'
    bandElement.style.left = `${box.left}px`
    bandElement.style.top = `${box.top}px`
    bandElement.style.width = `${box.right - box.left}px`
    bandElement.style.height = `${box.bottom - box.top}px`
  }

  function hide(): void {
    boxActive = false
    bandElement.style.display = 'none'
  }

  function inside(box: BandRect, point: { x: number; y: number }): boolean {
    return point.x >= box.left && point.x <= box.right && point.y >= box.top && point.y <= box.bottom
  }

  function polylineWorldInside(
    worldPoints: readonly THREE.Vector3[],
    box: BandRect,
    width: number,
    height: number,
  ): boolean {
    for (const point of worldPoints) {
      const screen = projectRaw(point, width, height)
      if (screen === null || !inside(box, screen)) return false
    }
    return true
  }

  /**
   * そのジオメトリの全頂点が矩形の中にあるか。1つでも外れたら即 false。
   * 早期打ち切りが要点: 27部品(2835面)の全三角形を毎回射影すると数十万点になるが、大半は1点目で落ちる。
   */
  function geometryInside(
    geometry: THREE.BufferGeometry,
    matrix: THREE.Matrix4,
    box: BandRect,
    width: number,
    height: number,
    keep: ((point: THREE.Vector3) => boolean) | null,
  ): boolean {
    const position = geometry.getAttribute('position')
    if (!position || position.count === 0) return false
    const world = new THREE.Vector3()
    // 断面で切られた頂点は数に入れない
    let kept = false
    for (let at = 0; at < position.count; at += 1) {
      world.set(position.getX(at), position.getY(at), position.getZ(at)).applyMatrix4(matrix)
      if (keep !== null && !keep(world)) continue
      kept = true
      const screen = projectRaw(world, width, height)
      if (screen === null || !inside(box, screen)) return false
    }
    return kept
  }

  /** `geometryInside` の頂点配列版(ボディをまとめたジオメトリの1面ぶん。`bodyMesh.ts`)。 */
  function positionsInside(
    positions: ArrayLike<number>,
    matrix: THREE.Matrix4,
    box: BandRect,
    width: number,
    height: number,
    keep: ((point: THREE.Vector3) => boolean) | null = null,
  ): boolean {
    const world = new THREE.Vector3()
    let kept = false
    for (let at = 0; at + 2 < positions.length; at += 3) {
      world.set(positions[at], positions[at + 1], positions[at + 2]).applyMatrix4(matrix)
      if (keep !== null && !keep(world)) continue
      kept = true
      const screen = projectRaw(world, width, height)
      if (screen === null || !inside(box, screen)) return false
    }
    return kept
  }

  function objectInside(
    object: THREE.Object3D & { geometry: THREE.BufferGeometry },
    box: BandRect,
    width: number,
    height: number,
    keep: ((point: THREE.Vector3) => boolean) | null = null,
  ): boolean {
    object.updateWorldMatrix(true, false)
    return geometryInside(object.geometry, object.matrixWorld, box, width, height, keep)
  }

  return {
    rect,
    show,
    hide,
    active: () => boxActive,
    polylineWorldInside,
    pointInside: (point, box) => inside(box, point),
    objectInside,
    positionsInside,
    dispose() {
      bandElement.remove()
    },
  }
}
