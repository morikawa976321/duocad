/**
 * 実機検査スクリプトが読むSTEPのパス(2026-08-15)。
 *
 * 旧 `duocad/server.py` の `DEFAULT_STEP` の移し先。起動オプション `--step` を廃止したので、
 * 「どのSTEPを読むか」はサーバではなく**読み込む側**が決める。
 */
export const PC_CASE_STEP =
  'C:\\Users\\tm976\\mf\\3d-models\\mATX_PC_case_20251122\\PC_case_20251122.step'

/** PC_CASE_STEP のソリッド数(2026-08-11実測。全2835面)。 */
export const PC_CASE_SOLIDS = 27
