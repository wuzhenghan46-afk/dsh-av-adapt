> **本机适配说明（DSH / Android 容器）**
>
> - 本 skill 由 DSH 从 ${SKILL_DIR} 加载。DSH 的 shell 工作目录是**工作区**而不是 skill 目录，所以下文所有调用 vw.py 的命令都已改写成绝对路径 `python3 ${SKILL_DIR}/vw.py …`，可直接复制执行。
> - **依赖已就绪**（2026-10-12 通过 `vw.py doctor` 自检）：Python 3.12.3、ffmpeg/ffprobe 6.1.1（含 `scdet`/`freezedetect`/`silencedetect`/`tblend`/`signalstats`/`tile`/`drawtext`，编译带 `libfreetype` + `libfontconfig`）、ImageMagick 6.9.12、Pillow 12.3.0、numpy 2.5.4、字体 DejaVuSans-Bold。
> - **可选依赖**：`faster-whisper` 1.2.1 **已装**（`asr` 语音转写可用，首次运行下载模型，`--model` 默认 `base`）；`rapidocr` **已装**（`ocr` 画面文字）。要补 OCR 时注意 Ubuntu 24.04 的 PEP 668 保护：`python3 -m pip install --break-system-packages --ignore-installed rapidocr-onnxruntime`（`--ignore-installed` 是为了绕过 apt 装的包没有 RECORD、pip 卸不掉的问题，装 faster-whisper 时踩过）。**没装 OCR 就别假装那条支路有结果**。
> - `--out` 用绝对路径或工作区相对路径。`probe` 会写出 `analysis_v.txt`（约 0.9 KB/帧，长视频很大），`timeline.json` 已含后续所需数据，那个中间产物可随时删。
> - **`asr` 首次运行必须走镜像**：本机直连 huggingface.co 不通（Network is unreachable），下载模型前先设环境变量 `HF_ENDPOINT=https://hf-mirror.com`，否则 `asr` 会挂在 `LocalEntryNotFoundError`。模型缓存到 `~/.cache/huggingface/`，下过一次之后就不再需要网络。
> - **`asr` 用本地模型最省事**：base 模型已下到 `/root/.cache/whisper-models/faster-whisper-base`，直接 `--model /root/.cache/whisper-models/faster-whisper-base` 加载，不走网络。本机 PyAV 只有 19.x（老版本没有 aarch64 wheel），它删掉了 `metadata_errors` 参数，而 faster-whisper 仍在用 —— 这个兼容补丁由本目录的 `adapt-dsh.sh` 维护，**pip 重装 faster-whisper 后要重跑一次该脚本**，否则 `asr` 会在解码音频时报 TypeError。
> - **粤语检测：base 模型做不到**（实测 Beyond《海阔天空》）。自动检测给出 `zh`（置信 0.894），
>   候选语言表里**根本没有 `yue`**（概率为 None）——即模型不认识粤语这个类别；强行 `--lang yue`
>   时那个"置信 1.00"是假象（指定语言时该值不代表检测），而且输出会崩坏成
>   `Prof Prof Prof…` 的重复 token。Whisper 官方支持 `yue`，但需要大模型（small 在本机下载失败）。
>   附带：转写成 `zh` 时仍能抓到可辨认的歌词（"從活有放你過心中的理想" ≈ "從沒有放棄過心中的理想"），
>   所以**内容识别部分有效，语言标注是错的**。另：`asr.json` 只存 `language`，没存屏幕上报的 `language_probability`。
> - **纯音乐/无人声素材上 `asr` 输出 `0 段` 是正确行为，不是失败**（实测：《三角洲行动》纯配乐剪辑）。`vad_filter=True` 会把没有语音活动的整段跳过，因此不会凭空编造歌词或台词。这种素材的"理解"只能靠画面图版 + `silencedetect`（0 个静音事件 = 全程连续音轨）+ 音频能量曲线（`ffmpeg -af astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level`）看段落结构；**曲名**用本目录的 `python3 ${SKILL_DIR}/bgm-id.py <视频>` 识别（工作区的 `song-id.py` 已改为转发到它，两个入口同一份实现）；AcoustID/chromaprint 那条路只覆盖正式发行唱片，独立发行与免版权曲常查不到，**风格与情绪**仍无法从视频本身得到（需音频分类模型）。
> - **`bgm-id.py` 的四级兜底链**（成本递增，命中即止）：① 整段 Shazam 识别 —— 实测能扛住「BGM 比人声低 15dB」的混合，且只要音频里有停顿，连 -25dB 的 BGM 都能从混合音里认出来；② 声道相减（仅立体声）；③ VAD 找语音空隙后逐段识别（**连续不停顿的口播没有空隙，会跳过**）；④ demucs 人声分离（未安装则跳过；装它要拉 PyTorch，下载约 200MB、占盘约 2GB，且高峰期货**实测容器出口只有 0.3~15 KB/s** 会卡很久 —— 跑 `bash ${SKILL_DIR}/install-demucs.sh` 让它后台慢慢下，进度看 `tail -f /tmp/demucs-install.log`）。**AcoustID 几乎不抗人声**：同一首曲子干净时命中 94.5%，混入说话立刻无匹配，所以它只做元数据补充，主力永远是 Shazam。
> - **策略4（demucs）实测价值有限，别指望它救场**：实测 BGM 比人声低 18dB 时**策略1 就直接识别成功**，而低到 -25dB 以下时，分离出的伴奏只剩 -48dB，无论怎么提音量，Shazam 都会**稳定误匹配到完全无关的曲子**（实测两次都指到同一首越南电音）—— 比识别不出来更糟。所以它的适用窗口只有 -18~-20dB 这个窄带，且 CPU 推理约 **8~10 倍实时**（25 秒素材要 4 分钟）。脚本已把增益限制在 +12dB 并在输出时打警告，**引用策略4的结果前必须回原片复核**。
> - **有硬字幕的中文视频，`ocr` 比 `asr` 准得多**：实测同一素材，ASR 把「纸扎人」听成「子杀人」、「素质极低」听成「数字极差」、「曹植」听成「超值」，而 OCR 全读对了，还能顺带捞出聊天记录截屏、商品包装字、水印等画面文字。所以**有字幕就先 OCR，没字幕才用 ASR**；但 OCR 仍有小错（实测把「某些主义」读成「耍起主义」），工具自己也会提示「引用前须裁切放大用视觉复核」——关键结论仍要回图版上亲眼确认。
> - **长视频（>10 分钟）不要直接 probe 全片**：用工作区的分段脚本先并行量测，`node skill 目录/vw-segment.mjs <视频> --segments N [--parallel P]`，它按关键帧切段（段间零重叠）、并行 probe、再把各段 timeline 合并成一份可直接喂 `plan`/`sheet` 的 `timeline.json`。实测 12 分钟素材 4 段 8 并发约 4 分钟，帧数与原始完全一致。
> - 已实测跑通 `probe → plan → sheet` 全链路（9 秒测试片：9 点骨架采样、3×3 图版、帧号与时间码烧录正常）。
> - 通过 `git pull` 更新本 skill 后，重跑 `bash ${SKILL_DIR}/adapt-dsh.sh` 恢复本适配。
