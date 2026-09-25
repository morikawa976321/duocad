/**
 * 実機検査スクリプトが読むSTEPのパス。
 *
 * 起動オプション `--step` は無いので、「どのSTEPを読むか」はサーバではなく**読み込む側**が決める。
 */
export const PC_CASE_STEP =
  'C:\\Users\\tm976\\mf\\3d-models\\mATX_PC_case_20251122\\PC_case_20251122.step'

/** PC_CASE_STEP のソリッド数(実測。全2835面)。 */
export const PC_CASE_SOLIDS = 27

/**
 * PC_CASE_STEP を**ブラウザから**読むと出る警告の札(実測。`view.py` が部品 `PC_case_20251122_20` の
 * 6面を三角形分割できず、その面は描かれない)。MCP から読んだときは配信されない。
 * 走行の最後に「エラーが出ていない」を見る本は、この文言に限って除く(文字列の完全一致。
 * 面の顔ぶれが変わったら**出るはずの札が無い**として赤になるので、そのとき実測し直す)。
 */
export const PC_CASE_IMPORT_WARNINGS = [
  "警告 境界の交差を解消できなかった面(PC_case_20251122_20): ['9:fold,overlap,touch', '11:fold,overlap,touch', '16:fold,overlap,touch', '18:fold,overlap,touch', '20:fold,overlap,touch', '22:fold,overlap,touch'](画面に出ません)",
  '警告 三角形分割が取れなかった面(PC_case_20251122_20): [9, 11, 16, 18, 20, 22](画面に出ません)',
]

/** 形状最適化の題材(ハンガーバーの取付具。`PC_CASE_STEP` と同じ置き場。ファイル名の綴りは実ファイルに合わせてある)。 */
export const HANGER_BAR_STEP = 'C:\\Users\\tm976\\mf\\3d-models\\hanger_bar_attatchment.step'
