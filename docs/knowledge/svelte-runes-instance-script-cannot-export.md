---
name: svelte-runes-instance-script-cannot-export
description: Svelte 5 runesモードの `<script lang="ts">`(インスタンス)は export を持てない。コンポーネントから型や定数を公開するときは `<script module>` へ置く
created: 2026-09-05
updated: 2026-09-05
---

Svelte 5 の runes モードでは、コンポーネントのインスタンススクリプト
(`<script lang="ts">`)に `export` を書けない —— プロパティの入口は `$props()` に
一本化されているため。**型や定数を他のファイルへ公開したいときは
`<script module lang="ts">` ブロックへ置く。**

```svelte
<script module lang="ts">
  export interface OptimizeNotice { kind: 'bad' | 'info'; lines: string[] }
</script>

<script lang="ts">
  const { notice }: { notice: OptimizeNotice | null } = $props()
</script>
```

duocad では `Toast.svelte`(`showToast` を公開)が先例。2026-09-05 に
`OptimizePanel.svelte` を書いたとき、型をインスタンス側へ書いて `npm run check` で
気づいた(実行時ではなくビルド時に出るので、見つけるのは早い)。
