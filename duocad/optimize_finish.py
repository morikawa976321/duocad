"""再仕上げ計画から完成化を実行し、検査に通った結果を文書へ追加する。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import optimize_run as adapter
from .optimize_events import RunEvents
from .optimize_plan import CompletionInputs, FinishPlan

if TYPE_CHECKING:
    from .document import Document
    from .optimize_run import _DebugSink, _ProgressPump
    from .topopt.voxelize import Grid
    from .topopt.complete import Completion, Context


@dataclass(frozen=True)
class MeshPlacement:
    grid: Grid

    def place(self, verts) -> list[float]:
        positions = adapter._mesh_to_world(verts, self.grid)
        adapter._check_mesh_placement(positions, self.grid)
        return positions


def completion_context(inputs: CompletionInputs, pump: _ProgressPump) -> Context:
    """計算入力に、この仕上げだけの進捗・中断・配置の口を結び付ける。"""
    from .topopt.complete import Context

    return Context(
        space_mask=inputs.space_mask, grid=inputs.grid, conditions=inputs.conditions, min_width=inputs.min_width.as_dict(),
        target_safety_factor=inputs.target_safety_factor, search_target_safety_factor=inputs.search_target_safety_factor,
        keep_shapes=list(inputs.keep_shapes), exclude_shapes=list(inputs.exclude_shapes), anchor_soup=inputs.anchor_soup,
        emit=pump.completion_message, check=pump.check, place=MeshPlacement(inputs.grid).place,
        search_status=inputs.search_status, search_termination=inputs.search_termination,
        passthrough=(adapter.OptimizeCancelled,))


@dataclass(frozen=True)
class CompletedStep:
    shape: Completion
    positions: list[float]
    indices: list[int]
    backtrack_report: dict | None
    backtrack_warning: str
    final_warning: str


def finish_shape(doc: "Document", index: int, plan: FinishPlan, *, pump: "_ProgressPump", dbg: "_DebugSink",
                  started: float, termination: str, chosen_by_user: bool, events: RunEvents) -> dict:
    """計画の指定ステップを完成化し、必要ならさかのぼって、合格形だけを文書へ足す。"""
    completed = complete_step(index, plan, pump=pump, dbg=dbg, started=started, termination=termination, events=events)
    if isinstance(completed, dict):
        return completed
    return insert_completed(doc, index, plan, completed, pump=pump, started=started,
                            termination=termination, chosen_by_user=chosen_by_user)


def complete_step(index: int, plan: FinishPlan, *, pump: "_ProgressPump", dbg: "_DebugSink",
                  started: float, termination: str, events: RunEvents) -> CompletedStep | dict:
    """指定ステップを完成化し、必要ならさかのぼって合格形を選ぶ。"""
    from .topopt import complete as completion
    from .topopt import final_check

    context = completion_context(plan.inputs, pump)
    accepted, best = plan.accepted, plan.best
    common, keep_room_warning = plan.common, plan.keep_room_warning
    unrepresented, mapping, target = plan.unrepresented, plan.mapping, plan.inputs.target_safety_factor
    space_grid = context.grid
    last = len(accepted) - 1
    if index == last:
        done = completion.complete(best.mask, float(best.safety_factor), float(best.material_mm3), context)
    else:
        shape = accepted[index]
        done = completion.complete(shape.mask(), float(shape.safety_factor), float(shape.material_mm3), context)
    backtrack_warning = ""
    backtrack_report = None
    if done.verdict in ("fail", "fill_infeasible"):
        # 完成形が本来の目標を割るか、空隙を埋めた形が6条件に落ちたら、探索の合格形をさかのぼって通る形を足す
        # (2026-10-11・12 本人決定)。通らなければ足さない(#129)
        back = completion.backtrack(accepted[:index + 1], done, context)
        steps_back = None if back.chosen_index is None else index - back.chosen_index
        backtrack_report = dict(reason=back.reason, steps_back=steps_back, checks=back.checks,
                                first_final_safety_factor=done.final_safety_factor,
                                first_material_mm3=done.material_mm3)
        if back.chosen is None:
            unsafe = done.verdict == "fail"
            return dict(adapter._not_done(
                common,
                started,
                keep_room_warning,
                state="final_unsafe" if unsafe else "fill_infeasible",
                warning=adapter._join_warnings(completion.shortfall_message(done, target) if unsafe else done.fill_failure,
                                       completion.exhausted_note(back)),
                unrepresented_regions=unrepresented,
                boundary_mapping=mapping,
                evaluations=plan.evaluations,
                termination=termination,
            ), final_safety_factor=done.final_safety_factor, final_check=done.checked, backtrack=backtrack_report,
                finished_step=index, steps=last + 1)
        backtrack_warning = completion.backtrack_message(done, back.chosen, steps_back, target)
        done = back.chosen
    # 最終確定マスク(0/1のbool)。`_DebugSink.rho` の閾値0.5はboolにも成り立つ。
    dbg.rho("final", space_grid, done.mask)
    checked = done.checked
    final_sf = done.final_safety_factor
    final_warning = final_check.neck_warning(checked["neck"])
    if final_sf is None:
        final_warning = adapter._join_warnings(final_warning, "完成形の安全率を測れなかった(拘束・荷重の節点が完成形に載らないか、解けなかった)")
    final_vertices, final_faces, surface = done.final_vertices, done.final_faces, done.surface
    positions = final_vertices.ravel().tolist()
    indices = final_faces.ravel().tolist()
    events.final_mesh(surface.verts_raw, surface.verts_smooth, surface.faces, space_grid,
                     final_vertices=final_vertices, final_faces=final_faces)

    return CompletedStep(done, positions, indices, backtrack_report, backtrack_warning, final_warning)


def insert_completed(doc: Document, index: int, plan: FinishPlan, completed: CompletedStep, *,
                     pump: _ProgressPump, started: float, termination: str, chosen_by_user: bool) -> dict:
    """完成化が選んだ形を追加し、外部応答の値を組み立てる。"""
    done = completed.shape
    positions, indices = completed.positions, completed.indices
    common, keep_room_warning, unrepresented, mapping = plan.common, plan.keep_room_warning, plan.unrepresented, plan.mapping
    backtrack_report, backtrack_warning = completed.backtrack_report, completed.backtrack_warning
    final_warning, final_sf, checked = completed.final_warning, done.final_safety_factor, done.checked
    last = len(plan.accepted) - 1
    # 名前の番号は画面と同じく 1 から(`index` は 0 から)。
    name = adapter._unique_result_name(doc, f"{adapter.RESULT_NAME}-step{index + 1}" if chosen_by_user else adapter.RESULT_NAME)
    pump.emit(phase="insert", message="保持・除外を反映した結果をメッシュボディとして足す")
    from .verbs import add_mesh_body

    # 結果は編集可能ソリッドとして持たない(2026-09-23 本人判断)。画面・STL は `finalize` が合格させた三角形をそのまま使う。
    body_id = add_mesh_body(doc, positions, indices, name=name)

    return dict(
        common,
        state="done",
        body_id=body_id,
        name=name,
        warning=adapter._join_warnings(
            adapter.format_region_warning(unrepresented) if unrepresented else "", keep_room_warning, done.fill_warning,
            backtrack_warning, final_warning) or None,
        empty_condition_faces=[],
        unrepresented_regions=unrepresented,
        boundary_mapping=mapping,
        safety_factor=done.safety_factor,
        search_safety_factor=done.search_safety_factor,
        final_safety_factor=final_sf,
        final_check=checked,
        backtrack=backtrack_report,
        filled_cells=int(done.fill["cells"]),
        material_mm3=done.material_mm3,
        evaluations=plan.evaluations,
        termination=termination,
        finished_step=index,
        steps=last + 1,
        triangle_count=len(indices) // 3,
        vertex_count=len(positions) // 3,
        seconds=time.perf_counter() - started,
    )

