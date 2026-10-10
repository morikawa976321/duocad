/**
 * 形状最適化(第3のモード)の4つの `CommandDef`。コア側の契約は `duocad/optimize.py` の `state_payload` docstring、
 * verbの実体は `duocad/verbs/optimize_setup.py`。
 *
 * 4つとも集合代入で対象は選択そのもの。選択引数は `min: 0` —— 空で確定 = その役割を全部外す、が唯一の解除手段なので、
 * 0件を拒まない(`missingSelection` を使わない)。選択の `max` が `null` なので Enter が要る(`command.ts` の `needsEnter`)。
 * 起動時のプリセレクトと、ピックの粒度(`shapeKind: 'body'` → `onWants(['body'])`)は `App.svelte` の役目。
 */
import type { BuildResult, CommandDef } from './command'

/** **対象はサーバへ送らない**(コアが選択から取る。`duocad-target-set-taken-only-from-selection`)。 */
const sendNothing = (): BuildResult<Record<string, never>> => ({ ok: true, args: {} })

/** 荷重の向きの選択肢。`down` はUI専用の言い回しで、送るのは `[0, 0, -1]`(`duocad/optimize.py` は世界軸を vector で表す)。 */
const LOAD_DIRECTION_OPTIONS = [
  { value: 'normal', label: '面の外向き法線', buttonId: 'load-direction-normal' },
  { value: '-normal', label: '面を押し込む向き', buttonId: 'load-direction-negnormal' },
  { value: 'down', label: '下向き(-Z)', buttonId: 'load-direction-down' },
] as const

function directionArg(value: string): 'normal' | '-normal' | [number, number, number] {
  if (value === 'down') return [0, 0, -1]
  return value as 'normal' | '-normal'
}

export const SET_PRESERVE_BODIES: CommandDef<'set_preserve_bodies'> = {
  id: 'set_preserve_bodies',
  label: '保持ソリッドを決める',
  buttonId: 'set-preserve-bodies',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'bodies',
      label: '保持するソリッド',
      shapeKind: 'body',
      min: 0,
      max: null,
    },
  ],
  build: sendNothing,
}

export const SET_EXCLUDE_BODIES: CommandDef<'set_exclude_bodies'> = {
  id: 'set_exclude_bodies',
  label: '除外ソリッドを決める',
  buttonId: 'set-exclude-bodies',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'bodies',
      label: '除外するソリッド',
      shapeKind: 'body',
      min: 0,
      max: null,
    },
  ],
  build: sendNothing,
}

export const SET_FIXTURES: CommandDef<'set_fixtures'> = {
  id: 'set_fixtures',
  label: '拘束面を決める',
  buttonId: 'set-fixtures',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'faces',
      label: '固定する面',
      shapeKind: 'face',
      min: 0,
      max: null,
    },
  ],
  build: sendNothing,
}

/**
 * 荷重を置く。選んだ面(1枚以上)+ 向き + 大きさ[N]。
 * 上書き代入: 選んだ面にだけ焼き、選択に入っていない荷重面は触らない(面ごとに値が違ってよいので、集合代入だと
 * 面Bを直すつもりの決定で面Aの値まで上書きされる。`duocad/verbs/optimize_setup.py`)。`min: 1` は空で決定して
 * 何も起きないのを避けるため。外すのは `CLEAR_LOADS`。
 */
export const SET_LOADS: CommandDef<'set_loads'> = {
  id: 'set_loads',
  label: '荷重を置く',
  buttonId: 'set-loads',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'faces',
      label: '力がかかる面',
      shapeKind: 'face',
      min: 1,
      max: null,
    },
    {
      kind: 'choice',
      name: 'direction',
      label: '向き',
      options: [...LOAD_DIRECTION_OPTIONS],
    },
    {
      kind: 'number',
      name: 'magnitude',
      label: '大きさ',
      unit: 'N',
      inputId: 'load-magnitude',
      // 大きさはAIが勝手に決めてよい数ではない(`duocad/catalog.py` の `dimension` の注)。
      reject: (value) => (value > 0 ? null : '大きさは0より大きい値(N)'),
    },
  ],
  build(values) {
    const direction = values.direction
    const magnitude = values.magnitude
    if (typeof direction !== 'string') return { ok: false, reason: '向きが選ばれていない' }
    if (typeof magnitude !== 'number' || !Number.isFinite(magnitude)) {
      return { ok: false, reason: '大きさが数値でない' }
    }
    return { ok: true, args: { direction: directionArg(direction), magnitude } }
  },
}

export const CLEAR_LOADS: CommandDef<'clear_loads'> = {
  id: 'clear_loads',
  label: '荷重を外す',
  buttonId: 'clear-loads',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'faces',
      label: '荷重を外す面',
      shapeKind: 'face',
      min: 0,
      max: null,
    },
  ],
  build: sendNothing,
}

/**
 * 最適化モードのツールバー一覧。状態機械は増やさない(`App.svelte` のコメントが原則)。
 * 荷重だけ「置く」と「外す」の2つある: 荷重は上書き代入なので、空の選択に「外す」の意味が無い。
 */
export const OPTIMIZE_MODE_COMMANDS: CommandDef[] = [
  SET_PRESERVE_BODIES,
  SET_EXCLUDE_BODIES,
  SET_FIXTURES,
  SET_LOADS,
  CLEAR_LOADS,
]

/**
 * verb名 -> `state.optimize` のどの配列を起動時のプリセレクトに使うか(`App.svelte` が読む)。
 *
 * `set_loads` / `clear_loads` は載せない(意図的): 載せると既存の荷重面が全部選択状態で現れ、面Bを直すつもりの
 * 決定で面Aまで上書きされる(例外も警告も出ない)。荷重の現状は3Dビューの矢印とラベル(`optimizeAnnotations.ts`)が描く。
 */
export const OPTIMIZE_PRESET_ROLE: Record<string, 'preserve_bodies' | 'exclude_bodies' | 'fixtures'> = {
  set_preserve_bodies: 'preserve_bodies',
  set_exclude_bodies: 'exclude_bodies',
  set_fixtures: 'fixtures',
}

/** 上の3つの役割の呼び名(`duocad/optimize.py` の `ROLE_LABEL` と同じ語)。 */
export const OPTIMIZE_ROLE_LABEL: Record<'preserve_bodies' | 'exclude_bodies' | 'fixtures', string> = {
  preserve_bodies: '保持ソリッド',
  exclude_bodies: '除外ソリッド',
  fixtures: '拘束面',
}

/**
 * 起動時に選択を空にするverb(`App.svelte` が読む)。上の表から外すだけだと前のverbの選択が残り、拘束面を確定した
 * 直後に荷重verbを起動するとその面を掴んだまま確定して `RoleConflict` で落ちる(実機検査で観測)。
 */
export const OPTIMIZE_EMPTY_START_VERBS: ReadonlySet<string> = new Set(['set_loads', 'clear_loads'])
