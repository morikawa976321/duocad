// 生成物 —— **手で直さない**。正は `duocad/catalog.py` の宣言で、
// 作り直すのは `python tools/generate_protocol_ts.py`(走らせ忘れは `tests/test_protocol.py` が落とす)。
import type { CommandState, ModeState, SelectionState } from '../session'

/** WebSocket から踏めるコマンド -> 分類(`catalog.Command.category`)。 */
export const COMMAND_CATEGORY = {
  set_visible: 'selection',
  set_sketch_visible: 'selection',
  measure: 'query',
  check_interference: 'query',
  modify_selection: 'selection',
  toggle_selection: 'selection',
  clear_selection: 'selection',
  sketch_create: 'sketch',
  sketch_create_offset: 'sketch',
  add_point: 'sketch',
  add_line: 'sketch',
  add_circle: 'sketch',
  add_ellipse: 'sketch',
  add_rectangle: 'sketch',
  add_arc: 'sketch',
  extrude: 'feature',
  revolve: 'feature',
  loft: 'feature',
  fillet: 'feature',
  chamfer: 'feature',
  shell_body: 'feature',
  boolean_body: 'feature',
  move_sketch_entities: 'sketch',
  move_body: 'feature',
  mirror_sketch_entities: 'sketch',
  envelope_regions: 'sketch',
  project_to_sketch: 'sketch',
  project_outline: 'sketch',
  mirror_body: 'feature',
  split_body: 'feature',
  delete: 'feature',
  delete_items: 'feature',
  rename: 'feature',
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
  close_document: 'file',
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
  inspect_selection: 'protocol',
  get_vertices: 'protocol',
  set_view_resolution: 'protocol',
  optimize_conditions: 'protocol',
  optimize_resolution: 'protocol',
  run_optimization: 'protocol',
  cancel_optimization: 'protocol',
  finish_optimization: 'protocol',
  optimize_steps: 'protocol',
  optimize_step_voxels: 'protocol',
  get_edges: 'protocol',
  get_edge_chains: 'protocol',
  ai_get_status: 'protocol',
  ai_configure: 'protocol',
  ai_set_thinking: 'protocol',
  ai_set_api_key: 'protocol',
  ai_prepare: 'protocol',
  ai_send: 'protocol',
  ai_cancel: 'protocol',
  fem_get_status: 'protocol',
  fem_set_device: 'protocol',
  begin_command: 'workspace',
  set_command_args: 'workspace',
  end_command: 'workspace',
  apply_command: 'workspace',
  enter_sketch: 'workspace',
  leave_sketch: 'workspace',
  enter_optimize: 'workspace',
  leave_optimize: 'workspace',
} as const

export type CommandName = keyof typeof COMMAND_CATEGORY

/** コマンドが受け取る引数の名前(宣言の全引数)。画面が共有の作業空間へ出す引数をこれで絞る(`CommandHost.svelte`)。 */
export const COMMAND_PARAMS: Record<CommandName, readonly string[]> = {
  set_visible: ['body_id', 'visible'],
  set_sketch_visible: ['sketch_id', 'visible'],
  measure: [],
  check_interference: ['clearance'],
  modify_selection: ['add', 'remove'],
  toggle_selection: ['refs'],
  clear_selection: [],
  sketch_create: ['base_plane'],
  sketch_create_offset: ['base_plane', 'distance'],
  add_point: ['sketch_id', 'point'],
  add_line: ['sketch_id', 'p0', 'p1'],
  add_circle: ['sketch_id', 'center', 'diameter'],
  add_ellipse: ['sketch_id', 'center', 'width', 'height', 'angle'],
  add_rectangle: ['sketch_id', 'corner', 'width', 'height', 'angle'],
  add_arc: ['sketch_id', 'center', 'radius', 'start_deg', 'end_deg'],
  extrude: ['distance', 'taper', 'mode', 'body_id'],
  revolve: ['axis', 'axis_line', 'angle', 'mode', 'symmetric', 'body_id'],
  loft: ['mode', 'start_point', 'end_point', 'shifts', 'body_id'],
  fillet: ['radius'],
  chamfer: ['distance'],
  shell_body: ['thickness'],
  boolean_body: ['target_ref', 'mode'],
  move_sketch_entities: ['dx', 'dy', 'copy', 'mode', 'angle', 'center', 'center_entity'],
  move_body: ['dx', 'dy', 'dz', 'copy', 'mode', 'angle', 'axis_edge', 'axis_point', 'axis_dir', 'center_vertex'],
  mirror_sketch_entities: ['axis'],
  envelope_regions: ['radius', 'offset'],
  project_to_sketch: ['sketch_id'],
  project_outline: ['sketch_id'],
  mirror_body: ['base_plane', 'plane_face'],
  split_body: ['base_plane', 'plane_face', 'sketch_id', 'keep'],
  delete: [],
  delete_items: ['bodies', 'sketches'],
  rename: ['target', 'name'],
  set_preserve_bodies: [],
  set_exclude_bodies: [],
  set_fixtures: [],
  set_loads: ['magnitude', 'direction'],
  clear_loads: [],
  pattern_rectangular: ['sketch_id', 'entities', 'dx', 'dy', 'nx', 'ny'],
  pattern_circular: ['sketch_id', 'entities', 'center', 'count', 'total_angle', 'rotate_copies'],
  import_svg: ['sketch_id', 'path', 'scale'],
  import_step: ['path'],
  export_step: ['path'],
  export_stl: ['path'],
  new_document: [],
  close_document: [],
  save_document: ['name'],
  rename_document: ['name'],
  open_document: ['name'],
  list_documents: [],
  copy_document: ['source', 'dest'],
  delete_document: ['name'],
  save_thumbnail: ['name', 'png'],
  get_thumbnail: ['name'],
  undo: [],
  redo: [],
  get_state: [],
  preview: ['verb', 'args'],
  inspect_selection: ['request_id'],
  get_vertices: ['body_id'],
  set_view_resolution: ['world_per_pixel', 'size_hint'],
  optimize_conditions: [],
  optimize_resolution: ['element_size_mm'],
  run_optimization: ['element_size_mm', 'target_safety_factor', 'max_evaluations'],
  cancel_optimization: [],
  finish_optimization: [],
  optimize_steps: [],
  optimize_step_voxels: [],
  get_edges: ['body_id'],
  get_edge_chains: ['body_id', 'chain_of'],
  ai_get_status: [],
  ai_configure: ['provider', 'model', 'base_url'],
  ai_set_thinking: ['thinking'],
  ai_set_api_key: ['key'],
  ai_prepare: [],
  ai_send: ['message'],
  ai_cancel: [],
  fem_get_status: [],
  fem_set_device: ['device'],
  begin_command: ['verb', 'args'],
  set_command_args: ['args'],
  end_command: [],
  apply_command: ['args'],
  enter_sketch: ['sketch_id'],
  leave_sketch: [],
  enter_optimize: [],
  leave_optimize: [],
}

/** コマンドが WebSocket で受け取る引数(`cmd` と `client_op` を除く。`?` は省略できる)。 */
export interface CommandArguments {
  set_visible: { body_id: string; visible: boolean }
  set_sketch_visible: { sketch_id: string; visible: boolean }
  measure: Record<string, never>
  check_interference: { clearance?: number }
  modify_selection: { add?: unknown[]; remove?: unknown[] }
  toggle_selection: { refs: unknown[] }
  clear_selection: Record<string, never>
  sketch_create: { base_plane?: string }
  sketch_create_offset: { base_plane?: string; distance: number }
  add_point: { sketch_id: string; point: unknown[] }
  add_line: { sketch_id: string; p0: unknown[]; p1: unknown[] }
  add_circle: { sketch_id: string; center: unknown[]; diameter: number }
  add_ellipse: { sketch_id: string; center: unknown[]; width: number; height: number; angle: number }
  add_rectangle: { sketch_id: string; corner: unknown[]; width: number; height: number }
  add_arc: { sketch_id: string; center: unknown[]; radius: number; start_deg: number; end_deg: number }
  extrude: { distance: number; taper?: number; mode: string }
  revolve: { axis?: string; axis_line?: object; angle?: number; mode: string; symmetric?: boolean }
  loft: { mode: string; start_point?: object; end_point?: object; shifts?: unknown[] }
  fillet: { radius: number | unknown[] }
  chamfer: { distance: number | unknown[] }
  shell_body: { thickness: number }
  boolean_body: { target_ref: object; mode: string }
  move_sketch_entities: { dx?: number; dy?: number; copy?: boolean; mode?: string; angle?: number; center?: unknown[]; center_entity?: object }
  move_body: { dx?: number; dy?: number; dz?: number; copy?: boolean; mode?: string; angle?: number; axis_edge?: object; axis_point?: unknown[]; axis_dir?: unknown[]; center_vertex?: object }
  mirror_sketch_entities: { axis: object }
  envelope_regions: { radius: number; offset: number }
  project_to_sketch: { sketch_id: string }
  project_outline: { sketch_id: string }
  mirror_body: { base_plane?: string; plane_face?: object }
  split_body: { base_plane?: string; plane_face?: object; sketch_id?: string | object; keep?: string }
  delete: Record<string, never>
  delete_items: { bodies?: unknown[]; sketches?: unknown[] }
  rename: { target: string; name: string }
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
  close_document: Record<string, never>
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
  inspect_selection: { request_id: number }
  get_vertices: { body_id: string }
  set_view_resolution: { world_per_pixel: number; size_hint?: number }
  optimize_conditions: Record<string, never>
  optimize_resolution: { element_size_mm?: number }
  run_optimization: { element_size_mm?: number; target_safety_factor?: number; max_evaluations?: number }
  cancel_optimization: Record<string, never>
  finish_optimization: Record<string, never>
  optimize_steps: Record<string, never>
  optimize_step_voxels: Record<string, never>
  get_edges: { body_id: string }
  get_edge_chains: { body_id: string; chain_of: unknown[] }
  ai_get_status: Record<string, never>
  ai_configure: { provider: string; model?: string; base_url?: string }
  ai_set_thinking: { thinking: string }
  ai_set_api_key: { key: string | null }
  ai_prepare: Record<string, never>
  ai_send: { message: string }
  ai_cancel: Record<string, never>
  fem_get_status: Record<string, never>
  fem_set_device: { device: string }
  begin_command: { verb: string; args?: object }
  set_command_args: { args: object }
  end_command: Record<string, never>
  apply_command: { args?: object }
  enter_sketch: { sketch_id: string }
  leave_sketch: Record<string, never>
  enter_optimize: Record<string, never>
  leave_optimize: Record<string, never>
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
  | (AppliedVerbBase & { verb: 'sketch_create'; sketch_id: string; frame: { origin: number[]; u: number[]; v: number[]; normal: number[] } })
  | (AppliedVerbBase & { verb: 'sketch_create_offset'; sketch_id: string; frame: { origin: number[]; u: number[]; v: number[]; normal: number[] } })
  | (AppliedVerbBase & { verb: 'add_point'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_line'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_circle'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_ellipse'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_rectangle'; entity_id: string })
  | (AppliedVerbBase & { verb: 'add_arc'; entity_id: string })
  | (AppliedVerbBase & { verb: 'extrude'; body_id: string; distance: number; mode: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'revolve'; body_id: string; angle: number; mode: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'loft'; body_id: string; sections: number; mode: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'fillet'; body_id: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'chamfer'; body_id: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'shell_body'; body_id: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'boolean_body'; target_body_id: string; mode: string; removed_bodies: string[]; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'move_sketch_entities'; mode: string; dx: number | null; dy: number | null; angle: number | null; center: number[] | null; copy: boolean; created: string[]; sketch_ids: string[] })
  | (AppliedVerbBase & { verb: 'move_body'; mode: string; dx: number | null; dy: number | null; dz: number | null; angle: number | null; axis_point: number[] | null; axis_dir: number[] | null; copy: boolean; created: string[]; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'mirror_sketch_entities'; axis_p0: number[]; axis_dir: number[]; created: string[]; sketch_ids: string[] })
  | (AppliedVerbBase & { verb: 'envelope_regions'; radius: number; offset: number; created: string[]; sketch_ids: string[]; approximated: number })
  | (AppliedVerbBase & { verb: 'project_to_sketch'; created: string[]; sketch_ids: string[]; approximated: number })
  | (AppliedVerbBase & { verb: 'project_outline'; created: string[]; sketch_ids: string[]; approximated: number })
  | (AppliedVerbBase & { verb: 'mirror_body'; plane_point: number[]; plane_normal: number[]; created: string[]; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'split_body'; plane_point: number[]; plane_normal: number[]; keep: string; created: string[]; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'delete'; bodies: string[]; entities: string[]; sketch_ids: string[] })
  | (AppliedVerbBase & { verb: 'delete_items'; bodies: string[]; sketches: string[] })
  | (AppliedVerbBase & { verb: 'rename'; target: string; kind: string; name: string })
  | (AppliedVerbBase & { verb: 'set_preserve_bodies'; bodies: string[] })
  | (AppliedVerbBase & { verb: 'set_exclude_bodies'; bodies: string[] })
  | (AppliedVerbBase & { verb: 'set_fixtures'; face_count: number })
  | (AppliedVerbBase & { verb: 'set_loads'; face_count: number; applied_count: number; magnitude: number; direction: string; direction_vector: number[] | null })
  | (AppliedVerbBase & { verb: 'clear_loads'; face_count: number })
  | (AppliedVerbBase & { verb: 'pattern_rectangular'; created: string[] })
  | (AppliedVerbBase & { verb: 'pattern_circular'; created: string[] })
  | (AppliedVerbBase & { verb: 'import_svg'; sketch_id: string; entity_ids: string[]; source_name: string; entity_count: number })
  | (AppliedVerbBase & { verb: 'import_step'; body_ids: string[] })
  | (AppliedVerbBase & { verb: 'undo'; undone: string })
  | (AppliedVerbBase & { verb: 'redo'; redone: string })
  | (AppliedChangeBase & { change: 'selection'; selection: SelectionState })
  | (AppliedChangeBase & { change: 'visibility'; body_id?: string; sketch_id?: string; visible: boolean; previous: boolean; changed: boolean })
  | (AppliedChangeBase & { change: 'document'; action: string; path: string | null; name: string | null })
  | (AppliedChangeBase & { change: 'command'; command: CommandState | null })
  | (AppliedChangeBase & { change: 'mode'; mode: ModeState })

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
  | 'document_open'
  | 'command'
  | 'mode'
  | 'generation'
