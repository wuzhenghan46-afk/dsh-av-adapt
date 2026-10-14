# web-access 的 DSH 适配

把这个 skill 从"假设你有桌面 Chrome"改造成"在 ARM64 容器里自建 headless Chromium"。

## 文件

| 文件 | 作用 |
|---|---|
| `adapt-dsh.sh` | 幂等补丁：路径替换 + 维护 SKILL.md 顶部的适配说明。`git pull` 后重跑即可恢复 |
| `dsh-adapt-note.md` | 适配说明的源文本（被 adapt-dsh.sh 插进 SKILL.md 顶部） |

## 适配要点

1. **路径**：上游全篇用 `${CLAUDE_SKILL_DIR}` 定位脚本，DSH 不设这个变量 → 批量替换为绝对路径。
2. **浏览器来源**：容器内 headless Chromium 120（**不能升级**，见主 README 坑#1），由 `chromium-autostart` 插件在 DSH 启动时自动拉起。
3. **绝不要设 `WEB_ACCESS_CDP_PORT`**：固定端口模式下 skill 取不到带 UUID 的 wsPath，CDP 握手必然失败（坑#3）。只设 `WEB_ACCESS_BROWSER=chromium`，走 DevToolsActivePort 文件发现。
4. **启动参数不可改**：`--headless=old`（new 模式截图不产帧）、`--disable-dev-shm-usage`（容器没有 /dev/shm）、**不要** `--disable-gpu`。
5. **没有登录态**：这是干净浏览器，遇到需要账号的站点要如实告知用户，不要假装能绕过。`find-url.mjs`（读本地书签/历史）在本机无数据源，不可用。
