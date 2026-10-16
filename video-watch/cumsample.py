#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cumsample.py —— 「按覆盖采样」原型：不要 query、不要 GPU、不要重模型。

思路（对应请教文档的问题 1/3/4）：
  1. ffmpeg 低帧率解码并缩成 32x18 缩略图（免费，不花 token）
  2. 在缩略图特征空间里做「k-center 贪心」：每次挑离已选帧最远的帧，
     直到最远盲点的距离 < τ（覆盖半径）。
        · 切点密的素材（MV）自动每个新镜头一帧，重复镜头自动合并
        · 连续运动的素材（录屏）自动按画面漂移量取点
        · 静止素材（PPT）自动只取每页一帧
     → 不用判型、不用「时长÷40」，帧数由内容决定。
  3. 另加「骨架」：无论内容如何，相邻采样点最大间隔不超过 --skeleton 秒
     （防止特征对某些变化不敏感时出现长盲区）
  4. 覆盖账本：每个采样点「代表」了哪些时间段；还有哪些时段没被覆盖（距离 > τ）。
     图版底部烧一条时间轴（绿=已覆盖，红=盲区；蓝线=本版采样点），
     让「这一遍哪里是空白」显式可见。

依赖：python3、numpy、Pillow、ffmpeg/ffprobe（都是你们环境里已有的）。

用法：
    python3 cumsample.py 视频.mp4 --out 输出目录
    python3 cumsample.py 视频.mp4 --out 输出目录 --metric both
    python3 cumsample.py 视频.mp4 --metric both --thumb 96x54    # 对局部小变化更敏感
    python3 cumsample.py 视频.mp4 --no-sheet          # 只出 plan.json + ledger.txt（秒级）
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

SHEET_W = 756          # 与 vw 图版一致：每格宽 = 756 // 列数 - 2
TOKENS_PER_SHEET = 345  # 实测值：一张图版约 345 token，与帧数无关

# 两种距离的经验默认 τ（在合成素材上标定，真实素材请先看 ledger 里相邻帧距离的分位数再调）
DEFAULT_TAU = {"global": 0.05, "local": 0.3, "both": 1.0}
# 自动 τ：取「相邻帧距离 p90 × 0.75」。
# 依据：合成素材上标定的 0.05 在真实素材上比 p50 还小（实测 p50=0.081），会把每对相邻帧
# 都判成新内容、疯狂选点直至撞上限。真实素材实测 τ=0.15（≈p90×0.75）在 MV 与录屏上均无盲区。
AUTO_TAU_FACTOR = 0.75
GLOBAL_REF, LOCAL_REF = 0.05, 0.3   # both 模式：两种距离各自除以参考值，取较大者（τ 因此变成相对单位 1.0）
GRID_X, GRID_Y = 8, 6   # local 距离把画面分成 8x6 个格子，取变化最大的那一格


# ----------------------------------------------------------------- ffmpeg 层
def _run(cmd):
    return subprocess.run(cmd, capture_output=True)


def video_info(path):
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height:format=duration",
              "-of", "json", path])
    if r.returncode != 0:
        raise RuntimeError("ffprobe 失败: " + r.stderr.decode(errors="ignore")[-300:])
    j = json.loads(r.stdout.decode())
    st = j["streams"][0]
    return float(j["format"]["duration"]), int(st["width"]), int(st["height"])


def read_thumbs(path, fps, tw=32, th=18):
    """整片按 fps 解码并缩成 tw x th 的 RGB，返回 (N, tw*th, 3) uint8。"""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-an", "-sn",
           "-vf", f"fps={fps},scale={tw}:{th}:flags=area,format=rgb24",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = _run(cmd)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("ffmpeg 解码失败: " + r.stderr.decode(errors="ignore")[-300:])
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    n = buf.size // (tw * th * 3)
    return buf[: n * tw * th * 3].reshape(n, tw * th, 3)


# ----------------------------------------------------------------- 距离
class Metric:
    """
    global : 缩略图逐像素差的均值（整体变化）
    local  : 把画面切成 8x6 格，每格算逐像素差的均值，取变化最大的那一格
             （对局部变化敏感：角落弹出的牌子、PPT 只换了字；
              代价是对运动物体经过某一格也更敏感）
    both   : max(global/0.05, local/0.3)——整体变了或某一格变了，都算「新」。τ 为相对单位（默认 1.0）
    local / both 不满足三角不等式，所以 k-center 在这里是启发式，不是严格的 2 近似。
    """

    def __init__(self, F, tw, th, mode="global"):
        assert tw % GRID_X == 0 and th % GRID_Y == 0, "缩略图宽须是 8 的倍数、高须是 6 的倍数"
        self.F, self.N, self.tw, self.th, self.mode = F, len(F), tw, th, mode

    def _reduce(self, pix):
        glob = pix.mean(axis=1)
        if self.mode == "global":
            return glob
        n = pix.shape[0]
        g = pix.reshape(n, GRID_Y, self.th // GRID_Y, GRID_X, self.tw // GRID_X)
        loc = g.mean(axis=(2, 4)).reshape(n, -1).max(axis=1)
        if self.mode == "local":
            return loc
        return np.maximum(glob / GLOBAL_REF, loc / LOCAL_REF)

    @staticmethod
    def _pix(a, b):
        return np.abs(a.astype(np.int16) - b.astype(np.int16)).mean(axis=2, dtype=np.float32) / 255.0

    def to(self, c):
        """所有帧到第 c 帧的距离。分块计算，限制内存。"""
        out = np.empty(self.N, np.float32)
        f = self.F[c]
        for s in range(0, self.N, 2048):
            out[s:s + 2048] = self._reduce(self._pix(self.F[s:s + 2048], f))
        return out

    def consecutive(self):
        """相邻帧距离（用来给 τ 的选择提供参考分位数）。"""
        out = np.zeros(max(self.N - 1, 0), np.float32)
        for s in range(0, self.N - 1, 2048):
            a, b = self.F[s:s + 2048], self.F[s + 1:s + 2049]
            m = min(len(a), len(b))
            out[s:s + m] = self._reduce(self._pix(a[:m], b[:m]))
        return out


# ----------------------------------------------------------------- 选点
def select(M, fps, tau, max_n, skeleton):
    N = M.N
    mind = np.full(N, np.inf, np.float32)
    assign = np.full(N, -1, np.int32)
    sel, reason = [], []

    def add(i, why):
        d = M.to(i)
        j = len(sel)
        sel.append(i)
        reason.append(why)
        m = d < mind
        mind[m] = d[m]
        assign[m] = j
        mind[i] = 0.0
        assign[i] = j

    # τ 的合法性：τ<0 无意义；τ=0 曾导致死循环（见下），这里统一拦掉
    if tau is None:
        tau = 0.0
    tau = float(tau)
    if tau < 0:
        raise ValueError("τ 不能为负数（当前 %.4f）；要「尽量多采」请用 --max 控预算" % tau)

    skeleton_cut = False
    if skeleton and skeleton > 0:
        step = max(1, int(round(skeleton * fps)))
        for i in range(0, N, step):
            # 骨架点也必须受总预算约束 —— 早先这里没有检查，
            # --max 2 配 30s 骨架会照样吐出 4 个点（2026-10-16 实测复现）。
            if len(sel) >= max_n:
                skeleton_cut = True
                break
            add(i, "skeleton")
    else:
        add(0, "first")

    if skeleton_cut:
        # 预算小到连骨架都放不下 —— 如实报告，不假装覆盖完整
        return sel, reason, mind, assign, "max(skeleton)"

    stopped = "tau"
    while True:
        if len(sel) >= max_n:
            stopped = "max"
            break
        i = int(np.argmax(mind))
        # 必须用 <= 而不是 < ：
        # 终止条件原为 mind[i] < tau，当 tau=0 且所有帧完全相同（mind 全 0）时
        # 「0 < 0」恒为假 → 永不退出，只能靠 max_n 兜住，
        # 于是一直重复添加同一帧（实测 100 帧全同、max=8 → [0,30,60,90,0,0,0,0]）。
        # 另外 mind[i] <= 0 意味着"这一帧已经被选过了"，也不该再选。
        if mind[i] <= tau or mind[i] <= 0:
            break
        add(i, "novelty")
    return sel, reason, mind, assign, stopped


def _runs(mask, fps, merge_gap=1):
    """布尔序列 -> [(t0, t1)] 时间段（允许 merge_gap 帧的缺口）。"""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    out, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - p > merge_gap + 1:
            out.append((s, p))
            s = i
        p = i
    out.append((s, p))
    return [(round(a / fps, 2), round((b + 1) / fps, 2)) for a, b in out]


def analyze(video, fps=4.0, tau=None, max_n=200, skeleton=30.0, metric="global", thumb=(32, 18), cols=5):
    t0 = time.time()
    dur, W, H = video_info(video)
    tw, th = thumb
    F = read_thumbs(video, fps, tw, th)
    N = len(F)
    M = Metric(F, tw, th, metric)
    cons = M.consecutive()
    # τ：默认 auto —— 用素材自己的 p90 定，避免合成素材标定值在真实视频上失效
    if tau is None or tau == "auto":
        p90 = float(np.percentile(cons, 90)) if len(cons) else DEFAULT_TAU[metric]
        tau = round(p90 * AUTO_TAU_FACTOR, 4)
        tau_source = f"auto = p90×{AUTO_TAU_FACTOR}（p90={p90:.4f}）"
    else:
        tau = float(tau)
        tau_source = "手动指定"
    sel, reason, mind, assign, stopped = select(M, fps, tau, max_n, skeleton)

    order = np.argsort(sel)                      # 按时间排序
    rank = np.empty(len(sel), int)
    rank[order] = np.arange(len(sel))
    assign_t = rank[assign]                      # 帧 -> 时间序号
    times = [round(sel[o] / fps, 2) for o in order]
    reasons = [reason[o] for o in order]

    samples = []
    for k, o in enumerate(order):
        rng = _runs(assign_t == k, fps)
        samples.append({
            "k": k + 1, "t": times[k], "reason": reasons[k],
            "sheet": k // (cols * cols) + 1,
            "row": (k % (cols * cols)) // cols, "col": k % cols,
            "represents_s": round(sum(b - a for a, b in rng), 1),
            "represents_ranges": rng,
        })
    gaps = np.diff([0.0] + times + [dur])
    gi = int(np.argmax(gaps))
    blind_a = ([0.0] + times)[gi]
    blind_b = (times + [dur])[gi]
    pc = np.percentile(cons, [50, 90, 99]) if len(cons) else [0, 0, 0]
    # 报告半径：盲区报告拿什么当尺子
    if tau and tau > 0:
        report_radius, report_radius_source = float(tau), "（= τ）"
    else:
        # 预算优先模式（τ=0）：τ 只表示"别停手"，不能拿来当覆盖尺子
        report_radius = float(pc[1]) * AUTO_TAU_FACTOR
        report_radius_source = ("（= 建议值 p90×%.2f = %.4f；"
                                "**这是质量目标，不是算法约束** —— τ=0 表示本次以 --max 预算为准）"
                                % (AUTO_TAU_FACTOR, report_radius))
    plan = {
        "video": os.path.abspath(video), "duration_s": round(dur, 2), "size": [W, H],
        "params": {"fps": fps, "tau": tau, "tau_source": tau_source, "max": max_n, "skeleton": skeleton,
                   "metric": metric, "thumb": [tw, th], "cols": cols},
        "coverage": {
            "n_frames_analyzed": N, "n_samples": len(sel),
            "n_skeleton": reasons.count("skeleton"),
            "n_novelty": reasons.count("novelty") + reasons.count("first"),
            "radius": round(float(mind.max()), 4), "mean_dist": round(float(mind.mean()), 4),
            "stopped_by": stopped,
            # 盲区按「报告半径」判，而不是一律按 τ：
            # τ=0（预算优先模式）下按 τ 判会把几乎整片算成盲区
            # （实测：120 点、τ=0 → 报「326s 未被覆盖」，报告失去意义）。
            # τ>0 时报告半径就是 τ；τ=0 时用建议值 p90×0.75 当**质量目标**。
            "report_radius": round(float(report_radius), 4),
            "report_radius_source": report_radius_source,
            "uncovered": [[a, b] for a, b in _runs(mind > report_radius, fps, merge_gap=2)],
            "max_blind_gap_s": [round(float(gaps[gi]), 1), round(blind_a, 1), round(blind_b, 1)],
            "consecutive_dist_p50_p90_p99": [round(float(x), 4) for x in pc],
            "n_sheets": (len(sel) + cols * cols - 1) // (cols * cols),
            "est_tokens": ((len(sel) + cols * cols - 1) // (cols * cols)) * TOKENS_PER_SHEET,
            "analysis_seconds": round(time.time() - t0, 1),
        },
        "samples": samples,
    }
    plan["_mind"] = mind          # 画时间轴用，写 json 前会去掉
    return plan


# ----------------------------------------------------------------- 账本文本
def mmss(t):
    t = int(round(t))
    return f"{t // 60}:{t % 60:02d}"


def ledger_text(plan):
    c, p = plan["coverage"], plan["params"]
    L = []
    L.append(f"视频  {os.path.basename(plan['video'])}   时长 {mmss(plan['duration_s'])}   "
             f"分析 {p['fps']}fps × {c['n_frames_analyzed']} 帧   耗时 {c['analysis_seconds']}s")
    q = c["consecutive_dist_p50_p90_p99"]
    sug = q[1] * 0.75
    L.append(f"距离  {p['metric']} @{p['thumb'][0]}x{p['thumb'][1]}   τ={p['tau']}"
             f"（{p.get('tau_source', '指定')}）   相邻帧距离 p50/p90/p99 = "
             + " / ".join(str(x) for x in q)
             + f"   → 建议 τ ≈ {sug:.4f}（p90×0.75）")
    nov = c["n_novelty"]
    nov_ratio = nov / max(1, c["n_samples"])
    L.append(f"采样  {c['n_samples']} 点（骨架 {c['n_skeleton']} + 新颖 {nov}，新颖占比 {nov_ratio:.0%}）   "
             f"{c['n_sheets']} 版 ≈ {c['est_tokens']} token   停止原因: "
             + ("覆盖半径<τ（相对 τ 已覆盖）" if c["stopped_by"] == "tau" else f"达到上限 --max={p['max']}（仍有盲区！）"))
    L.append(f"覆盖  实际达到半径 {c['radius']}   平均距离 {c['mean_dist']}")
    L.append(f"      盲区判据半径 {c.get('report_radius', p['tau'])}  {c.get('report_radius_source', '')}")
    if c["uncovered"]:
        tot = sum(b - a for a, b in c["uncovered"])
        L.append(f"盲区  共 {tot:.0f}s 超过判据半径：" + "  ".join(f"{mmss(a)}-{mmss(b)}" for a, b in c["uncovered"][:12])
                 + (" …" if len(c["uncovered"]) > 12 else ""))
    else:
        L.append(f"盲区  相对判据半径 {c.get('report_radius', p['tau'])} 无盲区（所有帧到最近采样点的距离都 ≤ 它）")
    g, a, b = c["max_blind_gap_s"]
    L.append(f"时间  相邻采样点最大间隔 {g}s（{mmss(a)}-{mmss(b)}）")

    # ---- 自检（本版本新增）--------------------------------------------
    # 教训：τ 过大时 k-center 会退化成「骨架的每 N 秒一帧」，却仍然报「无盲区」。
    L.append("")
    L.append("自检：")
    # ★ 判据修正（2026-10-16 审查发现）：
    #   原来只看**新颖占比**（比值）。但比值在「半失效区」会保持高位：
    #     实测 τ=0.20 → 42 点 / 新颖 74%（看着健康），实际比 239 点掉了 5.7 倍；
    #     实测 τ=0.24 → 15 点 / 新颖仅 4 个 / 1 版图版，自检**仍报「✓ 算法有效」**。
    #   → 覆盖能力是**绝对量**问题，不是比值问题。补一条绝对量判据：
    #     平均采样间隔超过 AVG_GAP_WARN 秒就告警。
    #   实测各档平均间隔（MV 330s）：τ=0.10 → 0.8s；0.15 → 1.4s；auto → 1.25s；
    #     0.20 → 7.9s；0.24 → 22s。取 5s 能提前抓住半失效，且默认 auto 远低于它。
    AVG_GAP_WARN = 5.0
    avg_gap = (dur / max(1, c["n_samples"])) if dur else 0.0
    bad_ratio = nov_ratio < 0.20
    bad_gap = avg_gap > AVG_GAP_WARN
    if bad_ratio or bad_gap:
        why = []
        if bad_gap:
            why.append(f"平均采样间隔 {avg_gap:.1f}s > {AVG_GAP_WARN:.0f}s")
        if bad_ratio:
            why.append(f"新颖点只占 {nov_ratio:.0%}（阈值 20%）")
        L.append(f"  ⚠ 采样过疏（{'；'.join(why)}）")
        if bad_ratio:
            L.append(f"    k-center 几乎没起作用，输出基本等于骨架的「每 {p['skeleton']:.0f} 秒一帧」，")
            L.append(f"    也就是它本来要取代的均匀采样。")
        if bad_gap:
            L.append(f"    全片 {mmss(dur)} 只取了 {c['n_samples']} 个点、{c['n_sheets']} 版图版 ——")
            L.append(f"    这个密度不足以支撑「看懂了」的结论。")
        L.append(f"    请把 τ 调小（从 {sug:.4f} 起试），或用 --max 指定预算。")
    else:
        L.append(f"  ✓ 算法有效：新颖点占 {nov_ratio:.0%}，"
                 f"平均采样间隔 {avg_gap:.1f}s，采样位置由内容变化驱动。")
    if bad_ratio or bad_gap or c["stopped_by"] != "tau":
        L.append("  ⚠ 本次结论不可当作「看懂了」的证据 —— 覆盖度是相对 τ 自洽的，")
        L.append("    只说明「所有帧都能被某个采样点代表」，不说明信息没被漏掉。")
    L.append(f"  平均距离 {c['mean_dist']}（越小说明帧越贴近其代表点；随 τ 收紧而下降是正常的）")

    big = sorted(plan["samples"], key=lambda s: -s["represents_s"])[:5]
    L.append("")
    L.append("代表段最长的 5 个采样点（重复/静止内容被合并到它们身上）：")
    for s in big:
        L.append(f"  #{s['k']:<3} {mmss(s['t'])}  代表 {len(s['represents_ranges'])} 段共 {s['represents_s']}s   "
                 f"[版{s['sheet']} 行{s['row'] + 1} 列{s['col'] + 1}]")
    return "\n".join(L)


# ----------------------------------------------------------------- 图版
def _font(size):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _heat(v):
    """v = 距最近采样点的距离 / τ。≤1 画绿（已覆盖，越靠近 τ 越浅），>1 画红（盲区）。"""
    if v > 1.0:
        return (225, 40, 40)
    return (int(30 + 60 * v), int(130 + 70 * v), int(75 + 35 * v))


def render_sheets(video, plan, outdir):
    from PIL import Image, ImageDraw
    p, cols = plan["params"], plan["params"]["cols"]
    per = cols * cols
    cw = SHEET_W // cols - 2
    W, H = plan["size"]
    ch = int(round(cw * H / W))
    dur, mind, tau = plan["duration_s"], plan["_mind"], p["tau"]
    fps = p["fps"]
    samples = plan["samples"]
    nsheet = (len(samples) + per - 1) // per
    font_c, font_h = _font(max(10, cw // 8)), _font(11)
    all_t = [s["t"] for s in samples]
    paths = []
    tmp = tempfile.mkdtemp(prefix="cumsample_")
    for sh_i in range(nsheet):
        chunk = samples[sh_i * per:(sh_i + 1) * per]
        rows = (len(chunk) + cols - 1) // cols
        head, strip = 16, 30
        img = Image.new("RGB", (cols * (cw + 2), head + rows * (ch + 2) + strip), (18, 18, 18))
        d = ImageDraw.Draw(img)
        d.text((4, 2), f"sheet {sh_i + 1}/{nsheet}  {mmss(chunk[0]['t'])}-{mmss(chunk[-1]['t'])}  "
                       f"{len(chunk)} samples  radius {plan['coverage']['radius']} (tau {tau})", fill=(220, 220, 220), font=font_h)
        for n, s in enumerate(chunk):
            fp = os.path.join(tmp, f"f{s['k']}.jpg")
            _run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{s['t']:.3f}", "-i", video,
                  "-frames:v", "1", "-vf", f"scale={cw}:{ch}", "-q:v", "3", fp])
            x, y = (n % cols) * (cw + 2), head + (n // cols) * (ch + 2)
            if os.path.exists(fp):
                img.paste(Image.open(fp).convert("RGB"), (x, y))
            lab = f"#{s['k']} {mmss(s['t'])}"
            tw = d.textlength(lab, font=font_c)
            d.rectangle([x, y, x + tw + 6, y + font_c.size + 4], fill=(0, 0, 0))
            d.text((x + 3, y + 1), lab, fill=(255, 255, 80), font=font_c)
        # 覆盖时间轴
        sy = head + rows * (ch + 2) + 2
        wpx = img.width
        n_fr = len(mind)
        for x in range(wpx):
            a = int(x / wpx * n_fr)
            b = max(a + 1, int((x + 1) / wpx * n_fr))
            v = float(np.max(mind[a:b])) / max(tau, 1e-9)
            d.line([(x, sy), (x, sy + 13)], fill=_heat(v))
        for t in all_t:
            x = min(wpx - 1, int(t / dur * wpx))
            d.line([(x, sy + 15), (x, sy + 22)], fill=(110, 110, 110))
        for s in chunk:
            x = min(wpx - 1, int(s["t"] / dur * wpx))
            d.line([(x, sy), (x, sy + 25)], fill=(60, 170, 255), width=2)
        d.text((4, sy + 16), "green=covered  RED=blind(>tau)  blue=this sheet", fill=(200, 200, 200), font=font_h)
        path = os.path.join(outdir, f"sheet_{sh_i + 1:02d}.jpg")
        img.save(path, quality=88)
        paths.append(path)
    return paths


# ----------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="按覆盖采样（k-center + 骨架 + 覆盖账本）")
    ap.add_argument("video")
    ap.add_argument("--out", default=None, help="输出目录（默认：视频同目录下 <名字>_cumsample）")
    ap.add_argument("--fps", type=float, default=4.0, help="分析帧率；短于 1/fps 秒的事件会漏（默认 4）")
    ap.add_argument("--metric", choices=["global", "local", "both"], default="global")
    ap.add_argument("--tau", default="auto",
                    help="覆盖半径阈值。默认 auto = 本素材的「相邻帧距离 p90 × 0.75」"
                         "（真实素材实测的可用值）；也可给具体数字（合成素材标定的 global=0.05 "
                         "在真实视频上过小，会把每对相邻帧都判成新内容）")
    ap.add_argument("--thumb", default="32x18",
                    help="缩略图尺寸，宽须为 8 的倍数、高须为 6 的倍数（默认 32x18；"
                         "想看清文字变化可用 96x54，内存约 9 倍）")
    ap.add_argument("--max", type=int, default=200, help="采样点上限（超过则报盲区）")
    ap.add_argument("--skeleton", type=float, default=30.0, help="相邻采样点最大间隔（秒），0=关闭")
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--no-sheet", action="store_true", help="只出 plan.json / ledger.txt，不渲染图版")
    a = ap.parse_args()

    out = a.out or os.path.splitext(a.video)[0] + "_cumsample"
    os.makedirs(out, exist_ok=True)
    tw, th = (int(v) for v in a.thumb.lower().split("x"))
    plan = analyze(a.video, a.fps, a.tau, a.max, a.skeleton, a.metric, (tw, th), a.cols)
    txt = ledger_text(plan)
    if not a.no_sheet:
        render_sheets(a.video, plan, out)
    mind = plan.pop("_mind")
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "ledger.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    with open(os.path.join(out, "times.txt"), "w") as f:
        f.write("\n".join(str(s["t"]) for s in plan["samples"]) + "\n")
    print(txt)
    print(f"\n输出目录: {out}")


if __name__ == "__main__":
    main()
