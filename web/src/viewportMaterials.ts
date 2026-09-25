/**
 * 「状態 × ホバー」のような組み合わせで材質を遅延生成して持つキャッシュ。面・辺・領域・スケッチの
 * 線と点(`viewport.ts`)・頂点マーカー(`vertexMarkers.ts`)が同じ形で使う ——
 * 系統ごとに材質を並べて作って分岐で選ぶと、1系統ずつ分岐と dispose の行が増える。
 *
 * `keyOf` が組み合わせを文字列の鍵にし、`make` は初回だけ呼ばれる(材質ごとの設定 ——
 * `depthTest` / `DoubleSide` / 不透明度 —— は `make` が持つ)。`seed` は既にある材質を鍵に
 * 結びつける(スケッチの線の `plain:0` は `sketchMaterial` そのもの。2つの独立インスタンスに
 * 分裂させない)。`dispose()` は種として渡したものも含めて全部捨てるので、**種の側で二重に捨てない**。
 */
export interface VariantCache<A extends unknown[], M extends { dispose(): void }> {
  get(...args: A): M
  dispose(): void
}

export function variantCache<A extends unknown[], M extends { dispose(): void }>(
  keyOf: (...args: A) => string,
  make: (...args: A) => M,
  seed: Array<[string, M]> = [],
): VariantCache<A, M> {
  const cache = new Map<string, M>(seed)
  return {
    get(...args) {
      const key = keyOf(...args)
      const found = cache.get(key)
      if (found) return found
      const made = make(...args)
      cache.set(key, made)
      return made
    },
    dispose() {
      for (const material of cache.values()) material.dispose()
      cache.clear()
    },
  }
}
