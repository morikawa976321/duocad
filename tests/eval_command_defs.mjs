/**
 * `web/src/commands/*.ts` の `CommandDef` を**実際に読み込み**、`tests/test_ui_command_wiring.py` が
 * 突き合わせる値(id・`needsEnter` の結果・サーバへ行かない選択肢・ピック引数の名前)を JSON で標準出力へ書く。
 *
 * Node の型除去は拡張子の無い相対 import(`from './command'`)を解決しない(Vite は補う)ので、
 * 解決に失敗したときだけ `.ts` を補う resolve フックを先に登録する。
 */
import { readdirSync } from 'node:fs'

import './ts_resolve_hook.mjs'

const directory = new URL('../web/src/commands/', import.meta.url)
const { needsEnter } = await import(new URL('command.ts', directory).href)

const found = new Map()
for (const file of readdirSync(directory).filter((name) => name.endsWith('.ts')).sort()) {
  const exported = await import(new URL(file, directory).href)
  for (const value of Object.values(exported)) {
    for (const def of Array.isArray(value) ? value : [value]) {
      if (def === null || typeof def !== 'object' || typeof def.id !== 'string' || !Array.isArray(def.args)) continue
      if (found.has(def.id)) continue
      // サーバへ行かない選択肢(`sends: null`)と、それを配信から戻す手がかり(`impliedBy`)
      const unsent = def.args.flatMap((arg) =>
        arg.kind === 'choice'
          ? arg.options.filter((option) => option.sends === null).map((option) => ({ arg: arg.name, value: option.value, impliedBy: option.impliedBy ?? null }))
          : [],
      )
      const picks = def.args.filter((arg) => arg.kind === 'pick').map((arg) => arg.name)
      found.set(def.id, { id: def.id, file, needsEnter: needsEnter(def), unsent, picks })
    }
  }
}
process.stdout.write(JSON.stringify([...found.values()]))
