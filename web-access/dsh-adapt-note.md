> **本机适配说明（DSH / Android 容器）**
>
> - 本 skill 由 DSH 从 本 skill 所在目录（下称 skill 目录） 加载；原文中所有 `CLAUDE_SKILL_DIR` 变量已就地替换为该绝对路径。
> - **CDP 分支可用，但浏览器是容器内的 headless Chromium**（不是桌面 Chrome，**不带任何登录态**）：
>   - 已装 Chromium 120 在 `/root/.cache/ms-playwright/chromium-1091/`，由 `/root/.dsh/web-access-browser/start-chromium.sh` 启动（幂等）。DSH 启动时会经 `dsh-plugin-chromium-autostart` 自动把它拉起来；手机重启后打开一次 DSH 即可，手动跑那个脚本只是立即需要时的兜底。
>   - `config.env` 只设了 `WEB_ACCESS_BROWSER=chromium`。**不要设 `WEB_ACCESS_CDP_PORT`**：固定端口模式下 skill 取不到带 UUID 的 wsPath，CDP 握手会失败（`Received network error or non-101 status code`）；走默认的 DevToolsActivePort 文件发现即可。
>   - 前置检查报「没有浏览器」或 proxy 连不上时，先执行 `bash /root/.dsh/web-access-browser/start-chromium.sh`，再重跑 `node skill 目录/scripts/check-deps.mjs`。
>   - 启动参数不要动：必须 `--headless=old`（new 模式下 CDP 截图不产帧）、必须 `--disable-dev-shm-usage`（容器没有 /dev/shm）、不要加 `--disable-gpu`（会让截图失败）。
>   - 本机内核是 Android 12 / Linux 5.10，跑不动 Chromium 156 及以上（缺 `MFD_NOEXEC_SEAL`，启动约 2 秒即 SIGTRAP 崩溃），不要"顺手升级"浏览器。
> - **没有登录态**：需要账号的站点（小红书、微博、推特等）在容器浏览器里处于未登录状态，遇到登录墙要如实告知用户，不要假装能绕过。`find-url.mjs`（读本地 Chrome 书签/历史）在本机没有数据源，不可用。
> - 常规文字类联网任务优先用 `web_search / web_fetch / curl / Jina`；需要动态渲染、页面交互、渲染截图时才走 CDP，不必每次都跑前置检查。
> - 通过 `git pull` 更新本 skill 后，重跑 `bash skill 目录/adapt-dsh.sh` 恢复本适配。
