#!/usr/bin/env bash
# 把上游 video-watch skill 适配到本机 DSH 环境（幂等，可反复运行）。
#
#   bash <skill 目录>/adapt-dsh.sh
#
# 做两件事：
#   1. 把 SKILL.md / README.md 里的 `python vw.py` 换成本机绝对路径写法
#      （DSH 的 shell 工作目录是工作区，不是 skill 目录，相对路径会找不到文件）
#   2. 在 SKILL.md 顶部维护本机适配说明（内容取自 dsh-adapt-note.md，每次重写）
# git pull 覆盖掉这些改动后，重跑本脚本即可恢复。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
esc=${DIR//\\/\\\\}; esc=${esc//&/\\&}; esc=${esc//|/\\|}   # 用 bash 内建替换转义 \ & |（sed 里的 \& 会产出裸 &）
SKILL="$DIR/SKILL.md"
NOTE="$DIR/dsh-adapt-note.md"
MARKER='> **本机适配说明（DSH / Android 容器）**'

[ -f "$SKILL" ] || { echo "✗ 找不到 $SKILL"; exit 1; }
[ -f "$NOTE" ]  || { echo "✗ 找不到 $NOTE"; exit 1; }

# ── 1. 命令相对路径 → 绝对路径 ──────────────────────────────────────────
for f in "$SKILL" "$DIR/README.md"; do
  [ -f "$f" ] || continue
  n=$(grep -c 'python vw\.py' "$f" 2>/dev/null || true)
  if [ "${n:-0}" -gt 0 ]; then
    sed -i "s|python vw\.py|python3 $esc/vw.py|g" "$f"
    echo "✓ $(basename "$f")：$n 处命令已改为绝对路径"
  else
    echo "· $(basename "$f")：无需替换"
  fi
done

# ── 2. 适配说明：删旧段 + 插新段（可重入） ──────────────────────────────
awk -v note="$NOTE" '
  /^> \*\*本机适配说明/ { skip = 1; next }
  # 只在「连续的引用块」内跳过：碰到非引用、非空行立刻恢复输出。
  # 早先版本要到遇到 "#" 开头的行才停 —— 适配段之后若直接是正文（上游改了
  # 文档结构就会这样），后面的内容会被整段吃掉。
  skip && !/^>/ && !/^$/ { skip = 0 }
  skip                   { next }
  { print }
  /^# video-watch/ {
    print ""
    while ((getline line < note) > 0) print line
    close(note)
  }
' "$SKILL" > "$SKILL.tmp" && mv "$SKILL.tmp" "$SKILL"
# 适配说明里的 ${SKILL_DIR} 占位符换成实际目录（否则说明里留着汉字占位符，复制执行会失败）
sed -i "s|\${SKILL_DIR}|$esc|g" "$SKILL"
# 必须核实真的写进去了：awk 的插入点靠标题行匹配，
# 上游若改了文档结构就会一句都插不进去，而脚本原先照样报"已更新"（谎报成功）
n=$(grep -cF "$MARKER" "$SKILL" || true)
if [ "${n:-0}" -eq 0 ]; then
  echo "✗ SKILL.md：没找到插入点，适配说明**未写入**"
  echo "  原因：上游文档的标题行变了。请手工把 dsh-adapt-note.md 的内容贴到 SKILL.md 顶部，"
  echo "  或更新本脚本里的插入点模式。"
  exit 1
fi
echo "✓ SKILL.md：适配说明已更新（$n 处）"


# ── 3. 兼容 shim：faster-whisper 调 PyAV 的参数在新版被移除 ───────────────
# PyAV 19 删掉了 av.open(..., metadata_errors=) 这个参数，而本机只能装到 19
# （老版本没有 aarch64 wheel），于是 asr 会在解码音频时 TypeError。
# 这里就地把那一行改成 try/except，pip 重装 faster-whisper 后重跑本脚本即可恢复。
AUDIO_PY=$(python3 -c "import faster_whisper.audio as a; print(a.__file__)" 2>/dev/null || true)
if [ -n "$AUDIO_PY" ] && [ -f "$AUDIO_PY" ]; then
  python3 - "$AUDIO_PY" <<'PYPATCH'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
old = '    with av.open(input_file, mode="r", metadata_errors="ignore") as container:'
new = ('    try:\n'
       '        container = av.open(input_file, mode="r", metadata_errors="ignore")\n'
       '    except TypeError:\n'
       '        # PyAV 19 起删掉了 metadata_errors（DSH 本机适配补丁）\n'
       '        container = av.open(input_file, mode="r")\n'
       '    with container:')
if 'metadata_errors（DSH 本机适配补丁）' in s:
    print('· faster_whisper/audio.py：PyAV 兼容补丁已在')
elif old in s:
    open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))
    print('✓ faster_whisper/audio.py：已打 PyAV 19 兼容补丁')
else:
    print('! faster_whisper/audio.py：未找到目标行，跳过（上游可能已适配）')
PYPATCH
else
  echo "· faster-whisper 未安装，跳过 PyAV 兼容补丁"
fi

echo "完成。"
