/** キー入力の共通の判定。Enter・Esc・Delete を拾うハンドラが同じ規則で見るために1か所に置く。 */

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

/** Enter に何も割り当てていない `<input>` の型。表示のチェックボックスやスライダーにフォーカスが残っていても、Enter は窓のものにする。 */
const ENTER_IDLE_INPUT_TYPES = new Set(['checkbox', 'radio', 'range'])

/**
 * フォーカス先が Enter を自分の操作に使うか(入力欄・ボタン・選択肢・リンク・開閉・編集可能な要素)。
 * 偽のときだけ、窓の Enter がコマンドの確定になる。ボタンを除くのは、フォーカスの残ったボタン(undo など)の Enter を奪うと
 * そのボタンでなくコマンドが確定するから。選択肢も同じ(AI パネルの思考の設定にフォーカスがある Enter)。
 */
export function ownsEnterKey(target: { tagName?: string; type?: string; isContentEditable?: boolean } | null): boolean {
  if (target === null) return false
  if (target.tagName === 'INPUT' && ENTER_IDLE_INPUT_TYPES.has((target.type ?? 'text').toLowerCase())) return false
  return ENTER_OWNING_TAGS.has(target.tagName ?? '') || target.isContentEditable === true
}

type KeyTarget = {
  tagName?: string
  type?: string
  isContentEditable?: boolean
  closest?(selector: string): unknown
}

/** 文字を打たない `<input>`。フォーカスが乗っているだけなので、Esc・Delete は窓のものとして扱う。 */
const NON_TEXT_INPUT_TYPES = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'])

/** フォーカス先が文字を打つ欄(入力欄・選択肢・編集可能な要素)か。真なら Delete・Backspace はその欄の編集で、選んだものの削除ではない。 */
export function isTextEntry(target: KeyTarget | null): boolean {
  if (target === null) return false
  if (target.isContentEditable === true) return true
  if (target.tagName === 'TEXTAREA' || target.tagName === 'SELECT') return true
  return target.tagName === 'INPUT' && !NON_TEXT_INPUT_TYPES.has((target.type ?? 'text').toLowerCase())
}

/**
 * フォーカス先が Esc を自分のものとして使うか(文字を打つ欄か、開いているダイアログの中)。真なら窓の Esc でコマンドを中断しない:
 * AI の入力欄・ファイル名の欄・設定で押した Esc が、起動中のコマンドと選択まで消してしまう。
 * コマンド自身の引数の欄はここでは区別しない(その欄の Esc は中断でよいので、呼び出し側が先に外す)。
 */
export function ownsEscapeKey(target: KeyTarget | null): boolean {
  if (target === null) return false
  return isTextEntry(target) || (typeof target.closest === 'function' && target.closest('dialog[open]') != null)
}
