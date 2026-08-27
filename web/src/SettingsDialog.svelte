<script lang="ts">
  /**
   * 設定ダイアログ — **一度決めたらほとんど変更しない設定**の置き場(2026-08-27本人決定)。
   *
   * 線引きは「**作業中に切り替えるものはその場のパネル、一度決めたら触らないものはここ**」。
   * AIの接続先(プロバイダ・モデル・エンドポイント)は作業中に触る側なので `AiPanel` に残る。
   *
   * **仕組みを作らない。** スキーマ駆動・カテゴリ・タブは持たず、平らな並びに行を足していく。
   *
   * **ビューポートを覆わない・暗くしない。** 画面の端に小さく出し、`::backdrop` は透明。
   * FOVは**見ながら決める**設定なので、判断材料を隠したらこの機能は使えないものになる。
   * native `<dialog>` + `showModal()` を使うので Esc・フォーカストラップ・top-layer が
   * ただで付き、**position:fixed / top-layer なのでレイアウトの大きさを持たない**
   * (＝`.viewport` の寸法＝カメラのアスペクトに触らない。styles.css の不変条件)。
   *
   * **状態の正は `App.svelte`**。ここは値と口を受け取るだけの presentational な部品で、
   * 双方向束縛をコンポーネントの境界に持ち込まない。
   */
  import { FOV_MAX, FOV_MIN } from './viewport'

  const { open, fov, onFov, onClose }: {
    open: boolean
    fov: number
    /** スライダーを動かした先の角度[度]。**その場で効く**(「適用」で確定させない)。 */
    onFov: (deg: number) => void
    onClose: () => void
  } = $props()

  let dialog: HTMLDialogElement

  // 開閉は props に従う。**`showModal()` は開いている <dialog> に呼ぶと例外**なので
  // 現在の状態を見てから呼ぶ。Esc で閉じたときは `onclose` が App 側へ返す。
  $effect(() => {
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  })
</script>

<dialog id="settings-dialog" bind:this={dialog} onclose={onClose}>
  <div class="settings-head">
    <strong>設定</strong>
    <span class="spacer"></span>
    <button id="settings-close" onclick={onClose} title="設定を閉じる">閉じる</button>
  </div>

  <!-- **平らな並び1つだけ。** 項目が増えたらここに行を足す。 -->
  <label>
    画角(FOV)
    <input
      id="settings-fov"
      type="range"
      min={FOV_MIN}
      max={FOV_MAX}
      step="1"
      value={fov}
      oninput={(event) => onFov(Number.parseFloat(event.currentTarget.value))}
    />
    <span id="settings-fov-value">{Math.round(fov)}°</span>
  </label>
  <p class="hint dim">
    小さいほどパースが弱く(正投影に近く)なる。見かけの大きさは変わらない。
  </p>
</dialog>

<style>
  /* **暗くしない。** 覆うと、見ながら決めるという設定の前提が壊れる。 */
  #settings-dialog::backdrop {
    background: transparent;
  }

  /* 画面の右端に小さく出す(`margin: auto` の中央寄せを打ち消す)。
     top-layer にいるので、この位置指定はどのパネルのレイアウトにも影響しない。 */
  #settings-dialog {
    position: fixed;
    inset: auto;
    top: 64px;
    right: 16px;
    width: 260px;
    margin: 0;
    padding: 8px 10px;
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--line);
  }

  .settings-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }

  .spacer {
    flex: 1 1 auto;
  }

  /* AiPanel の `<label>` + input に揃える。 */
  label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--dim);
  }

  /* 素の `input` は width:7em / text-align:right(styles.css)。スライダーには効かせない。 */
  #settings-fov {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
    padding: 0;
    accent-color: var(--accent);
  }

  #settings-fov-value {
    width: 3.5em;
    text-align: right;
    color: var(--text);
    flex: 0 0 auto;
  }

  .hint {
    margin: 6px 0 0;
    font-size: 11px;
  }
</style>
