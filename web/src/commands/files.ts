/**
 * ファイル操作 —— **`CommandDef` はここに1つだけある**(`import_step`。2026-09-05)。
 *
 * 保存/開く/新規/一覧/STEP書き出し/STL書き出しは `FilesPanel.svelte`(左サイドバー)が
 * 担当し、`session.runVerb(...)` を直接呼ぶ(`CommandHost` を経由しない)。
 *
 * **STEP読み込みだけが左サイドバーを離れてツールバーへ戻った**(2026-09-05本人決定。
 * 2026-08-27にここへ来た3つのうち、これ1つだけを引き上げる)。理由は本人明言
 * 「STEP読み込みはそもそもファイルバーじゃなくて通常モードの上部に出るべきだった」——
 * **書き出しと違って、これは文書を変えるverbである**(`changesDocument` が下で
 * `import_step` だけを真にしているのと同じ線)。操作ログに1行載り、undoで戻り、
 * 読んだ直後から面をクリックして押し出せる。それはファイルの出し入れではなく
 * モデリングの1手なので、verbの行(`CommandHost`)に並ぶのが正しい。
 *
 * **常設の入力欄にはしない。** 起動して初めてパス欄が出る他のverbと同じ形にすることで、
 * ツールバーに打ち込み欄が居座らない(方針 [[duocad-show-only-what-is-needed]])。
 * `commands/sketch.ts` の `IMPORT_SVG` がまったく同じ形をしている。
 */
import type { CommandDef } from './command'

/** 空文字だけを拒む(パスの解釈はサーバの1箇所に置く。`sketch.ts` の `pathLike` と同じ)。 */
function pathLike(label: string): (value: string) => string | null {
  return (value) => (value.trim() === '' ? `${label}を打つ` : null)
}

/**
 * STEPを読み込む。**引数はパスだけ**(targetを取らないverb)。
 *
 * `id` / `inputId` / `buttonId` は左サイドバーにいた頃と**同じ名前のまま**にしてある ——
 * 実機検査(`smoke.mjs` ほか5本)が名前で掴んでおり、押したときに起きること
 * (パスを打ってSTEPを読む)は変わっていないので、叩く口を変える理由が無い。
 * **変わったのは順序だけ**: 以前は「打つ→押す」、いまは「押す→打つ→Enter」である。
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
  build(values) {
    const path = values.path
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: 'パスが空' }
    return { ok: true, args: { path: path.trim() } }
  },
}

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
