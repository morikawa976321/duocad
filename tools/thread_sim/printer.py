"""プリンターの既定値(スライサーの初期設定)と、印刷の誤差・垂れのモデルの係数。

既定値は ElegooSlicer の system プロファイル(Centauri Carbon 0.4 ノズル・「0.20mm Standard」・Elegoo PLA)の
継承を解いた値。プロファイルが読めない機械でも同じ値で動くよう、読んだ値と同じものを下に固定で持つ
(`check_against_installed()` が食い違いを報告する)。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

PROCESS = "0.20mm Standard @Elegoo CC 0.4 nozzle"
MACHINE = "Elegoo Centauri Carbon 0.4 nozzle"
FILAMENT = "Elegoo PLA @ECC"


@dataclass(frozen=True)
class Printer:
    # --- スライサーの初期設定(Centauri Carbon・0.20mm Standard・Elegoo PLA) ---
    layer: float = 0.20
    first_layer: float = 0.20
    nozzle: float = 0.40
    outer_wall: float = 0.42  # outer_wall_line_width
    inner_wall: float = 0.45  # inner_wall_line_width
    walls: int = 2
    seam: str = "aligned"
    min_layer_time: float = 4.0  # slow_down_layer_time(PLA)
    # --- 誤差のモデル(既定は誤差なし。感度解析と較正で振る) ---
    contour_bias: float = 0.0  # 雄の外形の半径の系統誤差(+で太る)
    hole_bias: float = 0.0  # 雌の穴の半径の系統誤差(-で痩せる)
    layer_noise: float = 0.0  # 層ごとの半径のばらつき(標準偏差)
    seam_bump: float = 0.0  # 継ぎ目の半径方向の膨らみ(雄は外へ、雌は穴の内へ)
    seam_width: float = 0.6  # 継ぎ目の膨らみの周方向の幅(弧長)
    # --- 垂れのモデル: 支持率 φ < sag_phi_ok で δ = layer·sag_amp·((φok-φ)/φok)^sag_exp だけ下へ垂れる ---
    sag_phi_ok: float = 0.5
    sag_amp: float = 1.0
    sag_exp: float = 1.5
    # --- ビードの側面の丸み: 1 で半径 h/2 の半円、0 で角の立った矩形(層の段がそのまま出る) ---
    bead_round: float = 1.0

    def with_(self, **kw) -> "Printer":
        return replace(self, **kw)


DEFAULT = Printer()

# 試し刷り TP1(2026-10-22、本人の Centauri Carbon・初期設定・層 0.2)の嵌合の 20 件すべてと一致する係数(calibrate.py --fit)。
# 垂れは既定の 0〜0.25 倍で一致し、既定(1 倍)では 2 件外れる。ビードの側面は半円(既定)が最もよく合う
TP1_FIT = dict(sag_amp=0.0)
# TP1 の 20 件と一致し、かつ D6 のゲージの寸法誤差(0.05〜0.1)とも矛盾しない係数の組(頑健さを見る場面)
TP1_SCENARIOS = [("基準", {}), ("垂れ0.25", dict(sag_amp=0.25)), ("垂れ0.25 φ0.3", dict(sag_amp=0.25, sag_phi_ok=0.3)),
                 ("垂れ0.5 φ0.3", dict(sag_amp=0.5, sag_phi_ok=0.3))]


def _system_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / "ElegooSlicer" / "system"
    return p if p.is_dir() else None


def resolve(kind: str, name: str, vendor: str = "Elegoo") -> dict:
    """system プロファイルの継承(inherits)を解いた辞書。

    継承元の名前(fdm_process_common など)はベンダーごとに別の中身で重複している(Custom の
    fdm_process_common は壁3周・外壁100%、Elegoo のは壁2周・外壁0.42。実測)。だから同じベンダーの
    フォルダを先に引き、無いものだけ他(OrcaFilamentLibrary など)から引く。"""
    root = _system_dir()
    if root is None:
        raise FileNotFoundError("ElegooSlicer の system プロファイルが無い")
    index = {}
    files = sorted(root.rglob("*.json"), key=lambda f: (vendor not in f.relative_to(root).parts[:1], str(f)))
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and "name" in d and "type" in d:
            index.setdefault((d["type"], d["name"]), d)
    chain, d = [], index[(kind, name)]
    chain.append(d)
    while d.get("inherits"):
        d = index[(kind, d["inherits"])]
        chain.append(d)
    out = {}
    for c in reversed(chain):
        out.update(c)
    return out


def _width(v, nozzle):
    v = v[0] if isinstance(v, list) else v
    return float(v[:-1]) / 100 * nozzle if str(v).endswith("%") else float(v)


def check_against_installed() -> list[str]:
    """固定値と、この機械の ElegooSlicer のプロファイルの値の食い違い。"""
    proc, mach, fil = resolve("process", PROCESS), resolve("machine", MACHINE), resolve("filament", FILAMENT)
    nozzle = float(mach["nozzle_diameter"][0])
    got = dict(
        layer=float(proc["layer_height"]),
        first_layer=float(proc["initial_layer_print_height"]),
        nozzle=nozzle,
        outer_wall=_width(proc["outer_wall_line_width"], nozzle),
        inner_wall=_width(proc["inner_wall_line_width"], nozzle),
        walls=int(proc["wall_loops"]),
        seam=proc["seam_position"],
        min_layer_time=float(fil["slow_down_layer_time"][0]),
    )
    return [f"{k}: 固定 {getattr(DEFAULT, k)!r} / 機械 {v!r}" for k, v in got.items() if getattr(DEFAULT, k) != v]


def defaults_listing() -> dict:
    """印刷に効く既定値の一覧(文書に貼る用)。"""
    proc, fil = resolve("process", PROCESS), resolve("filament", FILAMENT)
    keys_p = ["layer_height", "initial_layer_print_height", "line_width", "outer_wall_line_width", "inner_wall_line_width",
              "wall_loops", "wall_generator", "wall_sequence", "seam_position", "detect_overhang_wall", "detect_thin_wall",
              "xy_hole_compensation", "xy_contour_compensation", "elefant_foot_compensation", "sparse_infill_density",
              "sparse_infill_pattern", "top_shell_layers", "bottom_shell_layers", "outer_wall_speed", "inner_wall_speed",
              "small_perimeter_speed", "small_perimeter_threshold", "overhang_1_4_speed", "overhang_2_4_speed",
              "overhang_3_4_speed", "overhang_4_4_speed", "enable_overhang_speed", "precise_outer_wall", "resolution",
              "enable_arc_fitting"]
    keys_f = ["nozzle_temperature", "fan_min_speed", "fan_max_speed", "slow_down_layer_time", "slow_down_min_speed",
              "overhang_fan_speed", "overhang_fan_threshold", "filament_max_volumetric_speed", "close_fan_the_first_x_layers"]
    return {**{k: proc.get(k) for k in keys_p}, **{k: fil.get(k) for k in keys_f}}
