# 依赖清单：装这个包需要下载/安装的全部东西

> 本清单是**实测记录**，不是理论列表。每一项都写清：装它干什么、从哪来、多大、本机装到的版本。
> 体积是本机实测值（Android 容器 / aarch64），换平台会有出入。

**总下载量（必需项）约 1.1 GB**，其中 Chromium 547MB + torch 431MB 占掉八成。
如果不用浏览器自动化、也不用 demucs 人声分离，**必需项只剩约 60MB**。

---

## 一、系统包（apt）

| 包 | 用途 | 体积 | 本机版本 |
|---|---|---|---|
| `ffmpeg` | 抽帧、量测、切段、音视频读写（**全包的地基**） | ~80MB | 6.1.1-3ubuntu5 |
| `imagemagick` | 拼图版、差分合成 | ~15MB | 6.9.12 |
| `fonts-dejavu-core` | `drawtext` 烧帧号/时间码 | ~2MB | — |
| `fonts-noto-cjk` | **截图里的中文**（缺了全是豆腐块） | ~60MB | — |
| `libchromaprint-tools` | 提供 `fpcalc`，`bgm-id.py` 查 AcoustID 用 | ~1MB | 1.5.1 |
| `python3-pil`、`python3-numpy` | 图像与数组 | ~20MB | Pillow 12.3.0 / numpy 2.5.4 |
| `libnss3`、`libnspr4` | Chromium 运行时依赖 | ~5MB | — |

```bash
apt-get update
apt-get install -y ffmpeg imagemagick fonts-dejavu-core fonts-noto-cjk \
                   libchromaprint-tools python3-pil python3-numpy libnss3 libnspr4
```

> ⚠️ **ffmpeg 必须是完整构建**：需要 `scdet` `freezedetect` `silencedetect` `tblend`
> `signalstats` `tile` `drawtext` 七个滤镜，且 `drawtext` 要求编译时带 `libfreetype`。
> 缺了不会当场报错，而是**在抽取帧时失败**，离真正原因很远。`vw.py doctor` 会先校验。

---

## 二、Python 包（pip）

Ubuntu 24.04 有 PEP 668 保护，**装和卸都要加 `--break-system-packages`**。

### 必需（看视频的主链路）

| 包 | 用途 | 本机版本 |
|---|---|---|
| `faster-whisper` | `asr` 语音转写 | 1.2.1 |
| `rapidocr-onnxruntime` | `ocr` 画面文字（会带 `onnxruntime` 1.31.0） | 1.4.4 |
| `shazamio` | `bgm-id.py` 的曲名识别主力 | 0.8.1 |
| `soundfile` | 音频读写 | 0.14.0 |

```bash
python3 -m pip install --break-system-packages \
  faster-whisper rapidocr-onnxruntime shazamio soundfile
# apt 装的包 pip 卸不掉（RECORD 缺失），撞上就加 --ignore-installed
```

### 可选：`audio-match.py` 用

| 包 | 用途 | 本机版本 |
|---|---|---|
| `librosa` | 和声特征（chroma CQT）、DTW | 1.0.0 |

```bash
python3 -m pip install --break-system-packages librosa
```

### 可选：demucs 人声分离（策略4）

**这三个加起来约 500MB，而且价值有限**（适用窗口窄、CPU 推理慢、可能误匹配，详见 README 的限制章节）。
不装也能用，`bgm-id.py` 会自动跳过策略4 并提示。

| 包 | 用途 | 来源 | 体积 | 本机版本 |
|---|---|---|---|---|
| `torch` | 推理后端，**必须 CPU 版** | 阿里云镜像 | 104MB 下载 / 431MB 装完 | 2.9.1+cpu |
| `torchaudio` | 配套（**版本必须与 torch 一致**） | 阿里云镜像 | 0.5MB | 2.9.1 |
| `demucs` | 分离模型（**必须 4.0.1**） | PyPI 清华源 | ~1MB + 依赖 | 4.0.1 |

```bash
bash <skill 目录>/install-demucs.sh                        # 有网
bash <skill 目录>/install-demucs-offline.sh <wheel 目录>    # 离线
```

**为什么不能用 PyPI 的 torch**：PyPI 上的是 CUDA 版，`import` 时强制要 `libcublasLt.so`，
还会顺手拖 `nvidia-cublas`(223MB) + `nvidia-cusparse`(162MB) + `nvidia-cudnn` 等一堆包。
必须用 CPU 版：`https://mirrors.aliyun.com/pytorch-wheels/cpu/`

**为什么锁 demucs 4.0.1**：4.1+ 依赖 `sphn`，那个要用 Rust 编译，本机装不上。

### 离线包的准备（在有网的机器上）

```bash
# 把这两个 wheel 下下来带上（字节数可用于校验完整性）：
#   torch-2.9.1+cpu-cp312-cp312-manylinux_2_28_aarch64.whl   （104,126,462 字节）
#   torchaudio-2.9.1-cp312-cp312-manylinux_2_28_aarch64.whl  （    476,017 字节）
# 放同一个目录，然后在目标机器上：
bash install-demucs-offline.sh <那个目录>
```

---

## 三、模型（首次运行时自动下载，之后离线可用）

| 模型 | 给谁用 | 来源 | 体积 | 缓存位置 |
|---|---|---|---|---|
| faster-whisper `base` | `asr` 转写 | HuggingFace | **142MB** | `~/.cache/huggingface/` |
| `htdemucs` | demucs 策略4 | PyTorch Hub | **81MB** | `~/.cache/torch/` |

> ⚠️ **本机直连 huggingface.co 不通**，下 whisper 模型前必须设镜像：
> ```bash
> export HF_ENDPOINT=https://hf-mirror.com
> ```
> 不设会挂在 `LocalEntryNotFoundError`。下过一次之后就不再需要网络。
>
> 也可以用本地路径绕开网络：`--model /root/.cache/whisper-models/faster-whisper-base`

---

## 四、浏览器（只有用 web-access 才需要）

| 组件 | 来源 | 体积 | 本机版本 |
|---|---|---|---|
| Chromium | `npx playwright@1.40.1 install chromium` | **547MB** | **120.0.6099.28** |

```bash
npx playwright@1.40.1 install chromium
apt-get install -y libnss3 libnspr4 fonts-noto-cjk
```

> ⚠️ **必须是 Chromium 120，不能用新版**。新版在 Linux 5.10 内核上启动约 2 秒即 SIGTRAP 崩溃，
> 根因是它调用了 `memfd_create(..., MFD_NOEXEC_SEAL)`，而那两个常量是 Linux 6.3 才有的。
> 详细排查过程见 README 的踩坑章节。

---

## 五、外部服务与密钥

| 服务 | 用途 | 怎么来 | 必需？ |
|---|---|---|---|
| **Shazam** | `bgm-id.py` 主力曲名识别 | 经 `shazamio` 免密钥访问 | 装 shazamio 即可 |
| **AcoustID** | 补充规范元数据（曲名/艺术家/专辑/厂牌） | 自己去 <https://acoustid.org/new-application> 申请 app key | **可选** |

AcoustID 的 key 读取顺序：环境变量 `ACOUSTID_KEY` → `~/.dsh/acoustid-key.txt` → 脚本内置兜底。
**没有 key 也能跑**，只是少一路元数据补充。

> 实测：AcoustID **几乎不抗人声**——同一首曲子干净音频命中 94.5%，混进说话立刻无匹配。
> 所以它在混合场景只做补充，**主力永远是 Shazam**。

---

## 六、评测用的数据集（只有想复现评测才需要）

| 数据 | 来源 | 体积 | 说明 |
|---|---|---|---|
| SHS100K 元数据 | `https://raw.githubusercontent.com/NovaFrost/SHS100K/master/list` | 11MB | **只有元数据**（set_id / ver_id / 曲名 / 艺人 / YouTube URL / 可下载标记），**不含音频** |
| Covers80 仓库 | `https://github.com/percyvirus/Covers80_Percy` | 23KB | **只有脚本和清单，无音频** |

**结论：这两个数据集都不能直接用** —— 真实音频要另外从 YouTube 抓，而我们要测的场景
（同一首歌的不同版本）本来就得自己准备素材。**用自己熟悉的歌反而更好**：
你知道正确答案，才能判断工具给的结果对不对。

---

## 七、一键核对（装完之后跑）

```bash
# 看视频这条链路
python3 <skill 目录>/vw.py doctor          # 期望「必需项就绪」
python3 <skill 目录>/bgm-id.py <任意音频>   # 期望能报出曲名，或如实说没匹配

# 联网这条链路
node <web-access skill 目录>/scripts/check-deps.mjs   # 期望 proxy: ready (Chromium)
```

---

## 八、本机实测的完整版本快照

```
系统        Ubuntu 24.04 aarch64 · 内核 5.10.246-android12 · 8 核 · Python 3.12.3 · Node 22.23.2
ffmpeg      6.1.1-3ubuntu5（含全部所需滤镜 + libfreetype）
ImageMagick 6.9.12
Chromium    120.0.6099.28（Playwright 构建）
Python      faster-whisper 1.2.1 · rapidocr-onnxruntime 1.4.4 · onnxruntime 1.31.0
            shazamio 0.8.1 · soundfile 0.14.0 · librosa 1.0.0
            torch 2.9.1+cpu · torchaudio 2.9.1 · demucs 4.0.1
            numpy 2.5.4 · Pillow 12.3.0
模型        faster-whisper base 142MB · htdemucs 81MB
```

---

> **有 DSH 操作，仅供参考。** 本清单来自一台具体 Android 手机容器上的实测，
> 版本号与体积都是**当时**的值；换环境、隔一段时间，数字都会变。
> 拿它当"要装哪些东西"的索引可以，当"照抄就能跑"的保证不行。
