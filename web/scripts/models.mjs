/**
 * 実機検査スクリプトが読むSTEPのパス。
 *
 * 起動オプション `--step` は無いので、「どのSTEPを読むか」はサーバではなく**読み込む側**が決める。
 */
export const PC_CASE_STEP =
  'C:\\Users\\tm976\\mf\\3d-models\\mATX_PC_case_20251122\\PC_case_20251122.step'

/** PC_CASE_STEP のソリッド数(実測。全2835面)。 */
export const PC_CASE_SOLIDS = 27

/** 形状最適化の題材(ハンガーバーの取付具。`PC_CASE_STEP` と同じ置き場。ファイル名の綴りは実ファイルに合わせてある)。 */
export const HANGER_BAR_STEP = 'C:\\Users\\tm976\\mf\\3d-models\\hanger_bar_attatchment.step'
