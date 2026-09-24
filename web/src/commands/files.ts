/**
 * ファイル操作 —— **`CommandDef` はここに1つだけある**(`import_step`)。
 *
 * 保存/開く/新規/一覧/STEP書き出し/STL書き出しは `FilesPanel.svelte`(左サイドバー)が
 * 担当し、`session.runVerb(...)` を直接呼ぶ(`CommandHost` を経由しない)。
 *
 * **STEP読み込みだけはverbの行(`CommandHost`)に並ぶ。** 書き出しと違って、これは
 * 文書を変えるverbである(`changesDocument` が下で `import_step` だけを真にしているのと
 * 同じ線)。操作ログに1行載り、undoで戻り、読んだ直後から面をクリックして押し出せる。
 * それはファイルの出し入れではなくモデリングの1手だからである。
 *
 * **常設の入力欄にはしない。** 起動して初めてパス欄が出る他のverbと同じ形にすることで、
 * ツールバーに打ち込み欄が居座らない。
 * `commands/sketch.ts` の `IMPORT_SVG` がまったく同じ形をしている。
 */
import { buildPathArgs, pathLike, type CommandDef } from './command'

/**
 * STEPを読み込む。**引数はパスだけ**(targetを取らないverb)。
 *
 * `id` / `inputId` / `buttonId` は実機検査(`smoke.mjs` ほか5本)が名前で掴んでいるので、
 * 押したときに起きること(パスを打ってSTEPを読む)を変えない限り変更しないこと。
 */
export const IMPORT_STEP: CommandDef = {
  id: 'import_step',
  category: 'file',
  label: 'STEP読み込み',
  buttonId: 'file-import-step',
  // プレビューは出さない。読んでみるまで何が入っているか分からないので、
  // 投機実行しても「確定前の姿」にならない(サーバの `PREVIEWABLE_VERBS` にも無い)。
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: 'STEP',
      inputId: 'file-import-path',
      placeholder: 'part.step',
      reject: pathLike('STEPのパス'),
    },
  ],
  build: buildPathArgs,
}

/**
 * そのverbは**文書を変えるか**(＝応答が状態配信で返るか)。
 *
 * 保存と書き出しは文書を変えないので、応答は `{"type":"file"}` で要求した接続にだけ返る。
 * 送った側が「応答待ち」を解く条件が違うので、ここで見分けられるようにしてある。
 * `open_document` / `import_step` は`CommandDef`の一覧を経由しないが、verb名で判定するので
 * そのまま扱える。`import_svg` は `commands/sketch.ts` の `'sketch'` 分岐から送られ、
 * `FilesPanel.svelte` はもう呼ばないが、文書を変えることに変わりはないのでここに残す
 * (同じverb名は同じ判定、を崩さないため)。
 */
export function changesDocument(verb: string): boolean {
  return verb === 'open_document' || verb === 'import_step' || verb === 'import_svg'
}
