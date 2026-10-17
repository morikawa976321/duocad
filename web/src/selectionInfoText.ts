/**
 * 選択1項目の寸法(`MeasureItem`)を、選択情報の表示(issue #140)の見出しと行にする。
 * 値は `measure` が返したものを書式化するだけで、ここで幾何を計算しない(AI が読む値と同じものを出す)。
 */
import { fixed3 } from './measureText'
import type { EntityKind } from './entityKinds'
import type { MeasureItem } from './session'

export interface InfoRow {
  label: string
  value: string
}

/** 見出しに要る名前の引き当て(ボディ・スケッチの表示名と、図形のスケッチ内の番号)。 */
export interface InfoNames {
  body(bodyId: string): string
  sketch(sketchId: string): string
  entity(sketchId: string, entityId: string): string
}

const SURFACE_NAMES: Record<string, string> = {
  plane: '平面',
  cylinder_full: '円筒(全周)',
  cylinder_partial: '円筒(部分)',
  torus: 'トーラス',
  cone: '円錐',
  bspline: '自由曲面',
}

const EDGE_NAMES: Record<string, string> = { line: '直線', circle: '円・円弧' }

const ENTITY_NAMES: Record<EntityKind, string> = {
  line: '線分',
  circle: '円',
  ellipse: '楕円',
  rectangle: '長方形',
  arc: '円弧',
  bezier: 'ベジェ曲線',
}

/** 点の名前(`sketch.py` の `EntityKindOps.points` の x キー)。 */
const POINT_NAMES: Record<string, string> = {
  x0: '始点',
  x1: '終点',
  cx: '中心',
  x: '基準の角',
  cx1: '制御点1',
  cx2: '制御点2',
}

function xyz(values: number[]): string {
  return `(${values.map(fixed3).join(', ')})`
}

function uv(u: number, v: number): string {
  return `(u ${fixed3(u)}, v ${fixed3(v)})`
}

function mm(value: number, unit = 'mm'): string {
  return `${fixed3(value)} ${unit}`
}

export function selectionInfoTitle(item: MeasureItem, names: InfoNames): string {
  switch (item.kind) {
    case 'face':
      return `面 · ${names.body(item.body)} #${item.index}`
    case 'edge':
      return `辺 · ${names.body(item.body)} #${item.index}`
    case 'vertex':
      return `頂点 · ${names.body(item.body)} #${item.index}`
    case 'body':
      return `ボディ · ${item.name}`
    case 'region':
      return `領域 · ${names.sketch(item.sketch)} #${item.index}`
    case 'sketch_entity': {
      const shape = `${ENTITY_NAMES[item.entity_kind] ?? item.entity_kind} ${names.entity(item.sketch, item.entity)}`
      if (item.point === null) return `${shape} · ${names.sketch(item.sketch)}`
      return `点 · ${names.sketch(item.sketch)} / ${shape}の${POINT_NAMES[item.point] ?? item.point}`
    }
  }
}

export function selectionInfoRows(item: MeasureItem): InfoRow[] {
  switch (item.kind) {
    case 'face':
      return [
        { label: '種類', value: SURFACE_NAMES[item.surface_kind] ?? item.surface_kind },
        { label: '面積', value: mm(item.area, 'mm²') },
        // 曲面の `normal` は面の中央の1点での向きでしかない(`query.describe_faces`)ので、平面だけ出す。
        ...(item.surface_kind === 'plane' ? [{ label: '法線', value: xyz(item.normal) }] : []),
        { label: '面積重心', value: xyz(item.center) },
      ]
    case 'edge':
      return [
        { label: '種類', value: EDGE_NAMES[item.edge_kind] ?? item.edge_kind },
        { label: '長さ', value: mm(item.length) },
        ...(item.radius !== undefined ? [{ label: '半径', value: mm(item.radius) }] : []),
        ...(item.center !== undefined ? [{ label: '中心', value: xyz(item.center) }] : []),
        { label: '始点', value: xyz(item.start) },
        { label: '終点', value: xyz(item.end) },
      ]
    case 'vertex':
      return [{ label: '座標', value: xyz(item.point) }]
    case 'body':
      return [
        { label: '体積', value: mm(item.volume, 'mm³') },
        { label: '面の数', value: String(item.face_count) },
        ...(item.center !== null ? [{ label: '体積重心', value: xyz(item.center) }] : []),
      ]
    case 'region':
      return [
        { label: '面積', value: mm(item.area, 'mm²') },
        { label: '面積重心', value: uv(item.center_uv[0], item.center_uv[1]) },
      ]
    case 'sketch_entity':
      if (item.position !== undefined) return [{ label: '座標', value: uv(item.position[0], item.position[1]) }]
      return entityRows(item.entity_kind, item.params ?? {}, item.length)
  }
}

function entityRows(kind: EntityKind, p: Record<string, number>, length: number | undefined): InfoRow[] {
  switch (kind) {
    case 'line':
      return [
        ...(length !== undefined ? [{ label: '長さ', value: mm(length) }] : []),
        { label: '始点', value: uv(p.x0, p.y0) },
        { label: '終点', value: uv(p.x1, p.y1) },
      ]
    case 'circle':
      return [
        { label: '中心', value: uv(p.cx, p.cy) },
        { label: '直径', value: mm(p.diameter) },
      ]
    case 'ellipse':
      return [
        { label: '中心', value: uv(p.cx, p.cy) },
        { label: '横の直径', value: mm(p.width) },
        { label: '縦の直径', value: mm(p.height) },
        { label: '角度', value: `${fixed3(p.angle)}°` },
        ...(length !== undefined ? [{ label: '周長', value: mm(length) }] : []),
      ]
    case 'arc':
      return [
        { label: '中心', value: uv(p.cx, p.cy) },
        { label: '半径', value: mm(p.radius) },
        { label: '開始角', value: `${fixed3(p.start_deg)}°` },
        { label: '終了角', value: `${fixed3(p.end_deg)}°` },
      ]
    case 'rectangle':
      return [
        { label: '基準の角', value: uv(p.x, p.y) },
        { label: '幅', value: mm(p.width) },
        { label: '高さ', value: mm(p.height) },
        { label: '角度', value: `${fixed3(p.angle ?? 0)}°` },
      ]
    case 'bezier':
      return [
        { label: '始点', value: uv(p.x0, p.y0) },
        { label: '終点', value: uv(p.x1, p.y1) },
      ]
  }
}
