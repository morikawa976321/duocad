import * as THREE from 'three'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'

import { SKETCH_LINE_WIDTH_PX } from './viewportConstants'

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

export function buildSketchLine(
  points: readonly THREE.Vector3[],
  material: LineMaterial,
): { object: Line2; geometry: LineGeometry } | null {
  if (points.length < 2) return null
  const geometry = new LineGeometry()
  geometry.setFromPoints([...points])
  return { object: new Line2(geometry, material), geometry }
}
