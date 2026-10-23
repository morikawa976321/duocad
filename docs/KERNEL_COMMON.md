# カーネルの共通幾何機構（issue #231・段階2）

基準 HEAD は `f1996c83`。振る舞いを変えず、段階1の再輸出80項目を削除した。

## 責務と依存の向き

| 層 | 責務 | 参照してよい共通層 |
|---|---|---|
| `brep_topology` | 列挙・添字・整数だけの由来射影 | なし |
| `brep_geometry` | 積分・法線・境界箱 | topology |
| `brep_checks` | null以外の形状検査・体積・BRepCheck | topology・geometry |
| `brep_history` | ビルダ履歴・IsSame・後継面の添字 | topology |
| `brep_operations` | コピー・結合・統合とその履歴・ブーリアンのビルダ作成 | topology・geometry・checks |
| `brep_planar` | 平面の座標系・補助面・面集合の演算 | topology |
| `mesh_geometry` | メッシュの幾何処理 | topology・checks |
| `kernel`・`kernel_*` | 操作・失敗方針・ブレンドの試行順 | 必要な共通層 |

共通層から個別操作を参照しない。操作間で内部関数を共有せず、共通層を直接参照する。操作間の公開の協力（例: 投影からスケッチの図形変換、特殊フィレットから辺の連鎖）は維持する。`kernel_tight` が `kernel_overflow.Unsupported` を参照するのは既存の例外契約のため。`Unsupported`・`Declined`・`FaceEditError` の定義場所と継承は変えていない。

`build_boolean` は引数とツールを設定して一度だけ Build し、ビルダを返す。完了判定とフィレット固有の Unsupported は overflow と tight のそれぞれの `_boolean` に残す。`outward_normal` は法線未定義なら None を返し、同じ例外・文面への変換はそれぞれの `_outward_normal` に残す。両操作の差し替え点は独立している。

`unify_same_domain` はコピーで試行し、妥当性・体積・面数を同じ順に検査してから元形状で本番を行う。コピーを本番結果へ置き換えないため、元面を鍵にした履歴が切れない。`kernel.copy_shape` の公開の入口は共通層への委譲として残す。コピーの幾何・三角形分割のフラグは各処理で元の値を維持する。

通常の OCCT → tight → 吸収と各段の検証は kernel に残す。意図的に異なる bbox・積分・許容差・列挙順・計算順は統合しない。平面集合の `_unified` は、ソリッドの採用判定付き `unify_same_domain` と異なる契約なので別に保つ。

## 段階2で移動した定義

| 旧経路 | 新経路 |
|---|---|
| `kernel._target_successors` | `brep_history.target_successors` |
| `kernel._through_history` | `brep_history.through_history` |
| `kernel._positions_in` | `brep_history.positions_in` |
| `kernel.normalize_provenance` | `brep_history.normalize_provenance` |
| `kernel._iter_faces_and_edges` | `brep_history.iter_faces_and_edges` |
| `kernel._shape_list` | `brep_history.shape_list` |
| `kernel._is_removed` | `brep_history.is_removed` |
| `kernel._lookup_by_identity` | `brep_history.lookup_by_identity` |
| `kernel._contains_identity` | `brep_history.contains_identity` |
| `kernel._fuse_all` | `brep_operations.fuse_all` |
| `kernel._unify_same_domain` | `brep_operations.unify_same_domain` |
| `kernel._BASE_PLANE_AXES` | `brep_planar._BASE_PLANE_AXES` |
| `kernel_sketch._ENVELOPE_TOLERANCE` | `brep_planar._ENVELOPE_TOLERANCE` |
| `kernel_sketch._ax3_from_frame` | `brep_planar._ax3_from_frame` |
| `kernel_sketch._ax3_from_plane` | `brep_planar._ax3_from_plane` |
| `kernel_sketch._boolean` | `brep_planar._boolean` |
| `kernel_sketch._dot3` | `brep_planar._dot3` |
| `kernel_sketch._frame_from_ax3` | `brep_planar._frame_from_ax3` |
| `kernel_sketch._plane_face` | `brep_planar._plane_face` |
| `kernel_sketch._shape_faces` | `brep_planar._shape_faces` |
| `kernel_sketch._smooth_breaks` | `brep_planar._smooth_breaks` |
| `kernel_sketch._to_pnt` | `brep_planar._to_pnt` |
| `kernel_sketch._to_uv` | `brep_planar._to_uv` |
| `kernel_sketch._unified` | `brep_planar._unified` |
| `kernel_sketch._union_faces` | `brep_planar._union_faces` |
| `kernel_overflow._solids` | `brep_topology._overflow_solids` |
| `kernel_overflow._volume` | `brep_geometry._overflow_volume` |

コピー本体は `brep_operations.copy_shape`、ビルダ作成本体は `brep_operations.build_boolean`、法線本体は `brep_geometry.outward_normal`。ブレンドの由来辞書へ verb・edges を付ける処理と面編集固有の関門は操作側に置く。

## 外した再輸出（80項目）

| 旧経路 | 呼び手の参照先 |
|---|---|
| `mesh_geometry.subshapes` | `brep_topology.subshapes` |
| `mesh_geometry.indexed_map` | `brep_topology.indexed_map` |
| `mesh_geometry.compound` | `brep_topology.compound` |
| `kernel._SUBSHAPE_KINDS` | `brep_topology._SUBSHAPE_KINDS` |
| `kernel.count_subshapes` | `brep_topology.count_subshapes` |
| `kernel._face_at` | `brep_topology._face_at` |
| `kernel._edge_at` | `brep_topology._edge_at` |
| `kernel._vertex_map` | `brep_topology._vertex_map` |
| `kernel._edge_map` | `brep_topology._edge_map` |
| `kernel._subshape_map` | `brep_topology._subshape_map` |
| `kernel._face_map` | `brep_topology._face_map` |
| `kernel.face_of` | `brep_topology.face_of` |
| `kernel.edge_of` | `brep_topology.edge_of` |
| `kernel.face_count` | `brep_topology.face_count` |
| `kernel.edge_count` | `brep_topology.edge_count` |
| `kernel.face_solid_indices` | `brep_topology.face_solid_indices` |
| `kernel.split_solids` | `brep_topology.split_solids` |
| `kernel._face_map_projection` | `brep_topology._face_map_projection` |
| `kernel.FACE_KINDS` | `brep_geometry.FACE_KINDS` |
| `kernel.FULL_CYLINDER_DEGREES` | `brep_geometry.FULL_CYLINDER_DEGREES` |
| `kernel.EDGE_KINDS` | `brep_geometry.EDGE_KINDS` |
| `kernel._ADAPTIVE_SURFACES` | `brep_geometry._ADAPTIVE_SURFACES` |
| `kernel._ADAPTIVE_CURVES` | `brep_geometry._ADAPTIVE_CURVES` |
| `kernel._ADAPTIVE_VOLUME_EPS` | `brep_geometry._ADAPTIVE_VOLUME_EPS` |
| `kernel.volume` | `brep_geometry.volume` |
| `kernel.centroid` | `brep_geometry.centroid` |
| `kernel._volume_props` | `brep_geometry._volume_props` |
| `kernel._faces_of` | `brep_geometry._faces_of` |
| `kernel._box_centre` | `brep_geometry._box_centre` |
| `kernel._needs_adaptive` | `brep_geometry._needs_adaptive` |
| `kernel.classify_face` | `brep_geometry.classify_face` |
| `kernel._bounding_box_extents` | `brep_geometry._bounding_box_extents` |
| `kernel._face_normal` | `brep_geometry._face_normal` |
| `kernel._face_props` | `brep_geometry._face_props` |
| `kernel.edge_length` | `brep_geometry.edge_length` |
| `kernel.classify_edge` | `brep_geometry.classify_edge` |
| `kernel.edge_axis` | `brep_geometry.edge_axis` |
| `kernel.edge_circle` | `brep_geometry.edge_circle` |
| `kernel.face_plane` | `brep_geometry.face_plane` |
| `kernel._point_and_direction` | `brep_geometry._point_and_direction` |
| `kernel._bounding_box` | `brep_geometry._bounding_box` |
| `kernel._NO_CHANGE_RTOL` | `brep_checks._NO_CHANGE_RTOL` |
| `kernel._checked_volume` | `brep_checks._checked_volume` |
| `kernel._require_valid` | `brep_checks._require_valid` |
| `kernel._check_tessellation_args` | `brep_checks._check_tessellation_args` |
| `kernel._is_closed_shell` | `brep_checks._is_closed_shell` |
| `kernel.is_closed_shell` | `brep_checks.is_closed_shell` |
| `kernel.is_solid` | `brep_checks.is_solid` |
| `kernel_brep._mass_and_center` | `brep_geometry._mass_and_center` |
| `kernel_brep.require_valid_solid` | `brep_checks.require_valid_solid` |
| `kernel_brep.inspect_solid` | `brep_checks.inspect_solid` |
| `kernel_brep._count_face_interferences` | `brep_checks._count_face_interferences` |
| `kernel_overflow._diagonal` | `brep_geometry._overflow_diagonal` |
| `kernel_overflow._edge_length` | `brep_geometry._overflow_edge_length` |
| `kernel_tight._area` | `brep_geometry._tight_area` |
| `kernel_tight._diagonal` | `brep_geometry._tight_diagonal` |
| `kernel_tight._a_point_on` | `brep_geometry._tight_a_point_on` |
| `kernel_tight._faces` | `brep_topology._tight_faces` |
| `kernel_tight._edges` | `brep_topology._tight_edges` |
| `kernel_extrude._shape_area` | `brep_geometry._extrude_shape_area` |
| `kernel_extrude._edge_length` | `brep_geometry._extrude_edge_length` |
| `kernel_extrude._area_centroid` | `brep_geometry._extrude_area_centroid` |
| `kernel_extrude._count` | `brep_topology._extrude_count` |
| `kernel_extrude._edges` | `brep_topology._extrude_edges` |
| `kernel_thread._volume_eps` | `brep_geometry._thread_volume_eps` |
| `kernel_face_edit._free_edge_count` | `brep_checks._face_edit_free_edge_count` |
| `kernel_face_edit._face_area` | `brep_geometry._face_edit_face_area` |
| `kernel_face_edit._face_normal` | `brep_geometry._face_edit_face_normal` |
| `kernel_face_edit._faces` | `brep_topology._face_edit_faces` |
| `kernel_face_edit._edges` | `brep_topology._face_edit_edges` |
| `kernel_face_edit._solids` | `brep_topology._face_edit_solids` |
| `kernel_face_edit._count` | `brep_topology._face_edit_count` |
| `kernel_face_edit._edge_faces` | `brep_topology._face_edit_edge_faces` |
| `kernel_face_edit._KINDS` | `brep_topology._face_edit_KINDS` |
| `kernel_interference.loose_bbox` | `brep_geometry._interference_loose_bbox` |
| `kernel_interference._area` | `brep_geometry._interference_area` |
| `kernel_interference._volume` | `brep_geometry._interference_volume` |
| `kernel_interference._volume_center` | `brep_geometry._interference_volume_center` |
| `kernel_interference._exact_bbox` | `brep_geometry._interference_exact_bbox` |
| `kernel_interference.obb_sides` | `brep_geometry._interference_obb_sides` |

## 寄せた呼び手と変更ファイル

共通関数の参照はモジュール名で修飾し、操作側に同名の別名を作らない。下表の呼び手は import と参照を新経路へ移行した。

| ファイル | 変更の要旨 |
|---|---|
| `duocad/brep_geometry.py` | 共有法線と既定精度の体積積分を移動。異なる積分実装は保持。 |
| `duocad/brep_topology.py` | 共有ソリッド列挙を移動。 |
| `duocad/interference.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/kernel.py` | 履歴・IsSame・結合・統合・コピー本体を下位層へ移動。操作方針とコピーの公開入口を保持。 |
| `duocad/kernel_brep.py` | 幾何量と形状検査の再輸出を除去。brep_geometry・brep_checks を直接参照。 |
| `duocad/kernel_chains.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/kernel_extrude.py` | kernel の結合・統合・後継面の引き当てを brep_operations・brep_history へ移行。段階1の再輸出を除去。 |
| `duocad/kernel_face_edit.py` | kernel の後継添字収集を brep_history へ移行。段階1の再輸出を除去。固有の関門と FaceEditError を保持。 |
| `duocad/kernel_interference.py` | 固有の bbox・積分・OBB の再輸出を除去し、brep_geometry を直接参照。 |
| `duocad/kernel_meshing.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/kernel_overflow.py` | 共有ブーリアン・法線・列挙・積分を下位層へ移行。例外と文言は保持。 |
| `duocad/kernel_project.py` | kernel_sketch の共有内部関数を brep_planar へ移行。公開の図形変換の協力は保持。 |
| `duocad/kernel_shell.py` | kernel の履歴正規化・IsSame 検索・後継添字収集を brep_history へ移行。コピーと履歴の対応を保持。 |
| `duocad/kernel_sketch.py` | 投影と共有する平面・補助面・集合演算を brep_planar へ移動。 |
| `duocad/kernel_split.py` | kernel の後継面の引き当てを brep_history へ移行。部品・面の並びと由来射影を保持。 |
| `duocad/kernel_thread.py` | kernel の後継面の引き当てを brep_history へ移行。既定精度と精度指定の体積積分を分離したまま参照。 |
| `duocad/kernel_tight.py` | overflow の内部関数参照を下位層へ移行。フィレット固有の例外と文言はローカルに保持。 |
| `duocad/mcp_server.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/measure.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/mesh_geometry.py` | 位相列挙・compound の再輸出を除去し、内部の参照も brep_topology へ修飾。 |
| `duocad/oplog.py` | 由来の説明を新経路に合わせる。 |
| `duocad/optimize_run.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/predicates.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/query.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/selection.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/session.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/topopt/acceptance.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/topopt/binary_search.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/topopt/voxelize.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/verbs/boolean_body.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/verbs/import_editable.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/verbs/move_body.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/verbs/project_to_sketch.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/view.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_bbox_cache.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_blend_chains.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_boundaries.py` | 新規下位層を許可し、逆参照・共通層の依存方向・再輸出・操作間の内部関数共有を禁止。 |
| `tests/test_coplanar_seams.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_delete_faces_acceptance.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_describe_shape.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_ellipse.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_envelope_regions.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_extrude.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_extrude_face_replay.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_extrude_faces.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_extrude_taper.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_face_count_once_per_body.py` | getattr と monkeypatch を selection が実際に引く brep_topology へ移行。呼び出し回数の契約を検証。 |
| `tests/test_face_culling.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_face_edit_kernel.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_face_edit_worker.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_face_tessellation_cache.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_feature_ui.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_files_ui.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_fillet.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_fillet_overflow.py` | 共通機構の参照と別プロセスに渡す Python 文字列を新経路へ移行。操作側 `_boolean` の差し替えは保持。 |
| `tests/test_fillet_tight.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_finalization.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_interference.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_interference_kernel.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_interference_session.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_io_step.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_kernel_brep.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_kernel_meshing.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_loft.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_mcp_selection.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_mcp_verbs.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_mirror_verbs.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_mixed_body_replay.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_move_verbs.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_offset_faces_acceptance.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_oplog_upgrade.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_optimization_trace.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_optimize_backend.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_pattern_live_replay.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_predicates.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_preview_off_the_loop.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_preview_result_reuse.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_protocol.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_revolve.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_selection.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_sketch_offset_plane.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_sketch_plane_freeze.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_split_body.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_stl_export.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_svg_import.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_thread.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tests/test_vertex_pick.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tools/bench_server_ops.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tools/thread_prototype.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `tools/thread_sim/testprint5.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `topopt_checks/check_boundary_meshing.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `topopt_checks/check_final_shape.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `topopt_checks/idle_validation/diagnose_cut_09.py` | 共通機構の import と利用箇所を新経路へ移行。 |
| `duocad/brep_history.py` | ビルダ履歴・面の同一性・後継添字の対応を所有。 |
| `duocad/brep_operations.py` | コピー・結合・統合・ブーリアンの共通処理を所有。 |
| `duocad/brep_planar.py` | スケッチと投影の共有幾何を所有。 |
| `tests/test_brep_shared.py` | 操作ごとの失敗方針と差し替え点を検査。 |

`tests/test_fillet_overflow.py` の `_boolean` の差し替えは既存の操作側ラッパに実際に効く。`test_face_count_once_per_body.py` は selection の参照と同じモジュールを差し替え、2ボディで2回、add/remove の合計で1回という実測で有効性を確かめる。

## 文書

- `docs/KERNEL_COMMON.md`: 責務・依存方向・移動と呼び手の全一覧・検証結果。
- `docs/DESIGN_DECISIONS.md`: 下位層の依存方向と操作側に残す方針を更新。
- `docs/README.md`: 本文書を索引へ追加。

## 前後一致と検証

比較スクリプト・HEAD の展開・生ログは `%TEMP%/duocad-231-stage2-f1996c83/` に保存。段階1の compare・supplement・triplets・split_followup とビット比較関数を再利用し、測定部分を変えず参照先だけ新経路へ合わせた。

前後比較: 12 + 12 + 10 + 1 ケースの4群すべて差0。体積・重心・bbox・BRepCheck・列挙順・接続・向き・配置・由来辞書・例外型と文面を比較。float は丸めず IEEE 754 のビット列を比較する。由来の未変更・変更・分割・統合・削除・新生の添字で後続操作を行い、保持した入力とコピーを各段で再測定。IsSame/IsEqual/IsPartner、失敗後の再実行、実 Session.preview 後の確定、undo/redo、JSONログ再生を含む。各実行の全 duocad モジュールの __file__ が指定 root 配下にあることを検査した。

AST監査: 499定義の本体・定数参照・条件と計算順を、新旧import参照と定義場所だけ正規化して比較し差0。ブーリアンと法線は下位層の本体と操作側の失敗処理を再接合して、元と同じASTになることを別途検査した。

実装途中の失敗: 移動したソリッド列挙で TopAbs_SOLID の import 漏れがあり、変更後比較の初回は NameError で失敗した。参照を補完し、4群を再実行して差0。AST監査の初回は同値の enum 表記と tight のラッパ参照を差として検出した。元の enum 表記を維持し、ラッパの再接合検査を含めて差0を確認した。

このブランチに存在しない `duocad/kernel_tight_surface.py` は対象外。旧経路は残していないため、呼び出し側が統合時に対応表の新経路へ合わせる。


## 実行したコマンド

以下の `P` は `%TEMP%/duocad-231-stage2-f1996c83`。`verify.py` は pytest を直接起動してログへ保存し、capped_run は使わない。実際の全引数は同フォルダの `targeted-command.json`・`full-command.json` に保存した。

| コマンド | 結果 |
|---|---|
| `.venv/Scripts/python.exe -X utf8 P/run.py before` / `after`、同 `P/check_results.py` | 4群・35ケース、差0。after 初回は enum の参照漏れで失敗し、修正後に4群を再実行。 |
| `.venv/Scripts/python.exe -X utf8 P/audit.py` | 499定義・差0、分離した2処理の再接合も一致。 |
| `.venv/Scripts/python.exe -X utf8 P/audit_all.py` | duocad 全体の1599定義・差0、操作固有の分離2処理も一致。 |
| `.venv/Scripts/python.exe -X utf8 P/reference_audit.py` | 再輸出80項目削除、旧静的参照0、例外の定義場所・継承一致。 |
| `.venv/Scripts/python.exe -X utf8 P/attribute_audit.py` | 全呼び手の操作モジュール属性に参照切れ0。 |
| `.venv/Scripts/python.exe -X utf8 P/undefined_audit.py` | 変更ファイルに新たに発生した未定義のグローバル名0。 |
| `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_face_count_once_per_body.py tests/test_split_body.py tests/test_sketch_plane_freeze.py tests/test_envelope_regions.py -q --tb=short` | 91 passed。 |
| `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_boundaries.py tests/test_brep_shared.py -q --tb=short` | 42 passed（境界の別名検出例を追加する前）。 |
| `.venv/Scripts/python.exe -X utf8 P/verify.py targeted` | 指定11ファイルと追加11ファイル: **489 passed / 1 failed**、324.63秒。別プロセス用文字列の `kernel.volume` が残っていたため AttributeError。文字列も新経路へ修正した。 |
| `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_fillet_overflow.py::test_rounding_a_post_on_a_lying_cylinder_past_its_end_does_not_kill_the_process -q --tb=short` | 修正後 **1 passed**、32.43秒。 |
| `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_boundaries.py tests/test_brep_shared.py tests/test_face_count_once_per_body.py -q --tb=short` | 最終状態 **55 passed**。OCP/build123d、逆参照、共通層の依存方向、再輸出、操作間の内部共有、差し替え点を検査。 |
| `.venv/Scripts/python.exe -m compileall -q duocad tests topopt_checks tools` | 成功。 |
| `git diff --check` | 成功。 |

指定11ファイルのほか、差し替え点を変えた `test_face_count_once_per_body.py` と、新規の `test_brep_shared.py`、平面共有機構の `test_project_to_sketch.py`・`test_envelope_regions.py`・`test_sketch_plane_freeze.py`・`test_sketch_offset_plane.py`・`test_region.py`、押し出し周辺の `test_extrude_faces.py`・`test_revolve.py`・`test_loft.py`、`test_bbox_cache.py` を対象にした。

全体 pytest は最後に1回だけ、`.venv/Scripts/python.exe -X utf8 -u -m pytest -q --tb=short` を直接起動した。capped_run は通していない。開始時の空きRAMは 8.48GB。結果: **3243 passed, 1 skipped in 678.41s (0:11:18)**、終了コード 0。生ログは `P/full.log`。
