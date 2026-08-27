/**
 * ファイル操作の判定 —— **`CommandDef` はもう1つも無い**(2026-08-28)。
 *
 * 保存/開く/新規/一覧/STEP読み込み/STEP書き出し/STL書き出しは `FilesPanel.svelte`(左サイド
 * バー)が担当し、`session.runVerb(...)` を直接呼ぶ(`SketchPanel.svelte` の
 * `onSetParameter` と同じ形。2026-08-27にツールバーの`CommandHost`から移設した)。
 * 残っていた `IMPORT_SVG` も2026-08-28に `commands/sketch.ts` へ移った ——
 * `sketch_id` を必須で取るスケッチのverbになり、ファイル操作ではなくスケッチモードの
 * コマンドになったため。**この結果、ツールバー(`CommandHost`)に載る `file` 分類の
 * コマンドは1つも無い。**
 *
 * ここに残るのは `changesDocument` だけ —— `FilesPanel.svelte` が直接呼ぶverbのうち
 * どれが文書を変えるか(応答が状態配信で返るか)を判定する、`App.svelte` 側の1関数。
 */

/**
 * そのverbは**文書を変えるか**(＝応答が状態配信で返るか)。
 *
 * 保存と書き出しは文書を変えないので、応答は `{"type":"file"}` で要求した接続にだけ返る。
 * 送った側が「応答待ち」を解く条件が違うので、ここで見分けられるようにしてある。
 * `open_document` / `import_step` は`CommandDef`の一覧を経由しないが、**verb名としては
 * 変わっていない**ので判定はそのまま残す。`import_svg` は2026-08-28に `commands/sketch.ts`
 * へ移り、`App.svelte` の `'sketch'` 分岐から送られるようになったが、**文書を変えることに
 * 変わりはない**ので、この判定からは外していない(`FilesPanel.svelte` はもう `import_svg` を
 * 呼ばないので実質使われないが、verb名で判定する形を保つほうが「同じverb名は同じ判定」を
 * 崩さない)。
 */
export function changesDocument(verb: string): boolean {
  return verb === 'open_document' || verb === 'import_step' || verb === 'import_svg'
}
