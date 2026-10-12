"""探索の最終形が完成形検査に落ちたとき、合格形をさかのぼる手順(`topopt.complete.backtrack`)。"""
from types import SimpleNamespace

import numpy as np

from duocad.topopt import binary_search, complete


def _accepted(safety_factors, cells=None):
    """探索の合格形の並び。`cells` を省くと 64 セルから1手ごとに1セルずつ減る。"""
    shapes = []
    for index, sf in enumerate(safety_factors):
        count = 64 - index if cells is None else cells[index]
        mask = np.zeros((10, 10, 10), bool)
        mask.ravel()[:count] = True
        model = binary_search.FeasibleModel(mask=mask, before_mask=mask, trial_index=index, report=dict(
            material_sha256="", volumes=dict(material_mm3=float(mask.sum())), checks=dict(safety=dict(safety_factor=sf))))
        shapes.append(binary_search.AcceptedShape.of(model))
    return shapes


def _context(target=2.0):
    return SimpleNamespace(target_safety_factor=target, emit=lambda phase, message: None, check=lambda: None,
                           passthrough=())


def _completion(mask, search_sf, material, final_sf, verdict):
    status = dict(safety_factor=final_sf, status=verdict)
    return complete.Completion(mask=mask, search_safety_factor=search_sf, safety_factor=search_sf,
                               material_mm3=material, fill=dict(cells=0),
                               checked=dict(safety_factor=final_sf, safety=status, neck={}))


def _by_ratio(ratio, target=2.0, calls=None):
    """完成形の安全率 = 探索の安全率 × ratio の題材。"""
    def complete_shape(mask, search_sf, material, ctx):
        if calls is not None:
            calls.append(search_sf)
        final_sf = search_sf * ratio
        return _completion(mask, search_sf, material, final_sf, 'pass' if final_sf >= target else 'fail')
    return complete_shape


def test_the_ratio_jumps_straight_to_the_newest_shape_that_should_pass():
    """落ちたら、「完成形 ÷ 探索」の比で一気に戻らず、1手ずつ・二分だけで検査を浪費している。"""
    accepted = _accepted([12.0, 10.0, 9.0, 8.5, 8.0, 7.5, 7.0, 6.7])
    failed = _completion(accepted[-1].mask(), 6.7, 57.0, 6.7 * 0.25, 'fail')
    calls = []
    result = complete.backtrack(accepted, failed, _context(), complete_shape=_by_ratio(0.25, calls=calls))
    # 目標 2.0 ÷ 比 0.25 = 8.0 以上の最も新しい形は 8.0(添字 4)。そこが通り、次の 7.5 は割るので2回で決まる
    assert result.reason == 'passed' and result.chosen_index == 4 and calls == [8.0, 7.5]
    assert [row['steps_back'] for row in result.checks] == [3, 2]


def test_after_a_pass_it_bisects_toward_the_lightest_shape_that_still_passes():
    """落ちたら、比の読みが外れて戻りすぎたとき、通る中で最も材料の少ない形へ寄せていない。"""
    accepted = _accepted([20.0, 18.0, 16.0, 14.0, 12.0, 10.0, 8.0, 6.0])
    # 探索の最終形の比 0.1 では 20 必要(添字 0)と読むが、実際の比は 0.2 で 10 以上(添字 5)が通る
    failed = _completion(accepted[-1].mask(), 6.0, 57.0, 0.6, 'fail')
    result = complete.backtrack(accepted, failed, _context(), complete_shape=_by_ratio(0.2))
    assert result.reason == 'passed' and result.chosen_index == 5
    assert len(result.checks) <= complete.MAX_BACKTRACK_CHECKS


def test_when_even_the_first_shape_fails_nothing_is_chosen_and_the_note_says_so():
    """落ちたら、最初の形まで割るのに通った形として返すか、何形を検査したかを利用者へ書いていない。"""
    accepted = _accepted([5.0, 4.0, 3.0])
    failed = _completion(accepted[-1].mask(), 3.0, 61.0, 1.0, 'fail')
    result = complete.backtrack(accepted, failed, _context(), complete_shape=_by_ratio(0.3))
    assert result.chosen is None and result.reason == 'exhausted' and result.checks[-1]['index'] == 0
    assert "最初の形まで" in complete.exhausted_note(result)


def test_unmeasured_or_infeasible_candidates_count_as_failures_and_the_checks_are_capped():
    """落ちたら、測れなかった・埋めた形が不合格の候補を通った形として返すか、検査の回数に上限が無い。"""
    accepted = _accepted([float(30 - i) for i in range(30)])
    failed = _completion(accepted[-1].mask(), 1.0, 35.0, 0.5, 'fail')

    def never(mask, search_sf, material, ctx):
        return _completion(mask, search_sf, material, None, 'unknown')

    result = complete.backtrack(accepted, failed, _context(), complete_shape=never)
    assert result.chosen is None and len(result.checks) <= complete.MAX_BACKTRACK_CHECKS
    assert all(row['verdict'] == 'unknown' for row in result.checks)


def test_nothing_to_go_back_to_without_an_earlier_shape():
    accepted = _accepted([3.0])
    failed = _completion(accepted[-1].mask(), 3.0, 64.0, 1.0, 'fail')
    result = complete.backtrack(accepted, failed, _context(), complete_shape=_by_ratio(0.3))
    assert result.chosen is None and result.reason == 'nothing_to_go_back' and result.checks == []


def test_a_candidate_that_raises_counts_as_a_failure_but_a_cancel_goes_through():
    """落ちたら、候補の集合演算が例外で落ちたときに見つけてあった合格形まで捨てるか、中断を候補の失敗として握りつぶす。"""
    import pytest

    accepted = _accepted([20.0, 18.0, 16.0, 14.0, 12.0, 10.0, 8.0, 6.0])
    failed = _completion(accepted[-1].mask(), 6.0, 57.0, 0.6, 'fail')
    passing = _by_ratio(0.2)

    def broken_at_14(mask, search_sf, material, ctx):
        if search_sf == 14.0:
            raise ValueError("集合演算の結果が自己交差する")
        return passing(mask, search_sf, material, ctx)

    result = complete.backtrack(accepted, failed, _context(), complete_shape=broken_at_14)
    assert result.reason == 'passed' and result.chosen is not None
    assert any(row['verdict'] == 'error' and 'ValueError' in row['error'] for row in result.checks)

    class Cancelled(RuntimeError):
        pass

    def cancelled(mask, search_sf, material, ctx):
        raise Cancelled()

    ctx = _context()
    ctx.passthrough = (Cancelled,)
    with pytest.raises(Cancelled):
        complete.backtrack(accepted, failed, ctx, complete_shape=cancelled)


def test_bisection_stops_once_the_material_left_to_win_is_small():
    """落ちたら、通った形と割った形の材料の差が小さいのに二分を続け、完成形検査(hanger で1回約20分)を浪費している。"""
    accepted = _accepted([12.0, 11.0, 10.0, 9.0, 8.0, 7.0, 6.0], cells=[1000, 998, 996, 994, 992, 990, 988])
    failed = _completion(accepted[-1].mask(), 6.0, 988.0, 1.5, 'fail')  # 比 0.25 → 8.0 以上の最も新しい形(添字 4)
    result = complete.backtrack(accepted, failed, _context(), complete_shape=_by_ratio(0.25))
    # 添字 4(992 セル)が通り、割った最終形(988 セル)との差は 0.4% なので二分しない
    assert result.reason == 'passed' and result.chosen_index == 4 and len(result.checks) == 1
    assert (1 - 988 / 992) < complete.BACKTRACK_MIN_GAIN


def test_a_final_shape_whose_filled_voids_fail_is_also_backtracked_and_the_warning_says_why():
    """落ちたら、空隙を埋めた形が6条件に落ちたとき(何も出ない)にさかのぼらないか、警告が「目標を割った」と取り違える。"""
    accepted = _accepted([9.0, 8.0, 7.0, 6.0, 5.0])
    failed = complete.Completion(mask=accepted[-1].mask(), search_safety_factor=5.0, safety_factor=5.0,
                                 material_mm3=60.0, fill=dict(cells=3), fill_failure="埋めた形が6条件に不合格")
    result = complete.backtrack(accepted, failed, _context(), complete_shape=_by_ratio(0.3))
    assert result.reason == 'passed' and result.chosen is not None
    message = complete.backtrack_message(failed, result.chosen, 4 - result.chosen_index, 2.0)
    assert "空隙を埋めた形が6条件に落ちた" in message and "割った" not in message


def test_when_every_candidate_fails_only_its_filled_voids_the_note_does_not_claim_the_target_was_missed():
    """落ちたら、さかのぼった形が空隙埋めで落ちただけなのに、測っていない安全率で「目標を割った」と書いている。"""
    accepted = _accepted([9.0, 8.0, 7.0])
    failed = complete.Completion(mask=accepted[-1].mask(), search_safety_factor=7.0, safety_factor=7.0,
                                 material_mm3=62.0, fill=dict(cells=3), fill_failure="埋めた形が6条件に不合格")

    def filled_voids_fail(mask, search_sf, material, ctx):
        return complete.Completion(mask=mask, search_safety_factor=search_sf, safety_factor=search_sf,
                                   material_mm3=material, fill=dict(cells=1), fill_failure="埋めた形が6条件に不合格")

    result = complete.backtrack(accepted, failed, _context(), complete_shape=filled_voids_fail)
    note = complete.exhausted_note(result)
    assert result.chosen is None and "空隙を埋めた形が6条件に落ちた" in note and "目標を割った" not in note


def _screened_by(passing_sfs):
    """安い段(空隙埋め)が、探索の安全率が `passing_sfs` にある形だけ通る題材。呼ばれた形の探索の安全率を `calls` に積む。"""
    calls = []

    def screen_shape(mask, search_sf, material, ctx):
        calls.append(search_sf)
        fill_failure = None if search_sf in passing_sfs else "埋めた形が6条件に不合格"
        return complete.Completion(mask=mask, search_safety_factor=search_sf, safety_factor=search_sf,
                                   material_mm3=material, fill=dict(cells=1), fill_failure=fill_failure)
    return screen_shape, calls


def test_shapes_whose_filled_voids_fail_are_skipped_by_the_cheap_stage_without_using_the_check_budget():
    """落ちたら、空隙埋めで落ちる形に完成形検査の枠を使い、通る軽い形へ届かない(dilution 180N で 15,168 に止まった件)。"""
    sfs = [float(40 - i) for i in range(30)]
    accepted = _accepted(sfs)
    failed = complete.Completion(mask=accepted[-1].mask(), search_safety_factor=sfs[-1], safety_factor=sfs[-1],
                                 material_mm3=35.0, fill=dict(cells=3), fill_failure="埋めた形が6条件に不合格")
    screen_shape, screened = _screened_by({sfs[9], sfs[3]})  # 通るのは添字 9 と 3 だけ(入り交じり)
    full = []

    def complete_shape(mask, search_sf, material, ctx):
        full.append(search_sf)
        return _completion(mask, search_sf, material, 3.0, 'pass')

    result = complete.backtrack(accepted, failed, _context(), complete_shape=complete_shape, screen_shape=screen_shape)
    assert result.reason == 'passed' and result.chosen_index == 9 and full == [sfs[9]]
    assert sum(1 for row in result.checks if row['stage'] == 'full') == 1
    assert len(screened) < 28 - 9  # 1手ずつ全部は見ていない(1, 2, 4, ... 手ずつ飛んでから見直す)


def test_the_cheap_stage_falls_back_to_full_checks_when_its_budget_runs_out(monkeypatch):
    """落ちたら、安い段の回数に上限が無く(hanger では1回 約4秒)、上限を超えた後も止まらない。"""
    monkeypatch.setattr(complete, "MAX_BACKTRACK_SCREENS", 3)
    sfs = [float(40 - i) for i in range(30)]
    accepted = _accepted(sfs)
    failed = complete.Completion(mask=accepted[-1].mask(), search_safety_factor=sfs[-1], safety_factor=sfs[-1],
                                 material_mm3=35.0, fill=dict(cells=3), fill_failure="埋めた形が6条件に不合格")
    screen_shape, screened = _screened_by(set())
    full = []

    def complete_shape(mask, search_sf, material, ctx):
        full.append(search_sf)
        return _completion(mask, search_sf, material, 3.0, 'pass')

    result = complete.backtrack(accepted, failed, _context(), complete_shape=complete_shape, screen_shape=screen_shape)
    assert len(screened) == 3 and result.chosen is not None and len(full) <= complete.MAX_BACKTRACK_CHECKS


def test_a_bisection_interval_whose_shapes_all_fail_the_cheap_stage_ends_the_search():
    """落ちたら、通った形と割った形の間がどれも空隙埋めで落ちるのに、その形へ完成形検査を回している。"""
    sfs = [12.0, 11.0, 10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0]
    accepted = _accepted(sfs, cells=[1000 - 40 * i for i in range(len(sfs))])
    failed = _completion(accepted[-1].mask(), 4.0, 680.0, 1.0, 'fail')  # 比 0.25 → 8.0 以上の最も新しい形(添字 4)
    screen_shape, _ = _screened_by({12.0, 11.0, 10.0, 9.0, 8.0})  # 添字 5〜7 は空隙埋めで落ちる
    result = complete.backtrack(accepted, failed, _context(), complete_shape=_by_ratio(0.25), screen_shape=screen_shape)
    assert result.reason == 'passed' and result.chosen_index == 4
    assert [row['index'] for row in result.checks if row['stage'] == 'full'] == [4]
