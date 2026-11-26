// Usage: node scripts/fal-generate.mjs <model_id> <input.json> <out_file>
// Key is read from the vault ("Fal.ai key") at runtime; never written to disk.
import { readFileSync, writeFileSync } from 'node:fs'
import { getSecret } from '../dist/web/vault.js'

const [model, inputPath, outPath] = process.argv.slice(2)
const key = getSecret('Fal.ai key')
if (!key) { console.error('Fal.ai key missing from vault'); process.exit(1) }
const H = { Authorization: `Key ${key}`, 'Content-Type': 'application/json' }
const input = JSON.parse(readFileSync(inputPath, 'utf-8'))
if (input.image_path) {
  const b = readFileSync(input.image_path)
  input.image_url = `data:image/jpeg;base64,${b.toString('base64')}`
  delete input.image_path
}
const sub = await fetch(`https://queue.fal.run/${model}`, { method: 'POST', headers: H, body: JSON.stringify(input) })
const subj = await sub.json()
if (!sub.ok) { console.error('submit failed', sub.status, JSON.stringify(subj).slice(0, 500)); process.exit(1) }
console.error('request_id', subj.request_id)
for (;;) {
  await new Promise(r => setTimeout(r, 5000))
  const st = await (await fetch(subj.status_url, { headers: H })).json()
  console.error('status', st.status)
  if (st.status === 'COMPLETED') break
  if (st.status !== 'IN_QUEUE' && st.status !== 'IN_PROGRESS') { console.error(JSON.stringify(st).slice(0, 500)); process.exit(1) }
}
const res = await (await fetch(subj.response_url, { headers: H })).json()
const url = res.video?.url || res.images?.[0]?.url
if (!url) { console.error('no output', JSON.stringify(res).slice(0, 500)); process.exit(1) }
writeFileSync(outPath, Buffer.from(await (await fetch(url)).arrayBuffer()))
console.log(JSON.stringify({ out: outPath, url, keys: Object.keys(res) }))
