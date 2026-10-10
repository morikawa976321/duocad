/**
 * 自動テスト(scripts/smoke*.mjs)がブラウザの中を見るための穴 `window.__duocad`。常に生やす(ローカル専用)。
 * **キー名は1つも変えない**(`web/scripts/*.mjs` が読む)。下の `$effect` が `ctx` の getter を読み、値が変わるたびに組み直す。
 * **`installTestHooks` は App の `<script>` 最上位(コンポーネント初期化中)で呼ぶ。** `onMount` の中だと `$effect` の持ち主が変わる。
 */
import type { PickTarget, Point2, SelectableKind } from './commands/command'
import type { DebugFlags } from './debugFlags'
import { emptyDebugCounts } from './optimizeDebug'
import type { OptimizeNotice } from './optimizeNotice'
import type {
  Applied,
  BasePlaneName,
  ConnectionStatus,
  DocumentInfo,
  EdgesPayload,
  FilePayload,
  MeasurePayload,
  OptimizeConditionsReport,
  OptimizeDebugPayload,
  OptimizeProgress,
  OptimizeResolutionPayload,
  ServerState,
  ShapeRef,
  SketchState,
} from './session'
import type { VertexMarker, Viewport } from './viewport'

export interface TestHookContext {
  viewport: () => Viewport | null
  status: () => ConnectionStatus
  model: () => ServerState | null
  partBodies: () => ServerState['bodies']
  selectedFaces: () => ShapeRef[]
  previewing: () => boolean
  showLocalGhost: (verb: string, args: Record<string, unknown>) => void
  lastApplied: () => Applied | null
  messages: () => string[]
  errorLog: () => string[]
  fov: () => number
  applyFov: (deg: number) => void
  stateEpoch: () => number
  optimizeMode: () => boolean
  modeKind: () => string
  wantedKinds: () => SelectableKind[]
  optimizeRunning: () => boolean
  optimizeCancelling: () => boolean
  optimizeConditions: () => OptimizeConditionsReport | null
  optimizeResolution: () => OptimizeResolutionPayload | null
  optimizeElementSizeMm: () => number | null
  optimizeProgress: () => OptimizeProgress | null
  optimizeNotice: () => OptimizeNotice | null
  debugFlags: DebugFlags
  optimizeDebugSpace: () => OptimizeDebugPayload | null
  optimizeDebugRho: () => OptimizeDebugPayload | null
  optimizeDebugAnchors: () => OptimizeDebugPayload | null
  sketches: () => SketchState[]
  sketchId: () => string | null
  hoverPoint: () => Point2 | null
  wantsBasePlanes: () => boolean
  wantsRegions: () => boolean
  edges: () => Record<string, EdgesPayload>
  edgeTargetBody: () => string | null
  wantsEdges: () => boolean
  wantedPick: () => PickTarget | null
  vertexCandidateBody: () => string | null
  selectedEntities: () => string[]
  vertexMarkers: () => VertexMarker[]
  measurement: () => MeasurePayload | null
  wantedVertices: () => boolean
  lastFile: () => FilePayload | null
  documentList: () => DocumentInfo[]
  currentDocumentName: () => string | null
}

export function installTestHooks(ctx: TestHookContext): void {
  $effect(() => {
    const viewport = ctx.viewport
    const model = ctx.model()
    const firstFace = ctx.selectedFaces()[0] ?? null
    const edgeTargetBody = ctx.edgeTargetBody()
    const measurement = ctx.measurement()
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: ctx.status() === 'open' && model !== null,
      status: ctx.status(),
      state: model,
      parts: ctx.partBodies(),
      getGeometry: (bodyId: string) => viewport()?.getGeometry(bodyId) ?? null,
      // 選択の正はコア。ここは共有集合の写し。
      selection: model?.selection ?? null,
      selected: firstFace === null ? null : { bodyId: firstFace.body, faceIndex: firstFace.index },
      previewing: ctx.previewing(),
      // いま出ている幽霊の中身。`previewing` はサーバ投機実行側だけを表すので、ローカル方式はこちらを見る。
      ghostInfo: () => viewport()?.ghostInfo() ?? { sketch: [], bodies: [] },
      // 幽霊を外から1回置く穴。移動2verbの `mode="rotate"` は UI がまだ引数を組めない(軸・中心の指し方が未決。
      // `duocad/catalog.py` の note)ので、ツールバーからは飛ばない。製品の分岐からは呼ばない。
      showLocalGhost: (verb: string, args: Record<string, unknown>) => ctx.showLocalGhost(verb, args),
      lastApplied: ctx.lastApplied(),
      messages: ctx.messages(),
      // エラーはトーストで消えるので控えを渡す。画面に出ている札の文言は `#toasts` の DOM が正。
      errors: ctx.errorLog(),
      projectFace: (bodyId: string, faceIndex: number) => viewport()?.project(bodyId, faceIndex) ?? null,
      frameAll: () => viewport()?.frameAll(),
      // 文書の状態ではないので、叩いてもコアへは何も飛ばない。
      fov: ctx.fov(),
      setFov: (deg: number) => ctx.applyFov(deg),
      // 断面(issue #6)。`handle` は画面座標なので、実機検査は決め打ちせずここから読んでドラッグする。
      section: () => viewport()?.sectionInfo() ?? null,
      // カメラの姿勢と世界座標の投影先。`projectFace` は遮蔽された面に null を返すので、
      // 見えていない側を含む比較(上面と下面のどちらが画面で上か)には使えない。
      cameraPose: () => viewport()?.cameraPose() ?? null,
      // ビューキューブの画面矩形。右上アンカーで寸法が変わると動くので、決め打ちせずここから読む。
      viewCubeRect: () => viewport()?.viewCubeRect() ?? null,
      // 基準平面のピック範囲の今の半幅[mm](`viewport.gridScale`)。
      gridScale: () => viewport()?.gridScale() ?? null,
      // グリッド3段(1/10/100mm)の今の不透明度と、出している平面・面外の軸。
      gridTiers: () => viewport()?.gridTiers() ?? null,
      // 可変解像度の目標偏差と分割数(見た目の滑らかさは目でしか分からない)。
      viewResolution: () => viewport()?.viewResolution() ?? null,
      // 状態配信を受けた回数。測り直しの往復が終わったかを外から見る唯一の手段。
      stateEpoch: ctx.stateEpoch(),
      // face単位の高精度化(視錐台+バックフェイスカリング)を切る/入れる。`smoke-face-culling.mjs` が導入前後を同じ走行で比べる。
      setFaceCulling: (enabled: boolean) => viewport()?.setFaceCulling(enabled),
      sketchPolylineCounts: () => viewport()?.sketchPolylineCounts() ?? null,
      projectPoint: (point: [number, number, number]) => viewport()?.projectPoint(point) ?? null,
      // F: 形状最適化。配りものそのものは `state.optimize` にある。
      optimizeMode: ctx.optimizeMode(),
      // 今のモード(`'idle' | 'sketch-pending' | 'sketch' | 'optimize'`)。`optimizeMode` と `activeSketch`
      // だけでは貼る先待ちが通常モードと同じ見え方になる。
      mode: ctx.modeKind(),
      wantedKinds: ctx.wantedKinds(),
      faceColor: (bodyId: string, faceIndex: number) => viewport()?.faceColor({ bodyId, faceIndex }) ?? null,
      optimizeAnnotationCount: () => viewport()?.optimizeAnnotationCount() ?? 0,
      // H: 走らせる側(どれも読むだけ)。
      optimizeRunning: ctx.optimizeRunning(),
      optimizeCancelling: ctx.optimizeCancelling(),
      optimizeConditions: ctx.optimizeConditions(),
      optimizeResolution: ctx.optimizeResolution(),
      optimizeElementSizeMm: ctx.optimizeElementSizeMm(),
      optimizeProgress: ctx.optimizeProgress(),
      optimizeNotice: ctx.optimizeNotice(),
      optimizeAnnotations: () => viewport()?.optimizeAnnotations() ?? { fixtures: [], loads: [] },
      // H2: 開発用のデバッグ表示(読むだけ。フラグが無ければ全部0)。画面にはフラグの状態を出さないので、
      // 検査はここと `location.search` で確かめる。
      debugFlags: ctx.debugFlags,
      // サーバから届いた生のまま(頂点の配列は数百KBなので外す)。`$state.snapshot` を通す: 素のスプレッドだと
      // 入れ子が `{}` になって外へ出る(puppeteer の `evaluate` は Proxy をたどらない。2026-09-05実測: `grid` が空)。
      optimizeDebugPayloads: () => {
        const strip = (payload: OptimizeDebugPayload | null, ...drop: string[]) => {
          if (payload === null) return null
          const copy = $state.snapshot(payload) as Record<string, unknown>
          for (const key of drop) delete copy[key]
          if (copy.fixed) delete (copy.fixed as Record<string, unknown>).points
          if (copy.load) delete (copy.load as Record<string, unknown>).points
          return copy
        }
        return {
          space: strip(ctx.optimizeDebugSpace(), 'elements'),
          rho: strip(ctx.optimizeDebugRho(), 'elements'),
          anchors: strip(ctx.optimizeDebugAnchors()),
        }
      },
      optimizeDebugCounts: () => viewport()?.optimizeDebugCounts() ?? emptyDebugCounts(),
      sketches: ctx.sketches(),
      activeSketch: ctx.sketchId(),
      sketch: ctx.sketches().find((item) => item.sketch_id === ctx.sketchId()) ?? null,
      hoverPoint: ctx.hoverPoint(),
      // 第2引数でスケッチを指定すると通常モードでも射影できる。コマンドを移るとツールバーの高さが変わり
      // 画素は持ち越せないので、同じ世界座標を指し直すのに使う(`smoke-regions`)。
      projectSketchPoint: (u: number, v: number, targetSketchId?: string) => {
        const target = targetSketchId ?? ctx.sketchId()
        return target === null ? null : (viewport()?.projectSketchPoint(target, u, v) ?? null)
      },
      // 指せる状態(スケッチのコマンド起動中)でしか座標を返さない。
      wantsBasePlanes: ctx.wantsBasePlanes(),
      projectBasePlane: (name: BasePlaneName) => viewport()?.projectBasePlane(name) ?? null,
      // 第2引数を渡せば通常モードでも射影できる(スケッチモード外には「今のスケッチ」が無い)。指せる状態でなければ null。
      wantsRegions: ctx.wantsRegions(),
      projectRegion: (index: number, targetSketchId?: string) => {
        const target = targetSketchId ?? ctx.sketchId()
        return target === null ? null : (viewport()?.projectRegion(target, index) ?? null)
      },
      edges: edgeTargetBody === null ? null : (ctx.edges()[edgeTargetBody] ?? null),
      edgeTargetBody,
      wantsEdges: ctx.wantsEdges(),
      // 直接ピックの待ち受け(回転軸・回転中心)。
      wantedPick: ctx.wantedPick(),
      vertexCandidateBody: ctx.vertexCandidateBody(),
      selectedEntities: ctx.selectedEntities(),
      projectEdge: (bodyId: string, edgeIndex: number) =>
        viewport()?.projectEdge(bodyId, edgeIndex) ?? null,
      vertexMarkers: ctx.vertexMarkers(),
      // 測定(issue #10)。`$state.snapshot` で素のオブジェクトにして出す: プロキシのままだと
      // puppeteer の `evaluate` が値を持ち帰れず `{}` になる(実測)。
      measurement: measurement === null ? null : $state.snapshot(measurement),
      wantedVertices: ctx.wantedVertices(),
      measurementInfo: () => viewport()?.measurementInfo() ?? null,
      highlightColors: () => viewport()?.highlightColors() ?? null,
      // ホバー強調で今明るくしているもの(明度しか変えないので highlightColors では読めない)。
      hoveredRef: () => viewport()?.hoveredRef() ?? null,
      projectVertex: (bodyId: string, vertexIndex: number) =>
        viewport()?.projectVertex(bodyId, vertexIndex) ?? null,
      parameters: model?.parameters ?? {},
      redoCount: model?.redo_count ?? 0,
      lastFile: ctx.lastFile(),
      documentList: ctx.documentList(),
      currentDocumentName: ctx.currentDocumentName(),
    }
  })
}
