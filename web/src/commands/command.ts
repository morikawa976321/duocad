/**
 * コマンド層 — **引数を集める共通の仕組み**(タスクB・2026-08-13)。
 *
 * ## なぜ1つにまとめるのか
 *
 * v1のverbは19個ある。verbごとにUIを作り込むと、19回「選択を読む・数値を打たせる・
 * プレビューする・Escで中断する」を書き直すことになり、そのうち何本かは必ず
 * 「選択を先に見ない」「Escで文書が変わる」のどちらかを取りこぼす。
 * **verbはここに載せるだけ**にして、状態機械は `CommandHost.svelte` の1つだけにする。
 *
 * ```
 * 起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc)
 * ```
 *
 * ## 引数収集は、まず共有選択集合を見る
 *
 * **これがAI→人間の受け渡しが成立する仕組み**(実装計画書§8b B)。AIが
 * `propose_selection` で辺の集合を提案 → 人が承認して verb を起動すると、
 * その選択がそのまま引数になる。空のときだけ人に問う。
 *
 * ここ(`command.ts`)は**Svelteに依存しない純粋な定義と道具**だけを持つ。
 * 状態機械・DOM・通信は `CommandHost.svelte` の側にある。
 */
import type { SelectionRef, SelectionState, ShapeKind, ShapeRef } from '../session'

/** 選択の種別。コアの `SELECTABLE_KINDS` と同じ4つ。 */
export type SelectableKind = ShapeKind | 'region'

/** 画面に出す種別の名前(件数表示・引数の問いかけに使う)。 */
export const KIND_LABELS: Record<SelectableKind, string> = {
  face: '面',
  edge: '辺',
  vertex: '頂点',
  region: '領域',
}

/** 種別ごとの数え方(「辺 4本」「面 2面」)。件数表示を1箇所に集める。 */
const KIND_COUNTERS: Record<SelectableKind, string> = {
  face: '面',
  edge: '本',
  vertex: '点',
  region: '個',
}

// --------------------------------------------------------------------------------------
// 参照(ShapeRef / RegionRef)の道具 — コアの `duocad/selection.py` と同じ判定を持つ
// --------------------------------------------------------------------------------------
/**
 * 参照の種別を返す。**RegionRef は `kind` キーを持たない**ので `sketch` の有無で見分ける
 * (§3.5のデータモデル。コアの `selection.ref_kind` と同じ規則)。
 */
export function refKind(ref: SelectionRef): SelectableKind {
  return 'sketch' in ref ? 'region' : (ref as ShapeRef).kind
}

/** 2つの参照が同じ実体を指すか(`provenance` は見ない。コアの `same_ref` と同じ)。 */
export function sameRef(left: SelectionRef, right: SelectionRef): boolean {
  const kind = refKind(left)
  if (kind !== refKind(right)) return false
  if (kind === 'region') {
    const a = left as { sketch: string; index: number }
    const b = right as { sketch: string; index: number }
    return a.sketch === b.sketch && a.index === b.index
  }
  const a = left as ShapeRef
  const b = right as ShapeRef
  return a.body === b.body && a.kind === b.kind && a.index === b.index
}

/** 一覧の `{#each}` キー・DOMの `data-` 属性に使う一意な文字列。 */
export function refKey(ref: SelectionRef): string {
  return 'sketch' in ref
    ? `region:${ref.sketch}:${ref.index}`
    : `${(ref as ShapeRef).kind}:${(ref as ShapeRef).body}:${ref.index}`
}

/** 面の参照を作る(ビューポートのピック結果から)。添字は1始まり。 */
export function faceRef(bodyId: string, faceIndex: number): ShapeRef {
  return { body: bodyId, kind: 'face', index: faceIndex }
}

/** 種別ごとの件数を「辺 4本 / 面 1面」の形にまとめる(選択パネルの要約)。 */
export function describeSelection(items: SelectionRef[]): string {
  const counts = new Map<SelectableKind, number>()
  for (const item of items) {
    const kind = refKind(item)
    counts.set(kind, (counts.get(kind) ?? 0) + 1)
  }
  if (counts.size === 0) return 'なし'
  return [...counts]
    .map(([kind, count]) => `${KIND_LABELS[kind]} ${count}${KIND_COUNTERS[kind]}`)
    .join(' / ')
}

// --------------------------------------------------------------------------------------
// 引数の仕様
// --------------------------------------------------------------------------------------
/**
 * 選択から取る引数。**まず共有選択集合を見て、足りないときだけ人に問う**。
 *
 * `max` が `null` なら何本でも(フィレットの辺集合)。`max` があるときは
 * 選択の先頭から `max` 個だけを使う(余分を勝手に捨てるのではなく、`max` を超えた分は
 * 引数に入らないことを画面に出す)。
 */
export interface SelectionArgSpec {
  kind: 'selection'
  name: string
  label: string
  shapeKind: SelectableKind
  min: number
  max: number | null
}

/** 打ち込む数値の引数(距離・半径・個数)。 */
export interface NumberArgSpec {
  kind: 'number'
  name: string
  label: string
  unit: string
  /** 入力欄の `id`(実機検査がここを叩く)。 */
  inputId: string
  placeholder: string
  /** 使えない値なら理由を返す(使えるなら null)。 */
  reject(value: number): string | null
}

export type ArgSpec = SelectionArgSpec | NumberArgSpec

/** 集まった引数の値。選択引数は参照の配列、数値引数は数。 */
export type ArgValues = Record<string, SelectionRef[] | number>

/** `build` の結果。作れないときは理由を返す(黙って何も送らない、をしない)。 */
export type BuildResult =
  | { ok: true; args: Record<string, unknown> }
  | { ok: false; reason: string }

export interface CommandDef {
  /** verb名。**プレビューと確定で同じものをサーバへ送る**(食い違うと嘘のプレビューになる)。 */
  id: string
  label: string
  /** ツールバーのボタンの `id`(実機検査がここを叩く)。 */
  buttonId: string
  args: ArgSpec[]
  /** サーバの `PREVIEWABLE_VERBS` に載っているか。載っていないverbは投機実行を求めない。 */
  previewable: boolean
  /** 集まった引数から、サーバへ渡すverbの引数を作る。 */
  build(values: ArgValues): BuildResult
}

// --------------------------------------------------------------------------------------
// コマンドの定義
// --------------------------------------------------------------------------------------
/**
 * 面の押し出し。**M1からある唯一のコマンドを、この骨格の上へ載せ替えたもの**。
 *
 * 1コマンドだけ先に移すことで骨格の妥当性が確かめられる(タスクBの「移行」)。
 * C・D・Eのコマンドはこの配列へ足すだけになる。
 */
export const EXTRUDE_FACE: CommandDef = {
  id: 'extrude_face',
  label: '押し出す',
  buttonId: 'extrude',
  previewable: true,
  args: [
    { kind: 'selection', name: 'face', label: '押し出す面', shapeKind: 'face', min: 1, max: 1 },
    {
      kind: 'number',
      name: 'distance',
      label: '距離',
      unit: 'mm',
      inputId: 'distance',
      placeholder: 'mm',
      // 既定値を入れないのと同じ理由で0を弾く(M1から): 打たずにEnterした結果
      // 意図しない距離で確定するのを防ぐ。正=足す / 負=削る。
      reject: (value) => (value === 0 ? '距離が0(正=足す / 負=削る)' : null),
    },
  ],
  build(values) {
    const faces = (values.face ?? []) as SelectionRef[]
    const face = faces[0] as ShapeRef | undefined
    const distance = values.distance
    if (!face) return { ok: false, reason: '面が選ばれていない' }
    if (typeof distance !== 'number' || !Number.isFinite(distance)) {
      return { ok: false, reason: '距離が数値でない' }
    }
    return {
      ok: true,
      args: { body_id: face.body, face_index: face.index, distance },
    }
  },
}

/** 使えるコマンドの一覧(ツールバーの並び)。C・D・Eはここへ足す。 */
export const COMMANDS: CommandDef[] = [EXTRUDE_FACE]

/**
 * 起動していないときに構えているコマンド。
 *
 * **明示的な「起動」の1手を挟まない**のは、M1の4手(面を選ぶ→数値を打つ→Enter)が
 * 本人の速度ベンチマークそのものであり、ここに1手足すと退行になるため。
 * 別のコマンドを起動すると入れ替わり、Escでここへ戻る。
 */
export const DEFAULT_COMMAND_ID = EXTRUDE_FACE.id

export function findCommand(id: string): CommandDef {
  const found = COMMANDS.find((command) => command.id === id)
  if (!found) throw new Error(`unknown command: ${id}`)
  return found
}

// --------------------------------------------------------------------------------------
// 引数収集の判定(状態機械が使う純粋な計算)
// --------------------------------------------------------------------------------------
/**
 * 選択集合から、この引数が食える参照を取り出す。
 *
 * **種別で絞る**(面のコマンドに辺を渡さない)。`max` があれば先頭からその数だけ。
 * 順序は選択の追加順のまま(コアが順序を保つ契約。フィレットの辺と半径の対応が
 * ずれないため)。
 */
export function itemsForArg(spec: SelectionArgSpec, selection: SelectionState | null): SelectionRef[] {
  const items = (selection?.items ?? []).filter((item) => refKind(item) === spec.shapeKind)
  return spec.max === null ? items : items.slice(0, spec.max)
}

/**
 * `max` を超えて選ばれている引数があれば注意書きを返す(無ければ空文字)。
 *
 * **黙って先頭だけ使わない**。押し出しは面1つのverbなので、面を2つ選んだまま
 * Enterを押せば片方は無視される —— それを画面に出さないと「押したのに動かない面」が
 * 出て、原因が選択にあることが分からない。
 */
export function overflowNote(command: CommandDef, selection: SelectionState | null): string {
  for (const spec of command.args) {
    if (spec.kind !== 'selection' || spec.max === null) continue
    const total = (selection?.items ?? []).filter((item) => refKind(item) === spec.shapeKind).length
    if (total > spec.max) {
      const counter = KIND_COUNTERS[spec.shapeKind]
      return `${spec.label}は${spec.max}${counter}まで(選択${total}${counter}のうち先頭を使う)`
    }
  }
  return ''
}

/** その引数が揃っているか。 */
export function argSatisfied(spec: ArgSpec, values: ArgValues): boolean {
  if (spec.kind === 'selection') {
    return ((values[spec.name] ?? []) as SelectionRef[]).length >= spec.min
  }
  const value = values[spec.name]
  return typeof value === 'number' && Number.isFinite(value) && spec.reject(value) === null
}

/** まだ揃っていない最初の引数(全部揃っていれば null)。 */
export function firstMissingArg(command: CommandDef, values: ArgValues): ArgSpec | null {
  return command.args.find((spec) => !argSatisfied(spec, values)) ?? null
}

/**
 * その引数について、人に何を問うかの1行。
 *
 * 数値引数は「打った値が使えない」場合にその理由を出す(空欄のときは何を打つかを出す)。
 */
export function promptFor(spec: ArgSpec, values: ArgValues): string {
  if (spec.kind === 'selection') {
    const have = ((values[spec.name] ?? []) as SelectionRef[]).length
    const need = spec.min - have
    return `${spec.label}を${need}${KIND_COUNTERS[spec.shapeKind]}選ぶ(クリック / Shift+クリックで追加)`
  }
  const value = values[spec.name]
  if (typeof value === 'number' && Number.isFinite(value)) {
    return spec.reject(value) ?? ''
  }
  return `${spec.label}を打つ(${spec.unit})`
}
