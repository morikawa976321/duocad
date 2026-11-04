/**
 * 選択の粒度 —— 起動中のコマンドが、指した部分ではなくその持ち主(ボディ・スケッチの図形)ごとに効くか。
 *
 * 層をまたぐ契約: コアは面・辺・頂点からボディを逆引きして効かせ(`selection.require_bodies` / `require_deletable`)、
 * スケッチの点からは図形を逆引きして効かせる(`require_deletable`・`mirror_sketch_entities`・反復の `entities`)。
 * そういうコマンドの起動中に画面が面や点をそのまま選ぶと、光っているものと効くものが食い違う。
 * 起動中は人のクリックと、起動したときの選択を、持ち主の参照へ畳む(`App.svelte`)。
 */
import { bodyRef, refKey, refKind, sketchEntityRef, type CommandDef, type PickTarget } from './command'
import type { SelectionRef, ShapeRef, SketchEntityRef } from '../session'

export interface SelectionGrain {
  /** 面・辺・頂点をボディの参照へ畳む(選択引数の `shapeKind` が `'body'`)。 */
  body: boolean
  /** スケッチの点を図形の参照へ畳む(`entities` 引数か、選択引数の `wholeEntities`)。 */
  entity: boolean
}

export const NO_GRAIN: SelectionGrain = { body: false, entity: false }

export function grainOf(command: CommandDef | null): SelectionGrain {
  const args = command?.args ?? []
  return {
    body: args.some((spec) => spec.kind === 'selection' && spec.shapeKind === 'body'),
    entity: args.some(
      (spec) => spec.kind === 'entities' || (spec.kind === 'selection' && spec.wholeEntities === true),
    ),
  }
}

/**
 * ホバーの粒度。次の素のクリックで入るものに揃える: 直接ピックを待っている間はそのピックの単位(鏡映面の「面を選ぶ」は
 * 面1枚、ブーリアンのターゲットはボディ、鏡映軸・分割のスケッチは線まるごと)、それ以外は選択の粒度。
 */
export function hoverGrainOf(grain: SelectionGrain, pick: PickTarget | null): SelectionGrain {
  return {
    body: pick === 'body' || (grain.body && pick !== 'face'),
    entity: pick === 'sketchEntity' || (grain.entity && pick !== 'sketchPoint'),
  }
}

/** その図形が点の図形か(`kind: 'point'`)。呼び出し側が配信されたスケッチから引く。 */
export type IsPointEntity = (sketchId: string, entityId: string) => boolean

/**
 * 参照を粒度の持ち主へ畳む。点の図形の点は畳まない: 図形そのものが点で、コアは `point` の無い参照を
 * `point: 'cx'` へ書き直す(`selection._as_point_of_point_entity`)ので、畳むと画面の鍵とコアの鍵が食い違う。
 */
export function foldRef(ref: SelectionRef, grain: SelectionGrain, isPointEntity: IsPointEntity): SelectionRef {
  const kind = refKind(ref)
  if (grain.body && (kind === 'face' || kind === 'edge' || kind === 'vertex')) return bodyRef((ref as ShapeRef).body)
  if (grain.entity && kind === 'sketch_entity') {
    const item = ref as SketchEntityRef
    const point = item.point ?? null
    if (point !== null && !isPointEntity(item.sketch, item.entity)) return sketchEntityRef(item.sketch, item.entity, null)
  }
  return ref
}

/** 並びを畳み、同じ持ち主を1つにする(最初に現れた順。コアの `require_bodies` と同じ)。 */
export function foldRefs(
  refs: readonly SelectionRef[],
  grain: SelectionGrain,
  isPointEntity: IsPointEntity,
): SelectionRef[] {
  const seen = new Set<string>()
  const folded: SelectionRef[] = []
  for (const ref of refs) {
    const next = foldRef(ref, grain, isPointEntity)
    const key = refKey(next)
    if (seen.has(key)) continue
    seen.add(key)
    folded.push(next)
  }
  return folded
}
