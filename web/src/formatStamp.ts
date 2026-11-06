/** 画面に出す日時の書式(`2026-11-05 14:30`)。ファイルパネルとトップ画面で同じ書式を使うための1か所。 */
export function formatStamp(epochSeconds: number | null): string {
  if (epochSeconds === null) return '—'
  const date = new Date(epochSeconds * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}
