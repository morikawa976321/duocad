/**
 * 自動テスト(scripts/smoke*.mjs)がブラウザの中を見るための穴 `window.__duocad`。常に生やす(ローカル専用)。
 * **キー名は1つも変えない**(`web/scripts/*.mjs` が読む)。読む本の無いキーは置かない(足すときは読む本と一緒に)。下の `$effect` が `ctx` の getter を読み、値が変わるたびに組み直す。
 * **`installTestHooks` は App の `<script>` 最上位(コンポーネント初期化中)で呼ぶ。** `onMount` の中だと `$effect` の持ち主が変わる。
 */
import type { PickTarget, Point2 } from './commands/command'
import type { Applied, BasePlaneName, ConnectionStatus, EdgesPayload, ServerState, SketchState } from './session'
import type { Viewport } from './viewport'

export interface TestHookContext {
  viewport: () => Viewport | null
  status: () => ConnectionStatus
  model: () => ServerState | null
  partBodies: () => ServerState['bodies']
  previewing: () => boolean
  lastApplied: () => Applied | null
  messages: () => string[]
  errorLog: () => string[]
  stateEpoch: () => number
  modeKind: () => string
  /** コマンド起動中の素のクリックがトグルになるか(`App.svelte` の `commandClickToggles`)。 */
  clickToggles: () => boolean
  sketches: () => SketchState[]
  sketchId: () => string | null
  hoverPoint: () => Point2 | null
  wantsBasePlanes: () => boolean
  edges: () => Record<string, EdgesPayload>
  edgeTargetBody: () => string | null
  wantsEdges: () => boolean
  wantedPick: () => PickTarget | null
  selectedEntities: () => string[]
}

export function installTestHooks(ctx: TestHookContext): void {
  $effect(() => {
    const viewport = ctx.viewport
    const model = ctx.model()
    const edgeTargetBody = ctx.edgeTargetBody()
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: ctx.status() === 'open' && model !== null,
      state: model,
      parts: ctx.partBodies(),
      getGeometry: (bodyId: string) => viewport()?.getGeometry(bodyId) ?? null,
      // 選択の正はコア。ここは共有集合の写し。
      selection: model?.selection ?? null,
      // 作業空間(起動中のコマンド・モード)もコアが正(issue #153)。画面はこれに従う。
      command: model?.command ?? null,
      serverMode: model?.mode ?? null,
      previewing: ctx.previewing(),
      lastApplied: ctx.lastApplied(),
      messages: ctx.messages(),
      // エラーはトーストで消えるので控えを渡す。画面に出ている札の文言は `#toasts` の DOM が正。
      errors: ctx.errorLog(),
      projectFace: (bodyId: string, faceIndex: number) => viewport()?.project(bodyId, faceIndex) ?? null,
      frameAll: () => viewport()?.frameAll(),
      // カメラの姿勢と世界座標の投影先。`projectFace` は遮蔽された面に null を返すので、
      // 見えていない側を含む比較(上面と下面のどちらが画面で上か)には使えない。
      cameraPose: () => viewport()?.cameraPose() ?? null,
      // 可変解像度の目標偏差と分割数(見た目の滑らかさは目でしか分からない)。
      viewResolution: () => viewport()?.viewResolution() ?? null,
      // 状態配信を受けた回数。測り直しの往復が終わったかを外から見る唯一の手段。
      stateEpoch: ctx.stateEpoch(),
      // face単位の高精度化(視錐台+バックフェイスカリング)を切る/入れる。`smoke-face-culling.mjs` が導入前後を同じ走行で比べる。
      setFaceCulling: (enabled: boolean) => viewport()?.setFaceCulling(enabled),
      sketchPolylineCounts: () => viewport()?.sketchPolylineCounts() ?? null,
      projectPoint: (point: [number, number, number]) => viewport()?.projectPoint(point) ?? null,
      // 今のモード(`'idle' | 'sketch-pending' | 'sketch' | 'optimize'`)。`optimizeMode` と `activeSketch`
      // だけでは貼る先待ちが通常モードと同じ見え方になる。
      mode: ctx.modeKind(),
      clickToggles: ctx.clickToggles(),
      sketches: ctx.sketches(),
      activeSketch: ctx.sketchId(),
      sketch: ctx.sketches().find((item) => item.sketch_id === ctx.sketchId()) ?? null,
      hoverPoint: ctx.hoverPoint(),
      // 第2引数でスケッチを指定すると通常モードでも射影できる。コマンドを移るとツールバーの高さが変わり
      // 画素は持ち越せないので、同じ世界座標を指し直すのに使う。
      projectSketchPoint: (u: number, v: number, targetSketchId?: string) => {
        const target = targetSketchId ?? ctx.sketchId()
        return target === null ? null : (viewport()?.projectSketchPoint(target, u, v) ?? null)
      },
      // 指せる状態(スケッチのコマンド起動中)でしか座標を返さない。
      wantsBasePlanes: ctx.wantsBasePlanes(),
      projectBasePlane: (name: BasePlaneName) => viewport()?.projectBasePlane(name) ?? null,
      // 第2引数を渡せば通常モードでも射影できる(スケッチモード外には「今のスケッチ」が無い)。指せる状態でなければ null。
      projectRegion: (index: number, targetSketchId?: string) => {
        const target = targetSketchId ?? ctx.sketchId()
        return target === null ? null : (viewport()?.projectRegion(target, index) ?? null)
      },
      edges: edgeTargetBody === null ? null : (ctx.edges()[edgeTargetBody] ?? null),
      wantsEdges: ctx.wantsEdges(),
      // 直接ピックの待ち受け(回転軸・回転中心)。
      wantedPick: ctx.wantedPick(),
      selectedEntities: ctx.selectedEntities(),
      projectEdge: (bodyId: string, edgeIndex: number) =>
        viewport()?.projectEdge(bodyId, edgeIndex) ?? null,
      // 画面が知っているその辺の連なり(フィレット・面取りの間だけ。届いていなければ null)。
      edgeChain: (bodyId: string, edgeIndex: number) => viewport()?.edgeChain({ bodyId, edgeIndex }) ?? null,
      sketchRebuilds: () => viewport()?.sketchRebuilds() ?? 0,
      drawCalls: () => viewport()?.drawCalls() ?? null,
      renderCount: () => viewport()?.renderCount() ?? null,
      // ホバー強調で今明るくしているもの。
      hoveredRef: () => viewport()?.hoveredRef() ?? null,
      projectVertex: (bodyId: string, vertexIndex: number) =>
        viewport()?.projectVertex(bodyId, vertexIndex) ?? null,
    }
  })
}
