/**
 * 形状最適化(第3のモード)の4つの `CommandDef`。
 *
 * コア側の契約は `duocad/optimize.py` の `state_payload` docstring、verbの実体は
 * `duocad/verbs/optimize_setup.py`。
 * **4つとも集合代入**である —— 「対象は確定選択そのもの」で、ここが送るのは
 * `set_loads` の大きさ・向きだけ(他の3つはサーバへ何も送らない)。
 *
 * **選択引数は `min: 0`**(既存のverbには無い形)。空の確定選択で確定する = その役割を
 * 全部外す、が唯一の解除手段なので、0件を「まだ選んでいない」として拒まない
 * (`missingSelection` を使わない理由)。
 *
 * **4つとも Enter が要る**(`command.ts` の `needsEnter`)——選択の `max` が `null` で
 * 対象を複数取りうるため(`tests/test_ui_command_wiring.py` の `ENTER_REQUIRED`)。
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
import type { CommandDef } from './command'

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

/** **保持ソリッド**を決める。確定選択のボディ(空でもよい)をそのまま集合にする。 */
export const SET_PRESERVE_BODIES: CommandDef = {
  id: 'set_preserve_bodies',
  category: 'feature',
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
  build() {
    // **対象はサーバへ送らない**(コアが確定選択から取る。`duocad-target-set-taken-only-from-selection`)。
    return { ok: true, args: {} }
  },
}

/** **除外ソリッド**を決める。`SET_PRESERVE_BODIES` と対(役割が違うだけ)。 */
export const SET_EXCLUDE_BODIES: CommandDef = {
  id: 'set_exclude_bodies',
  category: 'feature',
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
  build() {
    return { ok: true, args: {} }
  },
}

/** **拘束面**を決める。howは無い(面を選ぶだけ)。 */
export const SET_FIXTURES: CommandDef = {
  id: 'set_fixtures',
  category: 'feature',
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
  build() {
    return { ok: true, args: {} }
  },
}

/** **荷重面**を決める。面(空でもよい)+ 向き + 大きさ[N]。 */
export const SET_LOADS: CommandDef = {
  id: 'set_loads',
  category: 'feature',
  label: '荷重面を決める',
  buttonId: 'set-loads',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'faces',
      label: '力がかかる面',
      shapeKind: 'face',
      min: 0,
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
      placeholder: 'N',
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

/**
 * 最適化モードのツールバー一覧。**状態機械は増やさない**(`App.svelte` のコメントが原則)。
 * ソリッドを触るぶん2つ(粒度=ボディ)+ 面を触るぶん2つ(粒度=面)。
 */
export const OPTIMIZE_MODE_COMMANDS: CommandDef[] = [
  SET_PRESERVE_BODIES,
  SET_EXCLUDE_BODIES,
  SET_FIXTURES,
  SET_LOADS,
]

/** verb名 -> `state.optimize` のどの配列を起動時のプリセレクトに使うか(`App.svelte` が読む)。 */
export const OPTIMIZE_PRESET_ROLE: Record<string, 'preserve_bodies' | 'exclude_bodies' | 'fixtures' | 'loads'> = {
  set_preserve_bodies: 'preserve_bodies',
  set_exclude_bodies: 'exclude_bodies',
  set_fixtures: 'fixtures',
  set_loads: 'loads',
}
