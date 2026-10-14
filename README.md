# DSH 音视频能力适配包 —— **仅适用于 Android**

> 在一台 **Android 手机上的 Linux 容器**里，让两个 Agent Skill（联网 + 看视频）真正跑起来，
> 并补齐它们缺的工具链。
>
> **⚠️ 本包的一切都是针对 Android 容器环境做的**：内核 5.10、aarch64、没有 `/dev/shm`、
> 通过 proot 跑、Python 受 PEP 668 保护。**换到普通 x86 服务器或桌面 Linux 上，
> 里面大部分"坑"根本不存在，而本包的部分取舍（比如 Chromium 必须用 120）反而会成为负担。**
> 如果你是桌面/服务器环境，直接用上游 skill 即可，不需要本包。

- **适配目标**：Android 12 内核（Linux 5.10）+ Ubuntu 24.04 aarch64 容器，Agent 框架为 DSH
- **实测状态**：核心链路全部跑通并留证；未验证项在「已知限制」里逐条列出，不含糊
- **依赖清单**：见 [DEPENDENCIES.md](DEPENDENCIES.md)（装什么、从哪来、多大，都写了）

---

## 一、起源：这个包是怎么长出来的

**一句话**：本来是"看到别人写了个 skill，想在手机上试试"，结果是**一路修到能真正干活**，
沿途的每一步都留了实测记录。

完整时间线：

| 阶段 | 起点 | 撞上了什么 |
|---|---|---|
| **① 起因** | 看到 [eze-is/web-access](https://github.com/eze-is/web-access) 这个浏览器自动化 skill | 它假设你有桌面 Chrome/Edge，Android 容器里**根本没有浏览器**。于是自建 headless Chromium —— 然后发现 **Chromium 156 在 5.10 内核上启动 2 秒就 SIGTRAP**，一路查到 `memfd_create(MFD_NOEXEC_SEAL)` 是 Linux 6.3 才有的常量，最终退回 **Chromium 120** |
| **② 接着** | 想验证 [video-watch](https://github.com/CFITCorporation/video-watch-skill) 能不能看懂一段**纯音乐无人声**的视频 | 它的命令按"在 skill 目录里运行"写，而 DSH 的工作目录是工作区 → 批量改绝对路径；又发现 OCR/ASR 都没装 |
| **③ 补工具链** | 要转写语音、要读画面文字 | 装 faster-whisper（**HF 直连不通**，得走 hf-mirror）、装 rapidocr |
| **④ 长视频** | probe 是线性成本，1 小时素材要跑 30 分钟 | 写 `vw-segment.mjs` 分段并行 —— 又踩了切段重叠、时间戳吸附后又被取整两个坑 |
| **⑤ 想知道 BGM 是什么** | 上游没有这个能力 | 写 `bgm-id.py`（四级兜底）；为了确认"这首歌是不是变速减人声过的"，又写 `audio-match.py` |
| **⑥ 装 demucs** | 策略4 要人声分离 | PyPI 的 torch 是 CUDA 版、新版 demucs 要 Rust 编译、torchaudio 2.9 砍了音频 I/O —— 连撞三个 |
| **⑦ 审查** | 自己审不出自己的 bug | 请了第三方审（Cloud、Grok）和独立子 agent 复查，**每一轮都翻出真 bug**；其中一次翻出 **`silences` 结构理解错导致的致命崩溃**，另一次是**方向搞反了**（把"放慢 6%"写成"加速 6%"）。最后用 Shazam 做裁决才定案 |
| **⑧ 干净环境重装** | "新机器能不能照着装起来？" | 把依赖全卸掉重跑，**又抓到 3 个真 bug**（装 demucs 没加 `--no-deps` 导致 torchaudio 被换成不配套版本，等等） |
| **⑨ 方法论修正** | 用一条真实素材做端到端测试 | 发现"只跑 ASR 就下结论"是错的 —— 见下文「看图与听声必须同时做」 |

**这个包真正的价值不是那几个脚本，而是这份记录**：每一条坑都写清了现象、根因、解法，
以及"我是怎么发现的"。**其中大约一半的 bug 是我自己写的时候没想到、被测出来才知道的。**

---

## 二、致谢与来源

**没有下面这些项目，这个包不存在。**

### 直接来源（本包的适配对象）

| 项目 | 与本包的关系 | 地址 |
|---|---|---|
| **web-access** | **本包一切的起点 / 创意来源** —— 最初就是因为看到它，才想到在这台手机上做浏览器自动化 | <https://github.com/eze-is/web-access> |
| **video-watch** | **本包主要适配与借鉴的对象** —— 它的 `probe → plan → sheet` 量测框架是本包所有视频工具的地基 | <https://github.com/CFITCorporation/video-watch-skill> |

本包对这两个项目的改动**都是补丁形式**（`adapt-dsh.sh` 幂等应用 + 新增独立文件），
**不覆盖、不改写它们的原有实现**，`git pull` 上游更新后重跑适配脚本即可。
上游各自的许可证以它们原仓库为准。

### 工具与服务（本包依赖它们干活）

| 用途 | 项目 / 服务 |
|---|---|
| 语音转写 | [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（Whisper 的 CTranslate2 实现） |
| 画面文字 | [RapidOCR](https://github.com/RapidAI/RapidOCR) |
| 曲名识别 | [Shazam](https://www.shazam.com/)（经 [shazamio](https://github.com/dotX12/ShazamIO) 免密钥访问） |
| 音频指纹 | [Chromaprint](https://github.com/acoustid/chromaprint) / [AcoustID](https://acoustid.org/) |
| 人声分离 | [Demucs](https://github.com/adefossez/demucs) |
| 音频分析 | [librosa](https://librosa.org/) |
| 音视频处理 | [FFmpeg](https://ffmpeg.org/) · [ImageMagick](https://imagemagick.org/) |
| 浏览器 | [Chromium](https://www.chromium.org/)（Playwright 构建） |
| CPU 版 torch | [阿里云 pytorch-wheels 镜像](https://mirrors.aliyun.com/pytorch-wheels/cpu/) |
| 模型镜像 | [hf-mirror.com](https://hf-mirror.com/) |
| 评测数据集（**仅元数据**） | [SHS100K](https://github.com/NovaFrost/SHS100K) · [Covers80](https://github.com/percyvirus/Covers80_Percy) |

---

## 三、包含什么

```
README.md                 本文件
DEPENDENCIES.md           依赖清单：装什么、从哪来、多大
web-access/               web-access skill 的 DSH 适配补丁
video-watch/              video-watch skill 的适配补丁 + 四个原创工具
chromium-autostart/       DSH 插件：开机自动拉起 headless Chromium
extras/                   途中顺手写的辅助脚本（B 站检索/评论/弹幕、旧命令转发）
```

### 四个原创工具（上游没有）

| 工具 | 作用 | 关键实测数据 |
|---|---|---|
| **`vw-segment.mjs`** | 长视频按关键帧切段、并行 probe、合并成一份完整 timeline | 12 分钟素材分 4 段，合并后**帧数与原始完全一致**（18784 = 4696×4） |
| **`bgm-id.py`** | 从视频/音频识别 BGM，四级兜底（整段 → 声道相减 → VAD 找空隙 → 人声分离） | 能扛住「BGM 比人声低 15dB」；音频里有停顿则 −25dB 也能认出来 |
| **`audio-match.py`** | 判定**同一录音是否被整体变速/变调**（**不是**同源判定，边界见下） | 定案 Love Hurt beat ← 病名为爱：**放慢到约 94%、音高 −1 半音**，两参数自洽（方向经 Shazam 裁决验证） |
| **`install-demucs*.sh`** | demucs 在线/离线安装（绕开 CUDA 版 torch 与 Rust 依赖） | 干净环境重装实测过，含 `--no-deps` 与版本一致性检查 |

---

## 四、快速开始

### 1. 看视频（video-watch）

```bash
# 系统依赖
apt-get install -y ffmpeg imagemagick fonts-dejavu-core libchromaprint-tools python3-pil python3-numpy

# Python 依赖
python3 -m pip install --break-system-packages \
  faster-whisper rapidocr-onnxruntime shazamio soundfile librosa

# 应用适配（幂等，可重复跑）
bash <skill 目录>/adapt-dsh.sh

# 自检
python3 <skill 目录>/vw.py doctor          # 期望「必需项就绪」
```

### 2. 联网 + 浏览器自动化（web-access）

```bash
npx playwright@1.40.1 install chromium      # 必须 120，原因见踩坑章节
apt-get install -y libnss3 libnspr4 fonts-noto-cjk
bash /root/.dsh/web-access-browser/start-chromium.sh
node <skill 目录>/scripts/check-deps.mjs    # 期望 proxy: ready (Chromium)
```

### 3. 人声分离（可选，价值有限）

```bash
bash <skill 目录>/install-demucs.sh         # 约 500MB，装完读一遍它的警告
```

---

## 五、踩过的坑（完整清单）

### 浏览器 / CDP

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | **内核太老** | Chromium 156 启动约 2 秒 SIGTRAP 崩溃，gdb 显示 `brk #0`，strace 抓到 `memfd_create(..., MFD_NOEXEC_SEAL) = EINVAL` | 用 **Chromium 120**——它的构建基线早于 `MFD_NOEXEC_SEAL`/`F_SEAL_EXEC`（Linux 6.3 才有的常量），本机内核是 5.10 |
| 2 | **skill 变量不存在** | 上游用 `${CLAUDE_SKILL_DIR}` 定位脚本，DSH 不设这个变量 | 批量替换为绝对路径 |
| 3 | **固定端口模式握手失败** | `Received network error or non-101 status code` | 上游在 config-port 模式下 `wsPath` 为 null，兜底 URL 拼成 `/devtools/browser`（**缺 UUID**）。改用 DevToolsActivePort 文件发现：user-data-dir 设 `~/.config/chromium`、端口用 `0` |
| 4 | **CDP 截图超时** | `Page.captureScreenshot` 挂死 | `--headless=new` 下后台 tab 不产帧 → 换 **`--headless=old`** |
| 5 | **没有 `/dev/shm`** | 容器 `/dev` 只读，`mount` 还假成功（返回 0 但没挂上） | 必须 `--disable-dev-shm-usage`；且**不能**加 `--disable-gpu`（会让合成器停摆） |
| 6 | **中文字体缺失** | 截图里中文全是豆腐块 | `apt install fonts-noto-cjk` |

### 视频分析

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 7 | **probe 是线性成本** | 3 分钟素材 97 秒，外推 1 小时约 31 分钟 | 分段并行；实测 4 段 8 并发冷机 233s、连续满载后 396s（vs 串行约 390s）——**热节流影响很大，别按最好那次做计划** |
| 8 | **切片重叠导致时间戳漂移** | 逐个 `-ss … -c copy` 切段，每段起点对齐到**更早**的关键帧 → 4 段累计多 2.4 秒、31 帧 | 改用 ffmpeg **segment muxer**（按关键帧切、零重叠、一次切完、可读出实测偏移） |
| 9 | **吸附后又被取整** | 段边界吸附到关键帧后又 `Math.round` 回整秒，等于白吸附 | 保留关键帧精确时间；偏差从 +1.35s 降到 −0.12s |
| 10 | **纯音乐被 ASR 硬编** | 担心 Whisper 在纯音乐上产生幻觉文本 | 实测 `vad_filter=True` 会正确跳过 → 输出 `0 段` 是**正确行为**，不是失败 |
| 11 | **VAD 的 pad 会吃掉空隙** | `VadOptions.speech_pad_ms` 默认 400，把语音两端各外扩 0.4 秒，实测把 3.00 秒的静音报成 2.46 秒 | 显式设 `speech_pad_ms=0`；改后同一素材报 3.26 秒且**起点分毫不差** |
| 12 | **"有字幕没字幕"决定用哪个通道** | — | 实测有硬字幕的中文视频 **OCR 明显更准**：ASR 把「纸扎人」听成「子杀人」、「素质极低」听成「数字极差」，OCR 全对。**有字幕先 OCR，没字幕才 ASR** |
| 13 | **AcoustID 不抗人声** | 同一首曲子干净音频命中 94.5%，混入说话立刻无匹配 | 混合场景主力永远用 Shazam；AcoustID 只做元数据补充 |

### Python 工具链

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 14 | **apt 装的包 pip 卸不掉** | `Cannot uninstall typing_extensions: RECORD file not found` | pip 加 `--ignore-installed` |
| 15 | **装卸都要 `--break-system-packages`** | PEP 668 对 install 和 uninstall 一视同仁，漏了直接 `externally-managed-environment` 退出 | 回滚命令也要带上它 |
| 16 | **PyAV 19 删了参数** | `av.open() got an unexpected keyword argument 'metadata_errors'`，而 faster-whisper 还在用 | 打 try/except shim（老版 PyAV 无 aarch64 wheel，降级无路）；由 `adapt-dsh.sh` 维护 |
| 17 | **PyPI 的 torch 是 CUDA 版** | import 时强制要 `libcublasLt.so`；还会拖 cublas 223MB + cusparse 162MB + cudnn | 用 **CPU 版**：`https://mirrors.aliyun.com/pytorch-wheels/cpu/`（1.1~1.5 MB/s）；官方 download.pytorch.org 实测掉到 18 KB/s |
| 18 | **torchaudio 2.9 移除了音频 I/O** | demucs 保存报 `TorchCodec is required`，而 torchcodec 的 .so 本机加载不了 | 自己用 ffmpeg+soundfile 读写，**只调 demucs 的模型推理** |
| 19 | **新版 demucs 依赖 `sphn`** | 要用 Rust 编译，失败 | 退回 **demucs 4.0.1** |
| 20 | **分离后猛提音量会误匹配** | BGM −30dB 的样本分离后伴奏只剩 −48dB，提 33dB 后 Shazam **误匹配到一首无关的越南电音**（两次都指到同一首）| 增益限幅 +12dB，且输出强制警告「必须回原片复核」——**误匹配比识别不出来更危险** |
| 21 | **高峰期 pip 索引龟速** | 索引页实测 0.3~15 KB/s，30 秒 0 增长 | 大文件一律 `curl -C -` 断点续传，比 pip 稳 |

### 安装脚本本身（干净环境重装才暴露的）

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 22 | **装 demucs 时没加 `--no-deps`** | demucs 与 openunmix 都声明了 torch/torchaudio 依赖，放开解析后 pip 把已对齐的 **torchaudio 2.9.1 换成 PyPI 的 2.11.0** → 与 torch 2.9.1 不配套，原生库加载失败（报错还很隐晦：`Could not load this library: …/_torchaudio*.so`）| 第③步加 `--no-deps`，另加 torch/torchaudio 版本一致性检查 |
| 23 | **版本锁定的回退分支会装错** | 找不到锁定版本时脚本回退到"最新"，而"最新"恰恰是不配套的那个 | 一致性检查不通过就明确报错并给出重装命令，不静默继续 |
| 24 | **HTML 实体转义** | 镜像索引里 `+` 被写成 `&#43;`，而解码 `sed` 写在 `grep` **之后** → 正则永远匹配不到，静默走回退分支装错版本 | 顺序改成**先解码再匹配** |
| 25 | **proot 下解包极慢** | torch 有 1.3 万个文件，每个系统调用都被 ptrace 拦一次，实测解包 **10 分钟以上**（看起来像卡死） | 耐心等；跑之前留 3GB 以上可用内存 |
| 26 | **内存吃紧时 pip Bus error** | 可用内存掉到 ~300MB 且另有构建任务时，pip 解包过程 SIGBUS 崩溃 | `set -e` 会正确中止；别在有重活时装它 |

### 工具自身的逻辑 bug（被第三方审出来的）

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 27 | **`silences` 结构理解错**（致命） | 它是 `[{kind, t}]` 而不是数字数组，当成数字处理会在**任何有静音的素材上崩**（12 分钟口播必然命中），且崩在探针跑完之后，错误还被归因成"素材损坏" | 按对象映射 `{...e, t: +(e.t + off).toFixed(2)}` |
| 28 | **方向搞反了** | 报告里写"加速 6.4% / +1 半音"，实际是**放慢到 94% / −1 半音** | 造已知方向的样本验证；最终用 Shazam 裁决定案。**签了名的结论方向错，比算错数更严重** |
| 29 | **"声称修了但没生效"** | 死参数（改了三元表达式的 `:` 分支，实际走 `?` 分支）、漏改的调用点、`try` 只包了 import 没包推理 | 改完必须**让那个分支必定执行一遍**，不是"跑一下没崩" |

---

## 六、看图与听声必须同时做（方法论）

**素材**：`BV185pu6PEeF`（央视 PX「词条保卫战」报道的二次解说）。

**只做 ASR 会得到什么**（我先走的就是这条路）：

| 实际说的是 | ASR 听成 |
|---|---|
| 词条 | `次調` / `紫條` / `磁條` / `紙條`（同一个词四种错法）|
| 低毒 | 低**讀** |
| 百度百科 | 百分之**調** |
| 对二甲苯 | 對二**加本** |

**把画面调出来看之后纠正的**：

- 年份是 **2014**（词条编辑记录页上印着），不是我以为的 2011
- 改词条的人叫 **亚豆亚豆**，时间 **2014-03-30**
- 页面上写的是「**巨毒**」，解说里说的是"剧毒"（同音，得靠画面定夺）
- 受访者是 **毕啸天**（标题里的"毕导"）

**反过来画面给不出什么**：画面只回答"是什么、写了什么"，说不出"两边在打什么拉锯"——
那层叙事只能靠听。**两条通道各修了对方三处错。**

**两个结论**：

1. **流程上必须并行取证**：`grid` 图版 + `asr` 转写**一起出**，然后交叉核对。
   **不要先转写完再去看画面**——串行时人会满足于"已经知道讲了什么"，就不再去看了。
2. **`ocr` ≠ 看过画面**。`ocr` 是把画面文字抠成字符串，拿到的是**词**；
   场面信息（"这是翻拍屏幕"这个形式本身、蓝框标出的那条时间戳、实验室里的手、
   黑底白字的人名卡）只有真的看图才有。

---

## 七、实测边界与已知限制

**先说总的一句：本包能"看"和"听"，但它给的是证据，不是判断。凡是它给出的结论，都要问一句"这个结论的边界在哪"。**

### 7.1 环境限制（换环境即消失）

- **仅适用于 Android 容器**：Chromium 必须用 120、`--disable-dev-shm-usage`、proot 下装包极慢
  —— 这些都是本环境的产物，不是通用做法。
- **无登录态**：容器浏览器是全新的，需要账号的站点（小红书、微博等）处于未登录状态，
  遇到登录墙只能如实告知，**不要假装看到了**。
- **`find-url.mjs` 不可用**：它读本地 Chrome 书签/历史，容器里没有数据源。

### 7.2 能力边界（这是重点）

| 能力 | 能做什么 | **不能做什么** |
|---|---|---|
| **`bgm-id.py`** | 从混合音频里认出 BGM 是哪首曲子 | **不能判断人声部分是谁的**。实测案例（`BV1N2aV6wELJ`）：人声是自己录的 rap，但伴奏采样了 `Struggle Baby — Glizzy Gwapo`，工具照常报出那首歌——它匹配的是**伴奏的指纹**。见下 |
| **`audio-match.py`** | 判"这段音频是不是**那段录音本身**被整体变速/变调处理过" | **不能判"两段录音是不是同一首歌"**。实测：同一首歌的现场版 0.80、翻唱 0.73，而**无关曲目能到 0.74**——区间重叠，分不开。见下 |
| **`asr`** | 转写语音、给时间戳 | **粤语检测做不到**（base 模型候选语言里根本没有 `yue`，会把粤语判成 `zh`）；唱歌的歌词识别很差；型号越大越准但下载越慢 |
| **`ocr`** | 读画面里的硬字幕（中文视频上比 ASR 准） | **不是"看过画面"**；同音字仍会错（实测把「某些主义」读成「耍起主义」），关键结论必须回原图复核 |
| **demucs（策略4）** | 极端情况下的人声分离 | **适用窗口只有 −18~−20dB 这个窄带**；CPU 推理约 **8~10 倍实时**；低于 −25dB 时**会稳定误匹配到无关曲目**——比识别不出来更糟 |
| **`vw-segment.mjs`** | 长视频分段并行量测 | 并行收益受**热节流**影响：同一配置冷机 233s、连续满载后 396s |

### 7.3 Shazam 报的是「伴奏的出处」，不是整个作品

实测案例（`BV1N2aV6wELJ`）：视频里人声是自己录的 **rap**，但**伴奏直接采样**了
`Struggle Baby (feat. Yung Lyon) — Glizzy Gwapo`。工具照常报出那首歌，因为它匹配的是**伴奏的音频指纹**。

**看到匹配结果先分清两种情况——它们的正确解读完全不同**：

- **整段就是那首歌** → 结果说明音频来源
- **借了那首歌的伴奏**（rap / 翻唱 / remix / 二次创作）→ 结果**只说明 beat 的来源**，
  人声是谁的、是不是原创，**这个工具答不了**

### 7.4 `audio-match.py` 的实测边界

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
`+0.0466 / +0.1678 / +0.7530`（差 15 倍），而且三种基线下**无关对的增益都高于同源对**
——它主要反映基线素材本身。

**根因**：没有按节拍对齐（beat-synchronous chroma）、没有 DTW。
这属于 cover song identification（Covers80 / SHS100K 那类任务）。
我试做过 beat-sync + DTW 的最小原型，**判别力同样不足**（最无关的一对反而得 0.82，
高于同录音变速的 0.62），且归一化实现本身有疑点 ——
**要真正解决需要完整实现 + 在真实数据集上评测，不是能顺手补上的东西。**

### 7.5 未验证项（如实列出）

- **1 小时以上的素材**：目前验证到的最长是 751 秒（12.5 分钟，4 段并行 142 秒完成）。
  机制上分段数与耗时随长度线性增长、没有硬编码上限，但内存峰值、磁盘峰值与热节流后的
  实际耗时**都没有数据支撑**。要用于小时级素材请先小规模试跑。
- **demucs 的真实分离**：四包可用性与原生库加载已验证（`demucs_available()` 为 True），
  但**没跑过完整的端到端分离**——测试素材在策略1 就命中了，没走到策略4。
- **粤语转写**：检测做不到（见 7.2），转写更没有验证。
- **多人对话 / 强噪声场景**：未测。

---

## 八、验证方法

```bash
# 视频链路自检
python3 <skill 目录>/vw.py doctor

# 四个工具各自的最小验证
node    <skill 目录>/vw-segment.mjs <视频> --segments 2          # 分段并行
python3 <skill 目录>/bgm-id.py <视频>                             # BGM 识别
python3 <skill 目录>/audio-match.py <待判定> <参考> --baseline <无关素材>
#        ↑ 基线必须用 --baseline；写成位置参数会被当成另一首参考曲

# 联网链路自检
node <web-access skill 目录>/scripts/check-deps.mjs               # 期望 proxy: ready (Chromium)
```

---

## 九、环境要求

- **Android 容器**（本包的前提）：Linux 5.10 内核、aarch64、容器 `/dev` 只读、Python 受 PEP 668 保护
- Ubuntu 24.04 aarch64（或同类 ARM64 Linux 容器）
- Python 3.12、Node 22+
- ffmpeg ≥ 5.1（需含 `scdet`/`freezedetect`/`silencedetect`/`tblend`/`signalstats`/`tile`/`drawtext`，且编译带 libfreetype）
- ImageMagick 6.x/7.x
- 磁盘：Chromium 约 547MB、torch CPU 版约 431MB、whisper base 模型 142MB、demucs 模型 81MB

---

## 十、许可

- **本包的原创部分**（四个工具、适配脚本、文档）：可自由使用、修改、分发。
- **上游两个 skill**（web-access、video-watch）：版权与许可**以它们各自原仓库为准**，
  本包只提供补丁，不重新分发它们的代码。使用前请自行确认上游许可。
- **第三方工具与服务**（FFmpeg、Chromium、faster-whisper、Demucs、Shazam、AcoustID 等）：
  各自遵循其原有许可与服务条款，尤其注意 **Shazam / AcoustID 属于第三方服务，有各自的调用条款**。

---

## 附：这个包是怎么被审出来的

自测能发现的只有表层问题（路径、语法、异常输入）。
**下面每一个"要理解数据或语义才能看见"的 bug，都是第三方审出来的**：

| 谁发现的 | 发现了什么 |
|---|---|
| 我自己 | 硬编码路径、语法、幂等性、死代码、异常输入 —— 全是表层的 |
| Cloud / Grok 复核 | 方向性错误（致命）、残留数据、桶撞车、网络错误归因、VAD pad、方向打印把理论值当测量值 |
| 独立子 agent 复查 | **`silences` 结构理解错导致的致命崩溃**、两处"声称已修但没生效" |
| 干净环境重装 | 装 demucs 缺 `--no-deps` 导致 torchaudio 被换、HTML 转义导致静默装错版本 |
| 用真实素材做端到端 | "只跑 ASR 就下结论"是错的（见第六节） |

**结论：写完之后自己"验收"是这套流程里最弱的环节。**
有效的做法是——**读上游源码**（不靠记忆）、**造对抗性素材**（不用自己挑的用例）、
**让那个分支必定执行一遍**（不是"跑一下没崩"）。

---

## 免责声明

> # 有 DSH 操作，仅供参考

**本包包含大量针对 DSH 这个 Agent 框架的具体操作**，换到别的框架不能直接照搬，例如：

- `~/.dsh/skills/<名字>/` 的目录约定、`~/.dsh/AGENTS.md` 的加载方式
- `adapt-dsh.sh` 对 skill 文件就地改写（路径替换、PyAV 补丁）
- `chromium-autostart/` 是**装成 DSH 插件**才生效的（注册在 profile 的 `dsh.profile.bundles` 里）
- 各处出现的 `${SKILL_DIR}`、`VW_PY`、`VIDEO_WATCH_DIR` 等本包自定义的约定

**以及，本包是一台具体手机上的、一次具体适配的记录**——不是产品，不是库，也不承诺维护。

所有结论都来自实测，**但实测是有边界的**：未验证项已在「七、实测边界与已知限制」里逐条列出，
那些不是客套话，是真的没测过。

**请把本包当作"别人踩过的坑的笔记"来读，而不是当作可以直接依赖的方案。**
引用其中任何命令之前，先确认它适用于你自己的环境（尤其是**内核版本、CPU 架构、Python 版本**）。
