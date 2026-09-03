<script module lang="ts">
  /**
   * トースト — **エラーを人間に気づかせる唯一の場所**。
   *
   * クリック即適用のUIでは、**操作が拒まれたことがステータスバーの1行では気づけない**。
   * 情報(状態の説明・成功の報告)はステータスバーに残し、**「拒まれた・失敗した・
   * 繋がらない」だけ**をここへ出す。
   *
   * ## 呼び方(これが公開APIの全部)
   *
   * ```ts
   * import Toast, { showToast } from './Toast.svelte'
   * showToast('エラー[fillet] 半径が大きすぎる')
   * ```
   *
   * **文字列を1つ渡すだけ**にしてある(種別・寿命・位置の引数を持たせない)。後から
   * 「これは警告だから薄く」を足し始めると、呼ぶ側がその都度分類を判断することになり、
   * **エラー経路を1本にした意味が消える** —— ここへ来た時点でエラーである。
   *
   * インスタンスではなくモジュールに状態を置いてあるので、`bind:this` を配り歩かずに
   * どこからでも呼べる。**ただし呼んでよい場所は決まっている**: WSの応答を1箇所で受ける層
   * (`App.svelte` の `fail()`)から出す。各パネルが個別に叩き始めると、同じ失敗が
   * 2枚出たり、パネルを外すとエラーが消えたりする。
   *
   * ## 見た目より先に決めた3つ
   *
   * 1. **ビューポートの大きさを変えない。** `position: absolute` で `.main` の上に浮かせる
   *    (`styles.css` の不変条件: `.main` の中で大きさを持つのは `.viewport` だけ)。
   *    通常のフローに置くと、エラーが出た瞬間にカメラのアスペクトが変わり
   *    **さっき見えていた面がずれる** —— 打ち直しのために選び直す羽目になる。
   * 2. **クリックを吸わない。** 本体は `pointer-events: none` にし、閉じるボタンだけ受ける。
   *    CADの主対象はcanvasなので、失敗の直後に画面へ出た札が次のクリックを飲むのは最悪である
   *    (実機検査も生の座標でcanvasを叩いている)。
   * 3. **同じ文言は積まずに数える。** 再接続の試行のように同じエラーが連発する経路があり、
   *    素直に積むと画面がその1種類で埋まる。
   */

  /** 1枚。`timer` は自動消滅の予約(手で閉じたら取り消す)。 */
  type ToastItem = {
    id: number
    message: string
    /** この札が出ている間に同じ文言が来た回数。2以上のときだけ画面に出す。 */
    count: number
    timer: ReturnType<typeof setTimeout> | null
  }

  /**
   * 自動で消えるまで。**エラーは読み切れる長さが要る**ので、よくある3秒より長くしてある
   * (日本語1行を読んで、何をやり直すか決めるまでの時間)。
   */
  const LIFETIME_MS = 8000
  /** 同時に出す上限。溢れたら古い方から捨てる(画面がトーストで埋まる方が有害)。 */
  const MAX_VISIBLE = 5

  let items = $state<ToastItem[]>([])
  let lastId = 0

  /** 自動消滅の予約を取り消す(二重に消さないよう `null` に戻す)。 */
  function stop(item: ToastItem): void {
    if (item.timer === null) return
    clearTimeout(item.timer)
    item.timer = null
  }

  /** 寿命を数え直す(同じ文言が来たときは、最初の1枚の寿命を延ばす)。 */
  function restart(item: ToastItem): void {
    stop(item)
    item.timer = setTimeout(() => dismiss(item.id), LIFETIME_MS)
  }

  function dismiss(id: number): void {
    const found = items.find((item) => item.id === id)
    if (found === undefined) return
    stop(found)
    items = items.filter((item) => item.id !== id)
  }

  /** エラーを1件、画面へ出す。**これが公開APIの全部**(上のdocstring参照)。 */
  export function showToast(message: string): void {
    const text = message.trim()
    if (text === '') return // 空文字で空の札を出さない(`note()` と同じ扱い)
    // **出ている札の全部を見る**(直近の1枚だけでは足りない)。WSが切れている間は
    // 「WebSocketのエラー」と「接続が切れた」が交互に届くので、直近だけを見ると
    // A,B,A,B… と積み上がって画面が2種類のエラーで埋まる。
    const same = items.find((item) => item.message === text)
    if (same !== undefined) {
      same.count += 1
      restart(same) // まだ起きている事象なので寿命を数え直す
      return
    }
    const created: ToastItem = { id: (lastId += 1), message: text, count: 1, timer: null }
    const next = [created, ...items]
    // 溢れた分は**予約も取り消してから**捨てる(捨てた札のタイマーが後から起きないように)
    for (const dropped of next.slice(MAX_VISIBLE)) stop(dropped)
    items = next.slice(0, MAX_VISIBLE)
    // **`created` ではなく `items[0]` に予約を書く。** `$state` に入れた時点で中身は
    // プロキシに包まれており、素のオブジェクトを触ると「同じものに見えて別の口」になる。
    restart(items[0])
  }
</script>

<!-- `id="toasts"` は実機検査が文言を読むための穴(`.toast-text` が1枚ぶんの本文)。
     **中身が空でも枠は出す** —— 検査が「今は0件」を querySelectorAll で素直に書ける。 -->
<div id="toasts" class="toasts" role="log" aria-live="assertive" aria-relevant="additions">
  {#each items as item (item.id)}
    <div class="toast" data-toast-id={item.id}>
      <span class="toast-text">{item.message}</span>
      {#if item.count > 1}
        <span class="toast-count">{item.count}回</span>
      {/if}
      <button class="toast-close" aria-label="この通知を閉じる" onclick={() => dismiss(item.id)}>
        ×
      </button>
    </div>
  {/each}
</div>

<style>
  /* ビューポートの右下に浮かせる。`.main` が位置の基準(styles.css で position:relative)。
     右サイドバー(AIパネル)の幅は `--sidebar-w` が正 —— ここに 300px を書き写すと、幅を変えた日に
     トーストだけサイドバーの下へ潜る。 */
  .toasts {
    position: absolute;
    bottom: 12px;
    right: calc(var(--sidebar-w) + 16px);
    z-index: 20;
    display: flex;
    /* 新しい札をコーナー側(下)に置く。column-reverseなので配列の先頭(最新)が一番下に来る。 */
    flex-direction: column-reverse;
    gap: 6px;
    width: 360px;
    max-width: 40%;
    /* クリックはcanvasへ素通しする(上のdocstring 2) */
    pointer-events: none;
  }

  .toast {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 8px 6px 10px;
    background: var(--panel);
    border: 1px solid var(--accent-line);
    /* 左の太い帯で「これはエラー」を色で言う(琥珀の配色から外れない) */
    box-shadow:
      inset 3px 0 0 var(--accent),
      0 2px 10px oklch(0% 0 0 / 0.45);
    pointer-events: none;
  }

  .toast-text {
    flex: 1 1 auto;
    /* **折り返す。** エラーは末尾に理由が書いてあることが多く、`text-overflow: ellipsis`
       (ステータスバーの `.value` の作り)だと肝心の部分が消える。 */
    overflow-wrap: anywhere;
  }

  .toast-count {
    flex: 0 0 auto;
    font-size: 11px;
    color: var(--dim);
  }

  .toast-close {
    flex: 0 0 auto;
    padding: 0 6px;
    line-height: 1.2;
    border-color: transparent;
    background: transparent;
    color: var(--dim);
    /* 本体が素通しなので、受け取るのはこのボタンだけ */
    pointer-events: auto;
  }
</style>
