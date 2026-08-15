/**
 * MCP(stdio・JSON-RPC 2.0)のクライアント — `smoke-mcp.mjs` と `acceptance.mjs` が
 * 同じものを1つずつ持っていたので統合した(2026-08-15)。
 *
 * **stdoutに出てよいのはJSON-RPCの行だけ**という契約の検査点でもある。OCCTがC++から
 * 実fdへ吐く統計が混ざると接続が壊れるので、**JSONにならない行は捨てない**。
 * どう扱うかだけスクリプトによって違うので `onNoise` で受ける:
 *
 * - `smoke-mcp` … 既定のまま(例外にして検査を止める)
 * - `acceptance` … 記録して続ける(受入は落ちても最後まで走り、❌ を残す作り)
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

  /** 握手。`initialize` → `notifications/initialized`。戻り値は initialize の result。 */
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
