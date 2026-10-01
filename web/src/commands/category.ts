/**
 * コマンドの分類(どの経路で送るか)を、コアの宣言からの生成物で引く。
 *
 * `command.ts` に置かないのは、あちらを**実行時の依存が無い**ままにするため —— `tests/eval_*.mjs` が
 * Node で `command.ts` を直接読む。Node の型除去は拡張子の無い相対 import を解決しないので、
 * 値を import した時点で読めなくなる(`import type` は消えるので問題にならない)。
 */
import { COMMAND_CATEGORY } from '../generated/protocol'
import type { CommandCategory, CommandDef } from './command'

export function categoryOf(command: CommandDef): CommandCategory {
  return command.id === 'select_in_sketch' ? 'ui' : COMMAND_CATEGORY[command.id]
}
