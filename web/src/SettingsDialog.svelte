<script lang="ts">
  /**
   * 設定ダイアログ — 一度決めたらほとんど変えない設定の置き場(作業中に切り替えるものはその場のパネル。AIの接続先は `AiPanel`)。
   * スキーマ駆動・カテゴリ・タブは持たず、平らな並びに行を足す。
   *
   * ビューポートを覆わない・暗くしない・操作を止めない(FOV は見ながら決める設定なので、判断材料を隠せない)。
   * `showModal()` ではなく `show()`(非モーダル)で開く: `showModal()` は背後を inert にし、ビューポートもビューキューブも
   * pointer/keyboard イベントを受け取れなくなる。代わりに自前で持つもの: Esc で閉じる(非モーダルの `<dialog>` は
   * Esc を拾わないので `svelte:window`)、重なり順(top-layer に入らないので CSS で `z-index`)。フォーカストラップは戻さない。
   * レイアウトの大きさを持たないのは `position: absolute` による(`.viewport` の寸法＝カメラのアスペクトに触らない。styles.css の不変条件)。
   *
   * 状態の正は `displaySettings.svelte.ts`(画角)・サーバの `ai_status`(APIキー)・`fem_status`(GPU)。ここは値と口を受け取るだけ。
   * APIキーは書くだけで値は画面へ戻らない(サーバが返すのは置いてあるか・どこから読んだか・末尾4文字)。打ちかけは送ったら捨てる。
   */
  import { onMount } from 'svelte'
  import type { AiStatus, FemStatus } from './session'
  import { loadConfig, restart, restartServer } from './serverRestart.svelte'
  import { fail } from './Toast.svelte'
  import { FOV_DEFAULT, FOV_ORTHO } from './viewport'

  const { open, fov, onFov, aiStatus, onSetApiKey, femStatus, onFemDevice, onClose }: {
    open: boolean
    /** 今の画角[度]。`FOV_ORTHO`(0)なら完全正投影。UIは 0 か `FOV_DEFAULT` しか選べないが、チェックボックスは `fov === FOV_ORTHO` の1点だけで決まる。 */
    fov: number
    /** その場で効く(「適用」で確定させない)。 */
    onFov: (deg: number) => void
    /** `null` は接続前(キーの欄は打てない)。 */
    aiStatus: AiStatus | null
    onSetApiKey: (key: string | null) => void
    /** `null` は接続前。 */
    femStatus: FemStatus | null
    /** 装置を選ぶ(**その場で効く**。次に準備する計算から)。 */
    onFemDevice: (device: 'cpu' | 'gpu') => void
    onClose: () => void
  } = $props()

  const ortho = $derived(fov === FOV_ORTHO)

  let keyDraft = $state('')
  /** 環境変数のキーはファイルより優先されるので、ここで書いても効かない(欄ごと止める)。 */
  const keyFromEnv = $derived(aiStatus?.apiKeySource === 'env')
  const keyLine = $derived.by(() => {
    if (!aiStatus) return '接続を待っている'
    const hint = aiStatus.apiKeyHint ? `(…${aiStatus.apiKeyHint})` : ''
    if (aiStatus.apiKeySource === 'env') {
      return `環境変数 DUOCAD_ANTHROPIC_API_KEY で設定済み${hint}。こちらが優先されるので、ここでは変えられない。`
    }
    if (aiStatus.apiKeySource === 'file') return `設定済み${hint}。保存したキーは画面に戻らない。`
    return '未設定。保存するとリポジトリ直下の duocad_secrets.json に書く(gitには載らない)。'
  })

  function saveKey(): void {
    const key = keyDraft.trim()
    if (!key) return
    onSetApiKey(key)
    keyDraft = ''
  }

  /** GPU の部品を画面から入れられるか(安定版だけ。開発版は手で `uv sync --extra gpu`)。 */
  let canInstallGpu = $state(false)
  /** GPU の欄を出すか。GPU で解くのは形状最適化だけなので、形状最適化を出さない版(安定版)では欄ごと出さない。 */
  let showGpu = $state(false)
  onMount(() => {
    void loadConfig().then((config) => {
      canInstallGpu = config?.update_supported === true
      showGpu = config?.features?.shape_optimization !== false
    })
  })

  const femLine = $derived.by(() => {
    if (!femStatus) return '接続を待っている'
    if (femStatus.error) return femStatus.error
    if (femStatus.device === 'cpu') return 'CPU で解く(既定)。NVIDIA の GPU がある機械だけ有効にする。'
    if (!femStatus.cupyInstalled) {
      return canInstallGpu
        ? 'GPU の部品(CuPy)が入っていないので、CPU で解いている。下のボタンで入れる。'
        : 'GPU の部品(CuPy)が入っていないので、CPU で解いている。開発版では uv sync --extra gpu を手で走らせる。'
    }
    if (femStatus.gpuProblem) return `GPU を使えないので、CPU で解いている: ${femStatus.gpuProblem}`
    return `${femStatus.gpuName ?? 'GPU'} で解く。次に準備する計算から効く。答えは CPU と同じ。`
  })

  function installGpu(): void {
    void restartServer('api/update?mode=gpu', {
      title: 'GPU の部品を入れている…',
      detail: '約 2GB を取ってきて入れ、サーバを立て直す。終わると自動で読み直す(数分かかることがある)',
      failPrefix: 'GPU の部品を入れ始められなかった',
    }, fail)
  }

  let dialog: HTMLDialogElement

  // 開いている <dialog> に `show()` を呼ぶと例外なので、現在の状態を見てから呼ぶ。
  $effect(() => {
    if (open && !dialog.open) dialog.show()
    if (!open && dialog.open) dialog.close()
  })

  /**
   * Esc で閉じる。非モーダルの `<dialog>` は Esc を拾わない(close request は `showModal()` にしか来ない)ので window で拾う。
   * `preventDefault()` も `stopPropagation()` もしない: 同じ Esc を `CommandHost` が構え中コマンドの中断に使っている
   * (`CommandHost.svelte` の `onKeydown`)。
   */
  function onWindowKeydown(event: KeyboardEvent): void {
    if (!open || event.key !== 'Escape') return
    onClose()
  }
</script>

<svelte:window onkeydown={onWindowKeydown} />

<dialog id="settings-dialog" class="floating-panel" bind:this={dialog} onclose={onClose}>
  <div class="settings-head">
    <strong>設定</strong>
    <span class="spacer"></span>
    <button id="settings-close" onclick={onClose} title="設定を閉じる">閉じる</button>
  </div>

  <!-- 画角は正投影/透視(`FOV_DEFAULT`)の2値だけ。戻る先は常に `FOV_DEFAULT` なので戻る角度は覚えない
       (内部APIで70等を入れてから正投影を入切すると30に落ちるのは許容)。 -->
  <label class="field-row">
    正投影
    <input
      id="settings-ortho"
      type="checkbox"
      checked={ortho}
      onchange={(event) => onFov(event.currentTarget.checked ? FOV_ORTHO : FOV_DEFAULT)}
    />
  </label>
  <p class="hint dim">
    切り替えても見かけの大きさは変わらない。正投影ではパースが完全に消え、
    同じ長さの辺は奥行きが違っても同じ長さに写る。
  </p>

  <!-- パスワード欄にする(覗き込みと実機検査のスクリーンショットに値を写さない)。 -->
  <label class="field-row key-row">
    Claude APIキー
    <input
      id="settings-api-key"
      type="password"
      autocomplete="off"
      spellcheck="false"
      placeholder="sk-ant-..."
      bind:value={keyDraft}
      disabled={!aiStatus || keyFromEnv}
      onkeydown={(event) => {
        if (event.key === 'Enter') saveKey()
      }}
    />
  </label>
  <div class="key-actions">
    <button id="settings-api-key-save" onclick={saveKey} disabled={!aiStatus || keyFromEnv || !keyDraft.trim()}>
      保存
    </button>
    {#if aiStatus?.apiKeySource === 'file'}
      <button id="settings-api-key-delete" onclick={() => onSetApiKey(null)}>削除</button>
    {/if}
  </div>
  <p class="hint dim" data-api-key-status>{keyLine}</p>

  {#if showGpu}
    <!-- **答えは変わらない**(GPU の解は CPU とビット一致する。docs/TOPOPT_GPU.md)ので、確認なしでその場で切り替える。 -->
    <label class="field-row gpu-row">
      GPU で計算する
      <input
        id="settings-gpu"
        type="checkbox"
        checked={femStatus?.device === 'gpu'}
        disabled={!femStatus}
        onchange={(event) => {
          onFemDevice(event.currentTarget.checked ? 'gpu' : 'cpu')
          // 表示はサーバの確定値(`fem_status`)に揃える。拒否されて同じ値が返ると Svelte は checked を
          // 書き直さないので、押した分はここで戻しておく(受け付けられれば返事で付け直される)。
          event.currentTarget.checked = femStatus?.device === 'gpu'
        }}
      />
    </label>
    <p class="hint dim" data-fem-status>{femLine}</p>
    {#if femStatus?.device === 'gpu' && !femStatus.cupyInstalled && canInstallGpu}
      <div class="key-actions">
        <button id="settings-gpu-install" onclick={installGpu} disabled={restart.current !== null}>
          GPU の部品を入れる(約 2GB・再起動)
        </button>
      </div>
    {/if}
  {/if}
</dialog>

<style>
  /* 非モーダル(`show()`)の <dialog> は `::backdrop` を描かないので指定しない。 */

  /* ビューポート左上のブラウザ(`BrowserPanel.svelte`)のすぐ右。基準は `.stage`(App.svelte で `position: relative`)。
     `10px` と幅 `--browser-panel-w` はブラウザと共有(直値だと片方だけ動かした日に重なるか隙間が空く)。
     `position: absolute` なので `.viewport` の寸法に触れない(styles.css の不変条件)。
     非モーダルは top-layer に入らないので重なり順は `z-index` で決める: 隣のブラウザが `5` なのでそれより上。 */
  #settings-dialog {
    position: absolute;
    inset: auto;
    top: calc(10px + var(--toolbar-overlay-h, 0px));
    left: calc(10px + var(--browser-panel-w) + 10px);
    z-index: 10;
    width: 260px;
    margin: 0;
  }

  .settings-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }

  #settings-ortho {
    margin: 0;
  }

  .hint {
    margin: 6px 0 0;
  }

  .key-row,
  .gpu-row {
    margin-top: 12px;
  }

  #settings-gpu {
    margin: 0;
  }

  #settings-api-key {
    flex: 1;
    min-width: 0;
  }

  .key-actions {
    display: flex;
    justify-content: flex-end;
    gap: 6px;
    margin-top: 6px;
  }
</style>
