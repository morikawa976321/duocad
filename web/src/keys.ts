/** キー入力の共通の判定。Enter・Esc を拾うハンドラが同じ規則で見るために1か所に置く。 */

/**
 * IME の変換中に届いた keydown か。変換候補を確定する Enter と、変換を取り消す Esc は IME のもので、画面のコマンドではない。
 * `isComposing` が偽でも `keyCode === 229`(IME が処理したキー)を併せて見る: Safari は確定の Enter が `compositionend` の
 * 後に届いて `isComposing` が偽になる。Windows の Chrome は処理済みのキーを `key === 'Process'` で渡すので Enter の判定に掛からないが、
 * macOS の Chrome・Safari・Firefox は `key === 'Enter'` のまま渡す。
 */
export function isImeKey(event: { isComposing?: boolean; keyCode?: number }): boolean {
  return event.isComposing === true || event.keyCode === 229
}

/** Enter を自分で処理するフォーカス先のタグ。グローバルな Enter でコマンドを確定してはならない相手。 */
const ENTER_OWNING_TAGS = new Set(['INPUT', 'TEXTAREA', 'BUTTON', 'SELECT', 'A', 'SUMMARY'])

/**
 * フォーカス先が Enter を自分の操作に使うか(入力欄・ボタン・選択肢・リンク・開閉・編集可能な要素)。
 * 偽のときだけ、窓の Enter がコマンドの確定になる。ボタンを除くのは、フォーカスの残ったボタン(undo など)の Enter を奪うと
 * そのボタンでなくコマンドが確定するから。選択肢も同じ(AI パネルの思考の設定にフォーカスがある Enter)。
 */
export function ownsEnterKey(target: { tagName?: string; isContentEditable?: boolean } | null): boolean {
  if (target === null) return false
  return ENTER_OWNING_TAGS.has(target.tagName ?? '') || target.isContentEditable === true
}
