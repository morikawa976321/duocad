/**
 * **ある平面frame(原点・u軸・v軸)を受け取って、その面にグリッドを描くもの。**
 *
 * XY床はこの仕組みの一利用者にすぎない —— 呼び出し元が渡す `frame` を差し替えれば、
 * 基準平面でも任意面でも同じコードが同じように描く。
 *
 * ## 描き方(なぜ全画面三角形か)
 *
 * 1mm刻みを±500mmでジオメトリとして持つと1平面あたり約2000本になり現実的でない。
 * 全画面を覆う三角形を1枚だけ描き、**フラグメントごとに視線と平面の交点**を求めて
 * frame上の (u,v) を出す。線本数の爆発・タイルの作り直し・LODのちらつきが**原理的に**
 * 起きず、CPU側は毎フレーム行列を1つ作るだけで、ジオメトリには一切触らない。
 *
 * ## 段と不透明度
 *
 * 刻みは `gridScale.ts` の `TIERS`(1/10/100mm)で固定。段ごとの不透明度は
 * **画面上の線間隔[px]** だけで決まる1本の規則(`fadeFromSpacingPx`)で、段ごとの特例は無い。
 * シェーダーは同じ規則を**1画素ごとの局所的な線間隔**にも掛け、`min` で合成する ——
 * 視線が平面に対して寝ているところや遠いところでは、1画素の中に何本もの線が入って
 * 白く潰れる(モアレ)ので、そこは局所側が先に0へ落とす。定数(`FADE_MIN`/`FADE_FULL`)は
 * `gridScale.ts` から注入するので、**数値の正はTS側の1箇所**である。
 *
 * ## 深度(B群)
 *
 * **深度検査あり・`depthWrite: false` ＋ 視線方向の深度バイアス。** 全画面三角形なので
 * `gl_Position` は使えず、交点から自分で `gl_FragDepth` を書く —— `hit` を視線方向へ
 * `uDepthBias`(画面の高さに対する比、単位の意味は `viewportConstants.ts` の `DEPTH_BIAS_UNIT`)ぶん
 * 手前へ寄せた**深度専用のコピー**を射影し直して深度だけ取り出す。**色・UV・範囲判定に
 * 使う `hit` 自体は1mmも動かさない**ので、描画のズレがレイ検査(`basePlaneAt` の解析交差)
 * に漏れない。
 *
 * **`renderer.reversedDepthBuffer` とは併用できない** —— あれは深度の向きと範囲の規約を
 * 変えるので、ここの `gl_FragDepth` の式(`z/w * 0.5 + 0.5`)が黙って壊れる。
 */
import * as THREE from 'three'

import { FADE_FULL, FADE_MIN, TIERS, type UvBounds } from './gridScale'

/** 平面の枠。`p = origin + u * uAxis + v * vAxis`(コアが配る `SketchFrame` と同じ規則)。 */
export interface PlaneFrame {
  origin: readonly [number, number, number] | number[]
  u: readonly [number, number, number] | number[]
  v: readonly [number, number, number] | number[]
}

/**
 * 段ごとの線の半幅[px]。**3段を線の太さで見分ける**(細/中/太)。0.5px刻みの差は
 * アンチエイリアスで滑らかに出る(`lineCoverage` が距離を画素で測っている)。
 */
const TIER_HALF_WIDTH_PX = [0.5, 0.75, 1.1] as const

/** 段ごとの明るさ倍率。太さだけでなく明るさでも見分けられるようにする。 */
const TIER_BRIGHTNESS = [0.8, 1.0, 1.3] as const

/** 面内の軸線(u=0 / v=0)の半幅[px]。いちばん太い段より太くして、色と合わせて主線にする。 */
const AXIS_HALF_WIDTH_PX = 1.2

const VERTEX_SHADER = /* glsl */ `
varying vec2 vNdc;

void main() {
  // **NDCをそのまま置く**(全画面を覆う1枚の三角形)。頂点は (-1,-1) (3,-1) (-1,3)。
  vNdc = position.xy;
  gl_Position = vec4(position.xy, 0.0, 1.0);
}
`

const FRAGMENT_SHADER = /* glsl */ `
uniform mat4 uViewProjection;
uniform mat4 uInverseViewProjection;
uniform vec3 uOrigin;
uniform vec3 uAxisU;
uniform vec3 uAxisV;
uniform vec3 uNormal;
uniform vec3 uColor;
uniform vec3 uTierStep;
uniform vec3 uTierAlpha;
uniform vec3 uTierHalfWidth;
uniform vec3 uTierBrightness;
uniform vec3 uAxisColorU;
uniform vec3 uAxisColorV;
uniform vec2 uAxisOn;
uniform float uDepthBias;
uniform vec2 uBoundsCenter;
uniform float uBoundsHalf;

varying vec2 vNdc;

/** gridScale.ts の fadeFromSpacingPx と同じ規則(定数もTS側から注入している)。 */
float fadeFromSpacingPx(float spacingPx) {
  return smoothstep(FADE_MIN, FADE_FULL, spacingPx);
}

/** 周期1に正規化した座標 c の格子線の被覆率。距離は**画素**で測るのでAAが自動で付く。 */
float lineCoverage(float c, float halfWidthPx) {
  float w = max(fwidth(c), 1e-8);
  float distancePx = abs(fract(c - 0.5) - 0.5) / w;
  return 1.0 - smoothstep(halfWidthPx - 0.5, halfWidthPx + 0.5, distancePx);
}

/** 1本だけの線(座標0)の被覆率。fract を通さないので線が増えず、局所フェードは要らない。 */
float axisCoverage(float coordMm, float halfWidthPx) {
  float w = max(fwidth(coordMm), 1e-8);
  float distancePx = abs(coordMm) / w;
  return 1.0 - smoothstep(halfWidthPx - 0.5, halfWidthPx + 0.5, distancePx);
}

/** src を dst の上に重ねる(通常のアルファ合成。色は非プリマルチプライで持つ)。 */
vec4 over(vec4 dst, vec4 src) {
  float a = src.a + dst.a * (1.0 - src.a);
  if (a <= 0.0) return vec4(0.0);
  return vec4((src.rgb * src.a + dst.rgb * dst.a * (1.0 - src.a)) / a, a);
}

/**
 * 1段ぶんの層。**不透明度は「全体の値」と「その画素の局所値」の小さい方**を採る ——
 * 全体側(alpha)が段そのものの見え方を決め、局所側が寝た/遠いところの潰れを止める。
 */
vec4 tierLayer(vec2 uv, float stepMm, float alpha, float halfWidthPx, float brightness) {
  vec2 c = uv / stepMm;
  float wu = max(fwidth(c.x), 1e-8);
  float wv = max(fwidth(c.y), 1e-8);
  float alphaU = min(alpha, fadeFromSpacingPx(1.0 / wu));
  float alphaV = min(alpha, fadeFromSpacingPx(1.0 / wv));
  // u=0 / v=0 の1本は格子では描かない —— そこは軸であり、色を持つ平面が描く。
  // **無条件に抜く**(自分が軸の色を持っているかは見ない) —— 基準3平面を同時に出すと、
  // ある平面の軸が別の平面の「0の線」と完全に重なり、後から描いた灰色が軸の色を覆う。
  float notAxisU = 1.0 - axisCoverage(uv.x, halfWidthPx);
  float notAxisV = 1.0 - axisCoverage(uv.y, halfWidthPx);
  float coverage = max(
    lineCoverage(c.x, halfWidthPx) * alphaU * notAxisU,
    lineCoverage(c.y, halfWidthPx) * alphaV * notAxisV
  );
  return vec4(uColor * brightness, coverage);
}

void main() {
  // 画素の視線を、近平面上の点と「近→遠」のベクトルで表す(t は 0..1 で視錐台を渡る)。
  vec4 nearH = uInverseViewProjection * vec4(vNdc, -1.0, 1.0);
  vec4 farH = uInverseViewProjection * vec4(vNdc, 1.0, 1.0);
  if (abs(nearH.w) < 1e-12 || abs(farH.w) < 1e-12) discard;
  vec3 rayOrigin = nearH.xyz / nearH.w;
  vec3 rayDelta = farH.xyz / farH.w - rayOrigin;

  float denom = dot(uNormal, rayDelta);
  // 視線と平面がほぼ平行 → 交点が無い(真横から見たときの破綻を捨てる)。
  if (abs(denom) < 1e-7 * length(rayDelta)) discard;
  float t = dot(uNormal, uOrigin - rayOrigin) / denom;
  // カメラの裏(t<0)と、far より遠い(t>1)フラグメントは捨てる。
  if (t < 0.0 || t > 1.0) discard;

  vec3 hit = rayOrigin + t * rayDelta;
  vec3 rel = hit - uOrigin;
  vec2 uv = vec2(dot(rel, uAxisU), dot(rel, uAxisV));

  vec4 acc = vec4(0.0);
  acc = over(acc, tierLayer(uv, uTierStep.x, uTierAlpha.x, uTierHalfWidth.x, uTierBrightness.x));
  acc = over(acc, tierLayer(uv, uTierStep.y, uTierAlpha.y, uTierHalfWidth.y, uTierBrightness.y));
  acc = over(acc, tierLayer(uv, uTierStep.z, uTierAlpha.z, uTierHalfWidth.z, uTierBrightness.z));

  // 面内の軸。**格子より上に置く**(格子線と同じ場所に来るので、色が勝たないと軸に見えない)。
  if (uAxisOn.x > 0.5) {
    acc = over(acc, vec4(uAxisColorU, axisCoverage(uv.y, ${AXIS_HALF_WIDTH_PX.toFixed(2)})));
  }
  if (uAxisOn.y > 0.5) {
    acc = over(acc, vec4(uAxisColorV, axisCoverage(uv.x, ${AXIS_HALF_WIDTH_PX.toFixed(2)})));
  }

  // 描く範囲(正方形)で切る。**原点対称ではなく中心を持つ** —— 面のframeの原点は
  // 面の隅のことが多く、対称な枠だと面の片側へ大きく偏る。縁は距離を画素で測ってAAを付け、
  // きっぱり切る(ぼかしてフェードアウトさせない。本人指定「その部分までだけ描画する」)。
  if (uBoundsHalf > 0.0) {
    float edgeU = (abs(uv.x - uBoundsCenter.x) - uBoundsHalf) / max(fwidth(uv.x), 1e-8);
    float edgeV = (abs(uv.y - uBoundsCenter.y) - uBoundsHalf) / max(fwidth(uv.y), 1e-8);
    acc.a *= (1.0 - smoothstep(-0.5, 0.5, edgeU)) * (1.0 - smoothstep(-0.5, 0.5, edgeV));
  }

  if (acc.a <= 0.0) discard;

  // 深度: hit を視線方向へ「uDepthBias × hit の奥行きでの画面の高さ」だけ手前に寄せた
  // 深度専用コピーを射影する(色・UV・範囲判定に使った hit 自体はここまで一度も書き換えて
  // いない)。画面の高さはNDCの縦2単位なので、hit からNDCで縦へ 2·uDepthBias 動かした点
  // までの距離がそのまま寄せる量になる(透視・正投影を分けずに済む)。
  vec3 viewDir = normalize(rayDelta);
  vec4 hitClip = uViewProjection * vec4(hit, 1.0);
  vec3 hitNdc = hitClip.xyz / hitClip.w;
  vec4 shiftedH = uInverseViewProjection * vec4(hitNdc.x, hitNdc.y + 2.0 * uDepthBias, hitNdc.z, 1.0);
  float depthBiasMm = distance(shiftedH.xyz / shiftedH.w, hit);
  vec4 clip = uViewProjection * vec4(hit - viewDir * depthBiasMm, 1.0);
  gl_FragDepth = clamp(clip.z / clip.w * 0.5 + 0.5, 0.0, 1.0);

  gl_FragColor = acc;
  #include <colorspace_fragment>
}
`

/** frameを差し替えて使い回す、1面ぶんの無限グリッド。 */
export interface PlaneGrid {
  /** シーンへ足すオブジェクト。 */
  readonly object: THREE.Mesh
  /** 描く平面を差し替える(**u/v は単位ベクトルで直交していること**。コアが配るframeは満たす)。 */
  setFrame(frame: PlaneFrame): void
  /**
   * 面内の軸線(u=0 / v=0)の色。`null` の側は描かない —— 同じ世界軸を2枚の平面が
   * 二重に描かないため。
   *
   * **格子の側は u=0 / v=0 の1本を常に抜いてある**ので、`null` にするとその1本が
   * 空くことに注意する(基準3平面では、抜けた側を必ず別の平面が色付きで描いている)。
   * 線が要るのに色に意味が無い場面では、格子と同じ色を渡せばよい。
   */
  setAxisColors(uColor: number | null, vColor: number | null): void
  /** 格子線の色(`showBasePlanes` の「起動中」で差し替わる)。 */
  setColor(hex: number): void
  /** 段ごとの不透明度(`gridScale.ts` の `tierAlphas` が出した値)。 */
  setTierAlphas(alphas: readonly [number, number, number]): void
  /** カメラ行列(毎フレーム)。 */
  setCamera(viewProjection: THREE.Matrix4, inverseViewProjection: THREE.Matrix4): void
  /** 深度バイアス(画面の高さに対する比。層番号 × `DEPTH_BIAS_UNIT`)。 */
  setDepthBias(bias: number): void
  /**
   * 描く正方形(frame上の中心と半径[mm])。`null` で無限。
   *
   * **無限は既定ではない** —— 呼び出し元は内容を一回り大きく包む
   * 正方形(`gridScale.ts` の `boundingSquare`)を必ず渡す。`null` は「範囲が測れなかった」
   * ときの安全側の逃げ道で、装飾が消えるより広めに描いて残す方を採るためにだけ残してある。
   */
  setBounds(bounds: UvBounds | null): void
  setVisible(visible: boolean): void
  dispose(): void
}

/**
 * 全画面三角形は**一度だけ作って共有する**(3枚とも同じ3頂点を使う)。
 * `dispose` で参照が全部消えたときに捨てる。
 */
let sharedGeometry: THREE.BufferGeometry | null = null
let sharedGeometryUsers = 0

function acquireGeometry(): THREE.BufferGeometry {
  if (sharedGeometry === null) {
    sharedGeometry = new THREE.BufferGeometry()
    sharedGeometry.setAttribute(
      'position',
      new THREE.BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3),
    )
    // 全画面三角形はNDCに直置きするので、視錐台の判定にもソートにも意味が無い。
    sharedGeometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), Infinity)
  }
  sharedGeometryUsers += 1
  return sharedGeometry
}

function releaseGeometry(): void {
  sharedGeometryUsers -= 1
  if (sharedGeometryUsers <= 0 && sharedGeometry !== null) {
    sharedGeometry.dispose()
    sharedGeometry = null
    sharedGeometryUsers = 0
  }
}

export function createPlaneGrid(): PlaneGrid {
  const uniforms = {
    uViewProjection: { value: new THREE.Matrix4() },
    uInverseViewProjection: { value: new THREE.Matrix4() },
    uOrigin: { value: new THREE.Vector3(0, 0, 0) },
    uAxisU: { value: new THREE.Vector3(1, 0, 0) },
    uAxisV: { value: new THREE.Vector3(0, 1, 0) },
    uNormal: { value: new THREE.Vector3(0, 0, 1) },
    uColor: { value: new THREE.Color(0xffffff) },
    uTierStep: { value: new THREE.Vector3(TIERS[0], TIERS[1], TIERS[2]) },
    uTierAlpha: { value: new THREE.Vector3(0, 0, 0) },
    uTierHalfWidth: { value: new THREE.Vector3(...TIER_HALF_WIDTH_PX) },
    uTierBrightness: { value: new THREE.Vector3(...TIER_BRIGHTNESS) },
    uAxisColorU: { value: new THREE.Color(0xffffff) },
    uAxisColorV: { value: new THREE.Color(0xffffff) },
    uAxisOn: { value: new THREE.Vector2(0, 0) },
    uDepthBias: { value: 0 },
    uBoundsCenter: { value: new THREE.Vector2(0, 0) },
    uBoundsHalf: { value: 0 },
  }

  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: VERTEX_SHADER,
    fragmentShader: FRAGMENT_SHADER,
    // **B群の深度規約**: 検査あり・書き込み無し(`gl_FragDepth` は検査にだけ効く)。
    transparent: true,
    depthTest: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    defines: {
      FADE_MIN: FADE_MIN.toFixed(4),
      FADE_FULL: FADE_FULL.toFixed(4),
    },
  })

  const geometry = acquireGeometry()
  const mesh = new THREE.Mesh(geometry, material)
  // NDCへ直置きするので、視錐台カリングもレイ検査も通してはいけない。
  mesh.frustumCulled = false
  mesh.raycast = () => {}
  mesh.matrixAutoUpdate = false

  return {
    object: mesh,
    setFrame(frame) {
      const u = new THREE.Vector3(frame.u[0], frame.u[1], frame.u[2]).normalize()
      const v = new THREE.Vector3(frame.v[0], frame.v[1], frame.v[2]).normalize()
      uniforms.uOrigin.value.set(frame.origin[0], frame.origin[1], frame.origin[2])
      uniforms.uAxisU.value.copy(u)
      uniforms.uAxisV.value.copy(v)
      uniforms.uNormal.value.copy(u).cross(v).normalize()
    },
    setAxisColors(uColor, vColor) {
      uniforms.uAxisOn.value.set(uColor === null ? 0 : 1, vColor === null ? 0 : 1)
      if (uColor !== null) uniforms.uAxisColorU.value.setHex(uColor)
      if (vColor !== null) uniforms.uAxisColorV.value.setHex(vColor)
    },
    setColor(hex) {
      uniforms.uColor.value.setHex(hex)
    },
    setTierAlphas(alphas) {
      uniforms.uTierAlpha.value.set(alphas[0], alphas[1], alphas[2])
    },
    setCamera(viewProjection, inverseViewProjection) {
      uniforms.uViewProjection.value.copy(viewProjection)
      uniforms.uInverseViewProjection.value.copy(inverseViewProjection)
    },
    setDepthBias(bias) {
      uniforms.uDepthBias.value = bias
    },
    setBounds(bounds) {
      if (bounds === null) {
        uniforms.uBoundsHalf.value = 0
        return
      }
      uniforms.uBoundsCenter.value.set(bounds.centerU, bounds.centerV)
      uniforms.uBoundsHalf.value = Math.max(0, bounds.half)
    },
    setVisible(visible) {
      mesh.visible = visible
    },
    dispose() {
      material.dispose()
      releaseGeometry()
    },
  }
}
