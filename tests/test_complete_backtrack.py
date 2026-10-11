"""探索の最終形が完成形検査に落ちたとき、合格形をさかのぼる手順(`topopt.complete.backtrack`)。"""
from types import SimpleNamespace

import numpy as np

from duocad.topopt import binary_search, complete


def _accepted(safety_factors):
    shapes = []
    for index, sf in enumerate(safety_factors):
        mask = np.zeros((4, 4, 4), bool)
        mask.ravel()[:64 - index] = True
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
