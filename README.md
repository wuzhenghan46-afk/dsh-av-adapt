# DSH 音视频能力适配包

在一台 **Android 手机上的 Linux 容器**里，把两个 Agent Skill（联网 + 看视频）真正跑起来，
并补齐它们缺的工具链。所有改动都经过实测，踩过的坑写在下面。

适配目标环境：Android 12 内核（Linux 5.10）+ Ubuntu 24.04 aarch64 容器，Agent 框架为 DSH。

---

## 一、解决什么问题

| 原始状态 | 问题 | 本包的解法 |
|---|---|---|
| **web-access** skill | 它的 CDP 通道假设你有桌面 Chrome/Edge；手机容器里没有 | 容器内自建 headless Chromium + 让它被 skill 正确发现 |
| **video-watch** skill | 命令按"在 skill 目录里运行"编写，DSH 的工作目录是工作区 | 批量改绝对路径 + 依赖补全（OCR/ASR） |
| 长视频 | probe 是线性成本（≈0.5×时长），1 小时要跑 30 分钟 | `vw-segment.mjs` 分段并行量测 + 时间戳合并 |
| 视频里的 BGM 是什么 | 无现成工具 | `bgm-id.py` 四级兜底识别 |
| 这段音频是不是某首歌改的 | 指纹识别答不了（处理过就匹配不上） | `audio-match.py` 和声层面的同源判定 + 参数反推 |

---

## 二、包含什么

```
web-access/          skill 的 DSH 适配补丁（路径替换 + 浏览器说明）
video-watch/         skill 的适配补丁 + 四个原创工具
chromium-autostart/  DSH 插件：开机后自动拉起 headless Chromium
```

### 四个原创工具（上游没有，本包提供）

| 工具 | 作用 | 关键实测数据 |
|---|---|---|
| `vw-segment.mjs` | 长视频按关键帧切段、并行 probe、合并成一份完整 timeline | 12 分钟素材分 4 段，合并后**帧数与原始一致（默认模式）**（18784 = 4696×4），时长偏差约 0.2~0.8%（= 1/(fps×段长)，段越短、帧率越低偏差越大） |
| `bgm-id.py` | 从视频/音频识别 BGM 曲名，四级兜底 | 能扛住「BGM 比人声低 15dB」；有停顿则 -25dB 也能认 |
| `audio-match.py` | 判定**同一录音是否被整体变速/变调**（**不是**同源判定，边界见下） | 锁定 Love Hurt beat ← 病名为爱：**放慢到约 94%、音高 −1 半音**，且两参数自洽（经 Shazam 裁决验证方向） |

---

## 三、快速开始

### 1. web-access（联网 + 浏览器自动化）

```bash
# 装 Chromium（Playwright 的构建，必须是 120，原因见坑#1）
npx playwright@1.40.1 install chromium

# 装运行时依赖 + 中文字体（缺字体截图会是豆腐块）
apt-get install -y libnss3 libnspr4 fonts-noto-cjk

# 启动浏览器（幂等）
bash /root/.dsh/web-access-browser/start-chromium.sh

# 让 skill 接管（config.env 只设 WEB_ACCESS_BROWSER=chromium，别设端口，见坑#3）
node <skill>/scripts/check-deps.mjs     # 期望输出 proxy: ready (Chromium)
```

### 2. video-watch（看视频）

```bash
apt-get install -y ffmpeg imagemagick libchromaprint-tools python3-pil python3-numpy
#   libchromaprint-tools 提供 fpcalc，bgm-id.py 查 AcoustID 要用

python3 -m pip install --break-system-packages faster-whisper rapidocr-onnxruntime
python3 -m pip install --break-system-packages shazamio soundfile librosa
#   shazamio  → bgm-id.py 的曲名识别主力（缺了它脚本会直接告诉你装什么）
#   soundfile → bgm-id.py 策略4 的音频读写层（demucs 自己不依赖它）
#   librosa   → audio-match.py 的和声特征
python3 -m pip install --break-system-packages --ignore-installed typing-extensions  # 见坑#12

bash <skill>/adapt-dsh.sh              # 应用路径适配（幂等）
python3 <skill>/vw.py doctor           # 应报「必需项就绪」
```

### 3. demucs（可选，人声分离）

```bash
bash <skill>/install-demucs.sh         # 见坑#15~17，脚本已固化正确路径
```

---

## 四、踩过的坑（完整清单）

### 浏览器 / CDP

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | **内核太老** | Chromium 156 启动约 2 秒 SIGTRAP 崩溃，gdb 显示 `brk #0`，strace 抓到 `memfd_create(..., MFD_NOEXEC_SEAL) = EINVAL` | 用 **Chromium 120**——它的构建基线早于 `MFD_NOEXEC_SEAL`/`F_SEAL_EXEC` 这两个 Linux 6.3 才有的常量（本机内核是 5.10） |
| 2 | **skill 变量不存在** | 上游用 `${CLAUDE_SKILL_DIR}` 定位脚本，DSH 不设这个变量 | 批量替换为绝对路径 |
| 3 | **固定端口模式握手失败** | `Received network error or non-101 status code` | 上游在 config-port 模式下 `wsPath` 为 null，兜底 URL 拼成 `/devtools/browser`（**缺 UUID**）。改用 DevToolsActivePort 文件发现：user-data-dir 设为 `~/.config/chromium`、端口用 `0` |
| 4 | **CDP 截图超时** | `Page.captureScreenshot` 挂死 | `--headless=new` 下后台 tab 不产帧 → 换 **`--headless=old`** |
| 5 | **没有 /dev/shm** | 容器 `/dev` 只读，`mount` 还是假成功（返回 0 但没挂上） | 必须 `--disable-dev-shm-usage`，且**不能**加 `--disable-gpu`（会让合成器停摆） |
| 6 | **中文字体缺失** | 截图里中文全是豆腐块 | `apt install fonts-noto-cjk` |

### 视频分析

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 7 | **probe 是线性成本** | 3 分钟素材 97 秒，外推 1 小时约 31 分钟 | 分段并行；实测 4 段 8 并发冷机 233s、连续满载后 396s（vs 串行约 390s；热节流影响很大，别按最好那次做计划） |
| 8 | **切片重叠导致时间戳漂移** | 逐个 `-ss … -c copy` 切段，每段起点对齐到**更早**的关键帧 → 4 段累计多 2.4 秒、31 帧 | 改用 ffmpeg **segment muxer**（按关键帧切、零重叠、一次切完、可读出实测偏移） |
| 9 | **吸附后又被取整** | 段边界吸附到关键帧后又 `Math.round` 回整秒，等于白吸附 | 保留关键帧精确时间；偏差从 +1.35s 降到 -0.12s |
| 10 | **纯音乐被 ASR 硬编** | 担心 Whisper 在纯音乐上产生幻觉文本 | 实测 `vad_filter=True` 会正确跳过 → 输出 `0 段` 是**正确行为**，不是失败 |
| 11 | **OCR 与 ASR 谁准** | — | 实测有硬字幕的中文视频 **OCR 明显更准**：ASR 把「纸扎人」听成「子杀人」、「素质极低」听成「数字极差」，OCR 全对。所以**有字幕先 OCR，没字幕才 ASR** |
| 12 | **AcoustID 不抗人声** | 同一首曲子干净音频命中 94.5%，混入说话立刻无匹配 | 混合场景主力永远用 Shazam；AcoustID 只做元数据补充 |

### Python 工具链

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 13 | **apt 装的包 pip 卸不掉** | `Cannot uninstall typing_extensions: RECORD file not found` | pip 加 `--ignore-installed` |
| 14 | **PyAV 19 删了参数** | `av.open() got an unexpected keyword argument 'metadata_errors'`，而 faster-whisper 还在用 | 打 try/except shim（老版本 PyAV 全无 aarch64 wheel，降级无路）；脚本化在 `adapt-dsh.sh` 里 |
| 15 | **PyPI 的 torch 是 CUDA 版** | import 时强制要 `libcublasLt.so`；还会拖 cublas 223MB + cusparse 162MB + cudnn | 用 **CPU 版**：`https://mirrors.aliyun.com/pytorch-wheels/cpu/`（1.1~1.5 MB/s）；官方 download.pytorch.org 实测掉到 18 KB/s |
| 16 | **torchaudio 2.9 移除了音频 I/O** | demucs 保存报 `TorchCodec is required`，而 torchcodec 的 .so 本机加载不了 | 自己用 ffmpeg+soundfile 读写，**只调 demucs 的模型推理** |
| 17 | **新版 demucs 依赖 `sphn`** | 要用 Rust 编译，失败 | 退回 **demucs 4.0.1** |
| 18 | **分离后猛提音量会误匹配** | BGM -30dB 的样本分离后伴奏只剩 -48dB，提 33dB 后 Shazam **误匹配到一首无关的越南电音**（两次都指到同一首）| 增益限幅 +12dB，且输出强制警告「必须回原片复核」——**误匹配比识别不出来更危险** |
| 19 | **高峰期 pip 索引龟速** | 索引页实测 0.3~15 KB/s，30 秒 0 增长 | 大文件一律 `curl -C -` 断点续传，比 pip 稳 |

---

## 五、环境要求

- Ubuntu 24.04 aarch64（或同类 ARM64 Linux 容器）
- Python 3.12、Node 22+、ffmpeg ≥ 5.1（需含 `scdet`/`freezedetect`/`silencedetect`/`tblend`/`signalstats`/`tile`/`drawtext`，且编译带 libfreetype）
- ImageMagick 6.x/7.x
- 磁盘：Chromium 约 540MB、torch CPU 版约 430MB（装完实测）、demucs 模型约 81MB（首次跑策略4 时下载）

---

## 六、验证方法

```bash
python3 <skill>/vw.py doctor                    # 依赖自检
node <skill>/vw-segment.mjs <视频> --segments 2 # 分段并行
python3 <skill>/bgm-id.py <视频>                 # BGM 识别
python3 <skill>/audio-match.py <待判定> <参考> --baseline <无关素材>   # 基线必须用 --baseline，位置参数会被当成另一首参考曲
```

---

## 七、已知限制

- **无登录态**：容器浏览器是全新的，需要账号的站点（小红书、微博等）处于未登录状态，遇到登录墙要如实告知用户。
- **demucs 策略价值有限**：适用窗口只有 -18~-20dB 窄带，CPU 推理约 8~10 倍实时，且结果**可能误匹配**。
- **`find-url.mjs` 不可用**：它读本地 Chrome 书签/历史，容器里没有数据源。
- **并行的速度收益受热节流影响**：同一配置冷机 233s、连续满载后 396s。
- **超长素材（1 小时以上）未实测**：目前验证到的最长素材是 751 秒（12.5 分钟，4 段并行 142 秒完成）。
  1 小时以上的行为**未经实测** —— 机制上分段的段数与耗时都随长度线性增长，没有硬编码上限，
  但内存、磁盘峰值与热节流后的实际耗时都没有数据支撑。要用于小时级素材请先小规模试跑。
- **`audio-match.py` 需要无关素材做基线**：裸相似度没有语义（实测把 query 音高移位后相似度反而更高），没有基线就别下结论。

### `audio-match.py` 的实测边界（重要，别高估它）

**它只能判「这段音频是不是那段音频本身被整体变速/变调处理过」，不能判「两段录音是不是同一首歌」。**

实测（同一组 60 秒素材，绝对相似度，基线均 ≈0.59）：

| 场景 | 绝对相似度 | 能不能判 |
|---|---|---|
| **同一录音**变速 0.94× | **0.9567** | ✅ 清晰可判 |
| 同一首歌 · 现场版 | 0.8000 | ⚠️ |
| 同一首歌 · 女声翻唱 | 0.7271 | ⚠️ |
| 无关曲目 A | 0.6390 | ⚠️ |
| 无关曲目 B | **0.7399** | ❌ 比同源翻唱还高 |

「同歌不同录音」与「无关曲目」的分数**区间重叠**，因此无法判别。

**基线校正的"增益"更不能当判据**：同一条无关对换三种基线，增益分别是
+0.0466 / +0.1678 / +0.7530（差 15 倍），而且三种基线下**无关对的增益都高于同源对**
——它主要反映基线素材本身。

根因：没有按节拍对齐（beat-synchronous chroma）、没有 DTW。
标准做法属于 cover song identification（Covers80 / SHS100K 那类任务）。
我试做过一个 beat-sync + DTW 的最小原型，判别力同样不足
（最无关的一对反而得 0.82，高于同录音变速的 0.62），且归一化实现本身有疑点
—— 要真正解决需要完整实现 + 在真实数据集上评测，不是能顺手补上的东西。
