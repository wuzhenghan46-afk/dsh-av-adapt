// DSH 启动后自动拉起 web-access skill 用的 headless Chromium。
//
// 为什么需要：手机重启后容器里不会有浏览器进程，而 web-access 的前置检查
// 只会报「没有浏览器」，不会自己启动浏览器 —— 那样每次都得有人记得敲一次
// start-chromium.sh。这个插件让「打开 DSH」这一步顺带把 CDP 通道带起来。
//
// 只做一件事：延迟一会儿 spawn start-chromium.sh（脚本自身幂等，已经在跑
// 就直接返回），全程不阻塞启动，失败了只写日志，绝不让插件抛错影响 DSH。
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// 启动脚本位置：优先插件配置里的 scriptPath，其次环境变量，最后默认位置。
// 默认值与 web-access 适配包的约定一致 —— 换成别的路径请改这里或写进插件 config。
const DEFAULT_SCRIPT = path.join(os.homedir(), '.dsh', 'web-access-browser', 'start-chromium.sh')
const LOG = process.env.CHROMIUM_AUTOSTART_LOG || '/tmp/chromium-autostart.log'

function log(line) {
  try { fs.appendFileSync(LOG, `[${new Date().toISOString()}] ${line}\n`) } catch { /* 日志写不了也不能炸 */ }
}

export function apply(ctx, config = {}) {
  const delayMs = Number(config.delayMs ?? 8000)
  const SCRIPT = process.env.CHROMIUM_START_SCRIPT || config.scriptPath || DEFAULT_SCRIPT
  const timer = setTimeout(() => {
    if (!fs.existsSync(SCRIPT)) return log(`跳过：找不到 ${SCRIPT}（可用 config.scriptPath 或 CHROMIUM_START_SCRIPT 指定）`)
    log('检查并拉起 headless Chromium')
    let p
    try {
      // detached 的进程不要再挂回父进程的管道：启动脚本若在前台挂住，
      // out 字符串会一直涨；DSH 退出时管道一断，子进程也会被带走。
      // 启动脚本自己会把输出写进 /tmp/web-access-chromium.log，这里不需要转存。
      p = spawn('bash', [SCRIPT], { detached: true, stdio: 'ignore' })
    } catch (e) { return log('spawn 失败: ' + e.message) }
    p.on('error', (e) => log('运行失败: ' + e.message))
    p.on('exit', (code) => log(`start-chromium.sh 结束 code=${code}（详情见 /tmp/web-access-chromium.log）`))
    p.unref()
  }, delayMs)
  timer.unref?.()

  ctx.effect(() => () => clearTimeout(timer), 'chromium-autostart')
}
