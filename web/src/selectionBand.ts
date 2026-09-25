/**
 * ウィンドウ選択(左ドラッグ)の帯と、画面座標での包含判定。
 *
 * **矩形に完全包含されたものだけを選ぶ**(交差判定はしない。本人明言。向きも見ない)。
 * 判定はすべて**画面座標**で行う —— 「囲った」は人が画面で見たとおりの意味であって、
 * 世界座標の箱ではない(奥行きは関係ない)。
 *
 * **何を候補にするか(`collectInBox`)は本編に残す** —— 候補の選び方はクリックのゲートと
 * 同じでなければならず(辺は辺を出している間だけ、領域は要求している間だけ …)、
 * ピック統合と一緒に読む必要がある。ここが持つのは帯の DOM と「全頂点が矩形の中か」だけ。
 */
import * as THREE from 'three'

export interface BandRect {
  left: number
  top: number
  right: number
  bottom: number
}

/** 本編(`viewport.ts`)から借りるもの。 */
export interface SelectionBandDeps {
  /** 帯を載せる要素(`.viewport`)。 */
  container: HTMLElement
  /**
   * 世界座標 → コンテナ内の画素(カメラの裏だけ null。本編の `projectRaw`)。
   * 寸法は呼び出し側が1回の矩形選択の中でだけ読んで渡す(本編 `projectRaw` の注意)。
   */
  projectRaw(point: THREE.Vector3, width: number, height: number): { x: number; y: number } | null
}

export interface SelectionBand {
  /** 押した位置 `(fromX, fromY)` から今の位置 `(x, y)` までの矩形(引いた向きは見ない)。 */
  rect(fromX: number, fromY: number, x: number, y: number): BandRect
  /** 帯を出す(= 矩形を引いている最中になる)。 */
  show(fromX: number, fromY: number, x: number, y: number): void
  /** 帯を消す(引いていない状態へ戻す)。 */
  hide(): void
  /** 矩形を引いている最中か(引いていなければ、離したときの扱いはクリック)。 */
  active(): boolean
  /** 世界座標の折れ線の全点が矩形の中か。 */
  polylineWorldInside(worldPoints: readonly THREE.Vector3[], rect: BandRect, width: number, height: number): boolean
  /** そのオブジェクトの全頂点が矩形の中か(`keep` が偽の頂点は数えない。残る頂点が無ければ false)。 */
  objectInside(
    object: THREE.Object3D & { geometry: THREE.BufferGeometry },
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
  // **クリックを吸わない**。吸うと `clickable()`(elementFromPoint)が canvas を見失い、
  // 射影の穴が「その画素は取れない」と答え始める。
  bandElement.style.pointerEvents = 'none'
  bandElement.style.display = 'none'
  // **大きさには影響しない**(position:absolute なのでフローの外)。親を基準にするために
  // だけ `relative` を入れる —— `.viewport` の寸法指定そのものは動かさない(styles.css の不変条件)。
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
   * そのジオメトリの**全頂点**が矩形の中にあるか。1つでも外れたらその時点で false。
   *
   * **早期に打ち切るのが要点**である。27部品(2835面)の全三角形を毎回最後まで見ると
   * ドラッグを離すたびに数十万点を射影することになるが、実際には矩形の外にある面が
   * ほとんどで、1点目で落ちる。
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
    // 断面で切られた頂点は数に入れない(残る頂点が1つも無ければ見えていない)
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
    objectInside,
    dispose() {
      bandElement.remove()
    },
  }
}
