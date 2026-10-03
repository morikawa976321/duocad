/**
 * 自動テスト(scripts/smoke*.mjs)がブラウザの中を見るための穴 `window.__duocad`。
 * ローカル専用ツールなので常に生やしてある(「動くはず」で納品しないための道具であり、隠すと使えない)。
 *
 * **キー名は1つも変えない**(`web/scripts/*.mjs` が読む)。`ctx` は App の状態を読む getter の束で、
 * 下の `$effect` がそれを読むので、読んだ値が変わるたびに `window.__duocad` を組み直す。
 *
 * **`installTestHooks` は App の `<script>` の最上位(コンポーネントの初期化中)で呼ぶ。**
 * `onMount` の中で呼ぶと `$effect` の持ち主が変わる。
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
  /** 状態配信を受けた回数(`handleState` が増やすので App が持つ)。 */
  stateEpoch: () => number
  optimizeMode: () => boolean
  modeKind: () => string
  wantedKinds: () => SelectableKind[]
  optimizeRunning: () => boolean
  optimizeCancelling: () => boolean
  optimizeConditions: () => OptimizeConditionsReport | null
  optimizeResolution: () => OptimizeResolutionPayload | null
  optimizeStepIndex: () => number
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
    // 選択の先頭の面。**`selected` だけが使う**(互換のための穴)。
    const firstFace = ctx.selectedFaces()[0] ?? null
    const edgeTargetBody = ctx.edgeTargetBody()
    const measurement = ctx.measurement()
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: ctx.status() === 'open' && model !== null,
      status: ctx.status(),
      state: model,
      // ユーザーに見せる部品一覧(常に kind==="solid" だけ)
      parts: ctx.partBodies(),
      // 直近にviewportへ渡した生geometryを問い合わせる(正はviewportの描画に一本化してある)。
      getGeometry: (bodyId: string) => viewport()?.getGeometry(bodyId) ?? null,
      // **選択の正はコア**。ここは共有集合の写しであって、ブラウザ側の状態ではない。
      selection: model?.selection ?? null,
      // 互換のための穴: 選択のうち最初の面を `{bodyId, faceIndex}` で出す
      selected: firstFace === null ? null : { bodyId: firstFace.body, faceIndex: firstFace.index },
      previewing: ctx.previewing(),
      // いま出ている幽霊の中身(スケッチの図形 / ずらしたボディ)。**実機検査が
      // 「値を打ったら幽霊が追随したか」「Escで消えたか」を数値で見るための穴**。
      // `previewing` はサーバ投機実行の側だけを表すので、ローカル方式はここを見る。
      ghostInfo: () => viewport()?.ghostInfo() ?? { sketch: [], bodies: [] },
      // **幽霊を外から1回置く穴**(`setFov` / `frameAll` と同じ「実機検査が叩く手」)。
      // 移動2verbの `mode="rotate"` は**まだUIが引数を組めない** —— 軸・中心をどう指すかが
      // 未決で(`duocad/catalog.py` の note)、`CommandDef` に回転の引数が無いため、
      // ツールバーからは `mode` が飛ばない。それでも幽霊の側は先に実装してあるので、
      // **正しさを確かめる術**をここに開ける。読むのは `ghostInfo()`。
      // **製品の分岐からは呼ばない**(`ghostInfo` と同じ規律)。
      showLocalGhost: (verb: string, args: Record<string, unknown>) => ctx.showLocalGhost(verb, args),
      lastApplied: ctx.lastApplied(),
      messages: ctx.messages(),
      // **エラーはトーストへ出る**(消える)ので、実機検査が
      // 「走行中にエラーが出ていないか」を最後に一度で見られるよう控えを渡す。
      // 今まさに画面に出ている札は `#toasts` のDOMが正 —— 文言の検証はそちらを見ること。
      errors: ctx.errorLog(),
      projectFace: (bodyId: string, faceIndex: number) => viewport()?.project(bodyId, faceIndex) ?? null,
      // 視点をシーン全体へ戻す。**2026-08-23に画面のボタンを削除**した後も、実機検査が
      // 図形を画面内へ収めるために呼ぶ手だけはここに残す。
      frameAll: () => viewport()?.frameAll(),
      // 画角。**実機検査が「FOVを変えてから frameAll して収まるか」を見るための穴**
      // (スライダーを本物で動かすのとは別に、値を直接置ける口が要る)。
      // 文書の状態ではないので、ここを叩いてもコアへは何も飛ばない。
      fov: ctx.fov(),
      setFov: (deg: number) => ctx.applyFov(deg),
      // 断面(issue #6)。`handle` は画面座標なので、実機検査は決め打ちせずここから読んでドラッグする。
      section: () => viewport()?.sectionInfo() ?? null,
      // **視点の向きそのものを見るための穴**(2026-08-28のZ-up化で足した)。カメラの姿勢と
      // 「この世界座標が画面のどこに写るか」を返す。`projectFace` では測れない ——
      // あちらは遮蔽された面に null を返すので、**上面と下面のどちらが画面で上か**のように
      // 見えていない側を含む比較ができない。どちらも読むだけで、コアへは何も飛ばない。
      cameraPose: () => viewport()?.cameraPose() ?? null,
      // ビューキューブ(常設・26領域)の画面矩形。**実機検査がキューブの当たり判定とドラッグを
      // 本物の座標で叩くための穴**(タスク仕様の落とし穴3。右上アンカーなので寸法が変わると
      // 動く —— 決め打ちの座標を使わずここから読む)。
      viewCubeRect: () => viewport()?.viewCubeRect() ?? null,
      // 基準平面の**ピック範囲**の今の半幅[mm]。**実機検査が「指せる範囲がモデルの
      // 大きさへ広がった/縮んだ」を数値で見るための穴**(2026-08-30。`viewport.gridScale`)。
      gridScale: () => viewport()?.gridScale() ?? null,
      // グリッドの3段(1/10/100mm)の今の不透明度と、出している平面・面外の軸。
      // **実機検査が「段が連続にフェードする」「状態で平面が入れ替わる」を数値で見るための穴**
      // (2026-09-03。刻みが固定になり `gridScale().step` が何も語らなくなった置き換え)。
      gridTiers: () => viewport()?.gridTiers() ?? null,
      // 可変解像度。**実機検査が「寄ったら本当に細かくなったか」を数値で見るための穴**
      // (見た目の滑らかさは目でしか分からないので、代わりに目標偏差と分割数を出す)。
      viewResolution: () => viewport()?.viewResolution() ?? null,
      // 状態配信を受けた回数。**測り直しの往復が終わったかを外から見る唯一の手段**。
      stateEpoch: ctx.stateEpoch(),
      // face単位の高精度化(視錐台+バックフェイスカリング)を切る/入れる穴。
      // **導入前後を同じ走行の中で比べる**ために要る(`smoke-face-culling.mjs`)。
      setFaceCulling: (enabled: boolean) => viewport()?.setFaceCulling(enabled),
      sketchPolylineCounts: () => viewport()?.sketchPolylineCounts() ?? null,
      projectPoint: (point: [number, number, number]) => viewport()?.projectPoint(point) ?? null,
      // F: 形状最適化(第3のモード)。**実機検査がモードの出入りと`wantedKinds`(ボディ粒度)
      // を見るための穴** —— 配りものそのものは `state.optimize` にある。
      optimizeMode: ctx.optimizeMode(),
      // **今のモード**(`'idle' | 'sketch-pending' | 'sketch' | 'optimize'`。2026-09-03)。
      // `optimizeMode`(真偽)と `activeSketch`(ID or null)だけでは**貼る先待ち**が
      // 「通常モード」と同じ見え方になり、モードの遷移を外から検算できない。
      mode: ctx.modeKind(),
      wantedKinds: ctx.wantedKinds(),
      faceColor: (bodyId: string, faceIndex: number) => viewport()?.faceColor({ bodyId, faceIndex }) ?? null,
      optimizeAnnotationCount: () => viewport()?.optimizeAnnotationCount() ?? 0,
      // H: 走らせる側。**実機検査が「押せるか / 何反復目か / 何が通知されたか」を
      // 見るための穴**(どれも読むだけで、コアへは何も飛ばない)。
      optimizeRunning: ctx.optimizeRunning(),
      optimizeCancelling: ctx.optimizeCancelling(),
      optimizeConditions: ctx.optimizeConditions(),
      optimizeResolution: ctx.optimizeResolution(),
      optimizeStepIndex: ctx.optimizeStepIndex(),
      optimizeProgress: ctx.optimizeProgress(),
      optimizeNotice: ctx.optimizeNotice(),
      optimizeAnnotations: () => viewport()?.optimizeAnnotations() ?? { fixtures: [], loads: [] },
      // H2: 開発用のデバッグ表示。**読むだけ**の穴で、フラグを付けていなければ全部0。
      // 画面にフラグの状態を出さない(出すと「フラグ無しの画面と同じ」を壊す)ので、
      // 実機検査はここと `location.search` で確かめる。
      debugFlags: ctx.debugFlags,
      // **サーバから届いた生のまま**(面の添字・担当面積・間引く前の総数)。probe が
      // 画面の文字で出していた情報(`面アンカー: 固定=面3 20節点 / …`)の移し先で、
      // ここが無いと「何節点出たか」しか外から読めない。**頂点の配列は外す**(数百KB)。
      // **`$state.snapshot` を通す。** `$state` は入れ子まで Proxy にするので、
      // 素のスプレッドだと**入れ子のオブジェクトだけが `{}` になって外へ出る**
      // (puppeteer の `evaluate` は Proxy をたどらない。2026-09-05実測: `grid` が空だった)。
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
      // C: スケッチ。**実機検査が本物のクリックで描く**ための穴
      sketches: ctx.sketches(),
      activeSketch: ctx.sketchId(),
      hoverPoint: ctx.hoverPoint(),
      // **第2の呼び方**: スケッチを指定すると通常モードでも射影できる(領域の
      // `projectRegion` と同じ形)。**同じ世界座標の点を、画面の寸法が変わった後でも
      // もう一度指せる**ようにするための穴で、`smoke-regions` の「同じ場所が、要求中は
      // 領域・外すと面になる」がこれを使う(コマンドを移るとツールバーの高さが変わり、
      // 画素そのものは持ち越せない)。
      projectSketchPoint: (u: number, v: number, targetSketchId?: string) => {
        const target = targetSketchId ?? ctx.sketchId()
        return target === null ? null : (viewport()?.projectSketchPoint(target, u, v) ?? null)
      },
      // C: 基準平面。**実機検査が本物のクリックで貼る先を指す**ための穴
      // (指せる状態=スケッチのコマンドを起動している間しか座標を返さない)。
      wantsBasePlanes: ctx.wantsBasePlanes(),
      projectBasePlane: (name: BasePlaneName) => viewport()?.projectBasePlane(name) ?? null,
      // 領域。**第2引数を渡せば通常モードでも射影できる**(スケッチモードの外では
      // 「今のスケッチ」が無いので、どのスケッチの領域かを検査側が言う必要がある)。
      // 指せる状態でなければ null が返る(`viewport.projectRegion` が実際に当てて確かめる)。
      wantsRegions: ctx.wantsRegions(),
      projectRegion: (index: number, targetSketchId?: string) => {
        const target = targetSketchId ?? ctx.sketchId()
        return target === null ? null : (viewport()?.projectRegion(target, index) ?? null)
      },
      // D: 辺とフィーチャ。**実機検査が本物のクリックで辺を選ぶ**ための穴
      edges: edgeTargetBody === null ? null : (ctx.edges()[edgeTargetBody] ?? null),
      edgeTargetBody,
      wantsEdges: ctx.wantsEdges(),
      // **直接ピックの待ち受け**(回転軸・回転中心)。実機検査が「回転を選ぶと軸の辺を
      // 待つ」「指したら待ち受けが降りる」を外から読むための穴。
      wantedPick: ctx.wantedPick(),
      vertexCandidateBody: ctx.vertexCandidateBody(),
      selectedEntities: ctx.selectedEntities(),
      projectEdge: (bodyId: string, edgeIndex: number) =>
        viewport()?.projectEdge(bodyId, edgeIndex) ?? null,
      // 頂点。**実機検査が本物のクリックで頂点を外す**ための穴(出ていなければ null)。
      vertexMarkers: ctx.vertexMarkers(),
      // 測定(issue #10)。直近の結果と、3Dに出している線(出していなければ null)、
      // 頂点そのものを対象にしているか(候補の頂点が出て、辺より先に取れる)。
      // **`$state.snapshot` で素のオブジェクトにして出す** —— `$state` のプロキシのままだと
      // puppeteer の `evaluate` が値を持ち帰れず、実機検査が `{}` を読む(実測)。
      measurement: measurement === null ? null : $state.snapshot(measurement),
      wantedVertices: ctx.wantedVertices(),
      measurementInfo: () => viewport()?.measurementInfo() ?? null,
      // 選択の色そのものを読める穴。
      highlightColors: () => viewport()?.highlightColors() ?? null,
      // ホバー強調で今明るくしているもの。**「クリックすれば取れるもの」と実際に
      // クリックした結果が一致していること**を実機検査が確かめるための穴
      // (明度しか変えないので highlightColors では読めない)。
      hoveredRef: () => viewport()?.hoveredRef() ?? null,
      projectVertex: (bodyId: string, vertexIndex: number) =>
        viewport()?.projectVertex(bodyId, vertexIndex) ?? null,
      // E: パラメータ・履歴・ファイル。実機検査が「打ち直したら形が追随する」を見る穴
      parameters: model?.parameters ?? {},
      redoCount: model?.redo_count ?? 0,
      lastFile: ctx.lastFile(),
      documentList: ctx.documentList(),
      currentDocumentName: ctx.currentDocumentName(),
    }
  })
}
