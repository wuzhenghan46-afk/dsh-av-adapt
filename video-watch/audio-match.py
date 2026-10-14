#!/usr/bin/env python3
"""audio-match.py —— 判定两段音频是否同源，并反推被做过的变速变调参数。

用法：
    python3 audio-match.py <待判定音频> <参考音频> [更多参考...] [--baseline <无关素材>]...

    --baseline 可重复，指定与两者都无关的音频作为基线。
    也可以把基线文件命名为含 unrelated / baseline / 无关 的形式，工具会自己认出来。
    **不提供基线时结果不可用于下结论**（见下），工具会明确警告。

要解决的场景：某段 BGM 是"从某首歌调速/去人声改来的"吗？
指纹识别（Shazam/AcoustID）回答不了这个 —— 处理过的音频指纹对不上原曲。
本脚本从和声层面回答，并**反推出处理参数**。

⚠️ **适用范围（实测边界，务必先读）** —— 本工具**不能**判断"两段录音是不是同一首歌"：
  实测（同一组素材，只换基线）：
    · 同一录音变速 0.94×      → 绝对相似度 0.9567   ✅ 清晰可判
    · 同一首歌的现场版        → 0.8000              ⚠️
    · 同一首歌的女声翻唱      → 0.7271              ⚠️
    · 无关曲目 A              → 0.6390
    · 无关曲目 B（Love Hurt） → 0.7399   ← 比同源的翻唱还高 ✗
  「同歌不同录音」与「无关曲目」的分数区间**重叠**，因此：
    ✅ 能判：这段音频是不是**那段音频本身**被整体变速/变调处理过（>0.90 时）
    ❌ 不能判：两段**不同录音**（现场/翻唱）是不是同一首歌
  根因：没有按节拍对齐（beat-synchronous chroma）、没有 DTW。标准做法见
  cover song identification（Covers80 / SHS100K 那类任务）。

原理与坑（都是实测踩出来的）：
  · 直接用"和声相似度"判断没有意义：任意两段流行音乐都能拿到 0.7 上下的分。
  · 更隐蔽的坑：把 query 做音高移位后相似度**反而更高** —— 说明裸相似度没有语义。
    所以必须做基线校正，用一首**无关音频**在同样的(移位,伸缩)格子上去减。
  · 真正的判据在于"音高变化"与"速度变化"是否自洽：
    若只有改变播放速率这一种操作，则 半音数 ≈ 12×log2(速度比)。
    实测案例（Love Hurt beat ← 病名为爱）网格峰值落在 +1 半音 / ×0.94，
    而 12×log2(0.94) = -1.07 —— 两者吻合，才敢下"同源、且由单一放慢约 6% 的操作产生"的结论。
    （方向极易读反：我曾把它说成"加速 6.4%"，后经 Shazam 裁决纠正 —— 详见下方 main() 里的方向说明。）

依赖：librosa（CQT chroma）、ffmpeg（读 m4a 等格式）。
"""
import os
import subprocess
import sys
import tempfile
import warnings

# librosa 依赖的 joblib 在没有 /dev/shm 的容器里会打一堆 UserWarning，先静音
warnings.filterwarnings('ignore')

import numpy as np
import librosa

SR, HOP = 22050, 1024
SHIFTS = [-2, -1, 0, 1, 2, 3, 4]
SPEEDS = [0.90, 0.94, 0.98, 1.00, 1.02, 1.06, 1.10, 1.14]


def to_wav(p):
    """librosa 的 soundfile 后端读不了 m4a/aac，统一先转 wav。

    注意不要用 check=True：文件损坏时它会抛 CalledProcessError 堆栈，
    对使用者毫无帮助（实测踩过）。
    """
    if not os.path.exists(p):
        raise SystemExit(f'✗ 找不到文件：{p}')
    if p.lower().endswith('.wav'):
        return p
    # 不写死 /tmp/am_xxx.wav：并发跑或同名文件会互相覆盖，且退出后不清理
    fd, out = tempfile.mkstemp(prefix='am_', suffix='.wav')
    os.close(fd)
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', p,
                        '-ac', '1', '-ar', str(SR), out], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        # ffmpeg 的 stderr 是多行且带内存地址，只挑最后一行非空的当原因，保持输出干净
        lines = [l.strip() for l in (r.stderr or '').splitlines() if l.strip()]
        why = lines[-1][:120] if lines else '未知原因'
        raise SystemExit(f'✗ 无法解码音频：{p}（{why}）')
    return out


def chroma(path):
    y, _ = librosa.load(to_wav(path), sr=SR, mono=True)
    c = librosa.feature.chroma_cqt(y=y, sr=SR, hop_length=HOP)
    return c / (np.linalg.norm(c, axis=0, keepdims=True) + 1e-9)


def warp(c, s):
    """按比例伸缩时间轴（在 chroma 域插值，比音频域快得多）。

    插值会把单位向量的范数破坏掉（实测 ×0.94 后掉到 0.91~1.00），
    那样后续点积就不再是余弦相似度了，所以插值完必须重新按帧归一化。
    """
    T = c.shape[1]
    # 下限用 2（而不是 8）：T 很小时 max(8,…) 会把所有 s 都变成一个长度，
    # 于是「伸缩」这一维就失效了。至少保留两个端点才有插值意义。
    n = max(2, int(round(T * s)))
    idx = np.linspace(0, T - 1, n)
    out = np.stack([np.interp(idx, np.arange(T), c[k]) for k in range(12)])
    return out / (np.linalg.norm(out, axis=0, keepdims=True) + 1e-9)


def best(qc, tc):
    """在 tc 上滑动 qc，取最佳平均余弦相似度。

    两边谁长都要能滑：早先版本在 query 更长时只比开头一段，
    等于放弃了在长音频里找参考（评审指出）。
    """
    Tq, Tt = qc.shape[1], tc.shape[1]
    if Tq <= Tt:
        # 不一次性建 Tq×Tt 全矩阵：10 分钟素材能到 2.7GB（评审算过）。
        # 改用「沿 target 分块 + 累积对角线」的等价算法，内存降到 O(块大小)。
        L = Tt - Tq + 1
        acc = np.zeros(L, dtype=np.float64)
        CHUNK = max(1, int(4e6 // max(1, Tq)))          # 每块约 4M 元素
        for start in range(0, Tt, CHUNK):
            stop = min(Tt, start + CHUNK)
            blk = qc.T @ tc[:, start:stop]              # (Tq, stop-start)
            # 该块内第 i 行对应全局起点的区间 [start-i, stop-i)
            for i in range(Tq):
                lo, hi = max(0, start - i), min(L, stop - i)
                if lo < hi:
                    acc[lo:hi] += blk[i, lo + i - start:hi + i - start]
        return float((acc / Tq).max())
    M = tc.T @ qc                              # 反过来滑：拿短的一侧去扫长的一侧
    Lt = Tt
    diag = np.stack([M[i, i:i + (Tq - Tt) + 1] for i in range(Lt)])
    return float(diag.mean(axis=0).max())


def main(argv):
    # 解析 --baseline（可重复），其余为位置参数
    explicit, rest = [], []
    i = 0
    while i < len(argv):
        if argv[i] == '--baseline':
            if i + 1 >= len(argv):
                print('✗ --baseline 后面要跟文件路径'); return 2
            explicit.append(argv[i + 1]); i += 2
        elif argv[i].startswith('--baseline='):
            explicit.append(argv[i].split('=', 1)[1]); i += 1
        else:
            rest.append(argv[i]); i += 1

    if len(rest) < 2:
        print(__doc__)
        return 2
    q0 = chroma(rest[0])
    # 注意：--baseline 指定的文件也要算 chroma 并进入 named，
    # 否则下面 grid 里取不到它的格子（会 KeyError）。
    all_files = rest[1:] + explicit
    seen = set()
    named = []
    for p in all_files:
        # 用完整路径去重：ref/song.wav 与 other/song.wav 是两个文件，
        # 早先按 basename 去重会把参考曲误当基线（评审指出）
        key_ = os.path.abspath(p)
        if key_ in seen:
            continue
        seen.add(key_)
        named.append((p, chroma(p)))
    # 基线 = 显式 --baseline 的（按完整路径匹配），
    # 加上文件名含关键词的（兼容旧用法；关键词只在 basename 上匹配，避免目录名误伤）
    explicit_abs = {os.path.abspath(p) for p in explicit}
    bases = {p for p, _ in named if os.path.abspath(p) in explicit_abs}
    for p, _ in named:
        b = os.path.basename(p).lower()
        if any(k in b for k in ('unrelated', 'baseline', '无关')):
            bases.add(p)
    keys = [(p, c) for p, c in named if p not in bases]

    if not bases:
        print('⚠️  没有提供基线素材 —— 下面列出的是**未校正的裸相似度**，')
        print('    它对判定同源没有意义（任意两段流行乐都能到 0.7 上下，')
        print('    甚至把 query 音高移位后分数还会更高）。')
        print('    请用 --baseline 指定一首与两者都无关的音频后重跑。\n')
    if not keys:
        print('✗ 除了基线之外没有参考音频可比对')
        return 2

    grid = {}
    for r in SHIFTS:
        for s in SPEEDS:
            q = warp(np.roll(q0, r, axis=0), s)
            for n, c in named:
                grid[(r, s, n)] = best(q, c)

    for kn, _kc in keys:
        print(f'\n=== {kn}（相对基线增益）')
        print('        ' + ''.join(f'{s:>8.2f}' for s in SPEEDS))
        bestcell, bestval = None, -9
        for r in SHIFTS:
            row = []
            for s in SPEEDS:
                base = np.mean([grid[(r, s, b)] for b in bases]) if bases else 0.0
                v = grid[(r, s, kn)] - base
                row.append(v)
                if v > bestval:
                    bestval, bestcell = v, (r, s)
            print(f'  {r:+d}   ' + ''.join(f'{v:>8.4f}' for v in row))
        r, s = bestcell
        base = np.mean([grid[(r, s, b)] for b in bases]) if bases else 0.0
        print(f'  → 最大增益 {bestval:+.4f} @ 假想待判定 {r:+d} 半音 / 时间轴 ×{s:.2f}')
        print(f'     该格绝对相似度 {grid[(r,s,kn)]:.4f}，基线 {base:.4f}')

        # 判据以**绝对相似度分档**为主。基线校正后的"增益"只作辅助提示 ——
        # 实测它主要由基线素材决定：同一条无关对换三种基线，增益在
        # +0.0466 / +0.1678 / +0.7530 之间跳（差 15 倍），且三种基线下
        # 无关对的增益都**高于**同源对。所以增益不能当判据用。
        abs_sim = float(grid[(r, s, kn)])
        if abs_sim >= 0.90:
            verdict = '✅ 很像「同一录音被整体变速/变调处理」——这是本工具唯一可靠的结论'
        elif abs_sim >= 0.70:
            verdict = ('⚠️ 落在**不可判别区间**（实测无关曲目也能到 0.74，同源翻唱只有 0.73）——'
                       '本工具无法据此下结论，必须人工听或换方法（beat-sync + DTW）')
        else:
            verdict = '❌ 低于 0.70，大概率既非同录音也非同源'
        print(f'     判据：绝对相似度 {abs_sim:.4f} → {verdict}')
        if bestval < 0.05:
            print(f'     （基线校正增益仅 {bestval:+.4f}，且该指标不稳定，只作参考）')

        # 峰值落在网格边界 = 真值可能在网格之外：这时给出的方向和倍率都是错的。
        # 实测反例：query 降 5 个半音时峰值卡在 s=1.14 边界，脚本会打印
        # 「+2.27 半音、快 14%」——真值是 −5 半音、慢 25%，方向完全反。
        on_edge = (r in (SHIFTS[0], SHIFTS[-1])) or (s in (SPEEDS[0], SPEEDS[-1]))
        if on_edge:
            print(f'     ⚠ 峰值落在**搜索网格边界**（音高 {r:+d}、伸缩 ×{s:.2f}），'
                  '真值很可能在网格之外 → **本次给出的方向与倍率不可信**。')
            print('       扩大 SHIFTS / SPEEDS 的范围或换更长的片段后重跑，再下结论。')
            continue

        # 方向换算：谱面峰值 (r, s) 表示待判定原本比参考低 r 个半音、慢 (1-s) 比例。
        # 注意「测到的音高」就是 -r（谱面读数），不是 12×log2(s) ——
        # 后者是「若只改播放速率，音高本该是多少」的**理论值**，只用来和 -r 比对。
        # 早先把理论值当成测量值打印：格子落在 (+4, ×0.94) 时会先报「低 1.07 半音」，
        # 与谱面读到的「低 4 个半音」矛盾。
        measured = -int(r)                  # 谱面测到的音高偏移
        implied = 12 * np.log2(s)           # 单一变速操作下的理论音高偏移
        consistent = abs(implied - measured) < 0.8
        print(f'     方向：谱面测到待判定比参考 **{measured:+d} 半音**、'
              f'**{"慢" if s < 1 else "快"} {abs(1 - s) * 100:.0f}%**')
        if consistent:
            print(f'     自洽性：若只是改变播放速率，音高应为 12×log2({s:.2f}) = {implied:+.2f} 半音；'
                  f'实测 {measured:+d} → 两者吻合，支持「由单一改变播放速率的操作产生」')
        else:
            print(f'     自洽性：若只是改变播放速率，音高应为 12×log2({s:.2f}) = {implied:+.2f} 半音；'
                  f'实测却是 {measured:+d} → **对不上**，可能是分别调了速度与音高，或仅是同调性巧合')
        # 速度步长是 4%，别宣称比这更细的精度
        print(f'     （网格速度步长为 4%，参数精度以此为上限；本格为 ×{s:.2f}）')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
