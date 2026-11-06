/**
 * AI パネルのツール行。画面のコマンドと対応する verb は、コマンドのラベルと主な引数(数値と選択肢だけ、3つまで)で
 * `押し出す(距離 10・結果 結合)` の形にする。対応する定義の無い verb は内部名と JSON のまま出す。
 * 元の `name` + JSON は `toolRawLine`(ツールチップに残す)。
 */
import { choiceSends, type ArgSpec, type ChoiceArgSpec, type CommandDef } from './commands/command'
import type { AiToolCall } from './session'

const MAX_SHOWN_ARGS = 3

/** 見出しの括弧書き(`距離(正=スケッチの表側)` の補足)は行に入れない。 */
function shortLabel(label: string): string {
  return label.replace(/[(（].*$/, '').trim()
}

function choiceText(spec: ChoiceArgSpec, value: unknown): string | null {
  const key = JSON.stringify(value)
  const option = spec.options.find((candidate) => JSON.stringify(choiceSends(candidate)) === key) ?? spec.options.find((candidate) => candidate.value === value)
  return option === undefined ? null : option.label
}

function argText(spec: ArgSpec, value: unknown): string | null {
  if (spec.kind === 'number') {
    return typeof value === 'number' || typeof value === 'string' ? `${shortLabel(spec.label)} ${value}` : null
  }
  if (spec.kind === 'choice') {
    const text = choiceText(spec, value)
    return text === null ? null : `${shortLabel(spec.label)} ${text}`
  }
  return null
}

/** 元の表示(内部名 + 引数の JSON)。 */
export function toolRawLine(call: AiToolCall): string {
  const args = Object.keys(call.arguments).length ? JSON.stringify(call.arguments) : ''
  return `${call.name}${args ? ` ${args}` : ''}`
}

export function toolLine(call: AiToolCall, commandOf: (verb: string) => CommandDef | undefined): string {
  const command = commandOf(call.name)
  if (command === undefined) return toolRawLine(call)
  const shown: string[] = []
  for (const spec of command.args) {
    if (shown.length >= MAX_SHOWN_ARGS) break
    const value = call.arguments[spec.name]
    if (value === undefined) continue
    const text = argText(spec, value)
    if (text !== null) shown.push(text)
  }
  return shown.length === 0 ? command.label : `${command.label}(${shown.join('・')})`
}
