/**
 * 開発用のデバッグ表示 —— 塊(`?debug=space`)・平滑化前のボクセル(`?voxel=1`)・
 * 面アンカーの節点(`?debug=anchors`)。**既定(`setState(null)`)では子が1つも生まれない。**
 *
 * 出所と規約は `duocad/optimize_run.py` の `_DebugSink`。
 */
import * as THREE from 'three'

import {
  DEBUG_ANCHOR_FIXED_COLOR,
  DEBUG_ANCHOR_LOAD_COLOR,
  DEBUG_RHO_COLOR,
  DEBUG_SPACE_COLOR,
  LAYER,
} from './viewportConstants'
import type { OptimizeDebugCounts, OptimizeDebugState, OptimizeDebugVoxels } from './viewportTypes'

/** 何も出していないときの数(`counts()` の戻り値の形。`hiddenBodyId` は実機検査フックの既定にだけ要る)。 */
export function emptyDebugCounts(): OptimizeDebugCounts {
  return { space: 0, rho: 0, fixed: 0, load: 0, spaceStride: 1, rhoStride: 1, anchorsDepthTest: null, hiddenBodyId: null }
}

/** 本編(`viewport.ts`)から借りるもの。 */
export interface OptimizeDebugDeps {
  /** 結果ボディの表示切り替え(`?voxel=1` は階段を見せるために結果を隠す)。 */
  setBodyVisible(bodyId: string, visible: boolean): void
}

export interface OptimizeDebug {
  /** デバッグ表示を入れるグループ。`scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  setState(state: OptimizeDebugState | null): void
  clear(): void
  /** 隠している結果ボディの表示を当て直す(`update()` が作り直した後にも要る)。 */
  applyHidden(): void
  /** 実機検査フック専用。 */
  counts(): OptimizeDebugCounts
}

export function createOptimizeDebug(deps: OptimizeDebugDeps): OptimizeDebug {
  const debugRoot = new THREE.Group()
  let debugDisposables: Array<{ dispose(): void }> = []
  let debugCounts: Omit<OptimizeDebugCounts, 'hiddenBodyId'> = emptyDebugCounts()
  /** `?voxel=1` で隠している結果ボディ。**表示だけ**(文書には触れない)。 */
  let debugHiddenBodyId: string | null = null

  function clearOptimizeDebug(): void {
    debugRoot.clear()
    for (const item of debugDisposables) item.dispose()
    debugDisposables = []
    debugCounts = emptyDebugCounts()
  }

  /**
   * 要素の通し添字 → 立方体の `InstancedMesh`。
   *
   * **セル中心の規約**(正は `duocad/topopt/mesh.py` の `run_marching_cubes` の docstring): 要素 (i,j,k) の中心は
   * `origin + spacing*(i+0.5, j+0.5, k+0.5)`、その角の節点 (i,j,k) は `origin + spacing*(i,j,k)`。
   * **半要素ずらすと形が丸ごと動く**ので、ここが唯一の変換点である(サーバ側は素の添字しか送らない。
   * `session.ts` / `viewportTypes.ts` の型の注記はここを指す)。
   */
  function buildDebugVoxels(voxels: OptimizeDebugVoxels, material: THREE.Material): THREE.InstancedMesh {
    const [, ny, nz] = voxels.grid.shape
    const spacing = voxels.grid.spacing
    const [ox, oy, oz] = voxels.grid.origin
    const geometry = new THREE.BoxGeometry(spacing, spacing, spacing)
    const count = voxels.elements.length
    const mesh = new THREE.InstancedMesh(geometry, material, Math.max(count, 1))
    mesh.count = count
    const dummy = new THREE.Object3D()
    for (let i = 0; i < count; i += 1) {
      const flat = voxels.elements[i]
      const ex = Math.floor(flat / (ny * nz))
      const rest = flat - ex * ny * nz
      const ey = Math.floor(rest / nz)
      const ez = rest - ey * nz
      dummy.position.set(
        ox + spacing * (ex + 0.5),
        oy + spacing * (ey + 0.5),
        oz + spacing * (ez + 0.5),
      )
      dummy.updateMatrix()
      mesh.setMatrixAt(i, dummy.matrix)
    }
    mesh.instanceMatrix.needsUpdate = true
    // **指せない。** `frustumCulled` は既定のままでよい(`InstancedMesh` は
    // `computeBoundingSphere()` を自前で持つ)。
    mesh.computeBoundingSphere()
    // **`InstancedMesh` 自身も片付ける**(インスタンス行列のGPUバッファを持っている)。
    debugDisposables.push(geometry, material, mesh)
    return mesh
  }

  /**
   * 節点を球で描く。**深度検査を切って最前面**(`depthTest: false`)——
   * 形状の裏側にある節点(拘束面はたいていカメラの反対側)まで見えていないと、
   * 「見えているつもりで見えていない」になる。
   */
  function buildDebugNodes(
    points: number[],
    radius: number,
    color: number,
  ): THREE.InstancedMesh {
    const count = Math.floor(points.length / 3)
    const geometry = new THREE.SphereGeometry(radius, 12, 8)
    const material = new THREE.MeshLambertMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.6,
      depthTest: false,
      depthWrite: false,
    })
    const mesh = new THREE.InstancedMesh(geometry, material, Math.max(count, 1))
    mesh.count = count
    mesh.renderOrder = LAYER.debug
    const dummy = new THREE.Object3D()
    for (let i = 0; i < count; i += 1) {
      dummy.position.set(points[3 * i], points[3 * i + 1], points[3 * i + 2])
      dummy.updateMatrix()
      mesh.setMatrixAt(i, dummy.matrix)
    }
    mesh.instanceMatrix.needsUpdate = true
    mesh.computeBoundingSphere()
    debugDisposables.push(geometry, material, mesh)
    return mesh
  }

  /** `?voxel=1` で隠している結果ボディの表示を当て直す(`update()` が作り直した後にも要る)。 */
  function applyDebugHidden(): void {
    if (debugHiddenBodyId !== null) deps.setBodyVisible(debugHiddenBodyId, false)
  }

  function setOptimizeDebug(state: OptimizeDebugState | null): void {
    // 前回隠したボディを戻す(次の状態がまた隠すなら下でもう一度隠す)。
    if (debugHiddenBodyId !== null) deps.setBodyVisible(debugHiddenBodyId, true)
    debugHiddenBodyId = null
    clearOptimizeDebug()
    if (state === null) return

    if (state.space !== null && state.space.elements.length > 0) {
      // **半透明で薄く重ねる**(probe の `?debug=space` と同じ)。塊は「答えの周りの箱」
      // なので、不透明にすると中の形が1つも見えない。
      const mesh = buildDebugVoxels(
        state.space,
        new THREE.MeshLambertMaterial({
          color: DEBUG_SPACE_COLOR,
          transparent: true,
          opacity: 0.28,
          depthWrite: false,
        }),
      )
      mesh.renderOrder = LAYER.debug
      debugRoot.add(mesh)
      debugCounts.space = state.space.elements.length
      debugCounts.spaceStride = state.space.stride
    }

    if (state.rho !== null && state.rho.elements.length > 0) {
      // **平滑化する前のボクセルそのもの**(`?voxel=1`)。不透明で描く ——
      // marching cubes を通す前の階段が見えることがこの表示の目的である。
      const mesh = buildDebugVoxels(
        state.rho,
        new THREE.MeshLambertMaterial({ color: DEBUG_RHO_COLOR }),
      )
      debugRoot.add(mesh)
      debugCounts.rho = state.rho.elements.length
      debugCounts.rhoStride = state.rho.stride
    }

    if (state.anchors !== null) {
      // 球の半径は**格子の要素サイズ**に合わせる(節点の間隔が要素サイズなので、
      // 0.34要素なら隣とくっつかずに数えられる)。
      // **アンカー自身が持っている格子**を使う(サーバが必ず載せてくる)。
      // 塊や密度場を頼んでいないときでも同じ大きさになる。
      const radius = state.anchors.grid.spacing * 0.34
      const fixed = buildDebugNodes(state.anchors.fixed.points, radius, DEBUG_ANCHOR_FIXED_COLOR)
      const load = buildDebugNodes(state.anchors.load.points, radius, DEBUG_ANCHOR_LOAD_COLOR)
      debugRoot.add(fixed)
      debugRoot.add(load)
      debugCounts.fixed = fixed.count
      debugCounts.load = load.count
      // **材質から読み直す**(定数を写さない) —— 写すと、材質側だけ直したときに
      // 検査が古い値で合格し続ける。**両方の OR** にしてあるのは、片方だけ深度検査を
      // 戻した退化を見逃さないため。
      debugCounts.anchorsDepthTest =
        (fixed.material as THREE.Material).depthTest ||
        (load.material as THREE.Material).depthTest
    }

    debugHiddenBodyId = state.hiddenBodyId
    applyDebugHidden()
  }

  return {
    object: debugRoot,
    setState: setOptimizeDebug,
    clear: clearOptimizeDebug,
    applyHidden: applyDebugHidden,
    counts() {
      return { ...debugCounts, hiddenBodyId: debugHiddenBodyId }
    },
  }
}
