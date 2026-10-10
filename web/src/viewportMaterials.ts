/**
 * 「状態 × ホバー」のような組み合わせで材質を遅延生成するキャッシュ。`make` は鍵ごとに初回だけ呼ばれる。
 * `seed` は既にある材質を鍵に結びつける。`dispose()` は種も含めて全部捨てるので、**種の側で二重に捨てない**。
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
