/**
 * Node の型除去は拡張子の無い相対 import(`from './command'`)を解決しない(Vite は補う)ので、
 * 解決に失敗したときだけ `.ts` を補う resolve フックを登録する。`web/src` の TS を読む `eval_*.mjs` が
 * 先頭で `import './ts_resolve_hook.mjs'` する(静的 import は先に評価されるので、後続の動的 import に効く)。
 */
import { register } from 'node:module'

register(
  'data:text/javascript,' +
    encodeURIComponent(`
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context)
  } catch (error) {
    if (!specifier.startsWith('.') || specifier.endsWith('.ts')) throw error
    return next(specifier + '.ts', context)
  }
}`),
)
