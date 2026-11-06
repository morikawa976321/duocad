<script lang="ts">
  import { onMount } from 'svelte'
  import { attachFileDrop, decideDroppedFiles, DROP_GUIDE, type DropState, type DroppedFile } from './fileDrop'
  import { uploadDroppedFile } from './uploadDroppedFile'
  import { fail, inform } from './Toast.svelte'

  let { context, onImport, busy = $bindable(false), since = $bindable<number | null>(null) }: {
    context: () => DropState & { key: string; sketchId: string | null }
    onImport: (verb: 'import_step' | 'import_svg', path: string, sketchId: string | null) => void
    busy?: boolean
    since?: number | null
  } = $props()
  let dragging = $state(false)

  async function drop(files: File[], descriptions: DroppedFile[]): Promise<void> {
    const before = context()
    const decision = decideDroppedFiles(descriptions, { ...before, waiting: before.waiting || busy })
    if ('message' in decision) {
      inform(decision.message)
      return
    }
    busy = true
    since = performance.now()
    try {
      const path = await uploadDroppedFile(files[0])
      const after = context()
      const check = decideDroppedFiles(descriptions, after)
      if ('message' in check) inform(check.message)
      else if (before.key !== after.key) inform('送信中に接続・文書・作業状態が変わったので取り込まなかった(もう一度落とす)')
      else onImport(decision.verb, path, before.sketchId)
    } catch (error) {
      fail(error instanceof Error ? error.message : 'ファイルを送信できなかった')
    } finally {
      busy = false
      since = null
    }
  }

  onMount(() => {
    const detach = attachFileDrop(window, { show: (visible) => { dragging = visible }, drop })
    return detach
  })
</script>

{#if dragging}
  <div id="file-drop-overlay" role="status"><span class="tag">{DROP_GUIDE}</span></div>
{/if}

<style>
  #file-drop-overlay {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: grid;
    place-items: center;
    pointer-events: none;
    background: var(--accent-soft);
  }
  .tag {
    background: var(--panel);
    color: var(--text);
  }
</style>
