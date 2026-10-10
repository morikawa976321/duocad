/**
 * コマンドの分類をコアの宣言からの生成物で引く。`command.ts` に置かないのは、`tests/eval_*.mjs` が
 * Node で `command.ts` を直接読むため(値の import は、拡張子なし相対 import を解決しない Node の型除去で読めなくなる)。
 */
import { COMMAND_CATEGORY } from '../generated/protocol'
import type { CommandCategory, CommandDef } from './command'

export function categoryOf(command: CommandDef): CommandCategory {
  return command.id === 'select_in_sketch' ? 'ui' : COMMAND_CATEGORY[command.id]
}
