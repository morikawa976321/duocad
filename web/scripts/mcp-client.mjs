/**
 * MCP(stdio・JSON-RPC 2.0)のクライアント(MCP を叩く smoke が共有する)。
 *
 * サーバの stdout に出てよいのは JSON-RPC の行だけという契約の検査点でもある(OCCT が C++ から
 * 実fdへ吐く統計が混ざると接続が壊れる)。JSONにならない行は捨てず `onNoise` で受ける:
 * `smoke-mcp` は既定(例外で止める)、`acceptance` は記録して続ける。
 */
export class McpClient {
  /**
   * @param {import('node:child_process').ChildProcess} child stdio が pipe のサーバ
   * @param {{timeout?: number, onNoise?: (line: string) => void}} options
   */
  constructor(child, { timeout = 120000, onNoise } = {}) {
    this.child = child
    this.timeout = timeout
    this.onNoise =
      onNoise ??
      ((line) => {
        throw new Error(`MCPサーバのstdoutにJSON-RPC以外の行が出た: ${line}`)
      })
    this.nextId = 1
    this.pending = new Map()
    this.buffer = ''
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this.onData(chunk))
  }

  onData(chunk) {
    this.buffer += chunk
    let index = this.buffer.indexOf('\n')
    while (index >= 0) {
      const line = this.buffer.slice(0, index).trim()
      this.buffer = this.buffer.slice(index + 1)
      if (line) this.onLine(line)
      index = this.buffer.indexOf('\n')
    }
  }

  onLine(line) {
    let message
    try {
      message = JSON.parse(line)
    } catch {
      this.onNoise(line)
      return
    }
    const resolver = this.pending.get(message.id)
    if (!resolver) return
    this.pending.delete(message.id)
    resolver(message)
  }

  send(method, params) {
    const id = this.nextId
    this.nextId += 1
    const promise = new Promise((resolvePending, rejectPending) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        rejectPending(new Error(`MCPの応答が来ない: ${method}`))
      }, this.timeout)
      this.pending.set(id, (message) => {
        clearTimeout(timer)
        resolvePending(message)
      })
    })
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`)
    return promise
  }

  notify(method, params) {
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`)
  }

  /** 握手(`initialize` → `notifications/initialized`)。 */
  async handshake(clientName) {
    const initialized = await this.send('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: clientName, version: '0.0.1' },
    })
    if (initialized.error) {
      throw new Error(`initialize が失敗: ${JSON.stringify(initialized.error)}`)
    }
    this.notify('notifications/initialized', {})
    return initialized.result
  }

  /** ツールを呼び、返ってきたJSONテキストを object で返す(`isError` は例外)。 */
  async callTool(name, args) {
    const response = await this.send('tools/call', { name, arguments: args ?? {} })
    if (response.error) throw new Error(`${name}: ${JSON.stringify(response.error)}`)
    if (response.result.isError) throw new Error(`${name}: ${response.result.content[0].text}`)
    return JSON.parse(response.result.content[0].text)
  }
}

/**
 * MCP で矩形を押し出して箱を作る: `sketch_create` → `add_rectangle` → `set_selection`(領域1)
 * → `extrude` →(`clear_selection`)。戻り値は `{ sketchId, bodyId }`。
 *
 * `bodyId` は `extrude` の応答の `bodies[0].body_id`。fixture の寸法は呼び手が持つ
 * (寸法そのものが検査の条件になる本がある)。
 */
export async function addExtrudedRect(
  mcp,
  { corner, width, height, distance, mode = 'new', clear = true, basePlane = 'XY' },
) {
  const sketch = await mcp.callTool('sketch_create', { base_plane: basePlane })
  await mcp.callTool('add_rectangle', { sketch_id: sketch.sketch_id, corner, width, height })
  return extrudeFirstRegion(mcp, sketch.sketch_id, { distance, mode, clear })
}

/** 円を押し出す(`addExtrudedRect` の円版)。 */
export async function addExtrudedCircle(
  mcp,
  { center, radius, distance, mode = 'new', clear = true, basePlane = 'XY' },
) {
  const sketch = await mcp.callTool('sketch_create', { base_plane: basePlane })
  await mcp.callTool('add_circle', { sketch_id: sketch.sketch_id, center, diameter: 2 * radius })
  return extrudeFirstRegion(mcp, sketch.sketch_id, { distance, mode, clear })
}

async function extrudeFirstRegion(mcp, sketchId, { distance, mode, clear }) {
  await mcp.callTool('set_selection', { refs: [{ sketch: sketchId, index: 1 }] })
  const extruded = await mcp.callTool('extrude', { distance, mode })
  // スケッチを作ると誰が作っても画面がそのスケッチに入る(issue #153)。部品を作り終えたら通常モードへ戻す
  // (戻さないと、通常モードの入口 `#sketch-create` が画面に無いまま検査が始まる)。
  await mcp.callTool('leave_sketch')
  if (clear) await mcp.callTool('clear_selection')
  return { sketchId, bodyId: extruded.bodies[0].body_id }
}
