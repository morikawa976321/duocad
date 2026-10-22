"""試し刷りの一式(STL・一覧・結果を返すページ)を作る。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.testprint

出力は output/thread_sim/testprint/(git 管理外)。中身:
- 部品の STL(ボルトは頭を下、ナットは座面を下にして、そのまま並べて刷る向き)
- index.html: 組ごとの結果をクリックで選び、結果を1つのテキストにまとめてコピーするページ
- manifest.json: 各部品の寸法(結果の読み取りに使う)

識別: ボルトの頭の上面とナットの上面に、直径 1.2mm・深さ 0.6mm の凹みを番号の数だけ開ける(1〜6)。
ゲージの穴の板は、穴の小さい側の角を1か所面取りして向きを示す。
"""
from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import profiles as pf
from . import stl_export as sx

OUT = Path(__file__).resolve().parents[2] / "output" / "thread_sim" / "testprint"


def _dimples(shape, z_top: float, n: int, ring_r: float, d: float = 1.2, depth: float = 0.6):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    for i in range(n):
        a = 2 * math.pi * i / max(n, 1) + math.pi / 2
        x, y = (ring_r * math.cos(a), ring_r * math.sin(a)) if n > 1 else (ring_r, 0.0)
        cyl = sx.tp.cylinder(d / 2, z_top - depth, depth + 0.5)
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Trsf, gp_Vec
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(x, y, 0))
        shape = BRepAlgoAPI_Cut(shape, BRepBuilderAPI_Transform(cyl, tr, True).Shape()).Shape()
    return shape


@dataclass
class Piece:
    id: str
    kind: str  # bolt | nut | pin | holes
    label: str
    file: str
    params: dict = field(default_factory=dict)


def make_bolt(prof: pf.Profile, mark: int, length: float, af: float, head_h: float, sides: int = 6):
    """多角形の頭のボルト。識別の凹みは頭の上面(軸の付け根の円環、z=0)に開ける。"""
    shape = sx.bolt(prof, length, af, head_h, sides=sides)
    r_ring = 0.5 * (prof.r_max + af / 2 * 0.92)
    return _dimples(shape, 0.0, mark, ring_r=r_ring)


def _dimples_bottom(shape, z_bot: float, n: int, ring_r: float, d: float = 1.2, depth: float = 0.6):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec
    for i in range(n):
        a = 2 * math.pi * i / max(n, 1) + math.pi / 2
        x, y = ring_r * math.cos(a), ring_r * math.sin(a)
        cyl = sx.tp.cylinder(d / 2, z_bot - 0.5, depth + 0.5)
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(x, y, 0))
        shape = BRepAlgoAPI_Cut(shape, BRepBuilderAPI_Transform(cyl, tr, True).Shape()).Shape()
    return shape


def make_nut(prof: pf.Profile, mark: int, height: float, af: float, sides: int = 6):
    shape = sx.nut(prof, height, af, sides)
    r_ring = 0.5 * (max(r for _, r in sx.simplify_profile(prof)) + af / 2) * 0.98
    return _dimples(shape, height, mark, ring_r=r_ring)


def make_pin(d: float, length: float, af: float, head_h: float, mark: int):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Fuse
    c = 0.5
    pin = sx.tp.revolve([(0, -0.1), (d / 2, -0.1), (d / 2, length - c), (d / 2 - c, length), (0, length)])
    shape = BRepAlgoAPI_Fuse(sx._hex_prism(af, -head_h, head_h), pin).Shape()
    return _dimples_bottom(shape, -head_h, mark, ring_r=0.32 * af)


def make_hole_plate(diams, thickness: float, pitch: float):
    """一列に並んだ平らな穴の板。穴の入口は 0.3mm の 45° 面取り。小さい穴の側の角を1か所面取りして向きを示す。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
    n = len(diams)
    w = pitch
    L = pitch * n
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-w / 2, -w / 2, 0), L, w, thickness).Shape()
    for i, d in enumerate(diams):
        ch = 0.3
        hole = sx.tp.revolve([(0, -0.5), (d / 2 + ch + 0.5, -0.5), (d / 2, ch), (d / 2, thickness - ch),
                              (d / 2 + ch + 0.5, thickness + 0.5), (0, thickness + 0.5)])
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(i * pitch, 0, 0))
        plate = BRepAlgoAPI_Cut(plate, BRepBuilderAPI_Transform(hole, tr, True).Shape()).Shape()
    # 向きの印: 最初(小さい穴)の側の角を三角柱で切る
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in ((-w / 2 - 0.1, -w / 2 - 0.1), (-w / 2 + 2.5, -w / 2 - 0.1), (-w / 2 - 0.1, -w / 2 + 2.5)):
        poly.Add(gp_Pnt(x, y, -0.1))
    poly.Close()
    tri = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire(), True).Face(), gp_Vec(0, 0, thickness + 0.2)).Shape()
    return BRepAlgoAPI_Cut(plate, tri).Shape()


OUTCOMES_THREAD = ["入らない", "途中で止まる", "全長回るが固い", "手で軽く回る", "試していない"]
FLAGS_THREAD = ["ガタが大きい", "山を飛ぶ・抜ける", "ねじ山の印刷が崩れている"]


INTRO = (
    '<div class="intro"><p><b>刷り方</b>: <code>plate.stl</code> を ElegooSlicer で開き、プリンター「Elegoo Centauri Carbon 0.4 nozzle」・'
    'プロセス「0.20mm Standard @Elegoo CC 0.4 nozzle」・PLA のまま、設定は何も変えずに刷る(並べ替え・サポート不要)。</p>'
    '<p><b>試し方</b>: 冷めてから、工具・潤滑・削りなしで、ナットをボルトの先から手で回す。<b>初回のねじ込みで判定</b>し、固くなったら無理に進めず外す'
    '(軽くするための往復はしない)。締め付けずに、ナットがボルトの頭に当たるところまで進めば「全長」。'
    '<b>凹み1個(一番緩い)のナットから順に</b>試し、その系列で「入らない」か「途中で止まる」が出たら、残りは試さず「試していない」のままでよい。</p>'
    '<p><b>返し方</b>: 各行を1つクリック(点線の枠は当てはまるときだけ追加)。下の「コピー」を押し、出てきた1行を次のメッセージに貼る。途中で閉じても選択は残る。</p></div>'
)


def write_page(groups, path: Path, title: str, intro: str = INTRO, test_id: str = "TP1"):
    """groups: [(見出し, 説明, [(id, 表示名, 選択肢, 追加のチェック)])]。
    結果は 'id=選択肢[+チェック]' を並べた1行のテキストになる。"""
    def esc(s):
        return html.escape(str(s))

    blocks = []
    legend = {}
    for g in groups:
        head, desc, items = g[:3]
        svg = g[3] if len(g) > 3 else ""
        rows = []
        for iid, name, opts, flags in items:
            cells = "".join(
                f'<label><input type="radio" name="{esc(iid)}" value="{k}"{" checked" if o in ("試していない", "未回答") else ""}>'
                f"<span>{esc(o)}</span></label>" for k, o in enumerate(opts))
            fl = "".join(f'<label class="flag"><input type="checkbox" name="{esc(iid)}_f" value="{k}"><span>{esc(o)}</span></label>'
                         for k, o in enumerate(flags))
            rows.append(f'<div class="row"><div class="name">{esc(name)}</div><div class="opts">{cells}{fl}</div></div>')
            legend[iid] = [name, opts, flags]
        blocks.append(f'<section><h2>{esc(head)}</h2><div class="head"><p class="desc">{esc(desc)}</p>{svg}</div>{"".join(rows)}</section>')
    css = """
:root { --bg:#fafaf8; --fg:#222; --muted:#666; --line:#ddd; --chip:#eee; --on:#2b6cb0; --onfg:#fff; --flag:#b7791f; }
@media (prefers-color-scheme: dark) { :root { --bg:#1b1b1d; --fg:#e8e8e8; --muted:#aaa; --line:#333; --chip:#2a2a2e; --on:#5a9be0; --onfg:#111; --flag:#d69e2e; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0 auto; max-width:1000px; padding:16px; }
h1 { font-size:20px; } h2 { font-size:17px; margin:24px 0 4px; } .desc { color:var(--muted); margin:0 0 8px; }
.row { display:flex; flex-wrap:wrap; gap:6px 12px; align-items:center; padding:6px 0; border-top:1px solid var(--line); }
.name { min-width:220px; flex:1 1 220px; } .opts { display:flex; flex-wrap:wrap; gap:4px; flex:3 1 520px; }
label input { display:none; } label span { display:inline-block; padding:4px 10px; border-radius:14px; background:var(--chip); cursor:pointer; user-select:none; }
label input:checked + span { background:var(--on); color:var(--onfg); }
label.flag span { border:1px dashed var(--flag); } label.flag input:checked + span { background:var(--flag); color:var(--onfg); }
#out { width:100%; height:90px; font:13px/1.4 ui-monospace, monospace; background:var(--chip); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px; box-sizing:border-box; }
button { margin:8px 0; padding:8px 16px; border-radius:6px; border:0; background:var(--on); color:var(--onfg); font-size:15px; cursor:pointer; }
.sticky { position:sticky; bottom:0; background:var(--bg); padding:8px 0; border-top:2px solid var(--line); }
.intro { background:var(--chip); border-radius:8px; padding:8px 12px; } .intro p { margin:6px 0; }
.head { display:flex; flex-wrap:wrap; gap:12px; align-items:center; } .head .desc { flex:1 1 300px; }
svg.prof { width:260px; height:auto; max-width:100%; } svg.prof .b { fill:var(--on); opacity:.75; } svg.prof .n { fill:var(--flag); opacity:.6; }
svg.prof text { fill:var(--muted); font-size:10px; }
"""
    js = """
const KEY = 'thread-testprint-' + TEST_ID;
function save() {
  try {
    const st = {};
    document.querySelectorAll('input').forEach(e => { if (e.checked) st[e.name + '|' + e.value] = 1; });
    localStorage.setItem(KEY, JSON.stringify(st));
  } catch (e) {}
}
function restore() {
  try {
    const st = JSON.parse(localStorage.getItem(KEY) || 'null');
    if (!st) return;
    document.querySelectorAll('input').forEach(e => { e.checked = !!st[e.name + '|' + e.value]; });
  } catch (e) {}
}
function update() {
  const parts = [];
  for (const id of Object.keys(LEGEND)) {
    const el = document.querySelector('input[name="' + id + '"]:checked');
    let s = id + '=' + (el ? LEGEND[id][1][+el.value] : '?');
    document.querySelectorAll('input[name="' + id + '_f"]:checked').forEach(f => { s += '+' + LEGEND[id][2][+f.value]; });
    parts.push(s);
  }
  document.getElementById('out').value = 'ねじ試し刷り結果 ' + TEST_ID + ' ' + parts.join(' ');
}
document.querySelectorAll('input').forEach(e => e.addEventListener('change', () => { update(); save(); }));
document.getElementById('copy').addEventListener('click', async () => {
  update(); const t = document.getElementById('out');
  try { await navigator.clipboard.writeText(t.value); document.getElementById('msg').textContent = 'コピーしました'; }
  catch (e) {
    t.focus(); t.select();
    let ok = false; try { ok = document.execCommand('copy'); } catch (e2) {}
    document.getElementById('msg').textContent = ok ? 'コピーしました' : '文字列を選んだ状態にしました。Ctrl+C でコピーしてください';
  }
});
restore();
update();
"""
    page = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(title)}</title><style>{css}</style></head><body>"
            f"<h1>{esc(title)}</h1>"
            + intro
            + "".join(blocks)
            + '<div class="sticky"><textarea id="out" readonly></textarea><br><button id="copy">コピー</button> '
              '<span id="msg" class="desc"></span></div>'
            + f"<script>const TEST_ID = {json.dumps(test_id)}; const LEGEND = {json.dumps(legend, ensure_ascii=False)};{js}</script></body></html>")
    path.write_text(page, encoding="utf-8")


def profile_svg(bolt: pf.Profile, nut: pf.Profile, pitches: float = 2.0) -> str:
    """ボルト(青)とナット(橙)の山形の断面を、軸を縦にして2ピッチ分描く。半径は山の付近だけを拡大。"""
    P = bolt.pitch
    s = np.linspace(0, pitches * P, 400)
    rb, rn = bolt(s), nut(s)
    r0 = min(rb.min(), rn.min()) - 0.35 * bolt.depth - 0.1
    r1 = max(rb.max(), rn.max()) + 0.35 * bolt.depth + 0.1
    W, H = 260, 200
    sx_ = (W - 40) / (r1 - r0)
    sz_ = (H - 20) / (pitches * P)
    k = min(sx_, sz_)
    X = lambda r: 30 + (r - r0) * k
    Y = lambda z: H - 10 - z * k
    pb = " ".join(f"{X(r):.1f},{Y(z):.1f}" for z, r in zip(s, rb))
    pn = " ".join(f"{X(r):.1f},{Y(z):.1f}" for z, r in zip(s, rn))
    zt = pitches * P
    bolt_poly = f"{X(r0):.1f},{Y(0):.1f} {pb} {X(r0):.1f},{Y(zt):.1f}"
    nut_poly = f"{X(r1):.1f},{Y(0):.1f} {pn} {X(r1):.1f},{Y(zt):.1f}"
    return (f'<svg class="prof" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="山形の断面">'
            f'<polygon class="b" points="{bolt_poly}"/><polygon class="n" points="{nut_poly}"/>'
            f'<text x="2" y="12">軸↑</text><text x="{W - 60}" y="{H - 2}">外側→</text></svg>')


# ---------- 一式 ----------

SERIES = [
    # (記号, 表示名, 呼び径, 山形, 隙間の付け方, 頭とナットの角の数, ナットの隙間(雌雄の和。ボルトは呼び寸法のまま))
    # 隙間は、剛体の予測 c_geo + 寸法誤差(0〜0.3)の範囲を覆い、予測より小さい側も1つ入れる(astra の指摘)。
    # 半円は法線の隙間で谷が重なるまで(2(ρ+c) ≤ P)しか広げられないので、上端が足りないことがある
    ("A", "ISO M6×1", 6, lambda: pf.iso(6, 1.0), "radial", 6, [0.15, 0.3, 0.45, 0.6]),
    ("B", "45°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 45, 45), "radial", 4, [0.05, 0.15, 0.3, 0.45]),
    ("K", "30°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 30, 30), "radial", 5, [0.2, 0.4, 0.6]),
    ("C", "半円 D6 P2 ρ0.6(法線の隙間)", 6, lambda: pf.coil(6, 2.0, 0.6), "normal", 8, [0.15, 0.27, 0.4]),
    ("D", "45°台形 D3 P1.2 深さ0.48", 3, lambda: pf.trapezoid(3, 1.2, 0.48, 45, 45), "radial", 4, [0.05, 0.2, 0.35]),
    ("F", "半円 D3 P1.2 ρ0.4(法線の隙間)", 3, lambda: pf.coil(3, 1.2, 0.4), "normal", 8, [0.08, 0.14, 0.2]),
]
TEST_ID = "TP1"  # 一式の版。部品や隙間を変えたら上げる(結果の1行に入る)
GAUGES = [("G", 6.0, [6.0, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6]), ("H", 3.0, [3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6])]


def _sizes(D):
    af = max(1.6 * D, D + 4.0)
    return dict(af=af, head_h=max(0.6 * D, 3.0), length=2 * D + 2 if D <= 3 else 2 * D, nut_h=float(D))


def build(out: Path = OUT, only=None):
    """ナットは隙間の大きい順に 1,2,3…(緩い順に試すとボルトを先に傷めない)。"""
    out.mkdir(parents=True, exist_ok=True)
    pieces, groups = [], []
    for sid, name, D, mk, mode, sides, cs in SERIES:
        if only and sid not in only:
            continue
        prof = mk()
        sz = _sizes(D)
        f = f"{sid}0_bolt.stl"
        sx.write_stl(make_bolt(prof, 0, sz["length"], sz["af"], sz["head_h"], sides), out / f)
        pieces.append(Piece(f"{sid}0", "bolt", f"{name} のボルト", f, dict(D=D, mode=mode, series=sid)))
        items = []
        for i, c in enumerate(sorted(cs, reverse=True), 1):
            nut_prof = pf.nut_of(prof, c, mode)
            f = f"{sid}{i}_nut_c{c:g}.stl"
            sx.write_stl(make_nut(nut_prof, i, sz["nut_h"], sz["af"], sides), out / f)
            pieces.append(Piece(f"{sid}{i}", "nut", f"{name} のナット{i}(凹み{i}個)", f,
                                dict(D=D, mode=mode, clearance=c, series=sid)))
            items.append((f"{sid}{i}", f"ナット{i}(上面の凹み{i}個)", OUTCOMES_THREAD, FLAGS_THREAD))
        shape_ja = {4: "四角", 5: "五角", 6: "六角", 8: "八角"}[sides]
        mid = sorted(cs)[len(cs) // 2]
        groups.append((f"{sid}: {name}", f"頭とナットが{shape_ja}の組。ナットはボルトの先から手で回し、凹み1個のナットから順に試す。"
                       "1つ試したら外してから次へ。工具は使わない。(図: 青がボルト、橙がナット)", items,
                       profile_svg(prof, pf.nut_of(prof, mid, mode))))
    gitems = []
    for gid, d, holes in GAUGES:
        if only and gid not in only:
            continue
        sz = _sizes(d)
        f = f"{gid}0_pin.stl"
        sx.write_stl(make_pin(d, sz["length"], sz["af"], sz["head_h"], 0), out / f)
        pieces.append(Piece(f"{gid}0", "pin", f"ねじ無しのピン D{d:g}", f, dict(D=d)))
        f = f"{gid}_holes.stl"
        sx.write_stl(make_hole_plate(holes, sz["nut_h"], d + 4.0), out / f)
        pieces.append(Piece(f"{gid}_holes", "holes", f"穴の板 D{d:g}({holes[0]:g}〜{holes[-1]:g})", f, dict(holes=holes)))
        opts = [f"穴{k + 1}" for k in range(len(holes))] + ["どれも入らない", "未回答"]
        gitems.append((gid, f"ピン D{d:g}: すっと入る一番小さい穴", opts, []))
    if gitems:
        groups.append(("G・H: ねじ無しのピンと穴の板", "穴は角を切った端から順に1番、2番…で、端ほど小さい。"
                       "ピンが引っかからずにすっと入る一番小さい穴を選ぶ。", gitems))
    plate = make_plate(out, pieces)
    (out / "manifest.json").write_text(json.dumps(dict(test_id=TEST_ID, pieces=[p.__dict__ for p in pieces], plate=plate),
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    layout = layout_svg(out, pieces, plate["placed"])
    (out / "layout.svg").write_text(layout, encoding="utf-8")
    figure = ('<div class="intro"><p><b>配置図</b>(plate.stl を上から見た図。下が手前。部品は頭やナットの角の数で系列が、'
              'ナットは上面の凹みの数で番号が分かる)</p>' + layout + '</div>')
    write_page(groups, out / "index.html", "ねじ試し刷りの結果", intro=INTRO + figure, test_id=TEST_ID)
    if not only:
        predict(out)
    return pieces


PRED_VARIANTS = {"基準": {}, "垂れ無し": dict(sag_amp=0.0), "垂れ2倍": dict(sag_amp=2.0), "垂れ始めφ0.3": dict(sag_phi_ok=0.3),
                 "垂れ始めφ0.7": dict(sag_phi_ok=0.7), "ビード角立ち": dict(bead_round=0.0), "ビード丸み半分": dict(bead_round=0.5)}


def predict(out: Path = OUT) -> dict:
    """事前の予測(試し刷りの結果を見る前に固定する): 系列ごとの必要な隙間 c_geo と、その印刷の模擬の係数による幅、
    ナットごとの誤差なしの余り・掛かり代・遊び。ボルトは呼び寸法のまま(split=0)。"""
    from dataclasses import replace
    from . import evaluate as ev
    from .printer import DEFAULT
    res = {}
    for sid, name, D, mk, mode, _, cs in SERIES:
        prof = mk()
        des = ev.Design(name, prof, max(cs), mode=mode, L_nut=float(D), R_out=0.9 * D, split=0.0)

        def c_geo(pr):
            if mode == "normal":
                return ev.c_required(des, pr)
            return des.clearance - ev.evaluate(des, pr, with_strength=False).margin

        nuts = []
        for c in sorted(cs, reverse=True):
            e = ev.evaluate(replace(des, clearance=c), DEFAULT, with_strength=False)
            nuts.append(dict(c=c, margin=round(e.margin, 3), engagement=round(e.engagement, 3), play=round(e.play, 3)))
        res[sid] = dict(name=name, mode=mode, c_geo=round(c_geo(DEFAULT), 3), nuts=nuts,
                        c_geo_variants={k: round(c_geo(DEFAULT.with_(**kw)), 3) for k, kw in PRED_VARIANTS.items()})
    (out / "predictions.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def _read_stl(path: Path) -> np.ndarray:
    d = np.fromfile(path, dtype=np.uint8)
    rec = np.frombuffer(d[84:].tobytes(), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    return rec["v"].astype(float)  # (三角形, 3, 3)


def make_plate(out: Path, pieces, bed: float = 256.0, margin: float = 8.0, gap: float = 5.0) -> dict:
    """部品をベッドに並べた1つの STL(plate.stl)。系列ごとに1列(ボルト、ナット1,2,…の順)、ゲージは最後の列。
    各部品の底を z=0 に置く。並べ終えた範囲がベッドに収まらなければ断る。"""
    rows, order = [], []
    for p in pieces:
        key = p.id[0] if p.kind in ("bolt", "nut") else "G"
        if not rows or rows[-1][0] != key:
            rows.append((key, []))
        rows[-1][1].append(p)
    tris, y, placed = [], margin, []
    for key, ps in rows:
        x, row_h = margin, 0.0
        for p in ps:
            T = _read_stl(out / p.file)
            lo, hi = T.reshape(-1, 3).min(0), T.reshape(-1, 3).max(0)
            w, h = hi[0] - lo[0], hi[1] - lo[1]
            if x + w > bed - margin:  # 列が溢れたら折り返す
                x, y = margin, y + row_h + gap
                row_h = 0.0
            T = T - lo + np.array([x, y, 0.0])
            tris.append(T)
            placed.append(dict(id=p.id, x=float(x), y=float(y), w=float(w), h=float(h)))
            x += w + gap
            row_h = max(row_h, h)
        y += row_h + gap
    if y - gap > bed - margin:
        raise ValueError(f"ベッドに収まらない(奥行き {y:.0f}mm)")
    T = np.concatenate(tris)
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)
    rec = np.zeros(len(T), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, T
    with (out / "plate.stl").open("wb") as fh:
        fh.write(b"duocad thread_sim plate".ljust(80, b" "))
        fh.write(np.uint32(len(T)).tobytes())
        fh.write(rec.tobytes())
    return dict(depth=float(y - gap), placed=placed)


def layout_svg(out: Path, pieces, placed) -> str:
    """plate.stl を上から見た配置図(下が手前 = スライサーの上面図と同じ向き)。部品の輪郭は STL の XY の凸包、
    ボルト・ナット・ピンの中心に呼び径の円。色は CSS 変数(結果ページの明暗に合わせる)で、無ければ明るい既定色。"""
    from scipy.spatial import ConvexHull

    by_id = {p.id: p for p in pieces}
    depth = max(q["y"] + q["h"] for q in placed) + 8.0
    width = max(q["x"] + q["w"] for q in placed) + 8.0
    legend_w = 92.0
    W, H = width + legend_w, depth + 14.0
    fy = lambda y: depth - y  # 下が手前
    hue = {"A": "#d9e8fb", "B": "#dff3e4", "K": "#fbe3d9", "C": "#efe3fb", "D": "#dff3e4", "F": "#efe3fb", "G": "#eeeeee", "H": "#eeeeee"}
    fg, muted = "var(--fg, #222)", "var(--muted, #666)"
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.1f} {H:.1f}" style="width:100%;max-width:{W * 6:.0f}px;'
         f'font-family:system-ui,sans-serif" role="img" aria-label="plate.stl の配置図">',
         f'<rect x="0" y="0" width="{width:.1f}" height="{depth:.1f}" rx="2" style="fill:var(--chip, #f4f4f6)"/>']
    for q in placed:
        p = by_id[q["id"]]
        T = _read_stl(out / p.file)
        pts = T.reshape(-1, 3)
        xy = pts[:, :2] - pts.min(0)[:2] + np.array([q["x"], q["y"]])
        hull = xy[ConvexHull(xy).vertices]
        cx, cy = hull.mean(0)
        poly = " ".join(f"{a:.2f},{fy(b):.2f}" for a, b in hull)
        s.append(f'<polygon points="{poly}" style="fill:{hue.get(p.id[0], "#eee")};stroke:{fg};stroke-width:0.25"/>')
        D = p.params.get("D")
        if D and p.kind in ("bolt", "nut", "pin"):
            s.append(f'<circle cx="{cx:.2f}" cy="{fy(cy):.2f}" r="{D / 2:.2f}" style="fill:{"#fff" if p.kind == "nut" else "none"};'
                     f'stroke:{fg};stroke-width:0.2;stroke-dasharray:{"none" if p.kind == "nut" else "0.6 0.4"}"/>')
        label = p.id if p.kind != "holes" else f"{p.id[0]} 穴の板(角を切った端が穴1・小)"
        size = 2.2 if p.kind == "holes" else (2.6 if q["w"] >= 9 else 2.1)
        s.append(f'<text x="{cx:.2f}" y="{fy(cy) + size * 0.35:.2f}" text-anchor="middle" style="font-size:{size}px;font-weight:700;fill:#111;'
                 f'paint-order:stroke;stroke:#fff;stroke-width:0.6px">'
                 f'{html.escape(label)}</text>')
        if p.kind == "nut":
            s.append(f'<text x="{cx:.2f}" y="{fy(q["y"]) + 2.6:.2f}" text-anchor="middle" style="font-size:1.9px;fill:{muted}">'
                     f'隙間 {p.params["clearance"]:g}</text>')
    s.append(f'<text x="{width / 2:.1f}" y="{depth + 5:.1f}" text-anchor="middle" style="font-size:3px;fill:{fg}">'
             '▼ 手前(扉の側)。スライサーで上から見た図と同じ向き</text>')
    lx, ly = width + 4, 6.0
    s.append(f'<text x="{lx:.1f}" y="{ly:.1f}" style="font-size:3px;font-weight:700;fill:{fg}">列(手前から)</text>')
    shape_ja = {4: "四角", 5: "五角", 6: "六角", 8: "八角"}
    rows = [(sid, f"{sid}: {name}(頭とナットが{shape_ja[sides]})", "ナット1→" + str(len(cs)) + " の隙間 " + " / ".join(f"{c:g}" for c in sorted(cs, reverse=True)))
            for sid, name, D, mk, mode, sides, cs in SERIES if any(q["id"].startswith(sid) for q in placed)]
    rows += [("G", "G・H: ねじ無しのピンと穴の板(D6・D3)", "穴は直径 +0〜+0.6 を 0.1 刻み")]
    for sid, a, b in rows:
        ly += 6.5
        s.append(f'<text x="{lx:.1f}" y="{ly:.1f}" style="font-size:2.4px;fill:{fg}">{html.escape(a)}</text>')
        s.append(f'<text x="{lx + 2:.1f}" y="{ly + 2.9:.1f}" style="font-size:2.1px;fill:{muted}">{html.escape(b)}</text>')
    ly += 9
    for line in ("記号の数字 0 はボルト(G0・H0 はピン)。", "ナットの番号 = 上面の凹みの数(1 が最も緩い)。",
                 "実線の白丸はナットの穴、点線はボルト・ピンの軸。"):
        s.append(f'<text x="{lx:.1f}" y="{ly:.1f}" style="font-size:2.1px;fill:{muted}">{html.escape(line)}</text>')
        ly += 3.4
    s.append("</svg>")
    return "".join(s)


if __name__ == "__main__":
    import sys
    ps = build(only=set(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(len(ps), "部品を", OUT, "へ")
