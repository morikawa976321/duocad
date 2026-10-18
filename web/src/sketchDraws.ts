import { refsOfKind } from './commands/command'
import type { SelectionState, SketchState } from './session'
import type { SketchDraw } from './viewportTypes'

/** `editingId` は今編集しているスケッチ(モード外は null)。 */
export function buildSketchDraws(
  sketches: readonly SketchState[],
  selection: SelectionState | null,
  editingId: string | null,
): SketchDraw[] {
  const selectedRegions = refsOfKind(selection?.items ?? [], 'region')
  const selectedHandles = refsOfKind(selection?.items ?? [], 'sketch_entity')
  return sketches
    // 隠したスケッチは描かない。編集中のスケッチだけは可視性に関わらず描く
    // (`enterSketch` の表示復帰の往復を待たずに `lookAtSketch` が枠を要る)。
    .filter((item) => item.frame !== null && (item.visible || item.sketch_id === editingId))
    .map((item) => ({
      sketchId: item.sketch_id,
      revision: item.revision,
      frame: item.frame as NonNullable<typeof item.frame>,
      // `offset` も `base` 扱い(`SketchPlaneRef` 参照)。離れた位置には実物の境界が無く、
      // 面の実寸で絞る `faceUvBoxOnly` の対象にできない。
      plane:
        item.plane.kind === 'face'
          ? { kind: 'face' as const, bodyId: item.plane.body, faceIndex: item.plane.index }
          : { kind: 'base' as const },
      editing: item.sketch_id === editingId,
      livePatterns: item.patterns.length > 0,
      entities: item.drawn,
      regions: item.regions,
      selectedRegions: selectedRegions.filter((ref) => ref.sketch === item.sketch_id).map((ref) => ref.index),
      // 点・辺は全スケッチで指せる(スケッチモードの外。docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」)。
      handles: item.entities.map((entity) => ({
        entityId: entity.entity_id,
        kind: entity.kind as string,
        params: entity.params,
      })),
      selectedHandles: selectedHandles
        .filter((ref) => ref.sketch === item.sketch_id)
        .map((ref) => ({ entityId: ref.entity, point: ref.point })),
    }))
}

/**
 * 選択中の領域が、今のスケッチにまだ全部あるか。領域はパラメータ編集で分裂・消失しうる(§7)ので、
 * `runCommand` の `extrude` が先に断る。領域は共有選択集合にあって送る引数には入っていない —— 引数を見ると常に空で空振りする。
 *
 * false は通常の UI 操作では到達しない: 領域を変えうる verb はすべて `resets_selection=True`
 * (`duocad/catalog.py`)で選択が先に空になる
 * (`tests/test_server_selection_reset.py::test_set_parameter_clears_a_selected_region_that_the_edit_makes_vanish`)。
 */
export function regionsStillExist(selection: SelectionState | null, sketches: readonly SketchState[]): boolean {
  const refs = refsOfKind(selection?.items ?? [], 'region')
  return refs.every((ref) =>
    sketches.some(
      (item) => item.sketch_id === ref.sketch && item.regions.some((candidate) => candidate.index === ref.index),
    ),
  )
}
