"""条件確定・設計空間と境界対応・探索を、型付きの段階間データでつなぐ。"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

from . import optimize_run as adapter
from .optimize_events import RunEvents
from .optimize_finish import finish_shape
from .optimize_plan import CompletionInputs, FinishingShape, FinishPlan, MinimumWidth

if TYPE_CHECKING:
    from .document import Document
    from .optimize_run import ConditionBundle, OptimizeOptions, _DebugSink, _ProgressPump
    from .optimize_steps import OptimizationSteps
    from .topopt.binary_search import AcceptedShape, SearchConditions, SearchResult
    from .topopt.design_space import DesignSpace


class FinishRequest(Protocol):
    def __call__(self, final: bool = False) -> int | None: ...


@dataclass(frozen=True)
class ResolvedRun:
    options: OptimizeOptions
    bundle: ConditionBundle
    limits: dict
    spacing: float
    min_width: MinimumWidth
    search_target: float
    factor: float


@dataclass(frozen=True)
class PreparedSearch:
    resolved: ResolvedRun
    space: DesignSpace
    conditions: SearchConditions
    common: dict
    keep_room_warning: str
    unrepresented: list[dict]
    mapping: dict


@dataclass(frozen=True)
class SearchOutcome:
    result: SearchResult
    requested: int | None


@dataclass
class SearchObserver:
    space: DesignSpace
    factor: float
    started: float
    pump: _ProgressPump
    steps: OptimizationSteps | None
    should_cancel: Callable[[], bool] | None
    finish_request: FinishRequest | None

    def on_accept(self, shape: AcceptedShape, evaluation: int) -> dict:
        return self.steps.append(shape, evaluation, grid=self.space.grid)

    def on_evaluation(self, event: dict) -> None:
        latest = self.steps.latest() if self.steps is not None and event.get("outcome") in ("initial", "accepted") else None
        self.pump.send(
            phase="search",
            message=adapter._evaluation_message(
                event, design_elements=int(self.space.element_count), factor=self.factor,
                elapsed_seconds=time.perf_counter() - self.started),
            **event,
            **({} if latest is None else {"step": latest}),
        )

    def stop_requested(self) -> bool:
        return bool(self.should_cancel is not None and self.should_cancel()) or (
            self.finish_request is not None and self.finish_request() is not None)


def execute(
    doc: "Document",
    options: Optional[OptimizeOptions] = None,
    *,
    progress: Optional[Callable[[dict], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    debug: Optional[Callable[[dict], None]] = None,
    debug_kinds: Optional[Any] = None,
    steps: OptimizationSteps | None = None,
    finish_request: FinishRequest | None = None,
    events: RunEvents,
) -> dict:
    """入力条件 → 固定設計空間の導出(1回) → 全材料の評価とエネルギー順の削減 → 1×1 の空隙を埋めて6条件を取り直す
    (`thin_voids`)→ 最終確定マスクのメッシュ化 → 保持・除外のメッシュ集合演算 → メッシュボディとして文書へ
    (単一の製品経路。`docs/contracts/topopt-binary-search.md`)。

    元のボディは変えず、足すのは1つだけ(その `body_id` を返す)。探索候補の評価中はメッシュ化・STL出力を呼ばない
    (`docs/TOPOPT.md`「二値探索の方式と契約」)。後段へ渡るのは `binary_search.find_best_feasible` の最終確定マスクに
    1×1 の空隙を埋めたものだけ(埋めたときは6条件を取り直し、`safety_factor` はその値。探索時の値は `search_safety_factor`)。

    戻り値(`state` で分岐すること。`"done"` 以外では `body_id` は `None`):

        {"state": "done" | "empty_condition_faces" | "unrepresented_regions"
                  | "boundary_mapping_failed"
                  | "no_feasible_found" | "input_contradiction" | "resolution_insufficient"
                  | "fill_infeasible"                        # 空隙を埋めた形が6条件に不合格
                  | "final_unsafe",                          # 完成形の表面の応力(面積の上位値)で見た安全率が本来の目標を割る
         "search_safety_factor": float|None, "filled_cells": int|None,
         "body_id": BodyId | None, "name": str | None,
         "warning": str | None,
         "empty_condition_faces": [診断, ...],     # 空の条件面ごとの診断(`AnchorTable.condition_face_diagnoses`)
         "unrepresented_regions": [診断, ...],     # 事後検出(下記)
         "boundary_mapping": {...} | None,        # 確定表の検証(`anchor.condition_mapping`)
         "resolution": adapter.resolution_limits() の戻り, "element_size_mm": float,
         "min_width": {"requested_mm": float, "spacing_mm": float, "cells": int, "guaranteed_mm": float,
                       "source": "default" | "requested"},
         "min_width_keep_without_room": {"cells": int, "around_exclude": int, "around_outside": int},
         "grid": {"origin","spacing","shape"}, "design_elements": int,
         "safety_factor": float|None, "target_safety_factor": float,
         "search_target_safety_factor": float, "search_target_factor": float,
         "allowable": adapter.allowable_stress() の戻り,
         "material_mm3": float|None, "evaluations": int, "termination": str|None,
         "anchor_check": {"limit_mm","fixed_mm","load_mm"} | None,
         "triangle_count": int|None, "vertex_count": int|None,
         "final_safety_factor": float|None,      # 完成形(平滑化・集合演算の後)を細かい格子で解いた、部材域の表面の応力(面積の上位値)での安全率
         "final_check": topopt.final_check.inspect の戻り | None,   # 強度と、つなぎ目の幅の診断(3値。適合は出さない)
         "full_material_limit_load": {"factor","load_total_n","limit_load_total_n","safety_factor",
                                      "search_target_safety_factor"} | None,   # 全材料が安全率だけで落ちたとき、探索を始められる荷重
         "backtrack": {"reason","steps_back","checks","first_final_safety_factor","first_material_mm3"} | None,
                                                 # 探索の最終形が完成形検査で割り、合格形をさかのぼったときの記録
         "seconds": float}

    `target_safety_factor` は利用者の実際の目標、`search_target_safety_factor` は探索(と6条件の `safety`)に渡した目標
    (= 実際の目標 × `search_target_factor`)。`safety_factor` は探索の格子での値で、探索時の目標に対する合否であって
    実際の目標に対する実測ではない。`final_safety_factor` は完成形を間隔 0.67 mm 以下の格子で解き直し、部材域の表面の
    面積 4 mm² の上位値(要素数の上限で粗くしか測れないときは割り増す)で割った値で、係数を掛けずに実際の目標と比べる。
    割ったときは探索の合格形をさかのぼり、完成形検査に通る形を警告付きで足す(`topopt.complete.backtrack`、`backtrack` に記録)。
    どれも通らなければ `state="final_unsafe"` で結果を足さない(#129、2026-10-11 本人決定)。
    測れなかったとき(`final_safety_factor` が None)は警告を付けて足す。

    `min_width_keep_without_room` は、最小幅の規則の対象外にした保持ソリッドのうち、設計空間のどこにも m 立方が入らず
    保証が効かないセルの数(`binary_search.keep_cells_without_room`)。0 でなければ `warning` に出る。

    後半の `state`(`no_feasible_found` / `input_contradiction` / `resolution_insufficient` / `boundary_mapping_failed`)は
    `binary_search.SearchResult.status` をそのまま渡したもの。`boundary_mapping_failed` は探索の前、確定表の検証
    (`adapter._map_boundary_conditions`)で不成立になったときにもここから返す。

    戻り値は素の値だけでできている(`json.dumps` が通り、画面がWebSocketへそのまま載せられる)。
    `DesignSpace` や `AnchorTable` のような実体は外へ出さない。

    デバッグ表示は `debug(event: dict)` + `debug_kinds`(`DEBUG_KINDS` の部分集合)。頼まれたときだけ、塊(`space`)を
    導出直後に1回(`stage="derive"`)、面アンカーの節点(`anchors`。探索・FEMが使う確定表の節点)を境界条件の確定直後に
    1回(`stage="mapping"`)、最終確定マスク(`rho`。0/1の二値)を探索の後に1回(`stage="final"`)配る。
    戻り値には入れない(結果メッセージが数MBに膨らむため)。

    進捗は `progress(event: dict)`(`_ProgressPump`)。中断は `should_cancel()` が真になった時点で `adapter.OptimizeCancelled` を
    投げて降りる(`find_best_feasible` の `is_cancelled` にも同じ callable を渡す)。探索は評価と評価のあいだでしか
    中断を見ず、FEM評価1回の途中では降りない(hanger 2.4mm では1回 約100〜137秒)。

    失敗は例外で上がる。握り潰さないこと —— `OptimizeInputError` / `FaceMappingError`(RuntimeError 派生)/
    `adapter.OptimizeCancelled` と、下の層の `design_space.DesignSpaceDisconnectedError` / `DesignSpaceTooSmallError` /
    `DesignSpaceConflictError` / `DesignSpaceGeometryError` は、どれも「利用者が次に何をすればよいか」を文面に持っている。
    """
    started = time.perf_counter()
    resolved = resolve_conditions(doc, options, events)
    pump = adapter._ProgressPump(progress, should_cancel)
    dbg = adapter._DebugSink(debug, debug_kinds)
    prepared = prepare_search(doc, resolved, pump=pump, dbg=dbg, events=events, started=started)
    if isinstance(prepared, dict):
        return prepared
    outcome = search(doc, prepared, pump=pump, steps=steps, should_cancel=should_cancel,
                     finish_request=finish_request, events=events, started=started)
    if isinstance(outcome, dict):
        return outcome
    plan = make_finish_plan(doc, prepared, outcome.result)
    if steps is not None:
        steps.keep(len(plan.accepted))
        steps.plan = plan
    last = len(plan.accepted) - 1
    requested = outcome.requested
    if requested is None or not 0 <= requested < last:
        index = last
    else:
        index = int(requested)
    stopped = outcome.result.termination == 'cancelled'
    return finish_shape(doc, index, plan, pump=pump, dbg=dbg, started=started, events=events,
                        termination="stopped_by_user" if stopped else outcome.result.termination,
                        chosen_by_user=requested is not None)


def resolve_conditions(doc: Document, options: OptimizeOptions | None, events: RunEvents) -> ResolvedRun:
    """入力条件と実際に使う解像度・最小幅・探索目標を確定する。"""
    opts = replace(options) if options is not None else adapter.OptimizeOptions()
    search_target = adapter._search_target_safety_factor(opts)
    factor = float(opts.search_target_factor)
    bundle = adapter.build_conditions(doc)
    limits = adapter.resolution_limits(doc)
    # 梯子の外の値を渡されても、`derive` が実際に作る格子の要素サイズ(要求値以下)に揃える。
    spacing = adapter._snap_to_grid(
        limits["bbox_min"], limits["bbox_max"], adapter._resolve_element_size(limits, opts.element_size_mm)
    )
    min_width = adapter._resolve_min_width(
        spacing, opts.min_width_mm, max_cells=min(adapter._grid_shape(limits["bbox_min"], limits["bbox_max"], spacing))
    )

    events.snapshot('resolved-conditions', bundle=bundle, limits=limits, spacing=spacing,
                   min_width=min_width, allowable=adapter.allowable_stress(),
                   target_safety_factor=float(opts.target_safety_factor),
                   search_target_safety_factor=search_target, search_target_factor=factor)

    return ResolvedRun(opts, bundle, limits, spacing, MinimumWidth(**min_width), search_target, factor)


def prepare_search(doc: Document, resolved: ResolvedRun, *, pump: _ProgressPump, dbg: _DebugSink,
                   events: RunEvents, started: float) -> PreparedSearch | dict:
    """設計空間を一度だけ導出し、条件面と保持・除外の表現、境界対応を確認する。"""
    from .topopt import design_space as ds

    opts, bundle, limits = resolved.options, resolved.bundle, resolved.limits
    spacing, min_width, search_target, factor = resolved.spacing, resolved.min_width.as_dict(), resolved.search_target, resolved.factor
    pump.emit(phase="prepare", message=f"要素サイズ {spacing:.4g} mm で塊を導出する")

    # 塊はここで1回だけ導出し、探索の全候補がこの `space` を使う。
    space = ds.derive(bundle.conditions, spacing=spacing)
    pump.check()
    adapter._require_design_elements_within_limit(space.element_count, float(space.grid.spacing))
    pump.emit(
        phase="derive",
        message=f"塊 {space.element_count} 要素(格子 {space.grid.shape})",
        design_elements=space.element_count,
    )
    # 塊は1回だけ配る(`?debug=space`)。最後に届くのは密度場 `rho` だけ。
    dbg.space("derive", space)

    from .topopt import binary_search

    keep_room = binary_search.keep_cells_without_room(space.mask, space.keep, min_width["cells"], space.obstacle)
    events.snapshot('min-width-keep-without-room', **keep_room)
    keep_room_warning = adapter._keep_without_room_warning(min_width, keep_room)

    common = {
        "resolution": limits,
        "element_size_mm": float(space.grid.spacing),
        "min_width": min_width,
        "min_width_keep_without_room": keep_room,
        "grid": space.grid.as_dict(),
        "design_elements": int(space.element_count),
        "allowable": adapter.allowable_stress(),
        "target_safety_factor": float(opts.target_safety_factor),
        "search_target_safety_factor": search_target,
        "search_target_factor": factor,
        # 条件面が空で早期returnしたときは測れない(節点が0個)ので None のまま。
        "anchor_check": None,
        # 全材料が安全率だけで落ちたときだけ値を持つ(`adapter._full_material_limit_load`)。
        "full_material_limit_load": None,
        "backtrack": None,
    }

    # 条件面が本番の解像度で空になっていないか(`design_space.connect_conditions` は空のとき判定を降りるだけで
    # 上へ伝えない)。空なら診断を積み、探索を呼ばず早期return。
    empty_faces = adapter._empty_condition_faces(space)
    if empty_faces:
        from .topopt import anchor as an

        return adapter._not_done(
            common,
            started,
            keep_room_warning,
            state="empty_condition_faces",
            warning=an.format_condition_face_warning(empty_faces),
            empty_condition_faces=empty_faces,
            unrepresented_regions=[],
            boundary_mapping=None,
            evaluations=0,
            termination=None,
        )

    # 事後検出。保持・除外ソリッドがこの解像度で1つでも表現できていなければ最適化を回さずに戻る。
    unrepresented = adapter._region_representation(doc, bundle, space)
    if any(item["elements"] == 0 for item in unrepresented):
        return adapter._not_done(
            common,
            started,
            keep_room_warning,
            state="unrepresented_regions",
            warning=adapter.format_region_warning(unrepresented),
            unrepresented_regions=unrepresented,
            boundary_mapping=None,
            evaluations=0,
            termination=None,
        )

    # 境界条件の確定。導出出力に接する節点だけへ条件面を対応付け直した確定表を作って検証する。
    # 以後は全てこの表を使い、不成立なら探索もFEMも始めない。
    from .topopt import anchor as an

    table, mapping = adapter._map_boundary_conditions(space, bundle)
    pump.check()
    if mapping["status"] != "pass":
        return adapter._not_done(
            common,
            started,
            keep_room_warning,
            state="boundary_mapping_failed",
            warning=an.format_mapping_warning(mapping),
            unrepresented_regions=unrepresented,
            boundary_mapping=mapping,
            evaluations=0,
            termination="boundary_mapping_failed",
        )

    # 荷重・拘束が選んだ面に載ったかを確定表の節点で測り直す(`_verify_face_mapping` は面添字の対応、こちらは節点の位置。2枚目の網)。
    anchor_check = adapter._check_anchor_centroids(doc, space, table=table)
    common["anchor_check"] = anchor_check
    dbg.anchors("mapping", space, table=table)

    conditions = adapter._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
    return PreparedSearch(resolved, space, conditions, common, keep_room_warning, unrepresented, mapping)


def search(doc: Document, prepared: PreparedSearch, *, pump: _ProgressPump, steps: OptimizationSteps | None,
           should_cancel: Callable[[], bool] | None, finish_request: FinishRequest | None,
           events: RunEvents, started: float) -> SearchOutcome | dict:
    """探索し、最後の仕上げ要求を閉じて、中断と仕上げを判別する。"""
    from .topopt import binary_search

    resolved = prepared.resolved
    opts, factor, search_target = resolved.options, resolved.factor, resolved.search_target
    space, conditions, min_width = prepared.space, prepared.conditions, resolved.min_width.as_dict()
    common, keep_room_warning = prepared.common, prepared.keep_room_warning
    unrepresented, mapping = prepared.unrepresented, prepared.mapping
    search_kwargs: dict[str, Any] = {}
    if opts.max_evaluations is not None:
        search_kwargs["max_evaluations"] = int(opts.max_evaluations)
    search_kwargs["min_width_cells"] = min_width["cells"]
    budget_text = str(opts.max_evaluations) if opts.max_evaluations is not None else "既定"
    target_text = f"目標安全率 {opts.target_safety_factor:.3g}"
    if factor != 1.0:
        target_text += (f"(探索の格子では {search_target:.3g} = {opts.target_safety_factor:.3g} × {factor:.3g} "
                        "を満たす形を探す)")
    pump.emit(
        phase="search",
        message=f"形状の探索を始める(全材料の評価 → エネルギー順の削減。{target_text}・評価予算 {budget_text})",
    )
    pump.check()

    observer = SearchObserver(space, factor, started, pump, steps, should_cancel, finish_request)

    search_result = binary_search.find_best_feasible(
        space.mask, conditions, is_cancelled=observer.stop_requested, on_evaluation=observer.on_evaluation,
        on_accept=None if steps is None else observer.on_accept, **search_kwargs
    )
    events.snapshot(
        'search-result',
        status=search_result.status,
        termination=search_result.termination,
        evaluations=search_result.evaluations,
        material_mm3=(
            None if search_result.best_feasible is None
            else search_result.best_feasible.material_mm3
        ),
    )

    # 仕上げの要求は探索がどう終わっても読む(予算・枯渇で終わる直前に届いた要求を黙って捨てない)。
    requested = None if finish_request is None else finish_request(final=True)
    if search_result.termination == 'cancelled':
        # 中断時は製品文書へ勝手に結果を追加しない(best_feasible が見つかっていても。トレースには残した)。
        if (should_cancel is not None and should_cancel()) or requested is None:
            raise adapter.OptimizeCancelled("形状探索を中断した")
        if search_result.status != 'feasible' or not search_result.accepted:
            raise adapter.OptimizeCancelled("合格した形がまだ無いので、仕上げずに探索を止めた")

    if search_result.status != 'feasible':
        # `SearchResult.status` が 'feasible' 以外ならそのまま渡す(段階4の契約)。
        limit = adapter._full_material_limit_load(
            search_result, sum(float(item.get("magnitude") or 0.0) for item in doc.optimize.loads))
        return dict(adapter._not_done(
            common,
            started,
            keep_room_warning,
            state=search_result.status,
            warning=adapter._search_failure_warning(search_result, min_width, factor, limit),
            unrepresented_regions=unrepresented,
            boundary_mapping=mapping,
            evaluations=int(search_result.evaluations),
            termination=search_result.termination,
        ), full_material_limit_load=limit)

    return SearchOutcome(search_result, requested)


def make_finish_plan(doc: Document, prepared: PreparedSearch, result: SearchResult) -> FinishPlan:
    """探索の計算入力と合格形を引き継ぎ、文書と実行中の通知先から寿命を分ける。"""
    from .topopt.binary_search import AcceptedShape

    resolved, space = prepared.resolved, prepared.space
    bundle = resolved.bundle
    inputs = CompletionInputs(
        space_mask=space.mask, grid=space.grid, conditions=prepared.conditions, min_width=resolved.min_width,
        target_safety_factor=float(resolved.options.target_safety_factor),
        search_target_safety_factor=float(resolved.search_target),
        keep_shapes=tuple(doc.get_body(bid).shape for bid in bundle.keep_body_ids),
        exclude_shapes=tuple(doc.get_body(bid).shape for bid in bundle.exclude_body_ids),
        anchor_soup=space.soup, search_status=result.status, search_termination=result.termination)
    accepted = tuple(result.accepted) or (AcceptedShape.of(result.best_feasible),)
    best = result.best_feasible
    shape = FinishingShape(best.mask, float(best.safety_factor), float(best.material_mm3))
    return FinishPlan(inputs, accepted, shape, dict(prepared.common), prepared.keep_room_warning,
                      prepared.unrepresented, prepared.mapping, int(result.evaluations))
