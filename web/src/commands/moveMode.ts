/**
 * 移動の2つのverb(`move_body` / `move_sketch_entities`)が共有する**モードの読み方**。
 *
 * **どちらのコマンド定義にも置かない**(`features.ts` はソリッド・`sketch.ts` はスケッチの
 * 定義であって、片方をもう片方からimportすると分類の向きが逆になる)。綴りを2箇所に持つと、
 * 片方だけ直したときに**そのverbだけ黙って引数が揃わなくなる**。
 *
 * コア側は同じ綴りを `duocad/verbs/move_body.py` と `move_sketch.py` の両方に持っている
 * (verb同士がimportし合わない方針)。**TS側は1箇所**でよく、揃えるべきはコアとの綴りである。
 */
import type { ArgValues } from './command'

/** 「並進」「回転」の2択(`ChoiceArgSpec.options`。`buttonId` は使う側が付ける)。 */
export const MOVE_MODE_OPTIONS = [
  { value: 'translate', label: '並進' },
  { value: 'rotate', label: '回転' },
] as const

/**
 * **肯定形で書いてある**(`ArgSpecCommon.when` のdocstring) —— まだ `mode` を選んでいない
 * 間はどちらの述語も偽になり、収集は「種別を選ぶ」で止まる。`!== 'rotate'` のように
 * 否定で書くと、選ぶ前から並進側の引数を要求し始める。
 */
export function isTranslateMode(values: ArgValues): boolean {
  return values.mode === 'translate'
}

export function isRotateMode(values: ArgValues): boolean {
  return values.mode === 'rotate'
}

/**
 * 回転角として使えない値なら理由を返す。**360の倍数はコアが ValueError にする**
 * (0度・360度は移動なら何も変えず、複製なら元と完全に重なる)。
 */
export function rejectAngle(value: number): string | null {
  return value % 360 === 0 ? '角度が360の倍数(何も変わらない)' : null
}
