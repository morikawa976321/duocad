/** Python側の表との比較用に、実際のTSの図形種別・点のキーを返す。 */
import './ts_resolve_hook.mjs'

const { ENTITY_KIND_TABLE } = await import('../web/src/entityKinds.ts')
process.stdout.write(JSON.stringify(Object.fromEntries(
  Object.entries(ENTITY_KIND_TABLE).map(([kind, spec]) => [kind, spec.pointKeys]),
)))
