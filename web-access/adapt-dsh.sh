#!/usr/bin/env bash
# 把上游 web-access skill 适配到本机 DSH 环境（幂等，可反复运行）。
#
#   bash <skill 目录>/adapt-dsh.sh
#
# 做三件事：
#   1. 把 SKILL.md / README.md 里的 CLAUDE_SKILL_DIR 变量换成本 skill 的绝对路径
#      （DSH 不设置这个变量，上游默认面向 Claude Code）
#   2. 在 SKILL.md 顶部维护本机适配说明（内容取自 dsh-adapt-note.md，每次重写）
#   3. 保证「前置检查」标题不带「本机跳过」字样（CDP 现在可用）
# git pull 覆盖掉这些改动后，重跑本脚本即可恢复。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
esc=${DIR//\\/\\\\}; esc=${esc//&/\\&}; esc=${esc//|/\\|}   # 用 bash 内建替换转义 \ & |（sed 里的 \& 会产出裸 &）
SKILL="$DIR/SKILL.md"
NOTE="$DIR/dsh-adapt-note.md"
MARKER='> **本机适配说明（DSH / Android 容器）**'

[ -f "$SKILL" ] || { echo "✗ 找不到 $SKILL"; exit 1; }
[ -f "$NOTE" ]  || { echo "✗ 找不到 $NOTE"; exit 1; }

# ── 1. 路径变量 → 绝对路径 ──────────────────────────────────────────────
for f in "$SKILL" "$DIR/README.md"; do
  [ -f "$f" ] || continue
  if grep -qF '${CLAUDE_SKILL_DIR}' "$f"; then
    sed -i "s|\${CLAUDE_SKILL_DIR}|$esc|g" "$f"
    echo "✓ $(basename "$f")：CLAUDE_SKILL_DIR → $DIR"
  else
    echo "· $(basename "$f")：无需替换路径变量"
  fi
done

# ── 2. 适配说明：删旧段 + 插新段（可重入） ──────────────────────────────
awk -v note="$NOTE" '
  /^> \*\*本机适配说明/ { skip = 1; next }
  # 只在「连续的引用块」内跳过：碰到非引用、非空行就立刻恢复输出。
  # 早先版本只在遇到 "## " 二级标题时才停 —— 如果适配段之后直接是正文
  # （上游一改文档结构就会这样），后面的内容会被整段吃掉。
  skip && !/^>/ && !/^$/ { skip = 0 }
  skip                   { next }
  { print }
  /^# web-access Skill$/ {
    print ""
    while ((getline line < note) > 0) print line
    close(note)
    print ""
  }
' "$SKILL" > "$SKILL.tmp" && mv "$SKILL.tmp" "$SKILL"
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

# ── 3. 前置检查标题复原 ─────────────────────────────────────────────────
if grep -q '^## 前置检查（本机跳过' "$SKILL"; then
  sed -i 's|^## 前置检查（本机跳过.*$|## 前置检查|' "$SKILL"
  echo "✓ SKILL.md：前置检查标题已复原"
else
  echo "· SKILL.md：前置检查标题正常"
fi

echo "完成。"
