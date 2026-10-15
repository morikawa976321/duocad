"""カメラの見え方から目標のテセレーション精度を決める変換(可変解像度)。

**ここには幾何が無い** —— ただの算術で、OCPをimportしない(`kernel.py` から定数2つを読むだけ)。
この変換は**2つの言語に同じ式で存在する**: スケッチの円・円弧はブラウザ側で折れ線になり、
ソリッドと辺はこちらでテセレーションされる。両方が同じ粗さで描かれるには「画面の許容誤差 →
目標偏差」の変換だけ揃えればよい。

    Python: ここ(`duocad/resolution.py`)
    TS:     `web/src/adaptiveResolution.ts`

**2つの実装の定数が一致していることは `tests/test_adaptive_resolution.py` が機械で見る。**
片方だけ変えると同じ画面でスケッチの線とソリッドの辺の滑らかさが食い違う
(例外も出ない)ので、検査を外さないこと。

考え方:

1. 曲線が多角形に見えるのは**弦と弧の乖離(sagitta)が数画素になったとき**。許容誤差は
   世界座標のmmではなく画面の画素で決め、寄れば目標偏差はズームに追随して小さくなる。
2. 1画素あたりの世界長さ(`world_per_pixel`。`viewport.ts` の `worldPerPixel()`)は
   ビューポート側が持つ。**プロトコルはこの生の値を運ぶ** —— 導出済みの偏差を運ぶと
   クライアントが任意の偏差をサーバへ押し込めてしまい、変換式の持ち主が2つになる。
3. 角度偏差(ラジアン)は線形偏差から導く。半径 r の円弧が線形偏差 e を満たす半角は
   theta = sqrt(2e/r) なので、**角度偏差を sqrt(e) に比例させる**と、どの半径から角度偏差が
   支配的になるかがズームによらず一定になる。`deviation == kernel.DEFAULT_DEVIATION` で
   `kernel.DEFAULT_ANGULAR_DEFLECTION` にちょうど戻る(`tests/test_feature_ui.py` の
   小半径円弧の下限はその定数から導かれている)。

**角度はすべてラジアン**。OCCTの `BRepMesh_IncrementalMesh` / `GCPnts_TangentialDeflection` の
角度偏差パラメータがラジアンのため。

**分かっている代償(2026-08-30 実測、PCケース27部品2835面)**: 目標精度は**文書全体で1つ**
なので、大きな文書の一部へ深く寄ると**画面に出ていない部品まで同じ細かさで測り直す**。

    視点              1画素    目標偏差   三角形     測り直しの往復
    全体を収めた状態  1.11mm   0.390mm     65,707    2.8秒
    20段ズームイン    0.095mm  0.033mm    394,975    5.6秒
    30段ズームイン    0.028mm  0.010mm  1,180,763   14.5秒

全体表示では固定0.2mmより軽いが、深いズームでは上のとおり重い。次の一手は「Bodyごとに
画面占有率で精度を変える」で、プロトコルの形が変わる(`set_view_resolution` がBodyごとの値を
運ぶか、サーバが視錐台を知る)ため入れていない。小さな文書(1部品)では往復1秒未満。
"""
from __future__ import annotations

import math

from .kernel import DEFAULT_ANGULAR_DEFLECTION, DEFAULT_DEVIATION

#: 画面上で許す弦と弧の乖離[画素]。**この1つだけが滑らかさを決める。**
#: 1画素未満なのは、アンチエイリアスが効くと1画素未満のずれも角として見えるため。
TOLERANCE_PIXELS = 0.35

#: 目標偏差[mm]の下限(絶対)。1ミクロン。これ以上細かくしてもディスプレイに出ない一方、
#: `BRepMesh_IncrementalMesh` の三角形数だけが増える。
DEVIATION_MIN_ABS = 1.0e-3
#: 目標偏差[mm]の上限(絶対)。これ以上粗いと形が別物になる。
DEVIATION_MAX_ABS = 5.0
#: 目標偏差の下限(モデルの外接対角に対する比)。大きなモデルで下限が絶対値のままだと、
#: 引いた状態から一気に寄ったときに全体を1ミクロンで測り直しかねない。
DEVIATION_MIN_RELATIVE = 1.0e-5
#: 目標偏差の上限(同・比)。小さなモデルで `DEVIATION_MAX_ABS` が効くと形が消える。
DEVIATION_MAX_RELATIVE = 2.0e-2

#: 角度偏差[ラジアン]の下限。円1周あたり約314分割。
ANGULAR_MIN = 0.02
#: 角度偏差[ラジアン]の上限。OCCT自身の既定値(`IMeshTools_Parameters().Angle`)と同じで、
#: 円1周あたり約13分割。
ANGULAR_MAX = 0.5


def clamp_deviation(deviation: float, size_hint: float = 0.0) -> float:
    """目標偏差[mm]を安全な範囲へ丸める(上下限は上記の定数)。

    `size_hint` はモデルの外接対角[mm]。0以下・非有限なら比による上下限は使わない。
    """
    if not math.isfinite(deviation) or deviation <= 0.0:
        return DEFAULT_DEVIATION
    size = float(size_hint) if math.isfinite(size_hint) and size_hint > 0.0 else 0.0
    low = max(DEVIATION_MIN_ABS, size * DEVIATION_MIN_RELATIVE)
    # 極端に小さなモデルでは比の上限が絶対の下限を下回りうる。そのときは下限を採る(範囲反転を避ける)。
    high = max(low, min(DEVIATION_MAX_ABS, size * DEVIATION_MAX_RELATIVE) if size > 0.0 else DEVIATION_MAX_ABS)
    return min(high, max(low, float(deviation)))


def angular_for_deviation(deviation: float) -> float:
    """線形偏差[mm]に見合う角度偏差[ラジアン]。

    `sqrt(e)` に比例させる(モジュールdocstringの3)。
    """
    if not math.isfinite(deviation) or deviation <= 0.0:
        return DEFAULT_ANGULAR_DEFLECTION
    scaled = DEFAULT_ANGULAR_DEFLECTION * math.sqrt(float(deviation) / DEFAULT_DEVIATION)
    return min(ANGULAR_MAX, max(ANGULAR_MIN, scaled))


#: 目標偏差を丸める段の細かさ(1オクターブあたりの段数。`quantize_deviation`)。
DEVIATION_STEPS_PER_OCTAVE = 4


def quantize_deviation(deviation: float) -> float:
    """目標偏差を `2 ** (k / DEVIATION_STEPS_PER_OCTAVE)` の段へ**細かい側に**切り捨てる(角を出さない側へ倒す)。

    画面1画素の長さはカメラを戻しても浮動小数の最後の桁で変わる(実測: 1.1022631871338904 と 1.102263187133815)ので、
    そのまま偏差にするとサーバの面の三角形のキャッシュ(`view._FaceTessellations`)の鍵が毎回外れ、カメラを動かすたびに
    27 部品の全面を測り直していた(1回 1.2〜1.35 秒。issue #166)。段は 2^(1/4) ≈ 1.19 倍刻みで、
    `resolutionTracker.ts` の作り直しの閾値(1.25 倍)より細かい。
    """
    if not math.isfinite(deviation) or deviation <= 0.0:
        return deviation
    return 2.0 ** (math.floor(math.log2(deviation) * DEVIATION_STEPS_PER_OCTAVE) / DEVIATION_STEPS_PER_OCTAVE)


def deviation_floored(world_per_pixel: float, size_hint: float = 0.0) -> bool:
    """目標偏差が画素から決まらず、下限(`clamp_deviation`)まで引き上げられているか。

    引き上げられている間は偏差が画素に対して大きく、別々の回に測った面どうしの境目の隙間(偏差の2倍程度)が
    1画素を超えうる(`view.DocumentView._tessellate_body` が細かい面の組を同じ回に測ったものだけで組む)。
    """
    if not math.isfinite(world_per_pixel) or world_per_pixel <= 0.0:
        return False
    wanted = quantize_deviation(TOLERANCE_PIXELS * float(world_per_pixel))
    return clamp_deviation(wanted, size_hint) > wanted


def view_resolution(world_per_pixel: float, size_hint: float = 0.0) -> tuple:
    """`(deviation[mm], angular_deflection[rad])` を返す。**変換式の唯一の入口**。

    `world_per_pixel` は「画面1画素ぶんの世界長さ[mm]」(注視点の面で測る)。
    非有限・0以下なら既定値の組を返す(ズーム連動を入れる前と同じ粗さ)。
    目標偏差は段に丸めてから上下限を掛ける(`quantize_deviation`)。
    """
    if not math.isfinite(world_per_pixel) or world_per_pixel <= 0.0:
        return (DEFAULT_DEVIATION, DEFAULT_ANGULAR_DEFLECTION)
    deviation = clamp_deviation(quantize_deviation(TOLERANCE_PIXELS * float(world_per_pixel)), size_hint)
    return (deviation, angular_for_deviation(deviation))


#: 画面外・背を向けた面に使う粗さ(`coarse_view_resolution`)は、モデル全体がこの画素数に
#: 収まる程度に引いた視点の粗さと定義する。**ズームに依存しない**ので、寄っても粗い側の値が
#: 動かず `DocumentView` のキャッシュと版が生き、画面外のBodyは配り直しすら起きない。
#:
#: 512は全体を収めた視点の粗さに合わせた実測値(PCケースの外接対角 約600mm・1280x860 で
#: 1画素=1.11mm・目標偏差0.39mm。600/512 = 1.17mm でほぼ一致)。細かくすると画面外の面まで
#: 測り直して重くなり、粗くすると画面の縁で形が別物になる。
COARSE_REFERENCE_PIXELS = 512.0


def coarse_view_resolution(world_per_pixel: float, size_hint: float = 0.0) -> tuple:
    """**視錐台の外・カメラに背を向けた面**に使う `(deviation[mm], angular[rad])`。

    `view_resolution` と同じ変換式を「モデル全体が `COARSE_REFERENCE_PIXELS` 画素に収まる引き」の
    1画素に対して適用する。ズームで動かないので、寄っても見えていない側の粗さは変わらない。

    細かい側より細かくはしない(`max`)。`size_hint`(外接対角[mm])が無い/非正なら
    分けられないので細かい側と同じ値を返す。
    """
    fine = view_resolution(world_per_pixel, size_hint)
    if not math.isfinite(size_hint) or size_hint <= 0.0:
        return fine
    if not math.isfinite(world_per_pixel) or world_per_pixel <= 0.0:
        return fine
    coarse_per_pixel = max(float(world_per_pixel), float(size_hint) / COARSE_REFERENCE_PIXELS)
    coarse = view_resolution(coarse_per_pixel, size_hint)
    # 丸めの上下限が絡むので、値でも細かい側を下回らせない。
    if coarse[0] < fine[0]:
        return fine
    return coarse
