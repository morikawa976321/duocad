/**
 * 形状最適化の結果(`OptimizeResultPayload`)から、パネルに残す通知の文を組み立てる(`App.svelte` から
 * 切り出した純関数)。副作用(ステータスバーの `note` と `?voxel=1` の隠しボディ)は App に残る。
 */
import { shownMinWidthMm } from './minWidth'
import type { OptimizeResultPayload } from './session'

/**
 * パネルに残す通知1件(次に走らせるまで消えない)。`OptimizePanel.svelte` と `App.svelte` がここから読む
 * (`.svelte` の名前付きの型は tsc から読めないので、型は `.ts` に置く。`tsconfig.json` の注記)。
 */
export interface OptimizeNotice {
  /** `bad` は「結果が信用できない/走らせられない」、`info` は済んだことの報告。 */
  kind: 'bad' | 'info'
  lines: string[]
}

/**
 * 結果の通知。**「走ったのに何も増えていない」を、利用者が画面から読めるようにするため**。
 * `warning` はトーストにも出る(`onWarning` 経由)が、あちらは気づかせる役で、読み返す役はここ。
 */
export function optimizeNoticeFor(result: OptimizeResultPayload): OptimizeNotice {
  if (result.state !== 'done') {
    // **`warning` は`state`ごとにサーバが作った具体的な一言**(`optimize_run._search_failure_warning`
    // / `format_condition_face_warning` / `format_region_warning`)なので、ここで
    // 汎用の言い直しを重ねない —— 「文書は変わっていない」の1点だけ添える。
    return {
      kind: 'bad',
      lines: [result.warning ?? '結果を作れなかった', '文書は1ビットも変わっていない(結果のボディは足していない)'],
    }
  }
  const safety = result.safety_factor === null ? '—' : result.safety_factor.toFixed(2)
  const short = (value: number): string => String(Number(value.toPrecision(3)))
  const searched =
    `探索の格子での値。探索時の目標 ${short(result.search_target_safety_factor)} = ` +
    `目標 ${short(result.target_safety_factor)} × 係数 ${short(result.search_target_factor)}`
  const material = result.material_mm3 === null ? '—' : Math.round(result.material_mm3).toLocaleString()
  // `result.min_width.source` で既定/明示を見分ける(`duocad/optimize_run._resolve_min_width`)。
  // **保証値は切り捨てて出す**(`minWidth.ts` の規律。四捨五入だと 4.9998mm が「5mm」になる)。
  const minWidth =
    `最小幅 ${shownMinWidthMm(result.min_width.guaranteed_mm)}mm(` +
    (result.min_width.source === 'default' ? '既定: 要素サイズの2倍・1mmの大きい方' : '指定') +
    ')'
  return {
    kind: result.warning ? 'bad' : 'info',
    lines: [
      `結果を「${result.name}」として足した(三角形 ${result.triangle_count} 枚 ・ ` +
        `安全率 ${safety}(${searched}) ・ 体積 ${material}mm^3 ・ 評価 ${result.evaluations}回 ・ ` +
        `終了理由 ${result.termination ?? '—'} ・ ${Math.round(result.seconds)}秒)`,
      minWidth,
      '元のボディは1つも消えていない。結果は表示とSTL書き出しだけができる(編集はできない)',
      ...(result.warning ? [result.warning] : []),
    ],
  }
}
