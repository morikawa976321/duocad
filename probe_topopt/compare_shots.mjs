/**
 * 比較スクリーンショットの描画段(`compare_shots.py` の export と join に挟まれる)。
 *
 *   node probe_topopt/compare_shots.mjs
 *
 * `screenshots/smooth_compare/_data/<題材>_<左右>.json`(頂点はmm・格子中心を原点へ寄せ済み)
 * を読み、**固定カメラ・固定光源**で `<題材>_<方向>_<左右>.png` を書く。
 *
 * **カメラ・光源・材質・背景は `probe_topopt/web/index.html` の既定表示と同じ値**
 * (下の定数)。左右の画角は**題材ごとに左右両方の外接箱を合わせてから**決めるので、
 * 左右で1画素もずれない —— ずれた比較は無価値なので、そこだけは自動で揃える。
 *
 * three.js は `probe_topopt/web/vendor/three` から、puppeteer-core は duocad 本体の
 * `web/node_modules` から借りる(**新規インストールをしない**ための措置。`shot.mjs` と同じ)。
 * `--use-angle=swiftshader` は付けない(engine `headless-chrome-can-use-real-gpu`)。
 */
import { createRequire } from 'node:module'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'node:http'

const HERE = dirname(fileURLToPath(import.meta.url))
const DUOCAD = dirname(HERE)
const DATA = join(HERE, 'screenshots', 'smooth_compare', '_data')

const WIDTH = 900
const HEIGHT = 700
// index.html の既定表示から写した値。**片方だけ変えないこと**(比較の土俵が変わる)。
const FOV = 50
const BG = 0x0a0a12
const MESH_COLOR = 0x6fa8dc
// 方向はすべて単位ベクトルへ正規化してから使う。`iso` は index.html の `fitCamera()` と同じ。
const VIEWS = {
  iso: { dir: [-0.75, 0.5, 0.6], zoom: 1.0 },
  side: { dir: [-0.06, -1.0, 0.08], zoom: 1.0 },   // ほぼ真横。輪郭のシルエットが出る角度
  threeq: { dir: [0.62, 0.66, 0.42], zoom: 1.0 },  // iso の反対側からの3/4
  iso_zoom: { dir: [-0.75, 0.5, 0.6], zoom: 0.45 } // iso と同じ向きで距離だけ詰めた拡大
}
const VIEW_NAMES = { iso: 'iso', side: 'side', threeq: 'threeq', iso_zoom: 'zoom' }

const require = createRequire(join(DUOCAD, 'web', 'package.json'))
const { launch } = require('puppeteer-core')
const CHROME = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'
].filter(Boolean).find((p) => existsSync(p))
if (!CHROME) throw new Error('Chrome が見つからない(DUOCAD_BROWSER で指定する)')

const manifest = JSON.parse(readFileSync(join(DATA, 'manifest.json'), 'utf8'))

// three.js は module 形式なので file:// では import できない(CORS)。最小のHTTPサーバを立てる。
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json' }
const server = createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  if (url.pathname === '/page') {
    res.writeHead(200, { 'content-type': 'text/html' })
    res.end(PAGE)
    return
  }
  const rel = decodeURIComponent(url.pathname).replace(/^\/+/, '')
  const root = rel.startsWith('vendor/') ? join(HERE, 'web') : DATA
  const file = join(root, rel.startsWith('vendor/') ? rel : rel.replace(/^data\//, ''))
  if (!existsSync(file)) { res.writeHead(404); res.end('no'); return }
  const ext = file.slice(file.lastIndexOf('.'))
  res.writeHead(200, { 'content-type': MIME[ext] ?? 'application/octet-stream' })
  res.end(readFileSync(file))
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const PORT = server.address().port

const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#0a0a12}</style>
<script type="importmap">{"imports":{"three":"/vendor/three/three.module.js"}}</script>
</head><body><script type="module">
import * as THREE from 'three';
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(${WIDTH}, ${HEIGHT}, false);
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(${BG});
scene.add(new THREE.HemisphereLight(0xffffff, 0x333344, 0.65));
const dirLight = new THREE.DirectionalLight(0xffffff, 0.85);
dirLight.position.set(2, 3, 1);
scene.add(dirLight);
// **index.html には無い光源。** index.html は視点が1つ(iso)なので (2,3,1) の1灯で足りるが、
// ここは4方向から撮るので、背面側の視点(side)が真っ黒になる。カメラに追従する
// 弱い正面光を足して、どの向きでも輪郭の内側が読めるようにしてある(左右で同一)。
const headLight = new THREE.DirectionalLight(0xffffff, 0.45);
scene.add(headLight);
const camera = new THREE.PerspectiveCamera(${FOV}, ${WIDTH} / ${HEIGHT}, 0.1, 50000);
let current = null;
function b64(s) { const bin = atob(s); const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i); return u8.buffer; }
window.__show = async (name, side, view, dist, center) => {
  if (current) { scene.remove(current); current.geometry.dispose(); current.material.dispose(); }
  const res = await fetch('/data/' + name + '_' + side + '.json');
  const p = await res.json();
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(b64(p.verts_b64)), 3));
  g.setIndex(new THREE.BufferAttribute(new Uint32Array(b64(p.faces_b64)), 1));
  g.computeVertexNormals();
  current = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: ${MESH_COLOR} }));
  scene.add(current);
  const c = new THREE.Vector3(...center);
  camera.position.copy(new THREE.Vector3(...view.dir).normalize().multiplyScalar(dist).add(c));
  camera.near = Math.max(dist / 500, 0.01); camera.far = dist * 20;
  camera.up.set(0, 0, 1);
  camera.lookAt(c);
  headLight.position.copy(camera.position);
  camera.updateProjectionMatrix();
  renderer.render(scene, camera);
  return { triangles: renderer.info.render.triangles };
};
window.__ready = true;
</script></body></html>`

const browser = await launch({
  executablePath: CHROME,
  headless: 'new',
  args: [`--window-size=${WIDTH},${HEIGHT}`]
})
const page = await browser.newPage()
await page.setViewport({ width: WIDTH, height: HEIGHT })
page.on('pageerror', (e) => { throw e })
await page.goto(`http://127.0.0.1:${PORT}/page`, { waitUntil: 'load' })
await page.waitForFunction('window.__ready === true', { timeout: 30000 })

/**
 * 題材ごとに**左右両方の頂点が画面に収まる**カメラ距離を1つ決める(左右で画角がずれた
 * 比較は無価値なので、必ず共通の値を使う)。外接球ではなく**その視線方向での実際の
 * 投影**から決める —— 外接球で決めると細長い題材が画面の3分の1にしか映らず、
 * 問題にしている1要素幅の筋が見えない。
 */
function vertsOf (name, side) {
  const p = JSON.parse(readFileSync(join(DATA, `${name}_${side}.json`), 'utf8'))
  const buf = Buffer.from(p.verts_b64, 'base64')
  return new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength))
}

/** 左右**両方**の外接箱の中心。左右で同じ点を見るので比較の土俵は崩れない。 */
function centerFor (name) {
  const lo = [Infinity, Infinity, Infinity]
  const hi = [-Infinity, -Infinity, -Infinity]
  for (const side of ['left', 'right']) {
    const v = vertsOf(name, side)
    for (let i = 0; i < v.length; i += 3) {
      for (let a = 0; a < 3; a++) {
        lo[a] = Math.min(lo[a], v[i + a]); hi[a] = Math.max(hi[a], v[i + a])
      }
    }
  }
  return [0, 1, 2].map((a) => (lo[a] + hi[a]) / 2)
}

function distanceFor (name, dir, center) {
  const d = (() => { const n = Math.hypot(...dir); return dir.map((v) => v / n) })()
  // カメラは原点を見る。up=(0,0,1)。視線に平行な up は使わない題材が無いので特別扱いしない。
  const up = [0, 0, 1]
  const f = d.map((v) => -v)
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
  const norm = (a) => { const n = Math.hypot(...a); return a.map((v) => v / n) }
  const r = norm(cross(f, up))
  const u = cross(r, f)
  const vFov = (FOV * Math.PI) / 180
  const tanV = Math.tan(vFov / 2)
  const tanH = tanV * (WIDTH / HEIGHT)
  let dist = 0
  for (const side of ['left', 'right']) {
    const v = vertsOf(name, side)
    for (let i = 0; i < v.length; i += 3) {
      const p = [v[i] - center[0], v[i + 1] - center[1], v[i + 2] - center[2]]
      const along = p[0] * d[0] + p[1] * d[1] + p[2] * d[2]
      const x = Math.abs(p[0] * r[0] + p[1] * r[1] + p[2] * r[2])
      const y = Math.abs(p[0] * u[0] + p[1] * u[1] + p[2] * u[2])
      dist = Math.max(dist, along + x / tanH, along + y / tanV)
    }
  }
  return dist * 1.06
}

for (const name of manifest.cases) {
  const center = centerFor(name)
  for (const [key, view] of Object.entries(VIEWS)) {
    const dist = distanceFor(name, view.dir, center) * view.zoom
    for (const side of ['left', 'right']) {
      const info = await page.evaluate(
        (n, s, v, r, c) => window.__show(n, s, v, r, c), name, side, view, dist, center)
      if (!(info.triangles > 0)) {
        throw new Error(`${name}/${side}/${key}: 三角形が1つもGPUへ行っていない(真っ黒の合格を防ぐ)`)
      }
      const png = await page.screenshot({ type: 'png' })
      writeFileSync(join(DATA, `${name}_${VIEW_NAMES[key]}_${side}.png`), png)
      console.log(`  ${name}_${VIEW_NAMES[key]}_${side}.png  (${info.triangles} 三角形)`)
    }
  }
}

await browser.close()
server.close()
