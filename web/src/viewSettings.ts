/**
 * 画面の設定の永続化(localStorage)。
 *
 * **ここに入れてよいのはクライアント側だけの画面設定**(FOV・将来のグリッド表示など)。
 * **文書の状態(コアが持つもの)は絶対に入れない** —— 形・パラメータ・選択・可視性は
 * すべてPython側が正であり、ブラウザに写しを置いた時点で「どちらが本当か」が生まれる。
 * 判定は「これを失っても文書が1ミリも変わらないか」で、Yesのものだけがここに来る。
 *
 * **選べる値の正は `viewport.ts`**(`isSelectableFov`/`FOV_DEFAULT`)。ここで再定義しない ——
 * 正投影(`FOV_ORTHO`=0)を足したときのように、判定が2か所にあると片方だけ古くなる。
 *
 * **どの関数も例外を投げない。** localStorage は「使えない」設定(サードパーティCookieの
 * 遮断・プライベートモード・容量超過)で**読み書きの両方が throw する**ので、失敗は
 * 既定値へ倒して黙って続ける。保存できないことはユーザーが対処できる問題ではないので
 * トーストも出さない。
 */
import { FOV_DEFAULT, isSelectableFov } from './viewport'

/** localStorage の鍵。画面設定は `duocad.view.*` の下に置く。 */
const FOV_KEY = 'duocad.view.fov'

/** 保存されたFOV。無い・壊れている・選べない値・localStorageが使えない → `FOV_DEFAULT`。 */
export function loadFov(): number {
  try {
    const raw = window.localStorage.getItem(FOV_KEY)
    if (raw === null) return FOV_DEFAULT
    const value = Number.parseFloat(raw)
    if (!isSelectableFov(value)) return FOV_DEFAULT
    return value
  } catch {
    return FOV_DEFAULT
  }
}

/** FOVを保存する。書けなくても黙って続行する(呼び出し側は成否を見ない)。 */
export function saveFov(deg: number): void {
  try {
    window.localStorage.setItem(FOV_KEY, String(deg))
  } catch {
    // 書けないままでよい(次回は既定の30で起動する)。
  }
}
