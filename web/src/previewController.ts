import type { PreviewPayload } from './session'

export interface PreviewInput {
  verb: string
  args: Record<string, unknown>
  mode: 'server' | 'local'
  selection: string
}

type TimerKind = 'debounce' | 'refire'
interface PreviewTimer {
  token: number
  at: number
  input: PreviewInput
}

export interface PreviewState {
  key: string | null
  requestId: number
  responseId: number | null
  waitingSince: number | null
  server: PreviewInput | null
  debounce: PreviewTimer | null
  refire: PreviewTimer | null
  timerToken: number
  disposed: boolean
}

export type PreviewEvent =
  | { type: 'input'; input: PreviewInput | null; now: number }
  | { type: 'timer'; kind: TimerKind; token: number; now: number }
  | { type: 'clear'; resetKey: boolean }
  | { type: 'shapes'; now: number }
  | { type: 'disconnect' }
  | { type: 'detach' }
  | { type: 'dispose' }
  | { type: 'response'; payload: PreviewPayload }

export type PreviewEffect =
  | { type: 'send'; input: PreviewInput; id: number }
  | { type: 'local'; input: PreviewInput }
  | { type: 'render'; payload: PreviewPayload }
  | { type: 'clear' }
  | { type: 'hidden' }
  | { type: 'arm'; kind: TimerKind; timer: PreviewTimer; delay: number }
  | { type: 'stop'; kind: TimerKind }

const PREVIEW_DEBOUNCE_MS = 250
const PREVIEW_MAX_WAIT_MS = 750
const PREVIEW_REFIRE_MS = 250

export function initialPreviewState(): PreviewState {
  return {
    key: null, requestId: 0, responseId: null, waitingSince: null, server: null,
    debounce: null, refire: null, timerToken: 0, disposed: false,
  }
}

export function transitionPreview(
  previous: PreviewState,
  event: PreviewEvent,
): { state: PreviewState; effects: PreviewEffect[] } {
  if (previous.disposed) return { state: previous, effects: [] }
  const state = { ...previous }
  const effects: PreviewEffect[] = []
  function stop(kind: TimerKind): void {
    if (state[kind] !== null) effects.push({ type: 'stop', kind })
    state[kind] = null
  }
  function arm(kind: TimerKind, now: number, delay: number, input: PreviewInput): void {
    stop(kind)
    const timer = { at: now + delay, input, token: ++state.timerToken }
    state[kind] = timer
    effects.push({ type: 'arm', kind, timer, delay })
  }
  function clear(resetKey: boolean, advanceId: boolean, keepInput = false): void {
    if (!keepInput) {
      stop('debounce')
    }
    if (!advanceId) state.waitingSince = null
    stop('refire')
    if (resetKey) state.key = null
    if (advanceId) state.requestId += 1
    state.responseId = null
    state.server = null
    effects.push({ type: 'clear' })
  }
  function send(input: PreviewInput): void {
    stop('refire')
    state.server = input
    state.requestId += 1
    state.responseId = JSON.stringify([input.verb, input.args, input.selection]) === state.key
      ? state.requestId : null
    effects.push({ type: 'send', input, id: state.requestId })
  }

  switch (event.type) {
    case 'input': {
      const input = event.input
      const key = input === null ? null : JSON.stringify([input.verb, input.args, input.selection])
      if (key === state.key) break
      stop('debounce')
      state.key = key
      state.responseId = null
      if (input === null) {
        state.waitingSince = null
        clear(false, true)
      } else if (input.mode === 'local') {
        state.waitingSince = null
        effects.push({ type: 'local', input })
      } else {
        state.waitingSince ??= event.now
        const wait = Math.max(0, Math.min(PREVIEW_DEBOUNCE_MS, state.waitingSince + PREVIEW_MAX_WAIT_MS - event.now))
        arm('debounce', event.now, wait, input)
      }
      break
    }
    case 'timer': {
      const timer = state[event.kind]
      if (timer === null || timer.token !== event.token) break
      stop(event.kind)
      if (event.kind === 'debounce') state.waitingSince = null
      send(timer.input)
      break
    }
    case 'shapes':
      state.responseId = null
      effects.push({ type: 'hidden' })
      if (state.server !== null) arm('refire', event.now, PREVIEW_REFIRE_MS, state.server)
      break
    case 'clear':
      // サーバ側の失敗で消すときも、新しく打った未送信の入力は従来どおり送る。
      clear(event.resetKey, true, !event.resetKey)
      break
    case 'disconnect':
      state.responseId = null
      break
    case 'detach':
    case 'dispose':
      clear(true, false)
      state.disposed = event.type === 'dispose'
      break
    case 'response':
      if (state.responseId !== null && event.payload.preview_id === state.responseId) {
        effects.push({ type: 'render', payload: event.payload })
      }
      break
  }
  return { state, effects }
}

export interface PreviewController {
  update(input: PreviewInput | null): void
  cancel(): void
  clear(): void
  shapesChanged(): void
  disconnect(): void
  receive(payload: PreviewPayload): void
  detach(): void
  dispose(): void
}

interface PreviewDeps {
  send(verb: string, args: Record<string, unknown>, id: number): void
  local(verb: string, args: Record<string, unknown>): void
  render(payload: PreviewPayload): void
  clear(): void
  hidden(): void
  now(): number
  setTimer(callback: () => void, delay: number): ReturnType<typeof setTimeout>
  clearTimer(timer: ReturnType<typeof setTimeout>): void
}

export function createPreviewController(deps: PreviewDeps): PreviewController {
  let state = initialPreviewState()
  const timers: Partial<Record<TimerKind, ReturnType<typeof setTimeout>>> = {}
  function dispatch(event: PreviewEvent): void {
    const next = transitionPreview(state, event)
    state = next.state
    for (const effect of next.effects) {
      switch (effect.type) {
        case 'stop':
          if (timers[effect.kind] !== undefined) deps.clearTimer(timers[effect.kind]!)
          delete timers[effect.kind]
          break
        case 'arm':
          timers[effect.kind] = deps.setTimer(() => {
            dispatch({ type: 'timer', kind: effect.kind, token: effect.timer.token, now: deps.now() })
          }, effect.delay)
          break
        case 'send':
          deps.send(effect.input.verb, effect.input.args, effect.id)
          break
        case 'local':
          deps.local(effect.input.verb, effect.input.args)
          break
        case 'render':
          deps.render(effect.payload)
          break
        case 'clear':
          deps.clear()
          break
        case 'hidden':
          deps.hidden()
          break
      }
    }
  }
  return {
    update: (input) => dispatch({ type: 'input', input, now: deps.now() }),
    cancel: () => dispatch({ type: 'clear', resetKey: true }),
    clear: () => dispatch({ type: 'clear', resetKey: false }),
    shapesChanged: () => dispatch({ type: 'shapes', now: deps.now() }),
    disconnect: () => dispatch({ type: 'disconnect' }),
    receive: (payload) => dispatch({ type: 'response', payload }),
    detach: () => dispatch({ type: 'detach' }),
    dispose: () => dispatch({ type: 'dispose' }),
  }
}
