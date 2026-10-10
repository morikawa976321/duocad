/**
 * 開発用の表示フラグ(URLパラメータ)。`?debug=space` / `?debug=anchors` / `?voxel=1`。
 *
 * ビルドで落とさない: 利用者の環境で起きた問題を、その画面にフラグを付けて調べるための口だから。
 * フラグが無いときの画面は1ピクセルも変えない(既定はすべて `false`/空で、`App.svelte` は
 * `setOptimizeDebug(null)` すら呼ばない。既存の実機smokeが番人)。
 * 画面にフラグの状態は出さない(出すとフラグ無しの画面と差が出る)。確認は `location.search` と
 * `window.__duocad.optimizeDebugCounts()` で行う。
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

/**
 * クエリ文字列(`location.search`)からフラグを読む。`debug` は複数指定できる
 * (`?debug=space,anchors` でも `?debug=space&debug=anchors` でも)。
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

export function anyDebugFlag(flags: DebugFlags): boolean {
  return flags.space || flags.anchors || flags.voxel
}

/** サーバへ頼む種類(`run_optimization` の `debug`)。立っているフラグのぶんだけ頼む(頼まないものはサーバが配列を作らない)。 */
export function debugKindsFor(flags: DebugFlags): OptimizeDebugKind[] {
  const kinds: OptimizeDebugKind[] = []
  if (flags.space) kinds.push('space')
  if (flags.anchors) kinds.push('anchors')
  if (flags.voxel) kinds.push('rho')
  return kinds
}
