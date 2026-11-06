/** 落ちたら、ファイル本文・名前の送信か、送信の断りを画面へ渡す契約が崩れている。 */
import assert from 'node:assert/strict'
import { uploadDroppedFile } from '../web/src/uploadDroppedFile.ts'

const file = new File(['STEP bytes'], '日本語の部品.STP')
const pageUrl = 'http://127.0.0.1:19876/'
const calls = []
const maxBytes = 20 * 1024 * 1024
const config = () => Response.json({ upload_max_bytes: maxBytes })
const fetch = async (url, options) => {
  calls.push([url, options])
  return options.method === 'POST' ? Response.json({ path: 'C:/temp/日本語の部品.STP' }) : config()
}
assert.equal(await uploadDroppedFile(file, { fetch, pageUrl }), 'C:/temp/日本語の部品.STP')
assert.equal(calls.length, 2)
assert.equal(calls[0][0].href, `${pageUrl}duocad-config.json`)
const [url, options] = calls[1]
assert.equal(url.pathname, '/api/upload')
assert.equal(url.searchParams.get('filename'), file.name)
assert.equal(options.body, file)
assert.equal(options.headers['Content-Type'], 'application/octet-stream')
assert.equal(options.credentials, 'same-origin')

let posted = false
const withPost = (answer) => ({
  pageUrl,
  fetch: async (_url, options) => {
    if (options.method !== 'POST') return config()
    posted = true
    return answer()
  },
})
await assert.rejects(uploadDroppedFile(new File(['x'], 'part.exe'), {
  pageUrl, fetch: () => { throw new Error('must not request') },
}), /STEP.*SVG/)
const large = new File([new Uint8Array(maxBytes + 1)], 'large.step')
await assert.rejects(uploadDroppedFile(large, withPost(() => assert.fail('must not post'))), /大きすぎ.*20 MiB/)
assert.equal(posted, false)
await assert.rejects(uploadDroppedFile(file, withPost(() => {
  throw new Error('network error')
})), /サーバへの接続/)
await assert.rejects(uploadDroppedFile(file, {
  pageUrl, fetch: async () => { throw new Error('offline') },
}), /サーバへの接続/)
for (const status of [400, 403, 408, 413, 415, 500, 503]) {
  await assert.rejects(uploadDroppedFile(file, withPost(() => Response.json({ message: '送信を断りました' }, { status }))),
    /^Error: 送信を断りました$/)
}
await assert.rejects(uploadDroppedFile(file, withPost(() => new Response('HTML', { status: 500 }))), /HTTP 500/)
await assert.rejects(uploadDroppedFile(file, withPost(() => Response.json({}))), /パスがありません/)
await assert.rejects(uploadDroppedFile(file, withPost(() => Response.json(null))), /パスがありません/)
for (const reply of [new Response('<html>Vite</html>'), Response.json({ ws_url: 'ws://localhost:19877' })]) {
  let requests = 0
  await assert.rejects(uploadDroppedFile(file, {
    pageUrl: 'http://localhost:5173/',
    fetch: async () => { requests++; return reply },
  }), /Vite.*Python サーバ/)
  assert.equal(requests, 1)
}

globalThis.location = { href: pageUrl }
globalThis.fetch = fetch
assert.equal(await uploadDroppedFile(file), 'C:/temp/日本語の部品.STP')
console.log('ok')
