/**
 * スケッチのコマンド定義。状態機械・数値入力・中断は `CommandHost.svelte` の1つを使い回す。
 * 骨格の点(point)引数は、スケッチ平面をクリックして (u,v) を集める第3の集め方。
 *
 * 幽霊はブラウザで描く(`preview: 'local'`): 図形の形はブラウザ側で分かっており、サーバへ送らないので「Escで中断しても文書が変わらない」が構造的に成立する。
 * `sketch_id` はここでは付けない(今どのスケッチモードに居るかで決まる。`App.svelte` が送信時に足す)。
 */
import {
  buildPathArgs,
  isPlaneArgValue,
  pathLike,
  pickedRefOf,
  positive,
  type ArgValues,
  type CommandDef,
  type Point2,
} from './command'
import { angleArg, buildMove, copyOptions, isRotateMode, modeOptions, offsetArg } from './moveMode'
import {
  arcFromPoints,
  circleFromPoints,
  lineAngleDeg,
  lineFromPoints,
  lineLength,
  rectFromPoints,
} from './sketchGeometry'

/** 幽霊の組み立ては `../entityKinds` の唯一の表にある(README.md「モジュールとimport境界」)。ここは再エクスポートだけ。 */
export { ghostEntity } from '../entityKinds'

/** 打っていない `fromPoints` の寸法は `undefined`(集めた点から導く合図)。 */
function typed(values: ArgValues, name: string): number | undefined {
  const value = values[name]
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

/** 線分。寸法は長さ・角度(度・反時計回り・スケッチ平面のX軸から。`add_arc` と同じ規約)。サーバへ送るのは `p0`/`p1` だけ。 */
export const ADD_LINE: CommandDef<'add_line'> = {
  id: 'add_line',
  label: '線分',
  buttonId: 'sketch-line',
  preview: 'local',
  args: [
    { kind: 'point', name: 'p0', label: '始点' },
    { kind: 'point', name: 'p1', label: '終点' },
    {
      kind: 'number',
      name: 'length',
      label: '長さ',
      unit: 'mm',
      inputId: 'sketch-line-length',
      reject: positive('長さ'),
      fromPoints: true,
      // `build` は p0/p1 だけを送るので、長さは名前で引けない(NumberArgSpec.live)。
      live: (args) => {
        const p0 = args.p0 as Point2 | undefined
        const p1 = args.p1 as Point2 | undefined
        return p0 && p1 ? lineLength(p0, p1) : null
      },
    },
    {
      kind: 'number',
      name: 'angle_deg',
      label: '角度',
      unit: '度',
      inputId: 'sketch-line-angle',
      reject: () => null,
      fromPoints: true,
      live: (args) => {
        const p0 = args.p0 as Point2 | undefined
        const p1 = args.p1 as Point2 | undefined
        return p0 && p1 ? lineAngleDeg(p0, p1) : null
      },
    },
  ],
  build(values) {
    const p0 = values.p0 as Point2 | undefined
    const p1 = values.p1 as Point2 | undefined
    if (!p0 || !p1) return { ok: false, reason: '2点が要る' }
    const line = lineFromPoints(p0, p1, typed(values, 'length'), typed(values, 'angle_deg'))
    if (line === null) return { ok: false, reason: '長さは0より大きい値' }
    return { ok: true, args: { p0: line.p0, p1: line.p1 } }
  },
}

export const ADD_CIRCLE: CommandDef<'add_circle'> = {
  id: 'add_circle',
  label: '円',
  buttonId: 'sketch-circle',
  preview: 'local',
  args: [
    { kind: 'point', name: 'center', label: '中心' },
    { kind: 'point', name: 'rim', label: '円周の1点' },
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'sketch-radius',
      reject: positive('半径'),
      fromPoints: true,
    },
  ],
  build(values) {
    const center = values.center as Point2 | undefined
    const rim = values.rim as Point2 | undefined
    if (!center || !rim) return { ok: false, reason: '中心と円周の点が要る' }
    const circle = circleFromPoints(center, rim, typed(values, 'radius'))
    if (circle === null) return { ok: false, reason: '半径は0より大きい値' }
    return { ok: true, args: { center: circle.center, radius: circle.radius } }
  },
}

/** 長方形。マウスで置く長方形は軸平行に限る(`add_rectangle` の `angle` は省略され、コアの既定 0 が効く)。 */
export const ADD_RECTANGLE: CommandDef<'add_rectangle'> = {
  id: 'add_rectangle',
  label: '長方形',
  buttonId: 'sketch-rectangle',
  preview: 'local',
  args: [
    { kind: 'point', name: 'corner', label: '1つ目の角' },
    { kind: 'point', name: 'opposite', label: '対角' },
    {
      kind: 'number',
      name: 'width',
      label: '幅',
      unit: 'mm',
      inputId: 'sketch-width',
      reject: positive('幅'),
      fromPoints: true,
    },
    {
      kind: 'number',
      name: 'height',
      label: '高さ',
      unit: 'mm',
      inputId: 'sketch-height',
      reject: positive('高さ'),
      fromPoints: true,
    },
  ],
  build(values) {
    const corner = values.corner as Point2 | undefined
    const opposite = values.opposite as Point2 | undefined
    if (!corner || !opposite) return { ok: false, reason: '角と対角の2点が要る' }
    const rect = rectFromPoints(corner, opposite, typed(values, 'width'), typed(values, 'height'))
    if (rect === null) return { ok: false, reason: '幅と高さは0より大きい値' }
    return { ok: true, args: { corner: rect.corner, width: rect.width, height: rect.height } }
  },
}

/** 円弧。3点目は中心ではなく円弧上の点で、開始角・終了角は始点と終点から決まる。幾何は `sketchGeometry.ts` の `arcFromPoints`。 */
export const ADD_ARC: CommandDef<'add_arc'> = {
  id: 'add_arc',
  label: '円弧',
  buttonId: 'sketch-arc',
  preview: 'local',
  args: [
    { kind: 'point', name: 'p0', label: '始点' },
    { kind: 'point', name: 'p1', label: '終点' },
    { kind: 'point', name: 'via', label: '円弧上の点' },
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'sketch-arc-radius',
      reject: positive('半径'),
      fromPoints: true,
    },
  ],
  build(values) {
    const p0 = values.p0 as Point2 | undefined
    const p1 = values.p1 as Point2 | undefined
    const via = values.via as Point2 | undefined
    if (!p0 || !p1 || !via) return { ok: false, reason: '始点・終点・円弧上の点が要る' }
    const arc = arcFromPoints(p0, p1, via, typed(values, 'radius'))
    if (arc === null) return { ok: false, reason: '3点が一直線か、半径が弦の半分より小さい' }
    return {
      ok: true,
      args: {
        center: arc.center,
        radius: arc.radius,
        start_deg: arc.start_deg,
        end_deg: arc.end_deg,
      },
    }
  },
}

/** SVGの図形をいま開いているスケッチへ足す。引数はパスだけ(`scale` は単位の合わないSVG用の逃げ道で、常に出すと毎回1.0と打つ欄が増える。`catalog` の `ws=False`)。 */
export const IMPORT_SVG: CommandDef<'import_svg'> = {
  // id == サーバのコマンド名 == ログのverb名。
  id: 'import_svg',
  label: 'SVG読み込み',
  buttonId: 'sketch-import-svg',
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: 'SVG',
      inputId: 'sketch-svg-path',
      placeholder: 'logo.svg',
      reject: pathLike('SVGのパス'),
    },
  ],
  build: buildPathArgs,
}

/**
 * スケッチを作る(「スケッチ」を押して貼る先をクリックする2手)。
 * 分類は `sketch` だが `sketch_id` を取らず、まだどのスケッチも開いていない状態(スケッチモードの貼る先待ち `sketch-pending`)で走る唯一のverb。
 * 反復(`pattern_*`)の裏返しで、`App.svelte` の送信経路は `needsSketchId`(features.ts)と下の `runsOutsideSketch` で捌く。
 *
 * 基準平面のボタンは置かない(UI指針1「実物をクリックできるものにボタンを作らない」)。
 * 面に貼るときの `build` は `{}` を返す: 面の参照を引数に入れると対象集合の指定方法が2つになる(サーバは `require_selected(doc, "face", exactly_one=True)` で選択から取り、2枚なら `SelectionAmbiguous`)。
 */
export const SKETCH_CREATE: CommandDef<'sketch_create'> = {
  id: 'sketch_create',
  label: 'スケッチ',
  buttonId: 'sketch-create',
  preview: 'none',
  // 引数名 `target` はWSに載る名前(`base_plane`)とわざと変えてある: 値は「基準平面名」か「面は選択にある」で、`base_plane` だと後者が嘘になる。
  args: [{ kind: 'plane', name: 'target', label: 'スケッチを貼る先' }],
  build(values) {
    const plane = values.target
    if (!isPlaneArgValue(plane)) return { ok: false, reason: '貼る先が決まっていない' }
    return { ok: true, args: plane === 'face' ? {} : { base_plane: plane } }
  },
}

/**
 * オフセット平面にスケッチを作る。貼る先の指し方は `SKETCH_CREATE` と同じだが `distance` を持つので Enter が要る。
 * 別verbなのは、`sketch_create` に足すとその即適用まで変わるため(`duocad/sketch.py` の `sketch_create` docstring)。
 */
export const SKETCH_CREATE_OFFSET: CommandDef<'sketch_create_offset'> = {
  id: 'sketch_create_offset',
  label: 'スケッチ(オフセット平面)',
  buttonId: 'sketch-create-offset',
  preview: 'none',
  args: [
    // 引数名 `target` は `SKETCH_CREATE` と同じ理由。
    { kind: 'plane', name: 'target', label: '基準となる平面' },
    {
      kind: 'number',
      name: 'distance',
      label: 'オフセット距離',
      unit: 'mm',
      inputId: 'sketch-create-offset-distance',
      reject: () => null,
    },
  ],
  build(values) {
    const plane = values.target
    const distance = values.distance
    if (!isPlaneArgValue(plane)) return { ok: false, reason: '基準となる平面が決まっていない' }
    if (typeof distance !== 'number') return { ok: false, reason: 'オフセット距離が数値でない' }
    return {
      ok: true,
      args: plane === 'face' ? { distance } : { base_plane: plane, distance },
    }
  },
}

/** スケッチモードの貼る先待ち(`sketch-pending`)のツールバー。`App.svelte` が `startCommandId` でどちらかを起動済みにして入る。 */
export const SKETCH_PENDING_COMMANDS: CommandDef[] = [SKETCH_CREATE, SKETCH_CREATE_OFFSET]

/** そのverbがまだどのスケッチも開いていない状態で走るか(`sketch_id` を足さずに送るか)。`needsSketchId`(features.ts)の裏返し。導出せず列挙なので、新しいスケッチverbの既定は `sketch_id` 付きのまま。 */
export function runsOutsideSketch(verb: string): boolean {
  return verb === SKETCH_CREATE.id || verb === SKETCH_CREATE_OFFSET.id
}

/**
 * スケッチの点・辺の平行移動 / 回転 / 複製。対象はコアの選択から取る。
 * どのスケッチかは選んだ参照が持つので `sketch_id` を取らない(`runsWithoutSketchId`)。
 * 複製するか・並進か回転かは既定を置かず人に選ばせる(`move_body` と同じ理由)。回転軸は無い(2Dなので常に平面の法線。`duocad/verbs/move_sketch.py`)。中心は省略可で、既定はコアが決める(対象の代表点の平均)。
 */
export const MOVE_SKETCH_ENTITIES: CommandDef<'move_sketch_entities'> = {
  id: 'move_sketch_entities',
  label: '動かす',
  buttonId: 'sketch-move',
  // 幽霊の対象は引数に無いので、`App.svelte` が同じ選択集合から導く(`ghostSources`)。
  preview: 'local',
  args: [
    {
      kind: 'selection',
      name: 'target',
      label: '動かす点・辺',
      shapeKind: 'sketch_entity',
      min: 1,
      max: null,
    },
    { kind: 'choice', name: 'copy', label: '複製', options: copyOptions('sketch-move') },
    { kind: 'choice', name: 'mode', label: '種別', options: modeOptions('sketch-move') },
    { kind: 'number', ...offsetArg('dx', 'sketch-move', () => null) },
    { kind: 'number', ...offsetArg('dy', 'sketch-move', () => null) },
    {
      kind: 'pick',
      name: 'center_entity',
      label: '回転中心の点',
      target: 'sketchPoint',
      buttonId: 'sketch-move-center',
      optional: true,
      when: isRotateMode,
    },
    { kind: 'number', ...angleArg('sketch-move') },
  ],
  build(values) {
    return buildMove(values, {
      missingTarget: (given) => (((given.target ?? []) as unknown[]).length === 0 ? '点も辺も選ばれていない' : null),
      axes: ['dx', 'dy'],
      zeroReason: '移動量が両方0',
      rotateArgs: (angle, given) => {
        // 中心は参照のまま送る: 点の座標を出す規則(`point` 省略なら最初の点)はコアの `_point_of` が正で、数値に潰すと規則が2つになる。
        const center = pickedRefOf(given.center_entity)
        return { ok: true, args: { mode: 'rotate', angle, ...(center === null ? {} : { center_entity: center }) } }
      },
    })
  },
}

/**
 * スケッチ図形の鏡映複製。軸は既存の線分を1本クリックして指す。
 * 軸は共有選択集合を経由しない別チャンネル(`PickArgSpec`)で受ける: 載せると `require_sketch_entities` が軸の線分まで対象に数え、画面の件数と結果が食い違う。
 * 軸の既定は無いので、対象が揃うと軸のピックが開く。対象を選び直せるのは修飾キー付きクリックがピックへ行かないから(`App.svelte` の `onSketchPick`)。
 * 幽霊は出さない。対象は何個でも選べるので確定はEnter(`needsEnter`)。
 */
export const MIRROR_SKETCH_ENTITIES: CommandDef<'mirror_sketch_entities'> = {
  id: 'mirror_sketch_entities',
  label: '鏡映複製',
  buttonId: 'sketch-mirror',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'target',
      label: '鏡映する図形',
      shapeKind: 'sketch_entity',
      min: 1,
      max: null,
    },
    {
      kind: 'pick',
      name: 'axis',
      label: '鏡映軸の線分',
      target: 'sketchEntity',
      buttonId: 'sketch-mirror-axis',
    },
  ],
  build(values) {
    const targets = (values.target ?? []) as unknown[]
    if (targets.length === 0) return { ok: false, reason: '点も辺も選ばれていない' }
    // 参照のまま送る(`move_sketch_entities` の中心と同じ)。
    const axis = pickedRefOf(values.axis)
    if (axis === null) return { ok: false, reason: '鏡映軸の線分が指されていない' }
    return { ok: true, args: { axis } }
  },
}

/**
 * 選んだ複数の領域を、へこみに沿って包む単一の領域線を同じスケッチへ足す。
 * R(追従半径)と d(オフセット)はどちらも既定 0 mm で、R = d = 0 なら選択領域の和集合そのもの。R は幅 2R 未満の隙間とへこみを埋め、d は正で外へ・負で内へ動かす。
 * 結果が1つの領域にならないときはコアが個数を添えて拒む(確定時のみ、トーストで見える)。入力中の幽霊はサーバが文書を変えずに計算し(`preview_envelope_regions`)、分裂している間は警告色。
 * 対象の領域は選択から取り、`sketch_id` は選んだ領域が持つ(`runsWithoutSketchId`)。
 */
export const ENVELOPE_REGIONS: CommandDef<'envelope_regions'> = {
  id: 'envelope_regions',
  label: '包む',
  buttonId: 'sketch-envelope',
  preview: 'server',
  args: [
    {
      kind: 'selection',
      name: 'regions',
      label: '包む領域',
      shapeKind: 'region',
      min: 1,
      max: null,
    },
    {
      kind: 'number',
      name: 'radius',
      label: '追従半径 R',
      unit: 'mm',
      inputId: 'sketch-envelope-radius',
      reject: (value) => (value < 0 ? '追従半径 R は 0 以上' : null),
      default: 0,
    },
    {
      kind: 'number',
      name: 'offset',
      label: 'オフセット d',
      unit: 'mm',
      inputId: 'sketch-envelope-offset',
      reject: () => null,
      default: 0,
    },
  ],
  build(values) {
    const regions = (values.regions ?? []) as unknown[]
    const radius = values.radius
    const offset = values.offset
    if (regions.length === 0) return { ok: false, reason: '領域が選ばれていない' }
    if (typeof radius !== 'number') return { ok: false, reason: 'R が数値でない' }
    if (typeof offset !== 'number') return { ok: false, reason: 'd が数値でない' }
    return { ok: true, args: { radius, offset } }
  },
}

/** そのverbがスケッチモードの中で走るのに `sketch_id` を取らないか(どのスケッチかは選んだ参照 `{sketch, entity, point}` / `{sketch, index}` が持つ)。`runsOutsideSketch` の隣。導出せず列挙。 */
export function runsWithoutSketchId(verb: string): boolean {
  return verb === MOVE_SKETCH_ENTITIES.id || verb === MIRROR_SKETCH_ENTITIES.id || verb === ENVELOPE_REGIONS.id
}

/**
 * 何も描かないコマンド(スケッチモードの既定)。既定でないと、描くコマンドが常に点を待ち、領域を狙ったクリックが図形の頂点になる。
 * 確定が成功するとここへ戻る(`CommandHost.svelte` の `commandSucceeded()`)。
 */
export const SELECT_IN_SKETCH: CommandDef = {
  id: 'select_in_sketch',
  label: '選ぶ',
  buttonId: 'sketch-select',
  // 領域を直接クリックして選べるのでボタンは出さない。Escや確定後はここへ戻る。
  hidden: true,
  preview: 'none',
  args: [],
  build: () => ({ ok: false, reason: '領域をクリックして選ぶ(図形を描くなら上のボタン)' }),
}

/** スケッチモードのツールバー。既定は「選ぶ」。 */
export const SKETCH_COMMANDS: CommandDef[] = [
  SELECT_IN_SKETCH,
  ADD_LINE,
  ADD_CIRCLE,
  ADD_RECTANGLE,
  ADD_ARC,
  MOVE_SKETCH_ENTITIES,
  MIRROR_SKETCH_ENTITIES,
  ENVELOPE_REGIONS,
  IMPORT_SVG,
]
export const DEFAULT_SKETCH_COMMAND_ID = SELECT_IN_SKETCH.id
