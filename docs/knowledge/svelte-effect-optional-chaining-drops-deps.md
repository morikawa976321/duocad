---
name: svelte-effect-optional-chaining-drops-deps
description: Svelte 5の$effectで `obj?.method(dep)` と書くと、初回にobjがnullだと ?. が短絡してdepを読まず、依存ゼロのまま二度と再実行されない
created: 2026-08-22
updated: 2026-08-22
---

# Svelte 5: `$effect` の `?.` 短絡は依存の登録ごと飛ばす

2026-08-22、duocadで実測して踏んだ落とし穴。

```svelte
let viewport: Viewport | null = null   // $state ではない(onMountで代入)
const wants = $derived(...)            // $state から導出

$effect(() => {
  viewport?.showBasePlanes(wants)      // ← 初回 viewport===null で ?. が短絡
})                                     //    wants を一度も読まない = 依存ゼロ
```

`$effect` は**実際に読まれたリアクティブ値**だけを依存として登録する。初回実行時に
`?.` が短絡すると引数の `wants` が評価されないので、この effect は依存を1つも持たない
まま登録され、`wants` が何度変わっても**二度と再実行されない**。

**対処**: リアクティブ値を先にローカルへ読んでから渡す。

```svelte
$effect(() => {
  const on = wants        // 先に読む(ここで依存が登録される)
  viewport?.showBasePlanes(on)
})
```

**症状の出方が悪い**: 型検査も lint も通り、初期状態では「そもそも false なので何も
起きないのが正しい」ので、機能が丸ごと死んでいることに実機で触るまで気づかない
(duocadでは「コマンドを起動しても基準平面が指せない」として出た)。

同じ形は `if (a) { b?.f(c) }` のような**条件の中でしか読まないリアクティブ値**全般に
起きる。既存コードで `void selection` のように明示的に読む1行が置かれていることがあるが、
それはこの穴を塞ぐためのイディオムである。
