<script lang="ts">
  /**
   * トップ画面。何も開いていないとき(`state.document_open === false`)にビューポートを覆う。
   * 作業面は丸ごと見せず、空の無題の文書(「新規」直後)と見た瞬間に区別できるようにする。
   * できるのは開くことだけ。複製・削除・改名は `FilesPanel.svelte`。一覧はサーバの `list_documents` の写し。
   */
  import type { DocumentInfo } from './session'

  let {
    waiting,
    documents,
    filesPanelOpen,
    onOpen,
    onNew,
    onShowFiles,
    thumbnailUrl,
    onNeedThumbnail,
  }: {
    /** 最初の状態がまだ届いていない。覆うだけで中身は出さない。 */
    waiting: boolean
    documents: DocumentInfo[]
    filesPanelOpen: boolean
    onOpen: (name: string) => void
    onNew: () => void
    onShowFiles: () => void
    thumbnailUrl: (name: string) => string | null
    onNeedThumbnail: (name: string, version: number | null) => void
  } = $props()

  const RECENT_COUNT = 8

  const recent = $derived([...documents].sort((a, b) => b.mtime - a.mtime).slice(0, RECENT_COUNT))

  $effect(() => {
    for (const file of recent) onNeedThumbnail(file.name, file.thumb)
  })

  function stamp(epochSeconds: number): string {
    const date = new Date(epochSeconds * 1000)
    const pad = (value: number) => String(value).padStart(2, '0')
    return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  }
</script>

<div class="start-screen" id="start-screen">
  {#if !waiting}
    <div class="start-column">
      <span class="dim">文書を開いていない</span>

      {#if recent.length > 0}
        <div class="start-cards" id="start-recent">
          {#each recent as file (file.name)}
            {@const url = thumbnailUrl(file.name)}
            <button
              class="start-card"
              data-name={file.name}
              onclick={() => onOpen(file.name)}
              title={`${file.name} を開く`}
            >
              <span class="start-thumb">
                {#if url !== null}
                  <img src={url} alt="" draggable="false" />
                {:else}
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path
                      d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM4 7.5l8 4.5 8-4.5M12 12v9"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.2"
                      stroke-linejoin="round"
                    />
                  </svg>
                {/if}
              </span>
              <span class="start-name">{file.name}</span>
              <span class="start-date dim">{stamp(file.mtime)}</span>
            </button>
          {/each}
        </div>
      {/if}

      <div class="start-actions">
        <button id="start-new" onclick={onNew} title="空の文書から始める">新規</button>
        {#if !filesPanelOpen && documents.length > recent.length}
          <button id="start-show-files" onclick={onShowFiles}>すべてのファイル</button>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  /* position:absolute で `.viewport` の寸法には触らない(styles.css の不変条件)。 */
  .start-screen {
    position: absolute;
    inset: 0;
    z-index: 5;
    display: grid;
    place-items: center;
    overflow: auto;
    background: var(--bg);
  }

  .start-column {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 16px;
    padding: 24px 16px;
  }

  .start-cards {
    display: grid;
    grid-template-columns: repeat(4, 132px);
    gap: 10px;
  }

  .start-card {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
    padding: 6px;
    text-align: left;
    min-width: 0;
  }

  .start-thumb {
    display: grid;
    place-items: center;
    aspect-ratio: 1;
    background: var(--bg);
    border: 1px solid var(--line-soft);
    color: var(--dim-2);
  }

  .start-thumb img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  .start-thumb svg {
    width: 40%;
    height: 40%;
  }

  .start-name {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  .start-date {
    font-size: var(--fs-meta);
  }

  .start-actions {
    display: flex;
    gap: 8px;
  }

  @media (max-width: 760px) {
    .start-cards {
      grid-template-columns: repeat(2, 132px);
    }
  }
</style>
