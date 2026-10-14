#!/usr/bin/env node
// 顺藤摸瓜：多个种子视频的相关视频，去重汇总
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
const H = { 'User-Agent': UA, Referer: 'https://www.bilibili.com/' }
const seeds = process.argv.slice(2)
const sleep = ms => new Promise(r => setTimeout(r, ms))
const seen = new Map()
for (const s of seeds) {
  const d = await (await fetch('https://api.bilibili.com/x/web-interface/archive/related?bvid=' + s, { headers: H })).json()
  if (d.code !== 0) { console.log('FAIL', s, d.code); continue }
  d.data.forEach(v => { if (!seen.has(v.bvid)) seen.set(v.bvid, v) })
  console.log('种子 ' + s + ' → ' + d.data.length + ' 条，累计去重 ' + seen.size)
  await sleep(2200)
}
const arr = [...seen.values()].sort((a, b) => b.stat.reply - a.stat.reply)
console.log('\n=== 按评论数排序 ===')
arr.forEach(v => console.log(v.stat.reply + '评 ' + v.stat.view + '播 | ' + v.bvid + ' | ' + v.owner.name + ' | ' + v.title.slice(0, 52)))
const fs = await import('node:fs')
fs.writeFileSync('./bili-circle.json', JSON.stringify(arr.map(v => ({ bvid: v.bvid, title: v.title, up: v.owner.name, reply: v.stat.reply, view: v.stat.view, pub: v.pubdate })), null, 1))
