/**
 * 検査スクリプトの引数と出力 — 5本が同じものを持っていたので1つにした(2026-08-15)。
 *
 * `--key value` と `--flag` だけを解く素朴なもの。**検査スクリプトの引数はこれで足りている**
 * ので、依存(commander等)は入れない。
 */

/** `--key value` / `--flag` を `{key: value|'true'}` にする。 */
export function parseArgs(argv) {
  const parsed = {}
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i]
    if (!item.startsWith('--')) continue
    const key = item.slice(2)
    const next = argv[i + 1]
    if (next && !next.startsWith('--')) {
      parsed[key] = next
      i += 1
    } else {
      parsed[key] = 'true'
    }
  }
  return parsed
}

/** 1件通ったことを出す。 */
export function ok(message) {
  console.log(`  ok   ${message}`)
}

/** 検査を止める。**例外にする**(戻り値で失敗を返すと、呼び忘れが静かに通る)。 */
export function fail(message) {
  throw new Error(message)
}
