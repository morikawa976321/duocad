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
 * 量だけカメラ側へ寄せる(`viewportConstants.ts` の `DEPTH_BIAS_UNIT` と同じ量。`LineMaterial` の頂点シェーダーは
 * `project_vertex` を通らないので、両端をクリップ空間へ射影する直前に寄せる)。寄せないと、面に貼ったスケッチの線が
 * 面と奥行きを争って明滅する。
 * 断面のクリップは寄せる前の端点で判定する: `LineMaterial` は描く端点をそのままクリップの位置(`mvPosition`)に使うので、
 * 寄せた位置で判定すると切り口の近くの辺が消える・はみ出す。寄せるのは透視でカメラ面をまたぐ線分を `LineMaterial` が
 * 近接面まで詰めた後にする: 先に寄せると、寄せる量が端点ごとに違うため、詰めた端点から寄せる前の線分の上へ正確に戻せない。
 */
export function withLineDepthBias(material: LineMaterial, layer: number): LineMaterial {
  const bias = (layer * DEPTH_BIAS_UNIT).toFixed(8)
  material.depthTest = true
  material.depthWrite = false
  material.onBeforeCompile = (shader) => {
    const marker = 'vec4 clipStart = projectionMatrix * start;'
    const clipMarker = 'vec4 mvPosition = ( position.y < 0.5 ) ? start : end;'
    if (!shader.vertexShader.includes(marker) || !shader.vertexShader.includes(clipMarker)) {
      throw new Error('太線の材質に深度バイアスを掛けられない(LineMaterial の頂点シェーダーの形が変わった)')
    }
    shader.vertexShader = shader.vertexShader.replace(
      clipMarker,
      'vec4 mvPosition = ( position.y < 0.5 ) ? trimmedStart : trimmedEnd;',
    )
    shader.vertexShader = shader.vertexShader.replace(
      marker,
      `vec4 trimmedStart = start;
			vec4 trimmedEnd = end;
			float depthBiasMm = ${bias} * 2.0 / projectionMatrix[1][1];
			if ( isPerspectiveMatrix( projectionMatrix ) ) {
				start.xyz *= max( 1.0 - depthBiasMm * ( - start.z ) / length( start.xyz ), 0.0 );
				end.xyz *= max( 1.0 - depthBiasMm * ( - end.z ) / length( end.xyz ), 0.0 );
			} else {
				start.z += depthBiasMm;
				end.z += depthBiasMm;
			}
			${marker}`,
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
