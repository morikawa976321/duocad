<script lang="ts" module>
  /** 名前に拡張子を付けて返す。同じ拡張子が既にあれば重ねない(大小文字は見ない)。 */
  export function withSuffix(stem: string, suffix: string): string {
    const trimmed = stem.trim()
    const bare = trimmed.toLowerCase().endsWith(suffix.toLowerCase())
      ? trimmed.slice(0, -suffix.length)
      : trimmed
    return bare + suffix
  }

  export function withoutSuffix(name: string, suffix: string): string {
    return name.toLowerCase().endsWith(suffix.toLowerCase()) ? name.slice(0, -suffix.length) : name
  }
</script>

<script lang="ts">
  /**
   * 拡張子を欄の外に固定で出す入力欄(本人指示 2026-10-07)。送るときに `withSuffix` で付けるのは呼ぶ側。
   * 貼り付け時と欄を離れたときだけ、打った側の同じ拡張子を落とす。打っている途中では落とさない
   * (`a.jso` に `n` を足した瞬間に `a` へ戻ると、打った文字が消えたように見える)。
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
