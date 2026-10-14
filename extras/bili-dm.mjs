#!/usr/bin/env node
// 抓 B 站弹幕（只读）：node bili-dm.mjs BVxxxxx [段数]
const zlib = await import('node:zlib')
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
const H = { 'User-Agent': UA, Referer: 'https://www.bilibili.com/' }
const sleep = (ms) => new Promise(r => setTimeout(r, ms))
const bv = process.argv[2], segs = Number(process.argv[3] || 2)

const v = await (await fetch('https://api.bilibili.com/x/web-interface/view?bvid=' + bv, { headers: H })).json()
if (v.code !== 0) { console.log('FAIL view', v.code); process.exit(1) }
const cid = v.data.cid
console.log('# ' + v.data.title + '  cid=' + cid + '  UP=' + v.data.owner.name + '\n')

const out = []
for (let i = 1; i <= segs; i++) {
  const r = await fetch('https://api.bilibili.com/x/v1/dm/list.so?oid=' + cid, { headers: H })
  const buf = Buffer.from(await r.arrayBuffer())
  let xml = ''
  try { xml = zlib.inflateRawSync(buf).toString('utf8') } catch (e) {
    try { xml = zlib.inflateSync(buf).toString('utf8') } catch (e2) { xml = buf.toString('utf8') }
  }
  const m = xml.match(/<d p="([^"]*)">([^<]*)<\/d>/g) || []
  console.log('段 ' + i + ' → ' + m.length + ' 条弹幕')
  m.forEach(s => {
    const g = s.match(/<d p="([^"]*)">([^<]*)<\/d>/)
    const t = Number(g[1].split(',')[0])
    out.push({ t, text: g[2], mode: g[1].split(',')[1] })
  })
  break // list.so 一次给全量，不用循环
}
out.sort((a, b) => a.t - b.t)
out.forEach(d => {
  const mm = String(Math.floor(d.t / 60)).padStart(2, '0') + ':' + String(Math.floor(d.t % 60)).padStart(2, '0')
  console.log(mm + ' ' + d.text)
})
const fs = await import('node:fs')
fs.writeFileSync('./bili-dm-' + bv + '.json', JSON.stringify(out, null, 1))
console.log('\n共 ' + out.length + ' 条，存 bili-dm-' + bv + '.json')
