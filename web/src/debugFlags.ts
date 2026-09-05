/**
 * **開発用の表示フラグ(URLパラメータ)。** `?debug=space` / `?debug=anchors` / `?voxel=1`。
 *
 * ## なぜビルドで落とさないか
 *
 * **落とすと、利用者の環境で起きた問題を調べられなくなる。**
 * 「変な答えが出た」と言われた画面でその場でフラグを付けて塊とアンカーを見る、が
 * この3つの存在理由なので、開発ビルドにしか無いなら移す意味が無い。
 *
 * ## 代わりに守ること
 *
 * **フラグが無いときの画面は1ピクセルも変わらない。** ここが返す値は既定で
 * すべて `false`/空で、`App.svelte` はそのとき `setOptimizeDebug(null)` すら呼ばない。
 * 既存の実機smokeが番人である(製品の見た目を測る検査はフラグ無しで走る)。
 *
 * ## 決めていないこと
 *
 * **画面にフラグの状態を出さない。** 出すとフラグ無しの画面と差が出る(上の不変条件を
 * 破る)ので、確認はブラウザの `location.search` と `window.__duocad.optimizeDebugCounts()`
 * で行う。
 */
import type { OptimizeDebugKind } from './session'

/** 読み取った開発用フラグ。**すべて既定 false**(＝製品の見え方)。 */
export interface DebugFlags {
  /** `?debug=space` —— 塊(設計空間)のボクセルを薄く重ねる。 */
  space: boolean
  /** `?debug=anchors` —— 拘束/荷重に割り当たった格子節点を色分けした球で。 */
  anchors: boolean
  /** `?voxel=1` —— 完了時に平滑化メッシュを見せず、平滑化前のボクセルで止める。 */
  voxel: boolean
}

export const NO_DEBUG_FLAGS: DebugFlags = { space: false, anchors: false, voxel: false }

/**
 * クエリ文字列(`location.search`)からフラグを読む。
 *
 * **`debug` は複数指定できる** —— `?debug=space,anchors` でも
 * `?debug=space&debug=anchors` でも両方立つ。probe は1つしか見なかったが、
 * ここは「塊の中でアンカーがどこに付いたか」を一度に見たい場面があるので広げた。
 * 知らない値は黙って無視する(将来フラグが増えても古いURLが壊れない)。
 */
export function readDebugFlags(search: string): DebugFlags {
  const params = new URLSearchParams(search)
  const names = new Set<string>()
  for (const value of params.getAll('debug')) {
    for (const part of value.split(',')) {
      const name = part.trim()
      if (name) names.add(name)
    }
  }
  return {
    space: names.has('space'),
    anchors: names.has('anchors'),
    voxel: params.get('voxel') === '1',
  }
}

/** どれか1つでも立っているか(立っていなければ画面はデバッグの口を一切触らない)。 */
export function anyDebugFlag(flags: DebugFlags): boolean {
  return flags.space || flags.anchors || flags.voxel
}

/**
 * サーバへ頼む種類(`run_optimization` の `debug`)。
 *
 * **立っているフラグのぶんだけ**頼む —— 頼まないものはサーバが配列を1本も作らないので、
 * 既定の実行の費用は1ビットも増えない。
 */
export function debugKindsFor(flags: DebugFlags): OptimizeDebugKind[] {
  const kinds: OptimizeDebugKind[] = []
  if (flags.space) kinds.push('space')
  if (flags.anchors) kinds.push('anchors')
  if (flags.voxel) kinds.push('rho')
  return kinds
}
