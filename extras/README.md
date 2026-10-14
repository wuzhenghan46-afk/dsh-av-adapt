# extras —— 途中顺手写的辅助脚本

这些**不属于本包的核心适配**，是做这件事的过程中顺手写的、之后可能还用得上的小工具。
它们和主包的四个工具一样经过实测，但**依赖的是公开 API 的现状**——B 站改接口它们就会失效。

放进来的理由：做这个包时**反复需要"找素材、看别人怎么说"**，这几个脚本省了很多手工操作。
如果你的场景不涉及 B 站，可以整个目录删掉。

---

## B 站检索类（全部只读，不需要登录）

| 脚本 | 干什么 | 用法 |
|---|---|---|
| `bili-search.mjs` | 关键词搜视频（带 wbi 签名，含播放/弹幕数） | `node bili-search.mjs "关键词" [页码]` |
| `bili-up.mjs` | 查某个 UP 主的投稿列表 | `node bili-up.mjs <mid> [页数]` |
| `bili-related.mjs` | 顺藤摸瓜：多个种子视频的相关视频，去重汇总 | `node bili-related.mjs BVxxxx BVyyyy ...` |
| `bili-dm.mjs` | 抓某个视频的弹幕 | `node bili-dm.mjs BVxxxxx [段数]` |
| `bili-study.mjs` | 抓评论：按热门/最新，支持分页 | `node bili-study.mjs hot [页]` |

**共同特点**：

- **只读**，不登录、不发帖、不点赞
- cookie 是**运行时从 B 站首页现抓的匿名会话 cookie**（不是硬编码凭据）
- 输出 JSON 落在**当前目录**（相对路径），方便接着处理
- 需要 Node 18+（用全局 `fetch`），**无第三方依赖**

```bash
# 例子：搜素材 → 找相关 → 看评论
node bili-search.mjs "海阔天空" 1
node bili-related.mjs BV1WKg3z5Ehi BV1Tq4y1778D
node bili-study.mjs hot 2
```

> ⚠️ **请自行遵守 B 站的服务条款与 robots 约定**，控制请求频率。
> 这些脚本写来是为了少量取用素材，**不是爬虫框架**。

---

## `song-id.py` —— 旧命令入口

曲名识别实现已统一到 video-watch skill 的 `bgm-id.py`（四级兜底）。
本文件只是**保留旧命令名**，把参数原样转发过去。

```bash
python3 song-id.py <视频或音频> [更多文件...]
```

它按下面的顺序找实现，**都找不到时会给一句明确的报错**（不会静默失败）：

1. 环境变量 `VW_PY`（直接指向 `bgm-id.py`）
2. 环境变量 `VIDEO_WATCH_DIR`（指向 skill 目录）
3. 常见安装位置（`~/.dsh/skills/video-watch/` 等）
4. 本文件同级目录下的 `bgm-id.py`

---

## 与主包的关系

- 主包的四个工具（`vw-segment.mjs` / `bgm-id.py` / `audio-match.py` / `install-demucs*.sh`）
  在 `video-watch/` 目录下，**是核心交付物**；
- 本目录的东西**是可选的顺手工具**，删掉不影响主链路。
