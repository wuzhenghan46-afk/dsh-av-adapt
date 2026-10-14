#!/usr/bin/env node
// B 站评论学习：容器直连，只读。
// 用法：
//   node bili-study.mjs hot [页]
//   node bili-study.mjs view BV
//   node bili-study.mjs related BV
//   node bili-study.mjs reply BV [页数] [mode]       mode 3=热度(默认) 2=时间
//   node bili-study.mjs sub BV [取前几条热评]          楼中楼（对话形态）
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
const H = { 'User-Agent': UA, Referer: 'https://www.bilibili.com/' }
const sleep = (ms) => new Promise(r => setTimeout(r, ms))
async function j(url) {
  const r = await fetch(url, { headers: H })
  const t = await r.text()
  try { return JSON.parse(t) } catch { return { code: -1, raw: t.slice(0, 200) } }
}
const [cmd, arg, arg2, arg3] = process.argv.slice(2)

if (cmd === 'hot') {
  const d = await j('https://api.bilibili.com/x/web-interface/popular?ps=20&pn=' + (arg || 1))
  if (d.code !== 0) { console.log('FAIL', JSON.stringify(d).slice(0, 300)); process.exit(1) }
  d.data.list.forEach((v, i) => console.log((i + 1) + '. [' + v.stat.reply + '评] ' + v.bvid + ' | ' + v.tname + ' | ' + v.title.slice(0, 46)))
} else if (cmd === 'view') {
  const d = await j('https://api.bilibili.com/x/web-interface/view?bvid=' + arg)
  if (d.code !== 0) { console.log('FAIL', JSON.stringify(d).slice(0, 300)); process.exit(1) }
  console.log(JSON.stringify({ bvid: arg, aid: d.data.aid, title: d.data.title, reply: d.data.stat.reply, view: d.data.stat.view, owner: d.data.owner.name }, null, 1))
} else if (cmd === 'related') {
  const d = await j('https://api.bilibili.com/x/web-interface/archive/related?bvid=' + arg)
  if (d.code !== 0) { console.log('FAIL', d.code); process.exit(1) }
  d.data.slice(0, 20).forEach(v => console.log(v.bvid + ' | ' + v.owner.name + ' | ' + v.stat.reply + '评 ' + v.stat.view + '播 | ' + v.title.slice(0, 46)))
} else if (cmd === 'reply') {
  const pages = Number(arg2 || 3), mode = Number(arg3 || 3)
  const v = await j('https://api.bilibili.com/x/web-interface/view?bvid=' + arg)
  if (v.code !== 0) { console.log('FAIL view', v.code); process.exit(1) }
  console.log('# ' + v.data.title + '\n# aid=' + v.data.aid + '  ' + v.data.stat.reply + '条评论  UP=' + v.data.owner.name + '\n')
  const all = []; let next = 0
  for (let p = 1; p <= pages; p++) {
    const d = await j('https://api.bilibili.com/x/v2/reply/main?type=1&oid=' + v.data.aid + '&mode=' + mode + '&next=' + next + '&ps=20')
    if (d.code !== 0) { console.log('第' + p + '页 FAIL code=' + d.code); break }
    const rs = d.data.replies || []
    console.log('== 第' + p + '页 ' + rs.length + ' 条 ==')
    rs.forEach(r => {
      const msg = (r.content.message || '').replace(/\n/g, ' / ')
      all.push({ rpid: r.rpid, like: r.like, uname: r.member.uname, msg, rcount: r.rcount })
      console.log('[' + r.like + '赞' + (r.rcount ? ' ' + r.rcount + '回' : '') + '] ' + r.member.uname + ': ' + msg.slice(0, 110))
    })
    next = (d.data.cursor && d.data.cursor.next) || 0
    if (!next) { console.log('(到底了)'); break }
    await sleep(2400)
  }
  const fs = await import('node:fs')
  fs.writeFileSync('./bili-raw-' + arg + '.json', JSON.stringify(all, null, 1))
  console.log('\n共 ' + all.length + ' 条，存 bili-raw-' + arg + '.json')
} else if (cmd === 'sub') {
  const top = Number(arg2 || 6)
  const v = await j('https://api.bilibili.com/x/web-interface/view?bvid=' + arg)
  const d = await j('https://api.bilibili.com/x/v2/reply/main?type=1&oid=' + v.data.aid + '&mode=3&next=0&ps=20')
  const roots = (d.data.replies || []).filter(r => r.rcount > 0).slice(0, top)
  console.log('# ' + v.data.title + ' 挖 ' + roots.length + ' 条热评的楼中楼\n')
  const out = []
  for (const r of roots) {
    console.log('【' + r.like + '赞】' + r.member.uname + ': ' + r.content.message.replace(/\n/g, ' / ').slice(0, 60))
    await sleep(2400)
    const s = await j('https://api.bilibili.com/x/v2/reply/reply?type=1&oid=' + v.data.aid + '&root=' + r.rpid + '&pn=1&ps=20')
    const subs = (s.data && s.data.replies) || []
    subs.forEach(x => {
      const m = (x.content.message || '').replace(/\n/g, ' / ')
      out.push({ root: r.content.message.slice(0, 30), uname: x.member.uname, msg: m, like: x.like })
      console.log('    ↳ [' + x.like + '] ' + x.member.uname + ': ' + m.slice(0, 100))
    })
    await sleep(2400)
  }
  const fs = await import('node:fs')
  fs.writeFileSync('./bili-sub-' + arg + '.json', JSON.stringify(out, null, 1))
  console.log('\n楼中楼共 ' + out.length + ' 条')
} else console.log('用法: hot [页] | view BV | related BV | reply BV [页数] [mode] | sub BV [条数]')
