import * as THREE from 'three'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'

import { DEPTH_BIAS_UNIT, SKETCH_LINE_WIDTH_PX } from './viewportConstants'

export type SketchFatLine = Line2

export function createSketchLineMaterial(color: number, resolution: THREE.Vector2): LineMaterial {
  return new LineMaterial({
    color,
    linewidth: SKETCH_LINE_WIDTH_PX,
    resolution,
    depthTest: false,
  })
}

/**
 * 太線の材質を「立体に隠れる」描き方にする: 深度検査をし(書き込みはしない)、線の両端を視線方向へ深度バイアスの
 * 量だけカメラ側へ寄せた深度で描く(`viewportConstants.ts` の `DEPTH_BIAS_UNIT` と同じ量)。寄せないと、面に貼った
 * スケッチの線が面と奥行きを争って明滅する。
 * 寄せるのは深度だけ: 寄せた両端を別に射影し、その深度(NDC の z)を描く頂点へ入れる。xy・w・断面のクリップの位置は
 * `LineMaterial` のまま。頂点ごと寄せると、`LineMaterial` が描く端点をそのままクリップの位置に使い、クリップの値の
 * 透視補間も描く頂点の w で行うので、切り口の近くの辺が消える・はみ出す。NDC の z は画面上で線形に補間されるので、
 * 両端に寄せた深度を入れれば寄せた線分の深度と一致する。寄せる端点は、透視でカメラ面をまたぐ線分を `LineMaterial` が
 * 近接面まで詰めた後のもの。
 */
export function withLineDepthBias(material: LineMaterial, layer: number): LineMaterial {
  const bias = (layer * DEPTH_BIAS_UNIT).toFixed(8)
  material.depthTest = true
  material.depthWrite = false
  material.onBeforeCompile = (shader) => {
    const projectMarker = 'vec4 clipStart = projectionMatrix * start;'
    const positionMarker = 'gl_Position = clip;'
    if (!shader.vertexShader.includes(projectMarker) || !shader.vertexShader.includes(positionMarker)) {
      throw new Error('太線の材質に深度バイアスを掛けられない(LineMaterial の頂点シェーダーの形が変わった)')
    }
    shader.vertexShader = shader.vertexShader.replace(
      projectMarker,
      `${projectMarker}
			float depthBiasMm = ${bias} * 2.0 / projectionMatrix[1][1];
			vec4 biasedStart = start;
			vec4 biasedEnd = end;
			if ( isPerspectiveMatrix( projectionMatrix ) ) {
				biasedStart.xyz *= max( 1.0 - depthBiasMm * ( - start.z ) / length( start.xyz ), 0.0 );
				biasedEnd.xyz *= max( 1.0 - depthBiasMm * ( - end.z ) / length( end.xyz ), 0.0 );
			} else {
				biasedStart.z += depthBiasMm;
				biasedEnd.z += depthBiasMm;
			}
			vec4 biasedClipStart = projectionMatrix * biasedStart;
			vec4 biasedClipEnd = projectionMatrix * biasedEnd;`,
    )
    shader.vertexShader = shader.vertexShader.replace(
      positionMarker,
      `${positionMarker}
			vec4 biasedClip = ( position.y < 0.5 ) ? biasedClipStart : biasedClipEnd;
			gl_Position.z = biasedClip.z / biasedClip.w * gl_Position.w;`,
    )
  }
  material.customProgramCacheKey = () => `duocad-line-depth-bias-${bias}`
  return material
}

export function buildSketchLine(
  points: readonly THREE.Vector3[],
  material: LineMaterial,
): { object: Line2; geometry: LineGeometry } | null {
  if (points.length < 2) return null
  const geometry = new LineGeometry()
  geometry.setFromPoints([...points])
  return { object: new Line2(geometry, material), geometry }
}
