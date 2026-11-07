type UploadOptions = {
  fetch?: typeof fetch
  pageUrl?: string
}

const pageRequired = 'ファイルの送信は duocad が配信する画面でしか使えない(Vite の開発画面では使えない。Python サーバの画面を開く)'
const connectionFailed = 'ファイルを送信できなかった(サーバへの接続を確かめて、もう一度落とす)'

/** 返った一時パスは既存の import_step / import_svg の path に渡す。失敗は表示用の Error.message。 */
export async function uploadDroppedFile(file: File, options: UploadOptions = {}): Promise<string> {
  if (!/\.(step|stp|svg)$/i.test(file.name)) {
    throw new Error('取り込めるのは STEP(.step・.stp)と SVG(.svg)だけ')
  }

  const request = options.fetch ?? fetch
  const pageUrl = options.pageUrl ?? location.href
  let configResponse: Response
  try {
    configResponse = await request(new URL('/duocad-config.json', pageUrl), {
      cache: 'no-store', credentials: 'same-origin',
    })
  } catch {
    throw new Error(connectionFailed)
  }
  let maxBytes: number
  try {
    const config = await configResponse.json()
    maxBytes = config.upload_max_bytes
    if (!configResponse.ok || !Number.isSafeInteger(maxBytes) || maxBytes <= 0) throw new Error()
  } catch {
    throw new Error(pageRequired)
  }
  if (file.size > maxBytes) {
    throw new Error(`ファイルが大きすぎる(上限 ${maxBytes / (1024 * 1024)} MiB)`)
  }

  const url = new URL('/api/upload', pageUrl)
  url.searchParams.set('filename', file.name)
  let response: Response
  try {
    response = await request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      credentials: 'same-origin',
      body: file,
    })
  } catch {
    throw new Error(connectionFailed)
  }
  let payload: { path?: unknown; message?: unknown }
  try {
    payload = await response.json()
  } catch {
    throw new Error(`ファイルの送信結果を読めなかった(HTTP ${response.status})`)
  }
  if (!response.ok) {
    throw new Error(typeof payload?.message === 'string'
      ? payload.message
      : `ファイルを送信できなかった(HTTP ${response.status})`)
  }
  if (typeof payload?.path !== 'string' || !payload.path) {
    throw new Error('ファイルの送信結果に取り込み用のパスが無い(もう一度落とす)')
  }
  return payload.path
}
