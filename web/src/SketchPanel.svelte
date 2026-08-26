<script lang="ts">
  /**
   * スケッチのパネル — **モードの出入りと、置いた図形の打ち直し**。
   *
   * この企画の中心命題「手動操作が一級の操作モード」がUIに現れる場所。ここが持つのは
   * 3つだけで、どれも**コアを叩くだけ**(パネルは何も覚えない):
   *
   * 1. 既にあるスケッチに入る(再開)/抜ける/正面から見る —— **モードの操作だけ**
   * 2. 図形の一覧と**値の打ち直し**(`set_parameter`)——
   *    クリックだけで置いた後に寸法を直せるのが§3.4のパラメトリック・プリミティブ
   *
   * **領域の一覧は無い**。領域の選択は3Dクリックのみ(`__duocad.projectRegion` / viewport)
   * —— 描画コマンド起動中はクリックが点に取られるため、描画中に領域を選ぶにはEscで「選ぶ」へ
   * 戻る1手が増えるが、それは受け入れた帰結。
   *
   * **スケッチを作るボタンはここに無い**。作成はツールバーの「スケッチ」コマンド → 貼る先を
   * クリック、の2手に一本化してある —— 平面ボタンを残すと「ボタンで指す」と「クリックで指す」
   * の2つの指し方が並ぶ(UI指針1)。
   *
   * **見た目は作り込まない**(タスクの制約)。高さは固定してある —— 内容任せにすると
   * 図形が増えるたびにビューポートがリサイズされ、射影が変わって次のクリックが
   * 別の場所に当たる。
   */
  import type { SketchState } from './session'

  let {
    sketches,
    activeSketchId,
    onEnter,
    onExit,
    onLookAt,
    onSetParameter,
    selectedEntities = [],
    onToggleEntity = () => {},
  }: {
    sketches: SketchState[]
    activeSketchId: string | null
    onEnter: (sketchId: string) => void
    onExit: () => void
    onLookAt: () => void
    onSetParameter: (name: string, value: number) => void
    /**
     * 反復の対象に選んである図形。**共有選択集合とは別物**で、
     * コアの選択集合に図形は入らない(`commands/command.ts` の `EntitiesArgSpec`)。
     * 3Dでクリックしても図形は選べない(平面のクリックは点か領域になる)ので、
     * **図形を選べる場所はこの一覧だけ**である。
     */
    selectedEntities?: string[]
    onToggleEntity?: (entityId: string) => void
  } = $props()

  const active = $derived(sketches.find((item) => item.sketch_id === activeSketchId) ?? null)

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

  function shownPattern(pattern: SketchState['patterns'][number]): Array<[string, number]> {
    return Object.entries(pattern.params)
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
      <!-- 作るのはツールバーの「スケッチ」から(ここに平面ボタンは置かない)。
           残すのは既にあるスケッチを再開する手だけ。 -->
      <span class="value dim">
        {sketches.length === 0
          ? 'まだスケッチが無い(ツールバーの「スケッチ」→ 貼る先をクリック)'
          : '再開するスケッチを選ぶ'}
      </span>
      {#each sketches as item (item.sketch_id)}
        <button class="reopen" onclick={() => onEnter(item.sketch_id)}>
          再開 {item.plane.kind === 'base' ? item.plane.name : '面'}({item.entities.length})
        </button>
      {/each}
    {/if}
  </div>

  {#if active}
    <div class="list" id="sketch-entities">
      {#if active.entities.length === 0}
        <span class="value dim">まだ図形が無い(平面をクリックして描く)</span>
      {/if}
      {#each active.entities as entity (entity.entity_id)}
        <div
          class="item"
          class:picked={selectedEntities.includes(entity.entity_id)}
          data-entity={entity.entity_id}
          data-kind={entity.kind}
          data-picked={selectedEntities.includes(entity.entity_id)}
        >
          <!-- 反復の対象に取る/外す。種別のところがそのままボタン -->
          <button
            class="pick"
            id={`entity-${entity.entity_id}`}
            title="反復の対象にする"
            onclick={() => onToggleEntity(entity.entity_id)}
          >
            {entity.kind}
          </button>
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
      {#each active.patterns as pattern (pattern.pattern_id)}
        <div class="item" data-pattern={pattern.pattern_id} data-kind={pattern.kind}>
          <!-- 反復はラベルのみ(ボタンにしない) —— 図形の `kind` ボタンは
               「反復の対象に取る/外す」の意味だが、反復を反復の対象には取れない。 -->
          <span class="label" title={pattern.kind}>
            {pattern.kind === 'circular' ? '円形反復' : '矩形反復'}
          </span>
          <span class="param">元 {pattern.sources.length}個</span>
          {#each shownPattern(pattern) as [key, value] (key)}
            <label class="param" for={`param-${pattern.pattern_id}-${key}`}>{key}</label>
            <input
              id={`param-${pattern.pattern_id}-${key}`}
              data-param={key}
              type="text"
              inputmode="decimal"
              autocomplete="off"
              disabled={pattern.refs[key] === null}
              title={pattern.refs[key] ?? '素の数値(打ち直せない)'}
              value={editing[editKey(pattern.pattern_id, key)] ?? String(value)}
              oninput={(event) => {
                editing = {
                  ...editing,
                  [editKey(pattern.pattern_id, key)]: (event.target as HTMLInputElement).value,
                }
              }}
              onkeydown={(event) => {
                if (event.key !== 'Enter') return
                event.preventDefault()
                commitParam(pattern.pattern_id, key, pattern.refs[key])
              }}
            />
          {/each}
        </div>
      {/each}
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
    /* 右サイドバー(幅300px)に入って横幅が減ったぶん、ヘッダー行(編集中タグ+ボタン)が
       折り返す(下の`.row`参照)。折り返した分の高さをここで確保しないと図形の
       一覧欄が潰れ、入力欄が枠の外に出て操作できなくなる(smoke.mjs の半径打ち直しが
       ここで失敗する)。 */
    height: 168px;
    /* **縮ませない**。`height` だけでは縦に並べたときに flex が他のパネルと
       一緒に押し潰す —— パネルが増えると height の指定だけでは縮められ、
       図形の入力欄が枠の外へ出てクリックできなくなる。
       縮んでよいのはビューポート(`.main`)だけである。右サイドバーの直接の子でも
       同じ理由で明示する(サイドバーは overflow-y:auto で自分の中だけスクロールする)。 */
    flex: 0 0 auto;
    overflow: hidden;
    padding: 6px 8px;
    border-top: 1px solid var(--line);
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
    /* 300px幅だとタグ+ボタン2つが1行に収まらない。折り返さないと
       タグの文字自体が箱の中で折り返して縦に伸び、高さの見積もりが立たなくなる
       (上の`.sketch-panel`のコメント参照)。単位を1行ずつの
       折り返しにしておけば、増える高さは行数×行高で読める。 */
    flex-wrap: wrap;
    row-gap: 4px;
  }

  .list {
    flex: 1 1 auto;
    min-height: 0;
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
    background: var(--accent);
    color: var(--bg);
    padding: 1px 6px;
    border-radius: 3px;
  }

  /* 反復の対象に取ったことを示す(選ばれている、の意味を1色に保つ) */
  .item.picked button.pick {
    border-color: var(--accent);
  }
</style>
