# chromium-autostart —— DSH 插件

让 DSH 启动时自动拉起 web-access 需要的 headless Chromium，手机重启后不必手动敲命令。

## 安装

在 DSH profile 的 `package.json` 里加：

```json
{
  "dependencies": { "dsh-plugin-chromium-autostart": "link:/path/to/chromium-autostart" },
  "dsh": { "profile": { "bundles": ["...", "dsh-plugin-chromium-autostart"] } }
}
```

然后在该 profile 目录 `pnpm install`，重启 DSH 生效。验证：

```bash
dsh --profile <name> --dump-config | grep -A3 chromium-autostart
tail -f /tmp/chromium-autostart.log     # 插件日志
```

## 设计

延迟 8 秒后 spawn `start-chromium.sh`（**脚本自身幂等**，已在跑就直接返回），全程不阻塞启动，失败只写日志，绝不抛错影响 DSH。
