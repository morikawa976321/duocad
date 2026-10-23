import { choiceSends, syncedChoice, type ArgSpec, type ArgValues, type BuildResult, type CommandDef, type Point2 } from './commands/command'
import { COMMAND_PARAMS } from './generated/protocol'
import type { SelectionRef } from './session'

const PUBLISH_DEBOUNCE_MS = 250
type Args = Record<string, unknown>

export interface SharedArgsDraft {
  numberText: Record<string, string>
  textValues: Record<string, string>
  choices: Record<string, string>
  points: Record<string, Point2>
  picks: Record<string, SelectionRef>
  planes: Record<string, string>
}

export interface SharedArgsState {
  remote: Args
  lastPublished: Args
  sentKeys: string[]
  unmapped: string[]
  everLive: string[]
  focusedInputId: string | null
  pending: { args: Args; token: number } | null
  timerToken: number
  disposed: boolean
}

interface PublishContext {
  active: boolean
  committed: boolean
  specs: readonly ArgSpec[]
}

export type SharedArgsEvent =
  | { type: 'input'; args: Args | null }
  | { type: 'visible'; names: string[] }
  | { type: 'receive'; remote: Args; specs: readonly ArgSpec[]; draft: SharedArgsDraft; focusedInputId: string | null }
  | { type: 'timer'; token: number; context: PublishContext }
  | { type: 'flush'; args: Args | null; context: PublishContext }
  | { type: 'cancel' | 'reset' | 'dispose' }

export type SharedArgsEffect =
  | { type: 'stop' }
  | { type: 'arm'; token: number; delay: number }
  | { type: 'send'; args: Args }
  | { type: 'render'; draft: SharedArgsDraft }
  | { type: 'note'; message: string }

export function initialSharedArgsState(): SharedArgsState {
  return {
    remote: {}, lastPublished: {}, sentKeys: [], unmapped: [], everLive: [],
    focusedInputId: null, pending: null, timerToken: 0, disposed: false,
  }
}

export function publishedArgs(
  command: CommandDef, liveArgs: readonly ArgSpec[], values: ArgValues, built: BuildResult | null,
): Args {
  const allowed = new Set<string>(COMMAND_PARAMS[command.id as keyof typeof COMMAND_PARAMS] ?? [])
  const out: Args = {}
  for (const spec of liveArgs) {
    if (spec.kind === 'selection' || spec.kind === 'entities' || spec.kind === 'plane') continue
    let value: unknown = values[spec.name]
    if (spec.kind === 'choice') {
      const option = spec.options.find((candidate) => candidate.value === value)
      value = option === undefined ? undefined : choiceSends(option)
    }
    if (spec.kind === 'text' && spec.toShared !== undefined && typeof value === 'string') {
      value = spec.toShared(value)
    }
    if (value === undefined || value === null || value === '' || !allowed.has(spec.name)) continue
    out[spec.name] = value
  }
  // 点から導く寸法・基準平面は欄とサーバの引数名が異なるため、揃った build の結果を優先する。
  if (built?.ok) {
    for (const [name, value] of Object.entries(built.args)) {
      if (allowed.has(name) && value !== undefined && value !== null) out[name] = value
    }
  }
  return out
}

export function transitionSharedArgs(
  before: SharedArgsState, event: SharedArgsEvent,
): { state: SharedArgsState; effects: SharedArgsEffect[] } {
  if (before.disposed) return { state: before, effects: [] }
  let state = { ...before }
  const effects: SharedArgsEffect[] = []
  function stop(): void {
    if (state.pending !== null) effects.push({ type: 'stop' })
    state.pending = null
  }
  function publish(next: Args, context: PublishContext): void {
    if (!context.active || context.committed) return
    const fields = new Set(context.specs.filter((spec) =>
      spec.kind === 'number' || spec.kind === 'text' || spec.kind === 'point' || spec.kind === 'pick' || spec.kind === 'choice',
    ).map((spec) => spec.name))
    const delta: Args = {}
    for (const [name, value] of Object.entries(next)) {
      if (JSON.stringify(value) !== JSON.stringify(state.lastPublished[name])) delta[name] = value
    }
    for (const name of Object.keys(state.lastPublished)) {
      if (name in next) continue
      const cleared = fields.has(name) && state.everLive.includes(name) && !state.unmapped.includes(name)
      if (cleared || state.sentKeys.includes(name)) delta[name] = null
    }
    if (Object.keys(delta).length === 0) return
    const sentKeys = new Set(state.sentKeys)
    const unmapped = new Set(state.unmapped)
    for (const [name, value] of Object.entries(delta)) {
      if (value !== null) sentKeys.add(name)
      unmapped.delete(name)
    }
    state.sentKeys = [...sentKeys]
    state.unmapped = [...unmapped]
    state.lastPublished = { ...state.lastPublished, ...delta }
    for (const name of Object.keys(delta)) if (delta[name] === null) delete state.lastPublished[name]
    effects.push({ type: 'send', args: delta })
  }
  switch (event.type) {
    case 'input':
      if (event.args !== null) {
        stop()
        state.pending = { args: { ...event.args }, token: ++state.timerToken }
        effects.push({ type: 'arm', token: state.pending.token, delay: PUBLISH_DEBOUNCE_MS })
      }
      break
    case 'visible':
      state.everLive = [...new Set([...state.everLive, ...event.names])]
      break
    case 'timer': {
      if (state.pending === null || state.pending.token !== event.token) break
      const args = state.pending.args
      stop()
      publish(args, event.context)
      break
    }
    case 'flush':
      stop()
      state.focusedInputId = null
      if (event.args !== null) publish(event.args, event.context)
      break
    case 'cancel':
      stop()
      break
    case 'reset':
    case 'dispose':
      stop()
      state = { ...initialSharedArgsState(), timerToken: state.timerToken, disposed: event.type === 'dispose' }
      break
    case 'receive': {
      const remote = event.remote
      state.remote = { ...remote }
      state.focusedInputId = event.focusedInputId
      if (JSON.stringify(remote) === JSON.stringify(state.lastPublished)) break
      const draft = { ...event.draft }
      const kept: string[] = []
      const unmapped: string[] = []
      for (const spec of event.specs) {
        const value = remote[spec.name]
        if (spec.kind === 'number' || spec.kind === 'text') {
          const field = spec.kind === 'number' ? 'numberText' : 'textValues'
          const text = value === undefined ? '' : String(value)
          if ((draft[field][spec.name] ?? '') === text) continue
          if (event.focusedInputId === spec.inputId) {
            kept.push(`${spec.label}=${text === '' ? '(空)' : text}`)
            continue
          }
          draft[field] = { ...draft[field], [spec.name]: text }
        } else if (spec.kind === 'choice') {
          const synced = syncedChoice(spec, remote, state.lastPublished, draft.choices[spec.name])
          if ('unmapped' in synced) {
            unmapped.push(spec.name)
          } else if (synced.value === undefined) {
            const { [spec.name]: _removed, ...rest } = draft.choices
            draft.choices = rest
          } else {
            draft.choices = { ...draft.choices, [spec.name]: synced.value }
          }
        } else if (spec.kind === 'pick') {
          if (value !== null && typeof value === 'object') {
            draft.picks = { ...draft.picks, [spec.name]: value as SelectionRef }
          } else if (value === undefined) {
            const { [spec.name]: _removed, ...rest } = draft.picks
            draft.picks = rest
          } else {
            unmapped.push(spec.name)
          }
        } else if (spec.kind === 'point') {
          if (Array.isArray(value) && value.length === 2) {
            draft.points = { ...draft.points, [spec.name]: [Number(value[0]), Number(value[1])] }
          } else if (value === undefined) {
            const { [spec.name]: _removed, ...rest } = draft.points
            draft.points = rest
          } else {
            unmapped.push(spec.name)
          }
        } else if (spec.kind === 'plane') {
          // サーバ側の名前は base_plane。面は共有選択にあり、無ければ局所の値を保つ。
          if (typeof remote.base_plane === 'string') draft.planes = { ...draft.planes, [spec.name]: remote.base_plane }
        }
      }
      state.unmapped = unmapped
      state.lastPublished = { ...remote }
      effects.push({ type: 'render', draft })
      if (kept.length > 0) effects.push({ type: 'note', message: `外から ${kept.join(' / ')} に変わったが、打っている途中なので欄は変えていない` })
      break
    }
  }
  return { state, effects }
}

export function sharedArgsChangeNote(
  before: Args, after: Args, commandLabel: string, argLabel: (name: string) => string,
): string | null {
  const changed = [...new Set([...Object.keys(before), ...Object.keys(after)])].filter(
    (name) => JSON.stringify(before[name]) !== JSON.stringify(after[name]),
  )
  if (changed.length === 0) return null
  const shown = changed.map((name) => `${argLabel(name)}=${after[name] === undefined ? '(空)' : JSON.stringify(after[name])}`).join(' / ')
  return `外から「${commandLabel}」の引数が変わった: ${shown}`
}

interface SharedArgsDeps {
  context(): PublishContext
  send(args: Args): void
  render(draft: SharedArgsDraft): void
  note(message: string): void
  setTimer(callback: () => void, delay: number): ReturnType<typeof setTimeout>
  clearTimer(timer: ReturnType<typeof setTimeout>): void
}

export function createSharedArgsController(deps: SharedArgsDeps) {
  let state = initialSharedArgsState()
  let timer: ReturnType<typeof setTimeout> | null = null
  function dispatch(event: SharedArgsEvent): void {
    const next = transitionSharedArgs(state, event)
    state = next.state
    for (const effect of next.effects) {
      switch (effect.type) {
        case 'stop':
          if (timer !== null) deps.clearTimer(timer)
          timer = null
          break
        case 'arm':
          timer = deps.setTimer(() => dispatch({ type: 'timer', token: effect.token, context: deps.context() }), effect.delay)
          break
        case 'send': deps.send(effect.args); break
        case 'render': deps.render(effect.draft); break
        case 'note': deps.note(effect.message); break
      }
    }
  }
  return {
    update: (args: Args | null) => dispatch({ type: 'input', args }),
    visible: (names: string[]) => dispatch({ type: 'visible', names }),
    receive: (remote: Args, specs: readonly ArgSpec[], draft: SharedArgsDraft, focusedInputId: string | null) =>
      dispatch({ type: 'receive', remote, specs, draft, focusedInputId }),
    flush: (args: Args | null) => dispatch({ type: 'flush', args, context: deps.context() }),
    cancel: () => dispatch({ type: 'cancel' }),
    reset: () => dispatch({ type: 'reset' }),
    dispose: () => dispatch({ type: 'dispose' }),
  }
}
