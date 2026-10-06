<script lang="ts" module>
  /**
   * 打った名前に拡張子を付けて返す。打った側に同じ拡張子が既に付いていれば重ねない
   * (貼り付けた `model.json` が `model.json.json` にならないように)。大小文字は見ない。
   */
  export function withSuffix(stem: string, suffix: string): string {
    const trimmed = stem.trim()
    const bare = trimmed.toLowerCase().endsWith(suffix.toLowerCase())
      ? trimmed.slice(0, -suffix.length)
      : trimmed
    return bare + suffix
  }

  /** `withSuffix` の逆。拡張子が付いていなければそのまま。 */
  export function withoutSuffix(name: string, suffix: string): string {
    return name.toLowerCase().endsWith(suffix.toLowerCase()) ? name.slice(0, -suffix.length) : name
  }
</script>

<script lang="ts">
  /**
   * 拡張子を**欄の外に固定で出す**入力欄。打つのは名前だけで、拡張子は打つことも消すこともない
   * (本人指示 2026-10-07)。送るときに `withSuffix` で付けるのは呼ぶ側。
   *
   * 貼り付けたときと欄を離れたときは、打った側の同じ拡張子を落とす(欄に `model.json.json` と
   * 見えたまま `model.json` を送る食い違いを残さない)。打っている途中では落とさない ——
   * `a.jso` に `n` を足した瞬間に `a` へ戻ると、打った文字が消えたように見える。
   */
  let {
    id,
    suffix,
    value = $bindable(''),
    placeholder = '',
    onkeydown,
  }: {
    id: string
    suffix: string
    value?: string
    placeholder?: string
    onkeydown?: (event: KeyboardEvent) => void
  } = $props()

  function dropTypedSuffix(): void {
    value = withoutSuffix(value, suffix)
  }
</script>

<!-- 枠と凹んだ面は外側が持つ(styles.css の `input` と同じ見た目)。拡張子まで含めて1つの欄に見せる。 -->
<label class="suffix-field">
  <input
    {id}
    type="text"
    autocomplete="off"
    spellcheck="false"
    {placeholder}
    bind:value
    {onkeydown}
    oninput={(event) => {
      if (event instanceof InputEvent && event.inputType === 'insertFromPaste') dropTypedSuffix()
    }}
    onblur={dropTypedSuffix}
  />
  <span class="suffix" aria-hidden="true">{suffix}</span>
</label>

<style>
  .suffix-field {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    align-items: center;
    background: var(--input-bg);
    border: 1px solid var(--line-soft);
    cursor: text;
  }

  .suffix-field:focus-within {
    outline: 1px solid var(--accent);
  }

  .suffix-field input {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
    text-align: right;
    padding-right: 0;
    background: none;
    border: none;
  }

  .suffix-field input:focus {
    outline: none;
  }

  .suffix {
    flex: 0 0 auto;
    padding-right: 10px;
    color: var(--dim);
    user-select: none;
  }
</style>
