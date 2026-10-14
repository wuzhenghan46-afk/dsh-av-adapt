#!/usr/bin/env node
// 查 B 站 UP 主：node bili-up.mjs <mid> [页数]
const crypto = await import('node:crypto')
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36'
const TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
const md5 = s => crypto.createHash('md5').update(s).digest('hex')
const home = await fetch('https://www.bilibili.com/', { headers: { 'User-Agent': UA } })
const cookie = (home.headers.getSetCookie ? home.headers.getSetCookie() : []).map(c => c.split(';')[0]).join('; ')
const H = { 'User-Agent': UA, Referer: 'https://www.bilibili.com/', Cookie: cookie }
const nav = await (await fetch('https://api.bilibili.com/x/web-interface/nav', { headers: H })).json()
const fn = u => u.split('/').pop().split('.')[0]
const orig = fn(nav.data.wbi_img.img_url) + fn(nav.data.wbi_img.sub_url)
const mixin = TAB.map(i => orig[i]).join('').slice(0, 32)
function sign(p) {
  const wts = Math.floor(Date.now() / 1000); const o = { ...p, wts }
  const q = Object.keys(o).sort().map(k => encodeURIComponent(k) + '=' + encodeURIComponent(String(o[k]).replace(/[!'()*]/g, ''))).join('&')
  return q + '&w_rid=' + md5(q + mixin)
}
const mid = process.argv[2], pn = process.argv[3] || 1
const info = await (await fetch('https://api.bilibili.com/x/space/wbi/acc/info?' + sign({ mid }), { headers: H })).json()
if (info.code === 0) {
  const d = info.data
  console.log('昵称:', d.name, '| 等级', d.level, '| 粉丝', d.follower, '| 签名:', (d.sign || '').slice(0, 80))
} else console.log('acc/info code=', info.code, info.message)
const arc = await (await fetch('https://api.bilibili.com/x/space/wbi/arc/search?' + sign({ mid, ps: 30, pn, order: 'pubdate', platform: 'web', web_location: 1550101, tid: 0 }), { headers: H })).json()
if (arc.code !== 0) { console.log('arc/search code=', arc.code, arc.message) } else {
  const list = (arc.data.list.vlist) || []
  console.log('共', arc.data.page.count, '个视频，本页', list.length)
  list.forEach((v, i) => console.log((i + 1) + '. [' + v.play + '播] ' + v.bvid + ' | ' + (v.created ? new Date(v.created * 1000).toISOString().slice(0, 10) : '') + ' | ' + v.title.slice(0, 52)))
}
