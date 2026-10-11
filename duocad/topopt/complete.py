"""探索の合格形から、文書へ足す完成形を作る(`optimize_run._run` の後段)。

1形ぶんの手順(`complete`): 平滑化で潰れる 1×1 の空隙を埋める(埋めたら6条件を取り直す)→ 確定マスクのメッシュ化 →
保持を足し除外を引くメッシュ集合演算(`finalize`)→ 完成形検査(`final_check`)。

完成形検査が本来の目標安全率を割ったら、探索の合格形(`binary_search.SearchResult.accepted`)をさかのぼり、検査に
通る形を探す(`backtrack`。2026-10-11 本人決定、docs/TOPOPT_PEAK_GATE.md 6章)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from . import acceptance, final_check, thin_voids, trace
from . import mesh as topo_mesh
from . import min_width as min_width_rules
from .finalize import finalize

#: さかのぼるときに完成形検査を回す回数の上限(最初の形の検査は数えない)。1回に空隙埋め・集合演算・完成形検査がかかり、
#: hanger では数分。置いた値で実測ではない。
MAX_BACKTRACK_CHECKS = 5
#: 通る形が見つかった後、通った形と割った形の探索の材料の差がこの割合未満なら二分をやめる(それ以上寄せても減らせる材料が
#: 小さい)。hanger 180N では 5.9% の差を詰める二分 4 回(約 80 分)がすべて割った(docs/TOPOPT_PEAK_GATE.md 4-14)。置いた値。
BACKTRACK_MIN_GAIN = 0.03


@dataclass
class Context:
    """`complete` が使う、探索の前に決まっているもの。`emit(phase, message)` は進捗、`check()` は中断の確認、
    `place(verts_smooth)` はメッシュの頂点を実寸へ置いて格子に載っているかを確かめた平らな座標の並び。
    `passthrough` は `backtrack` が候補の失敗として握らずに通す例外の型(中断)。"""

    space_mask: np.ndarray
    grid: Any
    conditions: Any
    min_width: dict
    target_safety_factor: float
    search_target_safety_factor: float
    keep_shapes: list
    exclude_shapes: list
    anchor_soup: Any
    emit: Callable[[str, str], None]
    check: Callable[[], None]
    place: Callable[[Any], list]
    search_status: str = 'feasible'
    search_termination: Optional[str] = None
    passthrough: tuple = ()


@dataclass
class Completion:
    """1形ぶんの結果。`fill_failure` は埋めた形が6条件に落ちたときの warning 文(そのときメッシュ以降は無い)。"""

    mask: np.ndarray
    search_safety_factor: float
    safety_factor: float
    material_mm3: float
    fill: dict
    fill_warning: str = ''
    fill_failure: Optional[str] = None
    surface: Any = None
    final_vertices: Optional[np.ndarray] = None
    final_faces: Optional[np.ndarray] = None
    checked: Optional[dict] = None

    @property
    def final_safety_factor(self) -> Optional[float]:
        return None if self.checked is None else self.checked['safety_factor']

    @property
    def verdict(self) -> str:
        """`'fill_infeasible'` か、完成形検査の強度の `status`(`'pass'` / `'fail'` / `'unknown'`)。"""
        if self.fill_failure is not None:
            return 'fill_infeasible'
        return ((self.checked or {}).get('safety') or {}).get('status') or 'unknown'


@dataclass
class Backtrack:
    """`backtrack` の戻り。`chosen` は検査に通った形(無ければ None)、`checks` は回した検査の記録。"""

    chosen: Optional[Completion]
    chosen_index: Optional[int]
    checks: list = field(default_factory=list)
    reason: str = 'nothing_to_go_back'  # 'passed' | 'exhausted'(最初の形まで割った)| 'check_limit' | 'nothing_to_go_back'


def complete(mask: np.ndarray, search_safety_factor: float, material_mm3: float, ctx: Context) -> Completion:
    ctx.emit('fill', '平滑化で潰れる 1×1 の空隙を埋める')
    filled, fill = thin_voids.fill_tubes(mask, ctx.space_mask)
    done = Completion(mask=filled, search_safety_factor=float(search_safety_factor),
                      safety_factor=float(search_safety_factor), material_mm3=float(material_mm3), fill=fill)
    if fill['cells']:
        # 平滑化で潰れる 1×1 の空隙(管)を埋める(2026-10-07 本人判断: 探索後に1回)。埋めた形は探索の合格形と違うので
        # 6条件(FEM)を取り直し、納品は最後に検査に通った形にする。
        ctx.emit('fill', f"{fill['cells']} セルを埋めたので6条件を取り直す")
        report = acceptance.inspect(filled, filled, ctx.grid, **ctx.conditions.inspect_kwargs())
        # 最小幅の規則(m 立方の被覆)は6条件に無い。対角が空の配置(十字の中心など)では埋めたセルが m 立方に覆われない
        # (実測 2026-10-07: F2000_h2.4_w2 で 53 セル中 5、F2000_h2.2_w2 で 63 中 2、F1000_h2.4_w3 で 20 中 2。どれも保持セルに接しない)。
        # 覆われることを求めると実データの多くが落ちるので、保持セルと同じく対象外として数を warning に出す(`keep_cells_without_room` と同じ扱い)。
        uncovered = 0
        cells = int(ctx.min_width['cells'])
        if cells > 1:
            keep = ctx.conditions.keep_cells
            exempt = filled if keep is None else (filled | keep)
            covered = min_width_rules.cube_opening(exempt, cells)
            uncovered = int((filled & ~np.asarray(mask, dtype=bool) & ~covered).sum())
        trace.snapshot('tube-fill', **fill, search_safety_factor=done.safety_factor, report=report,
                       min_width=dict(cells=cells, uncovered_filled_cells=uncovered))
        if report['status'] != 'pass':
            failed = [key for key, check in report['checks'].items() if check['status'] != 'pass']
            safety = report['checks']['safety']
            done.fill_failure = (f"平滑化で潰れる 1×1 の空隙 {fill['cells']} セルを埋めた形が6条件に不合格({', '.join(failed)})。"
                                 f"探索の安全率 {done.safety_factor:.4g}、埋めた後 {safety.get('safety_factor', float('nan')):.4g}"
                                 f"(探索の目標 {ctx.search_target_safety_factor:.4g})")
            return done
        done.safety_factor = float(report['checks']['safety']['safety_factor'])
        done.material_mm3 = float(report['volumes']['material_mm3'])
        if uncovered:
            done.fill_warning = (f"埋めた 1×1 の空隙 {fill['cells']} セルのうち {uncovered} セルは最小幅 "
                                 f"{float(ctx.min_width['guaranteed_mm']):.4g} mm({cells}セル)の立方に覆われない"
                                 f"(2軸以上で両隣が材料なので、保持セルと同じく最小幅の規則の対象外として扱った)")
    else:
        trace.snapshot('tube-fill', **fill)

    ctx.emit('mesh', '確定マスクをメッシュ化する')
    # 候補評価中には呼ばれない入口(島除去・保持塗り直しを掛け直さない。`docs/TOPOPT.md`「二値探索の方式と契約」)。
    # `spacing` は既定(要素添字空間の1.0)のまま渡す: 実寸への変換は `place` が一括で行う契約で、
    # ここでも渡すと二重に掛かりメッシュが格子の外へ外れる。
    surface = topo_mesh.build_surface_from_mask(filled)
    positions = ctx.place(surface.verts_smooth)
    trace.snapshot('selected-result', status=ctx.search_status, termination=ctx.search_termination,
                   material_mm3=done.material_mm3, safety_factor=done.safety_factor, filled_cells=fill['cells'])
    final_vertices, final_faces = finalize(
        np.asarray(positions).reshape(-1, 3), surface.faces, ctx.keep_shapes, ctx.exclude_shapes,
        progress=lambda message: ctx.emit('finalize', message),
    )
    ctx.emit('final_check',
             f"完成形を細かい格子(間隔 {final_check.MEASURE_SPACING_MM:g} mm 以下。大きい題材は粗くして割り増す)で"
             "解き直して表面の応力を測り、つなぎ目の幅を調べる")
    ctx.check()
    checked = final_check.inspect(final_vertices, final_faces, grid=ctx.grid, conditions=ctx.conditions, mask=filled,
                                  min_width=ctx.min_width, target_safety_factor=ctx.target_safety_factor,
                                  keep_cells=ctx.conditions.keep_cells, anchor_soup=ctx.anchor_soup)
    trace.snapshot('final-check', **checked)
    done.surface, done.final_vertices, done.final_faces, done.checked = surface, final_vertices, final_faces, checked
    return done


def shortfall_message(done: Completion, target_safety_factor: float) -> str:
    return (f"完成形(平滑化・保持/除外の後)の表面の応力で見た安全率 {done.final_safety_factor:.4g} が目標 "
            f"{target_safety_factor:.4g} を下回る(探索時の値 {done.safety_factor:.4g}。"
            f"{final_check.peak_phrase(done.checked)})")


def backtrack(accepted: list, failed: Completion, ctx: Context, *, complete_shape=None) -> Backtrack:
    """完成形検査に落ちた `failed`(= `accepted[-1]` の完成形)から、`accepted`(`binary_search.AcceptedShape` の並び)を
    さかのぼって完成形検査に通る形を探す。

    最初は「完成形の安全率 ÷ 探索の安全率」の比が変わらないと見て、探索の安全率が 目標 ÷ 比 以上あった最も新しい形へ一気に
    戻る。割ればその形の比で同じことをする(比が取れなければ最初の形との中間)。通る形が見つかった後は、通った形と割った形の
    間を二分して、通る中で最も新しい(材料の少ない)形へ寄せる。二分は両者の材料の差が `BACKTRACK_MIN_GAIN` 未満になったら
    やめる。検査は `MAX_BACKTRACK_CHECKS` 回まで。
    候補の手順が例外で落ちたら(集合演算の自己交差など)その形は割ったものとして続ける(`ctx.passthrough` の型は通す)。
    `complete_shape` は1形ぶんの手順(既定は `complete`。検査が差し替える口)。
    """
    complete_shape = complete if complete_shape is None else complete_shape
    result = Backtrack(chosen=None, chosen_index=None)
    if len(accepted) < 2:
        return result
    target = float(ctx.target_safety_factor)
    last = len(accepted) - 1
    fail_hi, pass_lo = last, None

    def guided(done: Completion, below: int) -> int:
        sf = done.final_safety_factor
        if sf is None or not sf > 0.0 or not done.search_safety_factor > 0.0:
            return below // 2
        need = target * done.search_safety_factor / sf
        fitting = [i for i in range(below) if accepted[i].safety_factor >= need]
        return max(fitting) if fitting else 0

    probe = guided(failed, fail_hi)
    while len(result.checks) < MAX_BACKTRACK_CHECKS:
        ctx.check()
        shape = accepted[probe]
        ctx.emit('backtrack', f"完成形が目標を割ったので、探索を {last - probe} 手戻した形(探索の安全率 "
                              f"{shape.safety_factor:.4g})で作り直して検査する({len(result.checks) + 1}/{MAX_BACKTRACK_CHECKS})")
        try:
            done = complete_shape(shape.mask(), shape.safety_factor, shape.material_mm3, ctx)
        except ctx.passthrough:
            raise
        except Exception as error:
            result.checks.append(dict(index=probe, steps_back=last - probe, search_safety_factor=shape.safety_factor,
                                      material_mm3=shape.material_mm3, final_safety_factor=None, verdict='error',
                                      error=f'{type(error).__name__}: {error}'))
            done = None
        else:
            result.checks.append(dict(index=probe, steps_back=last - probe, search_safety_factor=shape.safety_factor,
                                      material_mm3=done.material_mm3, final_safety_factor=done.final_safety_factor,
                                      verdict=done.verdict))
        if done is not None and done.verdict == 'pass':
            pass_lo, result.chosen, result.chosen_index = probe, done, probe
        else:
            fail_hi = probe
        if pass_lo is None:
            if probe == 0:
                result.reason = 'exhausted'
                break
            probe = fail_hi // 2 if done is None else guided(done, fail_hi)
        else:
            if fail_hi - pass_lo <= 1:
                break
            if 1.0 - accepted[fail_hi].material_mm3 / accepted[pass_lo].material_mm3 < BACKTRACK_MIN_GAIN:
                break
            probe = (pass_lo + fail_hi) // 2
    else:
        result.reason = 'check_limit'
    if result.chosen is not None:
        result.reason = 'passed'
    trace.snapshot('backtrack', reason=result.reason, chosen_index=result.chosen_index, checks=result.checks,
                   accepted=len(accepted))
    return result


def backtrack_message(failed: Completion, chosen: Completion, steps_back: int, target_safety_factor: float) -> str:
    """戻した形を足すときの warning の一文。"""
    added = (chosen.material_mm3 / failed.material_mm3 - 1.0) * 100.0 if failed.material_mm3 > 0 else float('nan')
    return (f"探索の最終形は完成形の安全率 {failed.final_safety_factor:.4g} で目標 {target_safety_factor:.4g} を割ったので、"
            f"探索を {steps_back} 手戻した形を足した(材料 {added:+.1f}%、完成形の安全率 {chosen.final_safety_factor:.4g})")


def exhausted_note(result: Backtrack) -> str:
    """戻しても通らなかったときに、割った警告へ添える一文(戻す形が無ければ空)。"""
    if not result.checks:
        return ''
    deepest = max(row['steps_back'] for row in result.checks)
    where = '最初の形まで' if result.reason == 'exhausted' else f"最大 {deepest} 手まで"
    return f"探索の合格形を{where}さかのぼって {len(result.checks)} 形を検査したが、どれも目標を割った"
