#!/usr/bin/env node
// B 站搜索（wbi 签名，只读）：node bili-search.mjs "关键词" [页]
const crypto = await import('node:crypto')
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
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
const kw = process.argv[2], pn = process.argv[3] || 1
const u = 'https://api.bilibili.com/x/web-interface/wbi/search/type?' + sign({ search_type: 'video', keyword: kw, page: pn, page_size: 20, order: 'totalrank', platform: 'pc', duration: 0, tids: 0 })
const r = await (await fetch(u, { headers: H })).json()
if (r.code !== 0) { console.log('FAIL', r.code, r.message); process.exit(1) }
console.log('「' + kw + '」共 ' + r.data.numResults + ' 条，第 ' + pn + ' 页\n')
;(r.data.result || []).forEach((v, i) => {
  const t = (v.title || '').replace(/<[^>]*>/g, '')
  console.log((i + 1) + '. [' + v.play + '播 ' + v.danmaku + '弹] ' + v.bvid + ' | ' + v.author + ' | ' + t.slice(0, 54))
})
