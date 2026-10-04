// 生成物 —— **手で直さない**。正は `duocad/catalog.py` の宣言で、
// 作り直すのは `python tools/generate_protocol_ts.py`(走らせ忘れは `tests/test_protocol.py` が落とす)。
import type { SelectionState } from '../session'

/** WebSocket から踏めるコマンド -> 分類(`catalog.Command.category`)。 */
export const COMMAND_CATEGORY = {
  set_visible: 'selection',
  set_sketch_visible: 'selection',
  measure: 'query',
  modify_selection: 'selection',
  toggle_selection: 'selection',
  clear_selection: 'selection',
  sketch_create: 'sketch',
  sketch_create_offset: 'sketch',
  add_line: 'sketch',
  add_circle: 'sketch',
  add_rectangle: 'sketch',
  add_arc: 'sketch',
  extrude: 'feature',
  fillet: 'feature',
  chamfer: 'feature',
  boolean_body: 'feature',
  move_sketch_entities: 'sketch',
  move_body: 'feature',
  mirror_sketch_entities: 'sketch',
  envelope_regions: 'sketch',
  mirror_body: 'feature',
  delete: 'feature',
  set_preserve_bodies: 'feature',
  set_exclude_bodies: 'feature',
  set_fixtures: 'feature',
  set_loads: 'feature',
  clear_loads: 'feature',
  pattern_rectangular: 'feature',
  pattern_circular: 'feature',
  import_svg: 'sketch',
  import_step: 'file',
  export_step: 'file',
  export_stl: 'file',
  new_document: 'file',
  save_document: 'file',
  rename_document: 'file',
  open_document: 'file',
  list_documents: 'file',
  copy_document: 'file',
  delete_document: 'file',
  save_thumbnail: 'file',
  get_thumbnail: 'file',
  undo: 'history',
  redo: 'history',
  get_state: 'protocol',
  preview: 'protocol',
  get_vertices: 'protocol',
  set_view_resolution: 'protocol',
  optimize_conditions: 'protocol',
  optimize_resolution: 'protocol',
  run_optimization: 'protocol',
  cancel_optimization: 'protocol',
  get_edges: 'protocol',
} as const

export type CommandName = keyof typeof COMMAND_CATEGORY

/** コマンドが WebSocket で受け取る引数(`cmd` と `client_op` を除く。`?` は省略できる)。 */
export interface CommandArguments {
  set_visible: { body_id: string; visible: boolean }
  set_sketch_visible: { sketch_id: string; visible: boolean }
  measure: Record<string, never>
  modify_selection: { add?: unknown[]; remove?: unknown[] }
  toggle_selection: { refs: unknown[] }
  clear_selection: Record<string, never>
  sketch_create: { base_plane?: string }
  sketch_create_offset: { base_plane?: string; distance: number }
  add_line: { sketch_id: string; p0: unknown[]; p1: unknown[] }
  add_circle: { sketch_id: string; center: unknown[]; radius: number }
  add_rectangle: { sketch_id: string; corner: unknown[]; width: number; height: number }
  add_arc: { sketch_id: string; center: unknown[]; radius: number; start_deg: number; end_deg: number }
  extrude: { distance: number; mode: string }
  fillet: { radius: number | unknown[] }
  chamfer: { distance: number | unknown[] }
  boolean_body: { target_ref: object; mode: string }
  move_sketch_entities: { dx?: number; dy?: number; copy?: boolean; mode?: string; angle?: number; center?: unknown[]; center_entity?: object }
  move_body: { dx?: number; dy?: number; dz?: number; copy?: boolean; mode?: string; angle?: number; axis_edge?: object; axis_point?: unknown[]; axis_dir?: unknown[]; center_vertex?: object }
  mirror_sketch_entities: { axis: object }
  envelope_regions: { radius: number; offset: number }
  mirror_body: { base_plane?: string; plane_face?: object }
  delete: Record<string, never>
  set_preserve_bodies: Record<string, never>
  set_exclude_bodies: Record<string, never>
  set_fixtures: Record<string, never>
  set_loads: { magnitude: number; direction: string | unknown[] }
  clear_loads: Record<string, never>
  pattern_rectangular: { sketch_id: string; entities: unknown[]; dx: number; dy: number; nx: number; ny: number }
  pattern_circular: { sketch_id: string; entities: unknown[]; center: unknown[]; count: number; total_angle: number }
  import_svg: { sketch_id: string; path: string }
  import_step: { path: string }
  export_step: { path: string }
  export_stl: { path: string }
  new_document: Record<string, never>
  save_document: { name?: string }
  rename_document: { name: string }
  open_document: { name: string }
  list_documents: Record<string, never>
  copy_document: { source: string; dest: string }
  delete_document: { name: string }
  save_thumbnail: { name: string; png: string }
  get_thumbnail: { name: string }
  undo: Record<string, never>
  redo: Record<string, never>
  get_state: Record<string, never>
  preview: { verb: string; args: object }
  get_vertices: { body_id: string }
  set_view_resolution: { world_per_pixel: number; size_hint?: number }
  optimize_conditions: Record<string, never>
  optimize_resolution: Record<string, never>
  run_optimization: { element_size_mm?: number; target_safety_factor?: number; max_evaluations?: number }
  cancel_optimization: Record<string, never>
  get_edges: { body_id: string }
}

/**
 * verb の適用に共通するフィールド。`change?: undefined` は実行時には存在しない ——
 * `applied.change === 'selection'` のように、`verb` で絞る前に触れるようにするための置き石。
 */
interface AppliedVerbBase {
  seq: number
  client_op: number | null
  change?: undefined
}

/** 変更イベント(verb ではない。操作ログに載らない)に共通するフィールド。 */
interface AppliedChangeBase {
  client_op: number | null
  verb?: undefined
  seq?: undefined
}

/** 操作を出した画面への確認応答。`verb` か `change` で絞り込むと、その応答が載せる値が見える。 */
export type Applied =
  | (AppliedVerbBase & { verb: 'sketch_create'; sketch_id: string })
  | (AppliedVerbBase & { verb: 'sketch_create_offset'; sketch_id: string })
  | (AppliedVerbBase & { verb: 'add_line'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_circle'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_rectangle'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_arc'; entity_id: string })
  | (AppliedVerbBase & { verb: 'extrude'; body_id: string; distance: number; mode: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'fillet'; body_id: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'chamfer'; body_id: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'boolean_body'; target_body_id: string; mode: string; removed_bodies: string[]; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'move_sketch_entities'; mode: string; dx: number | null; dy: number | null; angle: number | null; center: number[] | null; copy: boolean; created: string[]; sketch_ids: string[] })
  | (AppliedVerbBase & { verb: 'move_body'; mode: string; dx: number | null; dy: number | null; dz: number | null; angle: number | null; axis_point: number[] | null; axis_dir: number[] | null; copy: boolean; created: string[]; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'mirror_sketch_entities'; axis_p0: number[]; axis_dir: number[]; created: string[]; sketch_ids: string[] })
  | (AppliedVerbBase & { verb: 'envelope_regions'; radius: number; offset: number; created: string[]; sketch_ids: string[] })
  | (AppliedVerbBase & { verb: 'mirror_body'; plane_point: number[]; plane_normal: number[]; created: string[]; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'delete'; bodies: string[]; entities: string[]; sketch_ids: string[] })
  | (AppliedVerbBase & { verb: 'set_preserve_bodies'; bodies: string[] })
  | (AppliedVerbBase & { verb: 'set_exclude_bodies'; bodies: string[] })
  | (AppliedVerbBase & { verb: 'set_fixtures'; face_count: number })
  | (AppliedVerbBase & { verb: 'set_loads'; face_count: number; applied_count: number; magnitude: number; direction: string; direction_vector: number[] | null })
  | (AppliedVerbBase & { verb: 'clear_loads'; face_count: number })
  | (AppliedVerbBase & { verb: 'pattern_rectangular'; pattern_id: string })
  | (AppliedVerbBase & { verb: 'pattern_circular'; pattern_id: string })
  | (AppliedVerbBase & { verb: 'import_svg'; sketch_id: string; entity_ids: string[]; source_name: string; entity_count: number })
  | (AppliedVerbBase & { verb: 'import_step'; body_ids: string[] })
  | (AppliedVerbBase & { verb: 'undo'; undone: string })
  | (AppliedVerbBase & { verb: 'redo'; redone: string })
  | (AppliedChangeBase & { change: 'selection'; selection: SelectionState })
  | (AppliedChangeBase & { change: 'visibility'; body_id?: string; sketch_id?: string; visible: boolean; previous: boolean; changed: boolean })
  | (AppliedChangeBase & { change: 'document'; action: string; path: string | null; name: string | null })

/** 状態配信(`ServerState`)が持つキー。 */
export type ServerStateKey =
  | 'bodies'
  | 'sketches'
  | 'parameters'
  | 'selection'
  | 'optimize'
  | 'log'
  | 'applied_count'
  | 'redo_count'
  | 'can_undo'
  | 'can_redo'
  | 'active_body_id'
  | 'units'
  | 'current_name'
  | 'saved'
