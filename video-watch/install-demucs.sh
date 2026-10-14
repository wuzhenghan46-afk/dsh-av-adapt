#!/usr/bin/env bash
# 安装 demucs（bgm-id.py 的策略 4）。
# 注意：torch/torchaudio 取阿里云镜像上的**最新** CPU 版（用 sort -V 选），
# 不锁定具体小版本 —— 实测过的组合是 torch 2.9.1+cpu / torchaudio 2.9.1，
# 但镜像更新后脚本会装到更新版本；demucs 固定 4.0.1（4.1+ 需要 Rust 编译）。
#
#   bash <skill 目录>/install-demucs.sh
#
# 为什么这么绕 —— 五个坑，都是实测撞出来的：
#  1. PyPI 上的 torch 是 **CUDA 版**（import 时强制要 libcublasLt.so），
#     装了还会拖 nvidia-* 一堆包（实测 cublas 223MB + cusparse 162MB + cudnn…）。
#     **必须用 CPU 版**：https://mirrors.aliyun.com/pytorch-wheels/cpu/
#     （实测 1.1~1.5 MB/s，99MB 的 torch-2.9.1+cpu 约 1 分钟；官方 download.pytorch.org
#      实测会掉到 18 KB/s，别用）
#  2. 新版 demucs（4.1+）依赖 sphn，要用 Rust 编译，本机失败 → 改用 **4.0.1**。
#  3. torchaudio 2.9 起移除了内置音频 I/O，命令行 demucs 保存结果会报
#     "TorchCodec is required"，而 torchcodec 在本机加载不了 .so。
#     → bgm-id.py 已改为**自己用 ffmpeg+soundfile 读写、只调模型推理**，绕过它。
#  4. 用清华 PyPI 索引解析依赖可能超时（高峰期实测索引页 0.3~15 KB/s）。
#     大文件一律用 curl -C - 断点续传单独下，比 pip 稳。
#  5. 首次推理会自动下 htdemucs 模型（约 80MB）。
#  6. **卸载也要 --break-system-packages**：PEP 668 的 externally-managed-environment
#     对 install 和 uninstall 一视同仁，漏了会直接报错退出。回滚命令：
#       python3 -m pip uninstall -y --break-system-packages torch torchaudio demucs soundfile
#  7. **干净环境下 torch 解包极慢**：本机是 proot 容器，每个系统调用都被 ptrace 拦一次，
#     torch 有 1.3 万个文件，实测解包要 10 分钟以上（看着像卡死，其实在跑）。
#     期间如果内存吃紧（实测可用内存掉到 300MB 左右、还有别的构建任务在跑），
#     pip 会 **Bus error** 崩掉 —— 脚本因 set -e 正确中止，但报错信息不直观。
#     建议：跑之前确认没有别的重活，可用内存留 3GB 以上。
set -euo pipefail   # 早先没 -e，校验失败也返回 0（评审指出）

PYPI="-i https://pypi.tuna.tsinghua.edu.cn/simple"
ALI="https://mirrors.aliyun.com/pytorch-wheels/cpu"
PKGS="${DEMUCS_PKGS:-$HOME/demucs-pkgs}"   # 可用 DEMUCS_PKGS 覆盖；容器里若挂了手机存储可指向 /sdcard/Download/demucs-pkgs
mkdir -p "$PKGS"

# 判断某个模块是否已装（注意：这个函数被下面的分支引用，别当成死代码删掉）
need() { python3 -c "import $1" 2>/dev/null; }

# ── 1. CPU 版 torch ────────────────────────────────────────────────────
if ! python3 -c "import torch; assert '+cpu' in torch.__version__" 2>/dev/null; then
  echo "① 装 CPU 版 torch…"
  # 优先取**实测过**的版本对（torch 2.9.1 + torchaudio 2.9.1），而不是跟着镜像漂到最新：
  # 镜像上 torch 最新 2.14.1 而 torchaudio 只到 2.11.0，两者对不上，装完 import 就炸。
  # 想换版本：DEMUCS_TORCH_VER=2.x.y bash install-demucs.sh
  WANT="${DEMUCS_TORCH_VER:-2.9.1}"
  # 顺序很重要：索引 HTML 里 "+" 被转义成 &#43;，必须先解码再匹配，
  # 否则 "torch-2.9.1+cpu" 永远匹配不到（实测：原写法选不出任何文件，静默走回退分支装错版本）
  F=$(curl -s "$ALI/" | sed 's/&#43;/+/g' | grep -oE "torch-${WANT}\+cpu[^\"]*cp312[^\"]*aarch64\.whl" | sort -uV | tail -1)
  if [ -z "$F" ]; then
    echo "  ! 镜像上没有 torch ${WANT}，回退到最新（可能与 torchaudio 版本不配套）"
    F=$(curl -s "$ALI/" | sed 's/&#43;/+/g' | grep -oE 'torch-2\.[0-9.]+[^"]*cp312[^"]*aarch64\.whl' | sort -uV | tail -1)
  fi
  [ -n "$F" ] || { echo "✗ 在 $ALI 没找到 cp312 aarch64 wheel"; exit 1; }
  echo "   文件: $F"
  curl -fL -C - --retry 5 -o "$PKGS/$F" "$ALI/$F" || { echo "✗ 下载失败：$F"; exit 1; }
  # --no-deps 是为了绕开 CUDA 那一坨，但 torch 自己的纯 Python 依赖还得补上
  python3 -m pip install --break-system-packages --no-deps -q "$PKGS/$F"
  python3 -m pip install --break-system-packages -q $PYPI filelock typing-extensions sympy networkx jinja2 fsspec
fi

# ── 2. CPU 版 torchaudio ───────────────────────────────────────────────
if ! need torchaudio; then
  echo "② 装 torchaudio（版本对齐已装的 torch）…"
  # 不要拿 torch 版本去拼 torchaudio 的文件名：两者版本并不同步
  # （实测阿里云 torch 最新 2.14.1、torchaudio 最新 2.11.0），拼出来必然 404。
  F=$(curl -s "$ALI/" | sed 's/&#43;/+/g' | grep -oE 'torchaudio-2\.[0-9.]+[^"]*cp312[^"]*aarch64\.whl' | sort -uV | tail -1)
  [ -n "$F" ] || { echo "✗ 在 $ALI 没找到 cp312 aarch64 torchaudio"; exit 1; }
  curl -fL -C - --retry 5 -o "$PKGS/$F" "$ALI/$F" && \
    # --no-deps 是为了绕开 CUDA 那一坨，但 torch 自己的纯 Python 依赖还得补上
  python3 -m pip install --break-system-packages --no-deps -q "$PKGS/$F"
  python3 -m pip install --break-system-packages -q $PYPI filelock typing-extensions sympy networkx jinja2 fsspec
fi

# ── 3. demucs 4.0.1 与其余依赖（不碰 torch，避免被换成 CUDA 版）───────
if ! need demucs; then
  echo "③ 装 demucs 4.0.1 及依赖…"
  # --no-deps 是关键：demucs 与 openunmix 都声明了 torch/torchaudio 依赖，
  # 放开解析会让 pip 把上面辛苦对齐的 CPU 版换成 PyPI 上的不配套版本
  # —— 实测被换成 torchaudio 2.11.0，与 torch 2.9.1 不匹配，原生库直接加载失败。
  # 它们真正缺的纯 Python 依赖下面单独装。
  python3 -m pip install --break-system-packages -q $PYPI --no-deps \
    "demucs==4.0.1" dora-search einops julius lameenc openunmix pyyaml tqdm
fi

# ── 3.5 策略4 还要用 soundfile 读写音频（demucs 自己不依赖它）──────────
if ! need soundfile; then
  echo "③.5 装 soundfile（demucs 的读写层，bgm-id.py 策略4 直接用它）…"
  python3 -m pip install --break-system-packages -q $PYPI soundfile
fi

# ── 3.9 一致性检查：torch 与 torchaudio 必须同版本 ──────────────────────
# 不同版本时 torchaudio 的原生库会加载失败，而且失败信息很隐晦
# （只报 "Could not load this library: .../lib/_torchaudio*.so"）。
TV=$(python3 -c "import torch;print(torch.__version__.split('+')[0])" 2>/dev/null)
AV2=$(python3 -c "import torchaudio;print(torchaudio.__version__.split('+')[0])" 2>/dev/null)
if [ -n "$TV" ] && [ -n "$AV2" ] && [ "$TV" != "$AV2" ]; then
  echo "✗ torch ($TV) 与 torchaudio ($AV2) 版本不一致 —— 原生库会加载失败。"
  echo "  多半是某步装依赖时被换掉了。删掉重来："
  echo "    python3 -m pip uninstall -y --break-system-packages torch torchaudio"
  echo "    bash $0"
  exit 1
fi

# ── 4. 验证 ────────────────────────────────────────────────────────────
echo "④ 验证…"
python3 - <<'PY'
try:
    import torch, torchaudio, demucs, soundfile
    cpu = '+cpu' in torch.__version__
    print(f"  torch      {torch.__version__}{'' if cpu else '  ← 不是 CPU 版！'}")
    print(f"  torchaudio {torchaudio.__version__}")
    print(f"  demucs     {demucs.__version__}")
    print(f"  soundfile  {soundfile.__version__}   ← bgm-id.py 策略4 的音频读写层")
    print('  ✓ 就绪（首次跑策略4 会下载 htdemucs 模型约 80MB）')
except Exception as e:
    print('  ✗ 验证失败:', type(e).__name__, e)
    raise SystemExit(1)
PY
