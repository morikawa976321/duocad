/**
 * スケッチの描画指示(`SketchDraw`)と、選んだ領域がまだあるかの判定。`App.svelte` から切り出した純関数で、
 * サーバの状態(`SketchState`)と選択(`SelectionState`)を引数で受ける。
 */
import { refsOfKind } from './commands/command'
import type { SelectionState, SketchState } from './session'
import type { SketchDraw } from './viewportTypes'

/** 描画指示を組み立てる(選択中の領域だけ強調色になる)。`editingId` は今編集しているスケッチ(モード外は null)。 */
export function buildSketchDraws(
  sketches: readonly SketchState[],
  selection: SelectionState | null,
  editingId: string | null,
): SketchDraw[] {
  const selectedRegions = refsOfKind(selection?.items ?? [], 'region')
  const selectedHandles = refsOfKind(selection?.items ?? [], 'sketch_entity')
  return sketches
    // **隠したスケッチは描かない**(コアの `sketches[].visible`。issue #7)。**編集中の
    // スケッチだけは可視性に関わらず描く** —— 隠したスケッチの行から入る経路は先に
    // 表示へ戻す(`enterSketch`)が、その往復を待たずに `lookAtSketch` が枠を要るため。
    .filter((item) => item.frame !== null && (item.visible || item.sketch_id === editingId))
    .map((item) => ({
      sketchId: item.sketch_id,
      frame: item.frame as NonNullable<typeof item.frame>,
      // **`offset` も `base` 扱い**(`SketchPlaneRef` のdocstring参照)。オフセット平面は
      // 実ジオメトリの面ではない —— 基準が面でも、そこから離れた位置には実物の境界が無い
      // ので、面の実寸で範囲を絞る `faceUvBoxOnly` の対象にはできない。
      plane:
        item.plane.kind === 'face'
          ? { kind: 'face' as const, bodyId: item.plane.body, faceIndex: item.plane.index }
          : { kind: 'base' as const },
      entities: item.drawn,
      regions: item.regions,
      selectedRegions: selectedRegions.filter((ref) => ref.sketch === item.sketch_id).map((ref) => ref.index),
      // **点・辺を出すのは編集中のスケッチだけ**。全スケッチに出すと、通常モードの
      // 画面が点だらけになるうえ、指せないもの(モード外のスケッチの図形)が
      // 指せそうに見える(UI指針: 必要なものだけを出す)。
      handles:
        item.sketch_id === editingId
          ? item.entities.map((entity) => ({
              entityId: entity.entity_id,
              kind: entity.kind as string,
              params: entity.params,
            }))
          : [],
      selectedHandles: selectedHandles
        .filter((ref) => ref.sketch === item.sketch_id)
        .map((ref) => ({ entityId: ref.entity, point: ref.point })),
    }))
}

/**
 * 選択中の領域が、今のスケッチにまだ全部あるか。
 *
 * **領域はパラメータ編集で分裂・消失しうる**(§7)。消えた領域を送るとサーバは添字が範囲外だと言うだけ
 * なので、`runCommand` の `extrude` はこれで先に断って、何が起きたかを画面へ出す。領域は共有選択集合に
 * あって送る引数には入っていない —— 引数を見に行くと常に空になり、この検査が黙って空振りする。
 *
 * false になる状況(選んだ領域が実際に消えている)は、領域を変えうる verb のすべてに
 * `resets_selection=True`(`duocad/catalog.py`)が掛かっているため、**通常の UI 操作では選択が先に空になり
 * 到達できない**(`tests/test_server_selection_reset.py::test_set_parameter_clears_a_selected_region_that_the_edit_makes_vanish`
 * が実測)。`web/scripts/smoke-regions.mjs` が `window.__duocad.regionsStillExist` へ合成データを渡して
 * false の分岐を直接検算するのはそのため(あちらの先頭コメント)。
 */
export function regionsStillExist(selection: SelectionState | null, sketches: readonly SketchState[]): boolean {
  const refs = refsOfKind(selection?.items ?? [], 'region')
  return refs.every((ref) =>
    sketches.some(
      (item) => item.sketch_id === ref.sketch && item.regions.some((candidate) => candidate.index === ref.index),
    ),
  )
}
