/**
 * 形状最適化(第3のモード)の4つの `CommandDef`。
 *
 * コア側の契約は `duocad/optimize.py` の `state_payload` docstring、verbの実体は
 * `duocad/verbs/optimize_setup.py`。
 * **4つとも集合代入**である —— 「対象は選択そのもの」で、ここが送るのは
 * `set_loads` の大きさ・向きだけ(他の3つはサーバへ何も送らない)。
 *
 * **選択引数は `min: 0`**(既存のverbには無い形)。空の選択で確定する = その役割を
 * 全部外す、が唯一の解除手段なので、0件を「まだ選んでいない」として拒まない
 * (`missingSelection` を使わない理由)。
 *
 * **4つとも Enter が要る**(`command.ts` の `needsEnter`)——選択の `max` が `null` で
 * 対象を複数取りうるため。
 *
 * **起動時のプリセレクトは `App.svelte` の役目**(`CommandHost` の `onStart` → 配りもの
 * `state.optimize` の該当配列をそのまま `modifySelection` へ渡す。`duocad/optimize.py` の
 * 「4つの配列はそのまま `modify_selection` へ渡せる」契約の実体)。ここでは引数の集め方
 * だけを宣言する。
 *
 * **ピックの粒度**(面 vs ボディ)も `App.svelte` の役目 —— `shapeKind: 'body'` を
 * 宣言すると `CommandHost` が `onWants(['body'])` を出すので、`App.svelte` の `onPick` は
 * それを見て面ではなくボディ全体を選択へ足す。
 */
import type { BuildResult, CommandDef } from './command'

/** **対象はサーバへ送らない**(コアが選択から取る。`duocad-target-set-taken-only-from-selection`)。 */
const sendNothing = (): BuildResult<Record<string, never>> => ({ ok: true, args: {} })

/** 荷重の向きの選択肢。**`down` はUI専用の言い回し**で、送るのは `[0, 0, -1]`
 *  (`duocad/optimize.py` のdocstring「世界軸は vector で表す」に合わせ、
 *  コア側へ軸名の語彙を増やさない)。 */
const LOAD_DIRECTION_OPTIONS = [
  { value: 'normal', label: '面の外向き法線', buttonId: 'load-direction-normal' },
  { value: '-normal', label: '面を押し込む向き', buttonId: 'load-direction-negnormal' },
  { value: 'down', label: '下向き(-Z)', buttonId: 'load-direction-down' },
] as const

/** `-Z` の言い回しを、コアが受け取れる形(単位ベクトル)へ均す。 */
function directionArg(value: string): 'normal' | '-normal' | [number, number, number] {
  if (value === 'down') return [0, 0, -1]
  return value as 'normal' | '-normal'
}

/** **保持ソリッド**を決める。選択のボディ(空でもよい)をそのまま集合にする。 */
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

/** **除外ソリッド**を決める。`SET_PRESERVE_BODIES` と対(役割が違うだけ)。 */
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

/** **拘束面**を決める。howは無い(面を選ぶだけ)。 */
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
 * **荷重を置く**。選んだ面(1枚以上)+ 向き + 大きさ[N]。
 *
 * **上書き代入**である —— 選んだ面にだけ焼き、選択に入っていない荷重面は触らない。
 * 大きさと向きが面ごとに違ってよいので、集合代入にすると「面Bを直すつもりで決定した瞬間に
 * 面Aの値も上書きされる」事故が構造的に避けられない(`duocad/verbs/optimize_setup.py`)。
 * **`min: 1` はその一部**: 0枚で決定できると「空で確定 = 全部外れる」を期待した人が、
 * 何も起きないまま次へ進む。外すのは `CLEAR_LOADS`。
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
      // **AIが勝手に決めてよい数ではない**(`duocad/catalog.py` の `dimension` の注)。
      // 0以下は「荷重が無い」を面の非選択で表す既定の外にあるので弾く。
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

/** **荷重を外す**。選んだ面を荷重から落とす(空で決定すれば全部外れる)。howは無い。 */
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
 * 最適化モードのツールバー一覧。**状態機械は増やさない**(`App.svelte` のコメントが原則)。
 * ソリッドを触るぶん2つ(粒度=ボディ)+ 面を触るぶん3つ(粒度=面)。
 *
 * **荷重だけ「置く」と「外す」の2つある。** 他の3つは集合代入なので空で確定すれば外れるが、
 * 荷重は上書き代入(面ごとに大きさ・向きを持つため)で、空の選択に「外す」の意味が無い。
 * 5つ目ではあるが**新しい状態機械ではない** —— 選ぶ→決定という既存の流れそのままである。
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
 * **`set_loads` はここに載せない(意図的)。** 載せると起動時に既存の荷重面が全部選択状態で
 * 現れ、面Bの値を直すつもりで決定した人が面Aの値まで上書きしてしまう —— 例外も警告も出ない。
 * 荷重は上書き代入なので、**選んだ面にしか書かない**ことが唯一の安全策であり、そのためには
 * 「何も選ばれていない状態から始める」必要がある。いま何に荷重が付いているかは
 * 3Dビューの矢印とラベル(`optimizeAnnotations.ts`)が常時描いているので、
 * プリセレクトが情報源である必要はない。
 *
 * `clear_loads` も同じ理由で載せない —— 全部選択された状態で決定すると全部外れる。
 */
export const OPTIMIZE_PRESET_ROLE: Record<string, 'preserve_bodies' | 'exclude_bodies' | 'fixtures'> = {
  set_preserve_bodies: 'preserve_bodies',
  set_exclude_bodies: 'exclude_bodies',
  set_fixtures: 'fixtures',
}

/** 上の3つの役割の呼び名(画面の通知に使う。`duocad/optimize.py` の `ROLE_LABEL` と同じ語)。 */
export const OPTIMIZE_ROLE_LABEL: Record<'preserve_bodies' | 'exclude_bodies' | 'fixtures', string> = {
  preserve_bodies: '保持ソリッド',
  exclude_bodies: '除外ソリッド',
  fixtures: '拘束面',
}

/**
 * 起動時に**選択を空にする**verb(`App.svelte` が読む)。
 *
 * 上の表に載っていないことと、**選択が空で始まることは別である。** 表から外すだけだと
 * 「前のverbで選んでいたものがそのまま残る」になり、拘束面を確定した直後に荷重verbを
 * 起動すると、その拘束面を掴んだまま確定して `RoleConflict` で落ちる(実機検査で観測)。
 * 荷重は上書き代入なので**選んだ面にしか書かない**ことが唯一の安全策であり、
 * そのためには「何も選ばれていない状態から始める」ことまで含めて保証する必要がある。
 */
export const OPTIMIZE_EMPTY_START_VERBS: ReadonlySet<string> = new Set(['set_loads', 'clear_loads'])
