/**
 * フィーチャのコマンド定義 — **AIからしか使えないverbが無くなる場所**。
 *
 * 領域押し出し・フィレット・面取り・矩形反復・円形反復の5つはコアにあり、ここは
 * `duocad/server.py` の `FEATURE_COMMANDS` に開いた口へ引数の集め方を書くだけである
 * (コアには何も足していない)。状態機械・数値入力・プレビュー・中断は
 * `CommandHost.svelte` の1つを使い回す。
 *
 * **対象集合はサーバへ送らない**。領域・辺は**コアの確定選択そのもの**で、ここが送るのは
 * 数値と選択肢だけ。どの辺を丸めるかはサーバ側の `Session` が自分の選択集合から取る
 * (`selection.require_*`)。選択引数の定義(`kind: 'selection'`)は残るが、**送るためでは
 * なく、人がまだ選んでいないことを画面に出し、辺のピッキングを起動するため**である
 * (`CommandHost` の `onWants`)。
 *
 * **図形(反復の対象)だけは選択集合から取れない**。コアの選択集合に入るのは面・辺・頂点・
 * 領域の4種で、スケッチの図形は入らない。スケッチパネルで選び、`entities` 引数として渡る。
 *
 * **反復にはプレビューを出さない**。押し出し・フィレット・面取りはサーバに投機実行させるが、
 * 反復の展開規則(全周は `360/count`、回転した長方形は4本の線分へ落とす、など)はコアが
 * 持っており、幽霊のためにブラウザへ二重実装すると「画面の予想と実際が違う」という最悪の
 * 食い違いを自作することになる。
 */
import type { ArgValues, CommandDef, Point2 } from './command'
import type { SelectionRef } from '../session'

/** 0以下を弾く(寸法として使えない)。**既定値を入れない**のがここの流儀。 */
function positive(label: string): (value: number) => string | null {
  return (value) => (value > 0 ? null : `${label}は0より大きい値`)
}

/** 1以上の整数だけを通す(個数。コアの `parameters.is_positive_int` と同じ判定)。 */
function count(label: string, least: number): (value: number) => string | null {
  return (value) => {
    if (!Number.isInteger(value)) return `${label}は整数`
    return value >= least ? null : `${label}は${least}以上`
  }
}

/**
 * 選択引数が1つでも空なら理由を返す(埋まっていれば null)。
 *
 * **値そのものは送らない。** 対象集合(面・辺・領域)はコアが自分の確定選択から取るので、
 * ここで見るのは「人がまだ選んでいない」を画面へ出すためだけである。
 */
function missingSelection(values: ArgValues, name: string, label: string): string | null {
  return ((values[name] ?? []) as SelectionRef[]).length === 0 ? `${label}が選ばれていない` : null
}

/**
 * 領域の押し出し。**手だけで板に穴を開けられるようになる1本**。
 *
 * `extrude_face`(面を押す)とは別のverbである。こちらはスケッチの領域を取り、
 * `join` で足し `cut` で引く。**向きを人に選ばせる**のは、既定の `join` に落ちると
 * 「切り取るつもりで足した」が黙って通り、しかも形が増えるので失敗に見えないため。
 */
export const EXTRUDE_REGIONS: CommandDef = {
  id: 'extrude',
  category: 'feature',
  label: '領域を押し出す',
  buttonId: 'extrude-regions',
  preview: 'server',
  args: [
    {
      kind: 'selection',
      name: 'regions',
      label: '押し出す領域',
      shapeKind: 'region',
      min: 1,
      max: null,
    },
    {
      kind: 'number',
      name: 'distance',
      label: '距離',
      unit: 'mm',
      inputId: 'regions-distance',
      placeholder: 'mm',
      reject: (value) => (value === 0 ? '距離が0(形が変わらない)' : null),
    },
    {
      kind: 'choice',
      name: 'mode',
      label: '向き',
      options: [
        { value: 'join', label: '結合', buttonId: 'extrude-mode-join' },
        { value: 'cut', label: '切り取り', buttonId: 'extrude-mode-cut' },
      ],
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'regions', '領域')
    const distance = values.distance
    const mode = values.mode
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof distance !== 'number') return { ok: false, reason: '距離が数値でない' }
    if (typeof mode !== 'string') return { ok: false, reason: '向きが選ばれていない' }
    return { ok: true, args: { distance, mode } }
  },
}

/**
 * フィレット。**辺を複数選べることが前提**(複数選択と辺のピッキングの両方が要る)。
 *
 * 半径は1つで全部の辺に効く(コアは辺ごとのリストも取るが、UIから辺ごとに違う半径を
 * 打たせる場面はまだ無い。要るようになったら選択パネル側に付ける)。
 *
 * **失敗の扱いは骨格の外にある**: OCCTのフィレットは例外を投げずに `IsDone()=False` で
 * 失敗し、しかも半径に対して単調でない(§7。r=10.0 失敗 / r=10.5 成功)。コアが例外へ
 * 変換し、サーバが理由を返し、`App.svelte` が**選択を保持したまま打った値を戻す**。
 */
export const FILLET: CommandDef = {
  id: 'fillet',
  category: 'feature',
  label: '丸める',
  buttonId: 'fillet',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'edges', label: '丸める辺', shapeKind: 'edge', min: 1, max: null },
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'fillet-radius',
      placeholder: 'mm',
      reject: positive('半径'),
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'edges', '辺')
    const radius = values.radius
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof radius !== 'number') return { ok: false, reason: '半径が数値でない' }
    return { ok: true, args: { radius } }
  },
}

/** 面取り。フィレットと同じ集め方(まっすぐな辺は平面・円い辺は円錐になる)。 */
export const CHAMFER: CommandDef = {
  id: 'chamfer',
  category: 'feature',
  label: '面取り',
  buttonId: 'chamfer',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'edges', label: '面取りする辺', shapeKind: 'edge', min: 1, max: null },
    {
      kind: 'number',
      name: 'distance',
      label: '長さ',
      unit: 'mm',
      inputId: 'chamfer-distance',
      placeholder: 'mm',
      reject: positive('長さ'),
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'edges', '辺')
    const distance = values.distance
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof distance !== 'number') return { ok: false, reason: '長さが数値でない' }
    return { ok: true, args: { distance } }
  },
}

/**
 * 矩形反復。`nx`/`ny` は**元を含めた個数**(Fusion360と同じ流儀・コアの契約)。
 *
 * `sketch_id` はここでは付けない —— どのスケッチかは「今どのスケッチモードに居るか」で
 * あって、コマンドが集める引数ではない(`App.svelte` が送信時に足す)。
 */
export const PATTERN_RECTANGULAR: CommandDef = {
  id: 'pattern_rectangular',
  category: 'feature',
  label: '矩形反復',
  buttonId: 'pattern-rectangular',
  preview: 'none',
  args: [
    { kind: 'entities', name: 'entities', label: '反復する図形', min: 1 },
    {
      kind: 'number',
      name: 'dx',
      label: 'X間隔',
      unit: 'mm',
      inputId: 'pattern-dx',
      placeholder: 'mm',
      reject: () => null, // 0 は「その方向には並べない」で正当(dx=0 の1列は普通に使う)
    },
    {
      kind: 'number',
      name: 'dy',
      label: 'Y間隔',
      unit: 'mm',
      inputId: 'pattern-dy',
      placeholder: 'mm',
      reject: () => null,
    },
    {
      kind: 'number',
      name: 'nx',
      label: 'X個数',
      unit: '個',
      inputId: 'pattern-nx',
      placeholder: '個',
      reject: count('X個数', 1),
    },
    {
      kind: 'number',
      name: 'ny',
      label: 'Y個数',
      unit: '個',
      inputId: 'pattern-ny',
      placeholder: '個',
      reject: count('Y個数', 1),
    },
  ],
  build(values) {
    const entities = (values.entities ?? []) as string[]
    const dx = values.dx
    const dy = values.dy
    const nx = values.nx
    const ny = values.ny
    if (entities.length === 0) return { ok: false, reason: '図形が選ばれていない' }
    if (
      typeof dx !== 'number' ||
      typeof dy !== 'number' ||
      typeof nx !== 'number' ||
      typeof ny !== 'number'
    ) {
      return { ok: false, reason: '間隔と個数が数値でない' }
    }
    // 2個以上並べるのに間隔が両方0なら、複製は元と完全に重なる。**黙って通さない**
    // (成功したのに何も増えて見えない = 原因の分からない不具合になる)。
    if (dx === 0 && dy === 0 && nx * ny > 1) {
      return { ok: false, reason: '間隔が両方0(複製が元と重なる)' }
    }
    if (nx === 1 && ny === 1) return { ok: false, reason: '個数が1×1(反復にならない)' }
    return { ok: true, args: { entities, dx, dy, nx, ny } }
  },
}

/**
 * 円形反復。**全周(360度)は `360/count`、全周未満は `total_angle/(count-1)`** の
 * 場合分けをコアが持っている(全周を `count-1` で割ると最後の複製が最初と重なる)。
 * UIはここを再実装せず、角度と個数をそのまま渡す —— 「全周8個で隣り合う角度が45度」に
 * なることが、その場合分けが効いていることの目視確認になる。
 *
 * 回転中心はスケッチ平面上のクリック(点引数)で取る。**中心と図形の中心が一致すると
 * コアが ValueError**(全複製が元と重なる)なので、その場合は理由が画面に出る。
 */
export const PATTERN_CIRCULAR: CommandDef = {
  id: 'pattern_circular',
  category: 'feature',
  label: '円形反復',
  buttonId: 'pattern-circular',
  preview: 'none',
  args: [
    { kind: 'entities', name: 'entities', label: '反復する図形', min: 1 },
    { kind: 'point', name: 'center', label: '回転中心' },
    {
      kind: 'number',
      name: 'count',
      label: '個数',
      unit: '個',
      inputId: 'pattern-count',
      placeholder: '個',
      reject: count('個数', 2),
    },
    {
      kind: 'number',
      name: 'total_angle',
      label: '全角度',
      unit: '度',
      inputId: 'pattern-angle',
      placeholder: '度',
      reject: (value) =>
        value > 0 && value <= 360 ? null : '全角度は 0より大きく360以下',
    },
  ],
  build(values) {
    const entities = (values.entities ?? []) as string[]
    const center = values.center as Point2 | undefined
    const total = values.count
    const angle = values.total_angle
    if (entities.length === 0) return { ok: false, reason: '図形が選ばれていない' }
    if (!center) return { ok: false, reason: '回転中心が要る' }
    if (typeof total !== 'number' || typeof angle !== 'number') {
      return { ok: false, reason: '個数と角度が数値でない' }
    }
    return { ok: true, args: { entities, center, count: total, total_angle: angle } }
  },
}

/**
 * ソリッドを触るモードで足すぶん(押し出し・丸める・面取り)。
 *
 * `App.svelte` が `COMMANDS`(`extrude_face`)の後ろへ繋ぐ。**反復はスケッチの
 * 図形を対象にするのでここには入らない**(下の `SKETCH_FEATURE_COMMANDS`)。
 */
export const SOLID_FEATURE_COMMANDS: CommandDef[] = [EXTRUDE_REGIONS, FILLET, CHAMFER]

/**
 * スケッチモードで足すぶん。**領域押し出しは両方に居る** —— 描いた直後にそのまま
 * 押し出せることと、モードを抜けてから領域を選んで押し出せることの両方が要る。
 *
 * **通常モードの側で領域を指す道は3Dクリックだけである** —— このコマンドを起動している
 * 間だけ全スケッチの領域が3Dで指せる(`App.svelte` の `wantsRegions` →
 * `viewport.pickRegions`。辺と同じ規律)。
 */
export const SKETCH_FEATURE_COMMANDS: CommandDef[] = [
  EXTRUDE_REGIONS,
  PATTERN_RECTANGULAR,
  PATTERN_CIRCULAR,
]

/**
 * そのverbは**スケッチの図形を対象にするか**(`sketch_id` を足して送る必要があるか)。
 *
 * `category` では分けられない —— 反復は「フィーチャだが対象がスケッチの図形」という
 * 位置にいる(コアでも `entities` を引数で受ける唯一のverbで、図形は選択集合に入らない)。
 */
export function needsSketchId(verb: string): boolean {
  return verb === PATTERN_RECTANGULAR.id || verb === PATTERN_CIRCULAR.id
}
