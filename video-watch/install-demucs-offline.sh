#!/usr/bin/env bash
# 离线安装 demucs：你把清单里的文件都下载到同一个目录后跑这个。
#
#   bash <skill 目录>/install-demucs-offline.sh [目录]
#
# 默认目录 $HOME/demucs-pkgs（可用 DEMUCS_PKGS 或第一个参数覆盖）
set -euo pipefail   # 与线上版一致：早先没有 -e，失败也返回 0
DIR="${1:-${DEMUCS_PKGS:-$HOME/demucs-pkgs}}"
[ -d "$DIR" ] || { echo "✗ 目录不存在：$DIR"; exit 1; }
n=$(ls "$DIR" | grep -cE '\.(whl|tar\.gz)$')
echo "在 $DIR 找到 $n 个安装包"
[ "$n" -gt 0 ] || { echo "✗ 没找到 .whl / .tar.gz"; exit 1; }
# 必须锁 4.0.1：4.1+ 依赖 sphn，要用 Rust 编译（本机装不上）
if ls "$DIR"/demucs-4.1* >/dev/null 2>&1 && ! ls "$DIR"/demucs-4.0.1* >/dev/null 2>&1; then
  echo "✗ 目录里只有 demucs 4.1+（需 Rust 编译）。请补下 demucs-4.0.1.tar.gz"
  exit 1
fi
python3 -m pip install --break-system-packages --no-index --find-links="$DIR" "demucs==4.0.1"
# 策略4 还要用 soundfile 读写音频（demucs 自己不依赖它）
python3 -c "import soundfile" 2>/dev/null || \
  python3 -m pip install --break-system-packages --no-index --find-links="$DIR" soundfile
echo "=== 验证 ==="
python3 -c "import demucs, torch, soundfile; print('✓ demucs + torch', torch.__version__, '+ soundfile', soundfile.__version__)" \
  || { echo "✗ 验证失败，看上面的报错"; exit 1; }
