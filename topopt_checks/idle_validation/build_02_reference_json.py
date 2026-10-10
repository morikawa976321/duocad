"""`refine_*.json` / `analytic_*.json` を集約して `02_reference.json` を書くCLI。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
TARGET = OUT / "02_reference.json"


def _load_cases() -> list[dict]:
    cases = []
    for pattern in ("refine_*.json", "analytic_*.json"):
        for path in sorted(OUT.glob(pattern)):
            if path.name == "02_reference.json":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if "records" in data:
                for rec in data["records"]:
                    cases.append({**rec, "source_file": str(path.relative_to(REPO)), "analytic_kind": path.stem})
            elif "record" in data:
                cases.append({**data["record"], "source_file": str(path.relative_to(REPO)), "analytic_kind": path.stem})
            else:
                cases.append({**data, "source_file": str(path.relative_to(REPO))})
    return cases


def main() -> int:
    cases = _load_cases()
    shapes = sorted({c.get("shape") for c in cases if c.get("shape")})
    levels: dict = {}
    loads: dict = {}
    for c in cases:
        sh = c.get("shape")
        if not sh:
            continue
        levels.setdefault(sh, set())
        loads.setdefault(sh, set())
        if "level" in c:
            levels[sh].add(int(c["level"]))
        if "load_n" in c:
            loads[sh].add(float(c["load_n"]))
    analytic = sorted({c.get("analytic_kind", "") for c in cases if c.get("analytic_kind", "").startswith("analytic")})
    failed = sum(1 for c in cases if c.get("status") in ("failed", "disconnected_at_this_level", "solve_failed"))
    # **手で決め打ちのリスト**(QA指摘: 「missingが常に空」は「要求を全部満たした」という意味に
    # 誤読されうる)。10_SYNTHESIS 付録A(旧 02_REFERENCE 7-1節)・8節が明記する、意図的に対象外とした/未実施の組み合わせを
    # ここにも複製する(機械可読側の読者がdocs/を読まなくても分かるように)。
    missing = [
        "hanger_step05(5%形)・hanger_transplant(移植トラス)は格子細分・ゾーン解析を未実施"
        "(01_PROTOCOL §4のH1/H2代表条件に該当しないため対象外。refine_shape.pyは選択肢として実装済み)。",
        "hangerの格子細分(level>=2)は未実施(design要素が約1200万に達し既知の安全上限を超えるため。"
        "10_SYNTHESIS 付録A(旧 02_REFERENCE 1-2節))。hanger_base/hanger_r2はネイティブ解像度(level=1)のみ。",
        "dilution_r-1のlevel=4(0.5mm)は未実施(設計要素約126万が既知の安全上限に近いため見送り)。",
        "Kirsch比較は同軸引張理論と実際の曲げ支配荷重との不一致、および局所maxの格子依存発散のため"
        "絶対値としては未校正(10_SYNTHESIS 付録A(旧 02_REFERENCE 5-2節))。",
    ]
    limitations = [
        "実CAD/平滑化境界に沿う独立コンフォーマルメッシュ(gmsh等)はこの環境に無く未実装。"
        "Richardson外挿と解析解比較(引張・曲げ・Kirsch)で代替した。",
        "断面力/モーメントは要素中心応力の面積積分による近似(せん断応力場の再構成なし)。",
        f"集約時点で status=failed/disconnected のレコードが {failed} 件ある。",
    ]
    doc = dict(
        cases=cases,
        coverage=dict(
            shapes_covered=shapes,
            levels_covered={k: sorted(v) for k, v in levels.items()},
            loads_covered={k: sorted(v) for k, v in loads.items()},
            analytic_checks=analytic,
            missing=missing,
        ),
        limitations=limitations,
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        files={},
        doc_cross_check=dict(case_count=len(cases), failed_count=failed),
    )
    _common.write_atomic(TARGET, json.dumps(doc, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"cases={len(cases)} failed={failed} -> {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
