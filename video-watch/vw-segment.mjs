#!/usr/bin/env node
// vw-segment.mjs —— 长视频分段并行量测，再把各段 timeline 合并回一份完整的。
//
// 为什么需要：video-watch 的 probe 是唯一的线性成本（≈0.5×视频时长，要逐帧解码
// 才能算出切点/冻结/运动曲线）。1 小时素材要跑约 30 分钟，中间产物
// analysis_v.txt 还会到上百 MB。切成 N 段并行跑，墙钟时间明显下降。
//
// 用法：
//   node vw-segment.mjs <视频> [选项]
//
//   --segments N      目标段数（与 --seg-seconds 二选一）
//   --seg-seconds S   目标每段秒数，默认 300（5 分钟）
//   --parallel P      并行度，默认 min(8, CPU-1)
//   --out DIR         输出目录，默认 <视频同目录>/vw_seg
//   --accurate        逐段重编码切片（边界精确，但慢；默认用 segment muxer 秒级切完）
//   --keep            保留切片文件（默认跑完删掉，省磁盘）
//   --vw <路径>       vw.py 的位置（默认与本脚本同目录）
//   --plan            合并后顺手跑一次 vw.py plan
//
// 产出（在 --out 下）：
//   timeline.json     合并后的完整时间轴，可直接喂 vw.py plan / sheet / report
//   segments.json     段边界、偏移、实测时长（复盘用）
//   seg_XXX/          各段中间产物（probe 结果、analysis_v.txt）
//
// 两个实测得来的设计决定：
//  1. 切片用 ffmpeg 的 segment muxer，不用逐个 `-ss … -c copy`。
//     后者会把每段起点对齐到**更早的**关键帧，段间重叠 —— 实测 4 段累计多出
//     2.4 秒、31 帧，时间戳越往后越偏。segment muxer 在关键帧处切且不重叠，
//     还能直接从 segment list 读出每段实际时长，用它累加得到真实偏移。
//  2. 合并时按**实测偏移**而非名义偏移，配合第 1 点，总时长偏差降到毫秒级。
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// vw.py 的位置解析：--vw 参数 → 环境变量 VW_PY → 与本脚本同目录。
// 不写死绝对路径，否则这个包复制到别的机器就跑不了。
const HERE = path.dirname(fileURLToPath(import.meta.url))
function resolveVw() {
  const i = process.argv.indexOf('--vw')
  const v = (i > -1 && process.argv[i + 1]) || process.env.VW_PY || path.join(HERE, 'vw.py')
  if (!fs.existsSync(v)) {
    console.error(`✗ 找不到 vw.py：${v}`)
    console.error('  三种指定方式（择一）：')
    console.error('    · 把本脚本放进 video-watch skill 目录（与 vw.py 同级）')
    console.error('    · --vw /path/to/vw.py')
    console.error('    · 环境变量 VW_PY=/path/to/vw.py')
    process.exit(1)
  }
  return v
}
const FFPROBE = 'ffprobe'
const FFMPEG = 'ffmpeg'

// ── 参数解析 ────────────────────────────────────────────────────────────
function num(v, flag) {
  // --segments foo 原先被 Number() 变成 NaN，然后静默退回默认值 —— 用户以为生效了
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) {
    console.error(`✗ ${flag} 需要一个正数，收到：${v}`)
    process.exit(2)
  }
  return n
}

function parseArgs(argv) {
  const o = { video: null, segments: 0, segSeconds: 300, parallel: 0, out: null,
              accurate: false, keep: false, plan: false }
  const rest = []
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const next = () => argv[++i]
    if (a === '--segments') o.segments = num(next(), '--segments')
    else if (a === '--seg-seconds') o.segSeconds = num(next(), '--seg-seconds')
    else if (a === '--parallel') o.parallel = num(next(), '--parallel')
    else if (a === '--out') o.out = next()
    else if (a === '--vw') { o.vw = next() }
    else if (a === '--accurate') o.accurate = true
    else if (a === '--keep') o.keep = true
    else if (a === '--plan') o.plan = true
    else if (a.startsWith('-')) { console.error('未知参数:', a); process.exit(2) }
    else rest.push(a)
  }
  o.video = rest[0]
  return o
}

// 记录在跑的子进程：某个并发分支失败时要把其余的收掉，
// 否则 node 退出后 ffmpeg/vw.py 还挂着（实测能残留 3 个 probe 进程）。
const liveProcs = new Set()
function killLive() {
  for (const p of liveProcs) { try { p.kill('SIGTERM') } catch { /* 已退出 */ } }
  liveProcs.clear()
}

function run(cmd, args) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    liveProcs.add(p)
    let out = '', err = ''
    const done = (r) => { liveProcs.delete(p); resolve(r) }
    p.stdout.on('data', (d) => { out += d })
    p.stderr.on('data', (d) => { err += d })
    p.on('error', (e) => done({ code: -1, out, err: err + e.message }))
    p.on('close', (code) => done({ code, out, err }))
  })
}

const log = (...a) => console.log(...a)
const fmt = (s) => {
  // 秒数四舍五入到 60 时要进位，否则 299.7 会打印成「4分60秒」
  let m = Math.floor(s / 60)
  let sec = Math.round(s % 60)
  if (sec === 60) { m += 1; sec = 0 }
  return `${m}分${String(sec).padStart(2, '0')}秒`
}

async function duration(file) {
  const r = await run(FFPROBE, ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', file])
  if (r.code === -1 && /ENOENT/.test(r.err || '')) {
    console.error('✗ 找不到 ffprobe —— 请先安装 ffmpeg（不是素材损坏）')
    process.exit(1)
  }
  return parseFloat((r.out || '').trim()) || 0
}

// ── 切片：segment muxer 一次切完，返回 [{file, start, dur}]（start 为实测偏移）──
async function cutAll(video, outDir, target) {
  const pattern = path.join(outDir, 'seg_%03d.mp4')
  const listFile = path.join(outDir, 'parts.csv')
  const args = ['-hide_banner', '-loglevel', 'error', '-y', '-i', video,
    '-c', 'copy', '-f', 'segment', '-segment_time', String(Math.max(1, Math.round(target))),
    '-reset_timestamps', '1', '-segment_list', listFile, '-segment_list_type', 'csv', pattern]
  const r = await run(FFMPEG, args)
  if (r.code !== 0) throw new Error('切片失败: ' + r.err.slice(-300))

  // csv 每行：文件名,段起始,段结束（相对原片的绝对秒）
  const parts = []
  if (fs.existsSync(listFile)) {
    for (const line of fs.readFileSync(listFile, 'utf8').trim().split('\n')) {
      const [name, s, e] = line.split(',')
      if (!name) continue
      const start = parseFloat(s), end = parseFloat(e)
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
        // 行格式不对就别塞进去：NaN 会一路传播到时间戳和统计量，最后无声地给出错结果
        console.error(`  ! 跳过无法解析的 segment 行：${line.slice(0, 80)}`)
        continue
      }
      parts.push({ file: path.join(outDir, path.basename(name.trim())), start, dur: +(end - start).toFixed(3) })
    }
  }
  if (!parts.length) {
    const files = fs.readdirSync(outDir).filter((f) => /^seg_\d+\.mp4$/.test(f)).sort()
    let acc = 0
    for (const f of files) {
      const d = await duration(path.join(outDir, f))
      parts.push({ file: path.join(outDir, f), start: +acc.toFixed(3), dur: +d.toFixed(3) })
      acc += d
    }
  }
  return parts
}

// ── --accurate：逐段重编码，边界精确到帧 ────────────────────────────────
async function cutAccurate(video, outDir, target, total, wantSegs) {
  // 段数必须听 --segments 的：原先用 ceil(total/target) 重算，
  // 与传入的段数不一致时会多切一段（评审实测 61 秒 7 段变 8 段）
  const n = wantSegs > 0 ? wantSegs : Math.max(1, Math.ceil(total / target))
  const parts = []
  for (let i = 0; i < n; i++) {
    const start = (total * i) / n, end = i === n - 1 ? total : (total * (i + 1)) / n
    const file = path.join(outDir, `seg_${String(i).padStart(3, '0')}.mp4`)
    // -ss 放在 -i 之后是「解码到该点再丢帧」，边界精确但慢：
    // 实测切最后 5 秒耗时 3.84s vs input-ss 的 1.55s（约 2.5 倍），整段的总解码量约为 n/2 倍全片。
    // （更正：input-ss 并不像通常以为的那样会吸附到关键帧 —— 实测两种写法首帧像素差 0.000，
    //   只有显式加 -noaccurate_seek 才会出现 1.079 的差异。）
    const r = await run(FFMPEG, ['-hide_banner', '-loglevel', 'error', '-y',
      '-i', video, '-ss', start.toFixed(3), '-t', (end - start).toFixed(3),
      '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20', '-c:a', 'aac', file])
    if (r.code !== 0) throw new Error(`切片失败 seg${i}: ${r.err.slice(-200)}`)
    parts.push({ file, start: +start.toFixed(3), dur: +(end - start).toFixed(3) })
  }
  return parts
}

async function pool(items, limit, worker) {
  const results = new Array(items.length)
  let idx = 0
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const my = idx++
      if (my >= items.length) return
      results[my] = await worker(items[my], my)
    }
  }))
  return results
}

// ── 合并：buckets 重编号（按实测偏移取整）+ 事件加偏移 + 统计量重算 ──────
function mergeTimelines(parts, sourceVideo) {
  const buckets = {}
  let cuts = [], freezes = [], silences = []
  let duration = 0, frames = 0, activeSum = 0
  const p95s = [], kinds = {}
  for (const { part, timeline: tl } of parts) {
    const off = part.start
    // 用「每桶自己的全局时刻」取整，而不是 round(段偏移)+段内秒 ——
    // 后者在偏移带小数时会与上一段撞车（偏移 299.6 → 段内第 0 桶进 300，
    // 覆盖上一段的第 300 秒）或留洞。
    for (const [k, v] of Object.entries(tl.buckets || {})) {
      const g = Math.round(off + Number(k))
      if (buckets[String(g)] === undefined) buckets[String(g)] = v
    }
    // 段的第 1~2 帧没有前帧可比，会在**段内起点附近**产生伪切点/伪冻结。
    // 判据必须是「段内相对时间」——早先版本按「距全局边界 < 0.35s」无差别删除，
    // 结果把恰好落在边界附近的**真实切点也删掉了**（实测 30 秒素材丢了 14.70s 那个切点：
    // 段边界 14.8s，真实切点 14.70s 被误杀，6 个变 5 个）。
    const EDGE = 0.2
    cuts = cuts.concat((tl.cuts || []).filter((t) => t > EDGE).map((t) => +(t + off).toFixed(2)))
    freezes = freezes.concat((tl.freezes || []).filter((t) => t > EDGE).map((t) => +(t + off).toFixed(2)))
    // silences 与 cuts/freezes 结构不同：它是 [{kind, t}] 对象数组（见 vw.py 的产出），
    // 按数字算会直接 TypeError。这里只平移 t，保留 kind 等字段。
    silences = silences.concat((tl.silences || []).map((e) => ({ ...e, t: +(e.t + off).toFixed(2) })))
    duration += tl.duration || 0
    frames += tl.frames || 0
    activeSum += (tl.active_ratio || 0) * (tl.duration || 0)
    if (typeof tl.motion_p95 === 'number') p95s.push(tl.motion_p95)
    if (tl.content_kind) kinds[tl.content_kind] = (kinds[tl.content_kind] || 0) + 1
  }
  // 补空档：段偏移带小数时 Math.round(off)+k 可能跳过某个全局秒。
  // 实测模拟：偏移 0/47.5/95/142.5 会漏掉 47 和 142 秒。
  // 用相邻桶顶上（秒级粒度下相邻秒差异很小），而不是留洞让 plan 的运动曲线断掉。
  const ks = Object.keys(buckets).map(Number).sort((a, b) => a - b)
  if (ks.length > 1) {
    for (let sec = ks[0]; sec <= ks[ks.length - 1]; sec++) {
      if (buckets[String(sec)] === undefined) {
        // 用显式 undefined 判断而不是 ||：当前 vw.py 的桶值是对象（实测非空），
        // 但若哪天改成数字，0 会被 || 当成「没有」，于是抄来旁边的值。
        buckets[String(sec)] = buckets[String(sec - 1)] !== undefined
          ? buckets[String(sec - 1)]
          : buckets[String(sec + 1)]
      }
    }
  }

  const first = parts[0].timeline
  const kind = Object.entries(kinds).sort((a, b) => b[1] - a[1])[0]?.[0] || '未知'
  return {
    // 必须是**原片**：各段 timeline 的 media 是 parts/seg_000.mp4，那个文件跑完就删了，
    // 下游若按这个字段去取素材会找不到（评审指出）。
    // 与上游约定保持一致：vw.py 在 timeline / sheet manifest / seq manifest 三处
    // 产出 media 时**一律用 basename**，而且从不拿它去打开文件（取素材始终走 args.media）。
    // 所以这里保持 basename，完整路径放在 segments.json（那个文件本来就为此存在）。
    // （曾按评审建议改成绝对路径，读完上游源码后回退：那是破坏契约，不是修复。）
    media: sourceVideo ? path.basename(sourceVideo) : first.media,
    info: { ...(first.info || {}), duration: +duration.toFixed(2), nb_frames: frames },
    content_kind: kind,
    advice: `由 ${parts.length} 段并行量测后合并（vw-segment.mjs）；段内起点 0.2s 内的伪切点/伪冻结已剔除，buckets 已补齐空档`,
    duration: +duration.toFixed(2),
    frames,
    cuts: cuts.sort((a, b) => a - b),
    freezes: freezes.sort((a, b) => a - b),
    silences: silences.sort((a, b) => a - b),
    // 逐帧 p95 无法从「逐秒桶」精确还原，取各段 p95 的均值作估计
    motion_p95: p95s.length ? +(p95s.reduce((a, b) => a + b, 0) / p95s.length).toFixed(3) : 0,
    active_ratio: duration > 0 ? +(activeSum / duration).toFixed(3) : 0,
    buckets,
    _merged: { segments: parts.length, per_segment_p95: p95s.map((x) => +x.toFixed(2)), offsets: parts.map((p) => p.part.start) },
  }
}

process.on('SIGINT', () => { killLive(); process.exit(130) })

// ── 主流程（整体包一层，任何未预期异常都给友好提示而不是抛堆栈）──────
try {
const opt = parseArgs(process.argv.slice(2))
if (!opt.video) {
  console.error('用法: node vw-segment.mjs <视频> [--segments N | --seg-seconds S] [--parallel P] [--out DIR] [--accurate] [--keep] [--plan] [--vw <vw.py 路径>]')
  process.exit(2)
}
if (!fs.existsSync(opt.video)) {
  console.error(`✗ 找不到文件：${opt.video}`)
  process.exit(1)
}

// 参数都合法了才去解析 vw.py —— 否则只想看用法时也会先被「找不到 vw.py」挡住
const VW = resolveVw()
// os.cpus() 在部分容器里返回空数组（实测本机某次为 0），会导致并行度恒为 1。
// availableParallelism() 读 cgroup 限制，更准；都拿不到就退回 4。
const cpus = (os.availableParallelism ? os.availableParallelism() : 0) || os.cpus().length || 4
const parallel = opt.parallel > 0 ? opt.parallel : Math.max(1, Math.min(8, cpus - 1))
const outDir = opt.out || path.join(path.dirname(path.resolve(opt.video)), 'vw_seg')
const segDir = path.join(outDir, 'parts')
fs.rmSync(segDir, { recursive: true, force: true })
fs.mkdirSync(segDir, { recursive: true })

const t0 = Date.now()
const total = await duration(opt.video)
if (!total || total <= 0) {
  console.error(`✗ 读不出时长，文件可能损坏或不是音视频：${opt.video}`)
  console.error('  （可以用 ffprobe 手工确认：ffprobe -v error -show_entries format=duration <文件>）')
  process.exit(1)
}
const target = opt.segments > 0 ? total / opt.segments : opt.segSeconds
log(`素材 ${path.basename(opt.video)}  ${fmt(total)}  目标每段 ${Math.round(target)}s  并行度 ${parallel}（CPU ${cpus}）`)

// ① 切片
log('\n① 切片…')
const ts1 = Date.now()
const parts = opt.accurate
  ? await cutAccurate(opt.video, segDir, target, total, Math.round(opt.segments))
  : await cutAll(opt.video, segDir, target)
const cutSecs = (Date.now() - ts1) / 1000
log(`  ${parts.length} 段，用时 ${cutSecs.toFixed(1)}s`)
parts.forEach((p, i) => log(`  seg${String(i).padStart(3, '0')}  偏移 ${p.start.toFixed(2)}s  时长 ${p.dur.toFixed(2)}s`))

// ② 并行 probe
log(`\n② 并行量测（${parts.length} 段 × ${parallel} 并发）…`)
const ts2 = Date.now()
const probed = await pool(parts, parallel, async (part, i) => {
  const d = path.join(outDir, `seg_${String(i).padStart(3, '0')}`)
  // 必须整个重建：只清 parts/ 的话，上次运行留下的 timeline.json 会在这里被读到，
  // 于是本轮 probe 全崩也能「合并成功」并报完成（评审实测复现）。
  fs.rmSync(d, { recursive: true, force: true })
  fs.mkdirSync(d, { recursive: true })
  const ts = Date.now()
  const r = await run('python3', [VW, 'probe', part.file, '--out', d])
  const tlFile = path.join(d, 'timeline.json')
  // 先看退出码，再看文件：只看文件存在会把「上次的残留」当成本次成果
  if (r.code !== 0) throw new Error(`seg${i} 量测失败（退出码 ${r.code}）: ${(r.err || r.out).slice(-300)}`)
  if (!fs.existsSync(tlFile)) throw new Error(`seg${i} 没产出 timeline.json: ${(r.err || r.out).slice(-300)}`)
  const timeline = JSON.parse(fs.readFileSync(tlFile, 'utf8'))
  log(`  seg${String(i).padStart(3, '0')} 完成  ${((Date.now() - ts) / 1000).toFixed(1)}s  切点 ${timeline.cuts?.length ?? 0} 个`)
  return { part, timeline }
})
const wall = (Date.now() - ts2) / 1000
log(`  量测墙钟 ${wall.toFixed(1)}s`)
// 对照基准：不分段跑全片 probe ≈ 0.52 × 时长（本机实测系数）。
// 注意并行收益受热节流影响很大：同一素材 4 段 8 并发，冷机实测 233s，连续满载后
// 同一配置变成 396s。别把某一次的数字当成稳定性能。
log(`  对照：不分段跑全片预计约 ${((total * 0.52) / 60).toFixed(1)} 分钟（按本机实测系数 0.52×时长估算，换机器会有出入）`)

// ③ 合并
log('\n③ 合并时间轴…')
const merged = mergeTimelines(probed, opt.video)
const mergedFile = path.join(outDir, 'timeline.json')
fs.writeFileSync(mergedFile, JSON.stringify(merged))
fs.writeFileSync(path.join(outDir, 'segments.json'), JSON.stringify({
  media: opt.video, total: +total.toFixed(2), parallel, accurate: opt.accurate,
  segments: probed.map((p) => ({ start: p.part.start, dur: p.part.dur })),
}, null, 1))
log(`  → ${mergedFile}`)
log(`  时长 ${merged.duration}s  帧 ${merged.frames}  切点 ${merged.cuts.length} 个  类型 ${merged.content_kind}`)
const drift = merged.duration - total
log(`  精度自检：与原始素材相差 ${drift >= 0 ? '+' : ''}${drift.toFixed(2)}s（${(Math.abs(drift) / total * 100).toFixed(2)}%）`)

if (!opt.keep) {
  for (const p of parts) try { fs.unlinkSync(p.file) } catch {}
  log('  （切片已删除，--keep 可保留）')
}

if (opt.plan) {
  log('\n④ 跑 plan…')
  const r = await run('python3', [VW, 'plan', mergedFile, '--skeleton', '16', '--max', '48'])
  log(r.out.trim().split('\n').slice(-6).join('\n'))
}

log(`\n完成，总用时 ${((Date.now() - t0) / 1000).toFixed(1)}s。`)
} catch (e) {
  killLive()      // 先收掉还在跑的 ffmpeg / probe，别让它们变成孤儿进程
  console.error(`\n✗ 执行失败：${e && e.message ? e.message : e}`)
  console.error('  常见原因：素材损坏/不是音视频、磁盘写满、ffmpeg 未安装。')
  process.exit(1)
}
