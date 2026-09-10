"""`simp_convergence_hanger_2.4mm.json` から SIMP 収束曲線の SVG を描く。

再実行は不要。history の compliance / change を使う。
(volfrac は体積制約で反復中ほぼ一定なので曲線にならない)

    .\\.venv\\Scripts\\python.exe topopt_checks\\plot_simp_volume_convergence.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\plot_simp_volume_convergence.py --metric change
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from duocad.topopt import OUTPUT_DIR

JSON_IN = OUTPUT_DIR / "simp_convergence_hanger_2.4mm.json"
SVG_OUT = OUTPUT_DIR / "simp_compliance_convergence_2.4mm.svg"

_COLORS = {
    "R": "#1f77b4",
    "1": "#ff7f0e",
    "2": "#2ca02c",
    "3": "#d62728",
    "4": "#9467bd",
    "5": "#8c564b",
}

_METRICS = {
    "compliance": ("コンプライアンス C", "コンプライアンス"),
    "change": ("密度変化量 change", "change"),
}


def _svg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _nice_ticks(y_min: float, y_max: float, count: int = 6) -> list[float]:
    if y_max <= y_min:
        return [y_min]
    span = y_max - y_min
    raw = span / max(count - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = mag if raw / mag < 2 else (2 * mag if raw / mag < 5 else 5 * mag)
    start = math.floor(y_min / step) * step
    ticks = []
    y = start
    while y <= y_max + step * 0.5:
        if y >= y_min - step * 0.01:
            ticks.append(y)
        y += step
    return ticks or [y_min, y_max]


def write_svg(payload: dict, path: Path, metric: str) -> None:
    width, height = 1000, 560
    ml, mr, mt, mb = 80, 210, 48, 72
    plot_w = width - ml - mr
    plot_h = height - mt - mb
    spacing = float(payload["spacing_mm"])
    change_tol = float(payload.get("change_tol", 0.01))
    y_label = _METRICS[metric][0]
    metric_key = metric

    series: dict[str, list[tuple[int, float]]] = {}
    for case in payload["cases"]:
        if case.get("error"):
            continue
        hist = case.get("history") or []
        if not hist:
            continue
        pts = [(int(row["it"]), float(row[metric_key])) for row in hist]
        series[case["case_id"]] = pts

    if not series:
        raise ValueError("描画できる曲線が無い")

    all_y = [p[1] for pts in series.values() for p in pts]
    x_min, x_max = 1, max(p[0] for pts in series.values() for p in pts)
    y_min = 0.0
    y_max = max(all_y) * 1.08

    def x_map(x: float) -> float:
        return ml + (x - x_min) / max(x_max - x_min, 1) * plot_w

    def y_map(y: float) -> float:
        return mt + plot_h - (y - y_min) / max(y_max - y_min, 1e-30) * plot_h

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="16">'
        f'hanger SIMP 収束 — 反復数 vs {y_label} (spacing={spacing}mm)</text>',
    ]

    for yv in _nice_ticks(y_min, y_max):
        yp = y_map(yv)
        lines.append(
            f'<line x1="{ml}" y1="{yp:.1f}" x2="{width-mr}" y2="{yp:.1f}" stroke="#ddd"/>'
        )
        label = f"{yv:.3g}"
        lines.append(
            f'<text x="{ml-6}" y="{yp+4:.1f}" text-anchor="end" font-family="sans-serif" '
            f'font-size="11" fill="#444">{label}</text>'
        )

    if metric == "change":
        ty = y_map(change_tol)
        lines.append(
            f'<line x1="{ml}" y1="{ty:.1f}" x2="{width-mr}" y2="{ty:.1f}" '
            f'stroke="#888" stroke-width="1.5" stroke-dasharray="6,4"/>'
        )
        lines.append(
            f'<text x="{width-mr-4}" y="{ty-6:.1f}" text-anchor="end" font-family="sans-serif" '
            f'font-size="11" fill="#666">収束閾値 {change_tol}</text>'
        )

    xticks = [1] + [t for t in range(10, x_max + 1, 10)] + ([x_max] if x_max % 10 else [])
    xticks = sorted(set(xticks))
    for xv in xticks:
        xp = x_map(xv)
        lines.append(f'<line x1="{xp:.1f}" y1="{mt}" x2="{xp:.1f}" y2="{mt+plot_h}" stroke="#eee"/>')
        lines.append(
            f'<text x="{xp:.1f}" y="{height-mb+18}" text-anchor="middle" font-family="sans-serif" '
            f'font-size="11">{xv}</text>'
        )

    legend_y = mt + 10
    for idx, (case_id, pts) in enumerate(series.items()):
        color = _COLORS.get(case_id, "#333")
        case = next(c for c in payload["cases"] if c["case_id"] == case_id)
        ratio = float(case["material_ratio"])
        vol = float(case["material_mm3"])
        d = f"M{x_map(pts[0][0]):.1f},{y_map(pts[0][1]):.1f}"
        d += "".join(f" L{x_map(x):.1f},{y_map(y):.1f}" for x, y in pts[1:])
        lines.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        ly = legend_y + idx * 18
        lines.append(
            f'<line x1="{width-mr+8}" y1="{ly}" x2="{width-mr+32}" y2="{ly}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
        lines.append(
            f'<text x="{width-mr+38}" y="{ly+4}" font-family="sans-serif" font-size="12">'
            f'{_svg_escape(f"ケース {case_id} ({ratio:.1%}, {vol/1e6:.2f}e6 mm³)")}</text>'
        )

    lines.append(
        f'<text x="{width/2}" y="{height-20}" text-anchor="middle" font-family="sans-serif" '
        f'font-size="13">反復数</text>'
    )
    lines.append(
        f'<text x="22" y="{mt+plot_h/2}" text-anchor="middle" font-family="sans-serif" '
        f'font-size="13" transform="rotate(-90 22 {mt+plot_h/2})">{y_label}</text>'
    )
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--metric",
        choices=sorted(_METRICS),
        default="compliance",
        help="縦軸 (default: compliance)",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    if not JSON_IN.is_file():
        print(f"入力が無い: {JSON_IN}", file=sys.stderr)
        return 1
    out = args.output or (
        OUTPUT_DIR / f"simp_{args.metric}_convergence_2.4mm.svg"
    )
    payload = json.loads(JSON_IN.read_text(encoding="utf-8"))
    write_svg(payload, out, args.metric)
    print(f"SVG: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
