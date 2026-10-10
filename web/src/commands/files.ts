/**
 * ファイル操作の `CommandDef` は `import_step` だけ。保存/開く/新規/一覧/書き出しは `FilesPanel.svelte` が
 * `session.sendCommand(...)` を直接呼ぶ(`CommandHost` を経由しない)。STEP読み込みは文書を変えるverbなのでverbの行に並ぶ。
 */
import { buildPathArgs, pathLike, type CommandDef } from './command'

/** STEPを読み込む。引数はパスだけ。`id` / `inputId` / `buttonId` は実機検査(`smoke.mjs` ほか)が名前で掴んでいる。 */
export const IMPORT_STEP: CommandDef<'import_step'> = {
  id: 'import_step',
  label: 'STEP読み込み',
  buttonId: 'file-import-step',
  // プレビューなし(サーバの `PREVIEWABLE_VERBS` にも無い)。
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
 * そのverbは文書を変えるか(＝応答が状態配信で返るか)。保存と書き出しは `{"type":"file"}` で要求した接続にだけ返り、
 * 送った側が応答待ちを解く条件が違う。verb名で判定するので、`CommandDef` の一覧に無い
 * `open_document` / `import_step` / `import_svg`(`commands/sketch.ts` から送られる)もそのまま扱える。
 */
export function changesDocument(verb: string): boolean {
  return verb === 'open_document' || verb === 'import_step' || verb === 'import_svg'
}
