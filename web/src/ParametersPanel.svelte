<script lang="ts">
  /**
   * パラメータ編集パネル — **パラメータプリミティブの存在理由がUIに現れる場所**。
   *
   * クリックだけで置いた図形が、後から**名前つきの値**として打ち直せる
   * (実装計画書§3.4 パラメータプリミティブ)。**1箇所直すと、その値を参照している
   * ものが全部追随する** —— 反復で作った穴が全部太くなるのがその典型で、
   * 「反復を1つのverbとして持つ」(§2)という設計の実利がここで初めて人に見える。
   *
   * `SketchPanel` にも図形ごとの入力欄があるが、あちらは**その図形の寸法**を直す口で、
   * 反復の間隔や個数のように**図形に属さない値**は出てこない。ここは文書の
   * `parameters`(名前 -> 値)を丸ごと出す側で、両方あって困らない
   * (どちらも `set_parameter` を送るだけで、状態は1つも持たない)。
   *
   * **パネルは何も覚えない。** 出しているのはコアの `state.parameters` そのもので、
   * 打ちかけの文字列だけがここにある(送ったら捨てる)。
   */
  import type { SketchState } from './session'

  let {
    parameters,
    sketches,
    onSetParameter,
    onNote = () => {},
  }: {
    /** 文書の名前つきの値(`state.parameters`)。 */
    parameters: Record<string, number>
    /** どの名前がどの図形の何かを言い当てるために使う(`entities[].refs`)。 */
    sketches: SketchState[]
    onSetParameter: (name: string, value: number) => void
    /** 打った値が使えないときに理由を出す(**黙って何もしない、をしない**)。 */
    onNote?: (message: string) => void
  } = $props()

  /** 打ちかけの値(名前 -> 文字列)。Enterまでコアへ送らない。 */
  let editing = $state<Record<string, string>>({})

  const names = $derived(Object.keys(parameters).sort())

  /**
   * その名前が何の寸法かを1行にする(例: `circle.radius`)。
   *
   * **当てられないものは名前だけ出す**。反復の間隔・個数は図形に属さないので
   * `refs` から辿れない —— 推測で「たぶん反復の値」と書くより、名前と値をそのまま
   * 出して人に判断させる方が正しい(コアが持っている情報以上のことを言わない)。
   */
  function ownerOf(name: string): string {
    for (const sketch of sketches) {
      for (const entity of sketch.entities) {
        for (const [key, ref] of Object.entries(entity.refs)) {
          if (ref === name) return `${entity.kind}.${key}`
        }
      }
    }
    return ''
  }

  function commit(name: string): void {
    const raw = (editing[name] ?? '').trim()
    const value = Number.parseFloat(raw)
    if (!Number.isFinite(value)) {
      // **理由を出す**。黙って戻ると、Enterを押したのに何も
      // 起きない画面になり、打ち間違えたのか受け付けない値なのかが分からない。
      onNote(`${name}: "${raw}" は数値として読めない`)
      return
    }
    onSetParameter(name, value)
    // **送ったら打ちかけを捨てる**(SketchPanelと同じ規律)。残すと、その後のundoや
    // AIの書き換えでコアの値が変わっても欄が古い文字列を映し続ける。
    const { [name]: _sent, ...rest } = editing
    editing = rest
  }
</script>

<section class="parameters-panel">
  <div class="row">
    <strong>パラメータ</strong>
    <span id="parameters-count" class="value dim" data-count={names.length}>
      {names.length === 0 ? '名前つきの値はまだ無い' : `${names.length} 個`}
    </span>
  </div>

  <div class="list" id="parameters-list">
    {#each names as name (name)}
      <div class="item" data-parameter={name}>
        <span class="label" title={name}>{name}</span>
        {#if ownerOf(name)}<span class="owner">{ownerOf(name)}</span>{/if}
        <input
          id={`parameter-${name}`}
          data-parameter-input={name}
          type="text"
          inputmode="decimal"
          autocomplete="off"
          value={editing[name] ?? String(parameters[name])}
          oninput={(event) => {
            editing = { ...editing, [name]: (event.target as HTMLInputElement).value }
          }}
          onkeydown={(event) => {
            if (event.key !== 'Enter') return
            event.preventDefault()
            commit(name)
          }}
        />
      </div>
    {/each}
  </div>
</section>

<style>
  /* 高さは固定する(内容で伸ばさない)。伸びるとビューポートがリサイズされ、
     射影が変わって直前に測ったクリック位置が別の場所に当たる。SketchPanelと同じ理由。
     **`flex: 0 0 auto` を明示する**。右サイドバーの直接の子であり、
     これが無いと flex-shrink の既定値(1)でサイドバーの残り空間が足りないときに
     縮められる —— SketchPanelが踏んだのと同じ壊れ方(このファイルのdocstring参照)。 */
  .parameters-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    height: 96px;
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
    gap: 6px;
  }

  .label {
    color: var(--dim);
    font-size: 12px;
    flex: 0 0 auto;
    max-width: 180px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .owner {
    color: var(--dim);
    font-size: 11px;
  }

  .item input {
    width: 90px;
  }

  .value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dim {
    color: var(--dim);
  }
</style>
