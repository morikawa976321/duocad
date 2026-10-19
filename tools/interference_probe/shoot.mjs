/**
 * 干渉チェックの試作の撮影台(shoot_export.py が書いた場面を1枚ずつ撮る)。
 *
 *   node tools/interference_probe/shoot.mjs [--only <場面名,...>] [--width 1200] [--height 800]
 *
 * web/scripts/shoot-topopt.mjs と同じ作り: three.js を headless Chrome で描いて page.screenshot。three.js と puppeteer-core は
 * web/node_modules から借りる(ワークツリーではジャンクションが要る。docs/DEV_NOTES.md「ワークツリー」)。
 * 半透明の部品は不透明な強調(重なり・接触面・線)の後に描く。三角形が1枚も GPU へ行かない場面は落とす。
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { parseArgs } from '../../web/scripts/args.mjs'
import { findBrowser, launchBrowser } from '../../web/scripts/browser.mjs'
import { distanceFor } from '../../web/scripts/framing.mjs'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const DATA = join(REPO_ROOT, 'output', 'interference_probe', 'shots', '_data')
const OUT = join(REPO_ROOT, 'output', 'interference_probe', 'shots')
const options = parseArgs(process.argv.slice(2))
const W = Number(options.width ?? 1200)
const H = Number(options.height ?? 800)
const BAND = 64
const FOV = 30
const BG = '#1d1f26'

const manifest = JSON.parse(readFileSync(join(DATA, 'manifest.json'), 'utf8'))
const wanted = options.only ? options.only.split(',') : manifest

const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;padding:0;background:${BG};overflow:hidden}
  #band{height:${BAND}px;width:${W}px;color:#e6e6f0;font:16px/1.35 system-ui,'Yu Gothic UI','Meiryo',sans-serif;
        padding:8px 12px;box-sizing:border-box}
  #band b{display:block;font-size:17px}
  #band span{color:#aab3c8;font-size:13px}
</style>
<script type="importmap">{"imports":{"three":"/vendor/three.module.js"}}</script>
</head><body>
<div id="band"><b id="title"></b><span id="caption"></span></div>
<script type="module">
import * as THREE from 'three';
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(${W}, ${H}, false);
renderer.setClearColor(new THREE.Color('${BG}'), 1);
renderer.sortObjects = true;
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color('${BG}');
scene.add(new THREE.HemisphereLight(0xffffff, 0x333344, 0.7));
const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
dirLight.position.set(2, -3, 4);
scene.add(dirLight);
const headLight = new THREE.DirectionalLight(0xffffff, 0.45);
scene.add(headLight);
const camera = new THREE.PerspectiveCamera(${FOV}, ${W} / ${H}, 0.1, 50000);
camera.up.set(0, 0, 1);
function b64(text) {
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out.buffer;
}
let group = null;
window.__show = async (name, dir, dist, centre) => {
  if (group) scene.remove(group);
  group = new THREE.Group();
  const sc = await (await fetch('/data/' + name + '.json')).json();
  document.getElementById('title').textContent = sc.title;
  document.getElementById('caption').textContent = sc.caption;
  for (const o of sc.objects) {
    if (!o.triangles) continue;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(b64(o.verts_b64)), 3));
    g.setIndex(new THREE.BufferAttribute(new Uint32Array(b64(o.faces_b64)), 1));
    g.computeVertexNormals();
    const translucent = o.opacity < 1;
    const m = new THREE.MeshLambertMaterial({
      color: o.color, transparent: translucent, opacity: o.opacity, depthWrite: !translucent,
      side: THREE.DoubleSide, polygonOffset: o.front, polygonOffsetFactor: o.front ? -4 : 0, polygonOffsetUnits: o.front ? -4 : 0,
    });
    const mesh = new THREE.Mesh(g, m);
    mesh.renderOrder = translucent ? 2 : (o.front ? 1 : 0);
    group.add(mesh);
    if (o.edges) {
      const lines = new THREE.LineSegments(new THREE.EdgesGeometry(g, 35),
        new THREE.LineBasicMaterial({ color: 0x5b6378, transparent: true, opacity: 0.55, depthWrite: false }));
      lines.renderOrder = 3;
      group.add(lines);
    }
  }
  scene.add(group);
  const c = new THREE.Vector3(...centre);
  camera.position.copy(new THREE.Vector3(...dir).normalize().multiplyScalar(dist).add(c));
  camera.near = Math.max(dist / 1000, 0.001);
  camera.far = dist * 50;
  camera.lookAt(c);
  headLight.position.copy(camera.position);
  camera.updateProjectionMatrix();
  renderer.info.reset();
  renderer.render(scene, camera);
  return renderer.info.render.triangles;
};
window.__ready = true;
</script></body></html>`

const THREE_DIR = join(REPO_ROOT, 'web', 'node_modules', 'three', 'build')
const server = createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1')
  if (url.pathname === '/page') {
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    response.end(PAGE)
    return
  }
  if (url.pathname === '/favicon.ico') {
    response.writeHead(204)
    response.end()
    return
  }
  const rel = decodeURIComponent(url.pathname).replace(/^\/+/, '')
  const file = rel.startsWith('vendor/') ? join(THREE_DIR, rel.slice(7)) : join(DATA, rel.replace(/^data\//, ''))
  if (!existsSync(file)) {
    response.writeHead(404)
    response.end('no')
    return
  }
  response.writeHead(200, { 'content-type': file.endsWith('.js') ? 'text/javascript' : 'application/json' })
  response.end(readFileSync(file))
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const PORT = server.address().port

/** 場面の注視点と、収める頂点(全部か、強調だけか)。 */
function framing(sc) {
  const pick = sc.fit === 'highlight' ? sc.objects.filter((o) => o.opacity >= 1) : sc.objects
  const verts = sc.fit_box
    ? [Float32Array.from([0, 1].flatMap((i) => [0, 1].flatMap((j) => [0, 1].flatMap((k) =>
        [sc.fit_box[i][0], sc.fit_box[j][1], sc.fit_box[k][2]]))))]
    : pick.filter((o) => o.triangles).map((o) => {
      const buf = Buffer.from(o.verts_b64, 'base64')
      return new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength))
    })
  const lo = [Infinity, Infinity, Infinity]
  const hi = [-Infinity, -Infinity, -Infinity]
  for (const v of verts) for (let i = 0; i < v.length; i += 3) for (let a = 0; a < 3; a += 1) {
    lo[a] = Math.min(lo[a], v[i + a])
    hi[a] = Math.max(hi[a], v[i + a])
  }
  const centre = sc.target ?? [0, 1, 2].map((a) => (lo[a] + hi[a]) / 2)
  return { centre, dist: distanceFor(sc.view, centre, verts, FOV, W / H) * (sc.zoom ?? 1) }
}

mkdirSync(OUT, { recursive: true })
const browser = await launchBrowser(findBrowser())
const written = []
try {
  const page = await browser.newPage()
  await page.setViewport({ width: W, height: H + BAND })
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  page.on('console', (message) => { if (message.type() === 'error') pageErrors.push(message.text()) })
  await page.goto(`http://127.0.0.1:${PORT}/page`, { waitUntil: 'load' })
  await page.waitForFunction('window.__ready === true', { timeout: 30000 })
  for (const name of wanted) {
    const sc = JSON.parse(readFileSync(join(DATA, `${name}.json`), 'utf8'))
    const { centre, dist } = framing(sc)
    const triangles = await page.evaluate((n, d, r, c) => window.__show(n, d, r, c), name, sc.view, dist, centre)
    if (!(triangles > 0)) throw new Error(`${name}: 三角形が GPU へ行っていない`)
    if (pageErrors.length > 0) throw new Error(`ページ側でエラー: ${pageErrors[0]}`)
    const path = join(OUT, `${name}.png`)
    writeFileSync(path, await page.screenshot({ type: 'png' }))
    written.push(path)
    console.log(`  ${path}  (三角形 ${triangles} / 距離 ${dist.toFixed(1)} mm)`)
  }
} finally {
  await browser.close()
  server.close()
}
console.log(`shoot: ${written.length} 枚`)
