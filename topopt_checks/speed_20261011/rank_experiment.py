r"""削る順位・一口の選び方の試作を、製品の入口(探索 → 完成形検査 → さかのぼり)で回して比べる。製品のコードは変えない。

    .venv\Scripts\python.exe topopt_checks\speed_20261011\rank_experiment.py --rule baseline --out <json>
    .venv\Scripts\python.exe topopt_checks\speed_20261011\rank_experiment.py --rule share --param 0.005 --out <json>

規則(`RULES`)は `PeelWalk` の一口ごとに「飛ばすか」を答える(`veto`)か、順位そのものを並べ替える(`reorder`)。
親の要素エネルギーは、段階4が作る `FeasibleModel` を横取りして引く。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\tm976\mf\duocad")
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

PARENTS: dict = {}   # id(親マスク) → FeasibleModel
STATS = dict(vetoed=0, walks=0, critical_cells=0, critical_events=0)
LAST_STRESS: dict = {}   # 直近の stress.evaluate の (von Mises [MPa], 代表応力 Q_V [MPa])
CRITICAL: set = set()    # 安全率で落ちた1手が消した、親の Q_V 以上のセル(選択肢 B)


def _install(bs, rule, param):
    """段階4の親(`FeasibleModel`)を横取りし、一口の判定(`min_width.bite_verdict`)の前に規則の `veto` を挟む。
    飛ばした一口は `skipped_surface` に数えられる(試作なので `PeelStats` の欄は足さない。数は `STATS['vetoed']`)。
    巻き添えの無い1セルの手(m>1 ではまれ)には挟まない。"""
    from duocad.topopt import min_width

    from duocad.topopt import stress

    class Registered(bs.FeasibleModel):
        # dataclass の __init__ は、元のクラスに __post_init__ が無いと派生側のものを呼ばないので __init__ を包む。
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            PARENTS[id(self.mask)] = self
            self.stress = dict(LAST_STRESS)  # 合格形は検査の直後に作られるので、直近の応力がこの形のもの

    real_evaluate = stress.evaluate

    def evaluate(*args, **kwargs):
        report = real_evaluate(*args, **kwargs)
        LAST_STRESS.update(vm=np.asarray(report.field.von_mises), q=float(report.representative_mpa))
        return report

    stress.evaluate = evaluate

    bs.FeasibleModel = Registered
    current = {}
    real_take = bs.PeelWalk.take

    def take(self, order, count):
        if getattr(self, '_rule_for', None) is not self.parent:
            STATS['walks'] += 1
            parent = PARENTS.get(id(self.parent))
            energy = None if parent is None or parent.energy is None else np.asarray(parent.energy, dtype=float)
            made = None if energy is None else rule(self.parent, energy, param)
            self._rule = made if callable(made) else None
            self._reorder = made if isinstance(made, Reorder) else None
            self._rule_for = self.parent
        current['walk'] = self
        if self._reorder is not None:
            order = self._reorder.apply(self, order, count)
        out = real_take(self, order, count)
        current['picked'] = out[1].picked
        return out

    bs.PeelWalk.take = take
    real_verdict = min_width.bite_verdict

    def bite_verdict(padded, bite):
        walk = current.get('walk')
        if walk is not None and walk._rule is not None:
            if walk._rule(np.ravel_multi_index(tuple(np.asarray(bite).T), walk.parent.shape)):
                STATS['vetoed'] += 1
                return 'surface', False
        return real_verdict(padded, bite)

    min_width.bite_verdict = bite_verdict

    if getattr(rule, 'learns_critical', False):
        real_evaluate_survivor = bs._evaluate_survivor

        def evaluate_survivor(survivor, before, conditions, **kw):
            report, trial = real_evaluate_survivor(survivor, before, conditions, **kw)
            walk = current.get('walk')
            parent = None if walk is None else PARENTS.get(id(walk.parent))
            safety = report.get('checks', {}).get('safety', {})
            if (parent is not None and current.get('picked') == 1 and safety.get('status') == 'fail'
                    and getattr(parent, 'stress', None)):
                removed = walk.parent & ~np.asarray(survivor, dtype=bool)
                hot = np.flatnonzero(removed & (parent.stress['vm'] >= parent.stress['q']))
                fresh = set(hot.tolist()) - CRITICAL
                if fresh:
                    CRITICAL.update(fresh)
                    STATS['critical_cells'] = len(CRITICAL)
                    STATS['critical_events'] += 1
                    walk.order = None  # 覚えた歩きを捨てる(飛ばす一口が増えたので、同じ親でも歩きが変わる)
            return report, trial

        bs._evaluate_survivor = evaluate_survivor


class Reorder:
    """順位の先頭 `head(count)` 個を、`score(walk, cell)` の小さい順に並べ直す(同点は元の順)。スコアは親ごとに覚える。"""

    def __init__(self, score, head):
        self.score, self.head, self.cache = score, head, {}

    def apply(self, walk, order, count):
        n = min(len(order), self.head(count))
        front = order[:n]
        keys = np.empty(n)
        for index, cell in enumerate(front.tolist()):
            value = self.cache.get(cell)
            if value is None:
                value = self.cache[cell] = self.score(walk, cell)
            keys[index] = value
        return np.concatenate([front[np.argsort(keys, kind='stable')], order[n:]])


def _bite_cells(walk, cell):
    """親のマスクで `cell` を起点にしたときに消える一口(起点+巻き添え)の平坦添字。"""
    from duocad.topopt import min_width

    shape = walk.parent.shape
    index = np.unravel_index(cell, shape)
    if walk.min_width_cells <= 1:
        return np.array([cell])
    extra = min_width.casualties(walk.parent, tuple(int(v) for v in index), walk.min_width_cells, walk.exempt)
    if not len(extra):
        return np.array([cell])
    return np.concatenate([[cell], np.ravel_multi_index(tuple(extra.T), shape)])


# ---------------------------------------------------------------- 規則
def rule_baseline(parent, energy, param):
    return None


def rule_share(parent, energy, param):
    """一口(起点+巻き添え)のエネルギーの合計が、親の全エネルギーの param 以上なら飛ばす。"""
    total = float(energy[parent].sum())
    flat = energy.ravel()
    return lambda removed: float(flat[removed].sum()) >= param * total


def rule_hot(parent, energy, param):
    """一口に、親の材料のエネルギーの上位 param(割合)のセルが入るなら飛ばす。"""
    threshold = float(np.quantile(energy[parent], 1.0 - param))
    flat = energy.ravel()
    return lambda removed: bool((flat[removed] >= threshold).any())


def rule_bite_energy(parent, energy, param):
    """順位の先頭(要求数の param 倍、2000〜20000 個)を、一口のエネルギーの合計の小さい順に並べ直す(①)。"""
    flat = energy.ravel()
    factor = 8.0 if param is None else param
    return Reorder(lambda walk, cell: float(flat[_bite_cells(walk, cell)].sum()),
                   lambda count: min(20000, max(2000, int(factor * count))))


def rule_bite_density(parent, energy, param):
    """同じく、一口のエネルギーの合計 ÷ 一口のセル数(体積あたり)で並べ直す。"""
    flat = energy.ravel()
    factor = 8.0 if param is None else param

    def score(walk, cell):
        cells = _bite_cells(walk, cell)
        return float(flat[cells].sum()) / len(cells)
    return Reorder(score, lambda count: min(20000, max(2000, int(factor * count))))


def rule_bite_max(parent, energy, param):
    """順位の先頭を、一口の中の「ならしたエネルギー」の最大の小さい順に並べ直す(Fable の A の鍵)。"""
    from duocad.topopt.binary_search import ENERGY_FILTER_SIZE
    from scipy import ndimage

    total = ndimage.uniform_filter(np.where(parent, energy, 0.0), size=ENERGY_FILTER_SIZE, mode='constant')
    count = ndimage.uniform_filter(parent.astype(float), size=ENERGY_FILTER_SIZE, mode='constant')
    smooth = np.where(parent, total / np.maximum(count, 1e-300), 0.0).ravel()
    factor = 8.0 if param is None else param
    return Reorder(lambda walk, cell: float(smooth[_bite_cells(walk, cell)].max()),
                   lambda count: min(20000, max(2000, int(factor * count))))


def rule_critical(parent, energy, param):
    """安全率で落ちた1手が消したセルのうち、親の von Mises が親の代表応力 Q_V 以上のものを覚え、それを含む一口を飛ばす(B)。
    覚えたセルは探索の終わりまで持つ(試作。戻す規則は無い)。"""
    return lambda removed: bool(CRITICAL) and not CRITICAL.isdisjoint(removed.tolist())


rule_critical.learns_critical = True

RULES = dict(baseline=rule_baseline, share=rule_share, hot=rule_hot, bite_energy=rule_bite_energy,
             bite_density=rule_bite_density, bite_max=rule_bite_max, critical=rule_critical)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rule", default="baseline", choices=sorted(RULES))
    p.add_argument("--param", type=float, default=None)
    p.add_argument("--model", default=str(REPO / "models" / "hanger_bar_attachment.json"))
    p.add_argument("--load-n", type=float, default=180.0)
    p.add_argument("--size", type=float, default=2.4)
    p.add_argument("--min-width-mm", type=float, default=10.0)
    p.add_argument("--target", type=float, default=2.0)
    p.add_argument("--device", default="gpu")
    p.add_argument("--out", required=True)
    a = p.parse_args()

    import _bootstrap  # noqa: F401
    from duocad import optimize_run
    from duocad.topopt import binary_search as bs, fem
    from _documents import open_session

    fem.FEM_DEVICE = a.device
    _install(bs, RULES[a.rule], a.param)
    session = open_session(a.model)
    doc = session.document
    if a.load_n and 'hanger' in Path(a.model).name:
        from sweep_smoothing_corpus import _set_load
        _set_load(doc, int(a.load_n))
    elif a.load_n:
        # hanger 以外: 全荷重の大きさを差し替える(topopt_checks/idle_validation/load_sweep_full_material.py と同じ)
        for load in doc.optimize.loads:
            load["magnitude"] = float(a.load_n)
            vec = load.get("vector")
            if isinstance(vec, (list, tuple)) and len(vec) == 3:
                norm = float(sum(c * c for c in vec)) ** 0.5 or 1.0
                load["vector"] = [c / norm * float(a.load_n) for c in vec]
            else:
                load["vector"] = [c * float(a.load_n) for c in load["direction"]]
    events = []
    started = time.perf_counter()
    result = optimize_run.run(doc, optimize_run.OptimizeOptions(
        element_size_mm=a.size, min_width_mm=a.min_width_mm, target_safety_factor=a.target),
        progress=lambda e: events.append(e) if isinstance(e, dict) and "evaluation" in e else None)
    out = dict(rule=a.rule, param=a.param, model=Path(a.model).name, load_n=a.load_n, size=a.size,
               min_width_mm=a.min_width_mm, seconds=time.perf_counter() - started,
               state=result.get("state"), termination=result.get("termination"),
               evaluations=result.get("evaluations"), material_mm3=result.get("material_mm3"),
               search_safety_factor=result.get("safety_factor"),
               final_safety_factor=result.get("final_safety_factor"), backtrack=result.get("backtrack"),
               diagnostic_path=result.get("diagnostic_path"), **STATS,
               search_events=[{k: e.get(k) for k in ("evaluation", "outcome", "candidate_safety_factor", "candidate_cells",
                                                     "safety_factor", "cells")} for e in events])
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, default=float), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "search_events"}, ensure_ascii=False, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
