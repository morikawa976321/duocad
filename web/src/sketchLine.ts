import * as THREE from 'three'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'

import { DEPTH_BIAS_UNIT, SKETCH_LINE_WIDTH_PX } from './viewportConstants'

export type SketchFatLine = Line2

export function createSketchLineMaterial(
  color: number,
  resolution: THREE.Vector2,
  linewidth = SKETCH_LINE_WIDTH_PX,
): LineMaterial {
  return new LineMaterial({
    color,
    linewidth,
    resolution,
    depthTest: false,
  })
}

/**
 * 太線の材質を「立体に隠れる」描き方にする: 深度検査をし(書き込みはしない)、線の両端をクリップ空間の深度バイアスの
 * 量だけカメラ側へ寄せる(`viewportConstants.ts` の `withDepthBias` と同じ量。`LineMaterial` の頂点シェーダーは
 * `project_vertex` を通らないので、両端を視点座標へ移した直後に寄せる)。寄せないと、面に貼ったスケッチの線が
 * 面と奥行きを争って明滅する。
 */
export function withLineDepthBias(material: LineMaterial, layer: number): LineMaterial {
  const bias = (layer * DEPTH_BIAS_UNIT).toFixed(8)
  material.depthTest = true
  material.depthWrite = false
  material.onBeforeCompile = (shader) => {
    const marker = 'vec4 end = modelViewMatrix * vec4( instanceEnd, 1.0 );'
    if (!shader.vertexShader.includes(marker)) {
      throw new Error('太線の材質に深度バイアスを掛けられない(LineMaterial の頂点シェーダーの形が変わった)')
    }
    shader.vertexShader = shader.vertexShader.replace(
      marker,
      `${marker}
			float depthBiasMm = ${bias} * 2.0 / projectionMatrix[1][1];
			if ( isPerspectiveMatrix( projectionMatrix ) ) {
				start.xyz *= max( 1.0 - depthBiasMm * ( - start.z ) / length( start.xyz ), 0.0 );
				end.xyz *= max( 1.0 - depthBiasMm * ( - end.z ) / length( end.xyz ), 0.0 );
			} else {
				start.z += depthBiasMm;
				end.z += depthBiasMm;
			}`,
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
