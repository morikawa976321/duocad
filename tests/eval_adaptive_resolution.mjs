/**
 * `web/src/adaptiveResolution.ts` を**実際に評価する**ための小さな口。
 *
 *   echo '[[0.05, 20], ["NaN", 0]]' | node tests/eval_adaptive_resolution.mjs
 *
 * `tests/test_adaptive_resolution.py` が呼ぶ。**定数の一致だけでは式の一致を保証できない**
 * (定数が揃ったまま `sqrt` を落とす・`*` を `+` にする等の書き換えが素通りする)ので、
 * 同じ入力を両方の言語で評価して数値を突き合わせる。**ソースの文字列比較はしない** ——
 * 書き方を変えただけで嘘をつくため。
 *
 * 入力は `[world_per_pixel, size_hint]` の配列。**非有限は文字列で運ぶ**
 * (`"NaN"` / `"Infinity"` / `"-Infinity"`)—— JSONに非有限の数値リテラルは無く、
 * Pythonの `json` が書く `NaN` を `JSON.parse` は受け取らない。
 *
 * 出力は入力と同じ並びの
 * `[deviation, angularDeflection, clampDeviation(w, s), angularForDeviation(w), deviationFloored(w, s)]`。
 * **公開関数すべてを通す**(`viewResolution` だけだと、寄り道の関数が式を変えても分からない)。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去。Node 23.6以降は既定で有効。v24で実測)。
 * 読めない場合は呼び出し側の検査が**理由を添えて落ちる**(skipしない —— skipは無人実行では
 * 緑と区別が付かない)。
 */
import { angularForDeviation, clampDeviation, deviationFloored, viewResolution } from '../web/src/adaptiveResolution.ts'

const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const cases = JSON.parse(Buffer.concat(chunks).toString('utf8'))

/** `"NaN"` などの文字列を数値へ戻す(素の数値はそのまま)。 */
const toNumber = (value) => (typeof value === 'number' ? value : Number(value))

const results = cases.map(([rawPerPixel, rawSize]) => {
  const perPixel = toNumber(rawPerPixel)
  const size = toNumber(rawSize)
  const resolution = viewResolution(perPixel, size)
  return [
    resolution.deviation,
    resolution.angularDeflection,
    clampDeviation(perPixel, size),
    angularForDeviation(perPixel),
    deviationFloored(perPixel, size),
  ]
})

process.stdout.write(JSON.stringify(results))
