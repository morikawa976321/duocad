/**
 * 画面の設定の永続化(localStorage)。
 *
 * ここに入れてよいのはクライアント側だけの画面設定。文書の状態(形・パラメータ・選択・可視性)は
 * Python側が正なので入れない(「これを失っても文書が変わらないか」が判定)。
 * 選べる値の正は `viewport.ts`(`isSelectableFov`/`FOV_DEFAULT`)で、ここで再定義しない。
 *
 * どの関数も例外を投げない。localStorage は遮断・プライベートモード・容量超過で
 * **読み書きの両方が throw する**ので、失敗は既定値へ倒して黙って続ける。
 */
import { FOV_DEFAULT, isSelectableFov } from './viewport'

const FOV_KEY = 'duocad.view.fov'
/** 常設の原点ギズモ(7項目まとめて1つの真偽値)。 */
const ORIGIN_VISIBLE_KEY = 'duocad.view.originVisible'

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

export function saveFov(deg: number): void {
  try {
    window.localStorage.setItem(FOV_KEY, String(deg))
  } catch {
    // 書けないままでよい。
  }
}

export function loadOriginVisible(): boolean {
  try {
    const raw = window.localStorage.getItem(ORIGIN_VISIBLE_KEY)
    // 'true'/'false' 以外(手で壊した値・別のコードが書いた値)は既定へ倒す。`false` に倒すと原点・軸・平面が黙って消える。
    return raw === 'false' ? false : true
  } catch {
    return true
  }
}

export function saveOriginVisible(visible: boolean): void {
  try {
    window.localStorage.setItem(ORIGIN_VISIBLE_KEY, String(visible))
  } catch {
    // 書けないままでよい。
  }
}
