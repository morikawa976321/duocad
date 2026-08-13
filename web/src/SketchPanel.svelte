<script lang="ts">
  /**
   * スケッチのパネル(タスクC・2026-08-13) — **モードの出入りと、置いた図形の打ち直し**。
   *
   * この企画の中心命題「手動操作が一級の操作モード」がUIに現れる場所。ここが持つのは
   * 3つだけで、どれも**コアを叩くだけ**(パネルは何も覚えない):
   *
   * 1. スケッチに入る/抜ける(基準平面3面 or 選択中の面に貼る)
   * 2. 図形の一覧と**値の打ち直し**(`set_parameter`)——
   *    クリックだけで置いた後に寸法を直せるのが§3.4のパラメトリック・プリミティブ
   * 3. 領域の一覧と選択(Dの押し出しがこの選択を引数に取る)
   *
   * **見た目は作り込まない**(タスクの制約)。高さは固定してある —— 内容任せにすると
   * 図形が増えるたびにビューポートがリサイズされ、射影が変わって次のクリックが
   * 別の場所に当たる(Bで実機で踏んだ落とし穴)。
   */
  import type { SelectionRef, ShapeRef, SketchState } from './session'

  let {
    sketches,
    activeSketchId,
    selectedFace,
    selection,
    onCreateOnBase,
    onCreateOnFace,
    onEnter,
    onExit,
    onLookAt,
    onSetParameter,
    onSelectRegion,
  }: {
    sketches: SketchState[]
    activeSketchId: string | null
    /** 選択中の最初の面(あれば「この面に貼る」が押せる)。 */
    selectedFace: ShapeRef | null
    selection: SelectionRef[]
    onCreateOnBase: (name: 'XY' | 'XZ' | 'YZ') => void
    onCreateOnFace: (face: ShapeRef) => void
    onEnter: (sketchId: string) => void
    onExit: () => void
    onLookAt: () => void
    onSetParameter: (name: string, value: number) => void
    onSelectRegion: (sketchId: string, index: number) => void
  } = $props()

  const active = $derived(sketches.find((item) => item.sketch_id === activeSketchId) ?? null)

  /** 選択中の領域の添字(このスケッチのぶんだけ)。 */
  const selectedRegions = $derived(
    new Set(
      selection
        .filter((item): item is { sketch: string; index: number } => 'sketch' in item)
        .filter((item) => item.sketch === activeSketchId)
        .map((item) => item.index),
    ),
  )

  /** 打ちかけの値(図形ID+キー -> 文字列)。Enterまでコアへ送らない。 */
  let editing = $state<Record<string, string>>({})

  function editKey(entityId: string, key: string): string {
    return `${entityId}:${key}`
  }

  function commitParam(entityId: string, key: string, paramName: string | null): void {
    if (paramName === null) return
    const raw = (editing[editKey(entityId, key)] ?? '').trim()
    const value = Number.parseFloat(raw)
    if (!Number.isFinite(value)) return
    onSetParameter(paramName, value)
    // **送ったら打ちかけを捨てる**。残すと、その後のundoやAIの書き換えでコアの値が
    // 変わっても欄が古い文字列を映し続ける(パネルは何も覚えない、が破れる)。
    const { [editKey(entityId, key)]: _sent, ...rest } = editing
    editing = rest
  }

  function shown(entity: SketchState['entities'][number]): Array<[string, number]> {
    return Object.entries(entity.params)
  }
</script>

<section class="sketch-panel" data-active={activeSketchId ?? ''}>
  <div class="row">
    <span class="label">スケッチ</span>
    {#if active}
      <span id="sketch-mode" class="tag on">
        編集中 {active.plane.kind === 'base' ? active.plane.name : '面に貼ったスケッチ'}
        ・図形 {active.entities.length} ・領域 {active.regions.length}
      </span>
      <button id="sketch-look" onclick={onLookAt}>正面から見る</button>
      <button id="sketch-exit" onclick={onExit}>抜ける</button>
    {:else}
      <span class="value dim">平面を選んで入る</span>
      {#each ['XY', 'XZ', 'YZ'] as const as name}
        <button id={`sketch-new-${name}`} onclick={() => onCreateOnBase(name)}>{name}</button>
      {/each}
      <button
        id="sketch-new-face"
        disabled={selectedFace === null}
        onclick={() => selectedFace && onCreateOnFace(selectedFace)}
      >
        選択中の面に貼る
      </button>
      {#each sketches as item (item.sketch_id)}
        <button class="reopen" onclick={() => onEnter(item.sketch_id)}>
          再開 {item.plane.kind === 'base' ? item.plane.name : '面'}({item.entities.length})
        </button>
      {/each}
    {/if}
  </div>

  {#if active}
    <div class="lists">
      <div class="list" id="sketch-entities">
        {#if active.entities.length === 0}
          <span class="value dim">まだ図形が無い(平面をクリックして描く)</span>
        {/if}
        {#each active.entities as entity (entity.entity_id)}
          <div class="item" data-entity={entity.entity_id} data-kind={entity.kind}>
            <span class="label">{entity.kind}</span>
            {#each shown(entity) as [key, value] (key)}
              <label class="param" for={`param-${entity.entity_id}-${key}`}>{key}</label>
              <input
                id={`param-${entity.entity_id}-${key}`}
                data-param={key}
                type="text"
                inputmode="decimal"
                autocomplete="off"
                disabled={entity.refs[key] === null}
                title={entity.refs[key] ?? '素の数値(打ち直せない)'}
                value={editing[editKey(entity.entity_id, key)] ?? String(value)}
                oninput={(event) => {
                  editing = {
                    ...editing,
                    [editKey(entity.entity_id, key)]: (event.target as HTMLInputElement).value,
                  }
                }}
                onkeydown={(event) => {
                  if (event.key !== 'Enter') return
                  event.preventDefault()
                  commitParam(entity.entity_id, key, entity.refs[key])
                }}
              />
            {/each}
          </div>
        {/each}
      </div>

      <div class="list" id="sketch-regions">
        {#if active.regions.length === 0}
          <span class="value dim">閉領域なし</span>
        {/if}
        {#each active.regions as region (region.index)}
          <button
            class="region"
            class:on={selectedRegions.has(region.index)}
            id={`region-${region.index}`}
            data-region={region.index}
            onclick={() => onSelectRegion(active.sketch_id, region.index)}
          >
            領域 {region.index} ・ {region.area.toFixed(1)} mm²
          </button>
        {/each}
      </div>
    </div>
  {/if}
</section>

<style>
  /* 高さを固定する(内容で伸ばさない)。伸びるとビューポートがリサイズされ、
     射影が変わって直前に測ったクリック位置が別の場所に当たる。 */
  .sketch-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    height: 96px;
    overflow: hidden;
    padding: 6px 8px;
    border-top: 1px solid #2a3038;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }

  .lists {
    display: flex;
    gap: 12px;
    flex: 1 1 auto;
    min-height: 0;
  }

  .list {
    flex: 1 1 0;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .item {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .label {
    color: var(--dim);
    font-size: 12px;
    flex: 0 0 auto;
  }

  .param {
    color: var(--dim);
    font-size: 11px;
  }

  .item input {
    width: 68px;
  }

  .value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dim {
    color: var(--dim);
  }

  .tag.on {
    background: #ffb454;
    color: #1a1206;
    padding: 1px 6px;
    border-radius: 3px;
  }

  button.region.on {
    border-color: #ffb454;
  }
</style>
