import { shownMinWidthMm } from './minWidth'
import type { NeckDiagnosis, OptimizeResultPayload } from './session'

/** パネルに残す通知1件。型を `.ts` に置くのは、`.svelte` の名前付きの型が tsc から読めないため(`tsconfig.json` の注記)。 */
export interface OptimizeNotice {
  /** `bad` は「結果が信用できない/走らせられない」、`info` は済んだことの報告。 */
  kind: 'bad' | 'info'
  lines: string[]
}

/** 結果の通知。`warning` はトーストにも出る(`onWarning` 経由)が、読み返す役はここ。 */
export function optimizeNoticeFor(result: OptimizeResultPayload): OptimizeNotice {
  if (result.state !== 'done') {
    // `warning` は `state` ごとにサーバが作った具体的な一言(`optimize_run._search_failure_warning` 等)なので、
    // 言い直しを重ねず「文書は変わっていない」だけ添える。
    return {
      kind: 'bad',
      lines: [
        ...finishedStepLines(result),
        result.warning ?? '結果を作れなかった',
        '文書は1ビットも変わっていない(結果のボディは足していない)',
      ],
    }
  }
  const short = (value: number): string => String(Number(value.toPrecision(3)))
  const searchSafety = result.safety_factor === null ? '—' : result.safety_factor.toFixed(2)
  const searched =
    `探索時 ${searchSafety}: 平滑化前の形・探索の格子での値。探索時の目標 ${short(result.search_target_safety_factor)} = ` +
    `目標 ${short(result.target_safety_factor)} × 係数 ${short(result.search_target_factor)}`
  // 主に出すのは完成形を解き直した値(`topopt.final_check`)。
  const finalSafety =
    result.final_safety_factor == null
      ? '測れなかった'
      : `${result.final_safety_factor.toFixed(2)}(完成形を細かい格子で解き直した、表面の面積 4mm² の上位値での値)`
  const material = result.material_mm3 === null ? '—' : Math.round(result.material_mm3).toLocaleString()
  // 保証の対象は要求幅。格子上の立方規則(m セル)は探索の規則で、部材の太さの保証ではない(docs/TOPOPT_MIN_WIDTH.md)。
  // 格子上の値は切り捨てて出す(`minWidth.ts`)。
  const minWidth =
    `最小幅 要求 ${short(result.min_width.requested_mm)}mm(` +
    (result.min_width.source === 'default' ? '既定: 選べるいちばん細い段' : '指定') +
    `)・探索の規則は ${result.min_width.cells}セル = ${shownMinWidthMm(result.min_width.guaranteed_mm)}mm の立方`
  return {
    kind: result.warning ? 'bad' : 'info',
    lines: [
      `結果を「${result.name}」として足した(三角形 ${result.triangle_count} 枚 ・ ` +
        `安全率 ${finalSafety} ・ 体積 ${material}mm^3 ・ 評価 ${result.evaluations}回 ・ ` +
        `終了理由 ${result.termination ?? '—'} ・ ${Math.round(result.seconds)}秒)`,
      ...finishedStepLines(result),
      `安全率の${searched}`,
      minWidth,
      ...neckLines(result.final_check?.neck ?? null),
      '元のボディは1つも消えていない。結果は表示とSTL書き出しだけができる(編集はできない)',
      ...(result.warning ? [result.warning] : []),
    ],
  }
}

/** 利用者が選んだステップを仕上げた結果なら、その番号の行(画面の番号は 1 から。`finished_step` は 0 から)。 */
function finishedStepLines(result: OptimizeResultPayload): string[] {
  if (result.finished_step === undefined || result.steps === undefined) return []
  if (result.termination === 'stopped_by_user')
    return [`探索を止めて、ステップ ${result.finished_step + 1} / ${result.steps} を仕上げた`]
  if (result.termination === 'finished_after_run')
    return [`実行の後に選んだステップ ${result.finished_step + 1} / ${result.steps} を、探索をやり直さずに仕上げた`]
  return []
}

/**
 * つなぎ目の診断の行(docs/TOPOPT_MIN_WIDTH.md 3章)。「適合」「検証済み」は出さない —— 標本化で見えない細部があるので、
 * 言えるのは「疑いあり / 未確定 / 疑いは観測されなかった」まで。端は常に未検証。
 */
export function neckLines(neck: NeckDiagnosis | null): string[] {
  if (neck === null) return []
  const mm = (value: number): string => String(Number(Math.max(value, 0).toPrecision(3)))
  const suspects = neck.events.filter((e) => e.verdict === 'suspect')
  let joint: string
  if (neck.status === 'suspect') {
    const worst = suspects.reduce((a, b) => (a.diameter_mm[1] <= b.diameter_mm[1] ? a : b))
    joint =
      `つなぎ目: 幅不足の疑い ${suspects.length} か所(最も細い所の直径 ${mm(worst.diameter_mm[0])}〜${mm(worst.diameter_mm[1])}mm、` +
      `要求 ${mm(neck.required_mm)}mm)`
  } else if (neck.status === 'undetermined') {
    joint =
      `つなぎ目: 未確定(${neck.reason ?? `要求幅の近くで判別できない変化 ${neck.events.length} 件・微小片 ${neck.micro_pieces} 件`}。` +
      `誤差 ±${mm(neck.epsilon_mm)}mm)`
  } else {
    joint = `つなぎ目: 幅不足の疑いは観測されなかった(標本間隔 ${mm(neck.sampling_mm)}mm・誤差 ${mm(neck.epsilon_mm)}mm より細かい首は見えない)`
  }
  const end =
    neck.end_probe_mm === null
      ? '端は未検証(参考値も取得できなかった)'
      : `端は未検証(参考: 球が入っていた所での完成形の最小の直径 ${mm(neck.end_probe_mm)}mm。先端部は含まない)`
  return [joint, end]
}
