#!/usr/bin/env python3
"""verify.py —— 给"我看懂了吗"一个可量化的检验

现在最要命的问题是：**没有任何 ground truth**。我说"看懂了"，拿什么证明？
这个工具提供两种不依赖人工标注的检验。

检验 A：插针（needle）
----------------------
往素材里**故意插进已知的东西**，看采样/理解流程能不能发现它。
答案是自己放的，所以**不需要标注**。

两种插针：
  1. 天然针 —— 素材里本来就有的、我知道确切时刻的画面
     （例：《首都》MV 4:57.4 / 4:59.1 那两块「北京/香港」书法牌子）
  2. 合成针 —— 用 ffmpeg 把字卡/短镜头插进素材随机位置

用法：
    python3 verify.py needle 视频.mp4 --at 297.4,299.1 --tol 3.0        # 用 cumsample 现采
    python3 verify.py needle 视频.mp4 --at 297.4 --plan 目录/plan.json  # 查已有的采样方案
    python3 verify.py needle 视频.mp4 --at 297.4 --synthesize out.mp4 --at 100 --insert-text "测试"

检验 B：留出帧（holdout）
-------------------------
**先写总结，再抽没用过的帧，先预测内容再看。** 猜错的那些就是"没看到的东西"，
猜错率 = 覆盖失败率。

**顺序不能颠倒** —— 先看图再"预测"等于作弊。所以拆成三步：

    python3 verify.py holdout gen   视频.mp4 --plan 目录/plan.json --n 25 --out 目录
        → 只给时刻，不给图。产出 holdout.json（预测字段留空）
    （填预测：把 holdout.json 里每条的 "predict" 写上你预计画面里有什么）
    python3 verify.py holdout reveal 目录/holdout.json     → 这时才出图
    python3 verify.py holdout score  目录/holdout.json     → 算意外率 + 逐条对照

留出帧的挑选：**避开已采样点**（默认距任何采样点 <5 秒的不要），
这样测的才是"没看到的部分"。
"""

import argparse
import json
import os
import random
import subprocess
import sys

import numpy as np


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def video_info(path):
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height",
              "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    v = [x for x in r.stdout.strip().split("\n") if x.strip()]
    if len(v) < 3:
        sys.exit("ffprobe 失败：" + path)
    return int(v[0]), int(v[1]), float(v[2])


def mmss(t):
    return "%d:%02d" % (int(t // 60), int(t % 60))


def load_plan(p):
    """从 plan.json 里取采样时刻。"""
    d = json.load(open(p, encoding="utf-8"))
    return sorted(x["t"] for x in d.get("samples", []))


# --------------------------------------------------------------- 检验 A：插针

def cmd_needle(a):
    W, H, dur = video_info(a.video)
    needles = [float(x) for x in a.at.split(",") if x.strip()]
    if a.plan:
        times = load_plan(a.plan)
        src = "已有方案 " + a.plan
    else:
        # 现采：调 cumsample
        import tempfile
        tmp = tempfile.mkdtemp(prefix="verify_")
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cumsample.py"),
               a.video, "--out", tmp, "--no-sheet", "--max", str(a.max)]
        if a.tau:
            cmd += ["--tau", str(a.tau)]
        print("正在采样（cumsample）…")
        r = _run(cmd)
        if not os.path.exists(os.path.join(tmp, "plan.json")):
            sys.exit("采样失败：\n" + (r.stdout + r.stderr)[-600:])
        times = load_plan(os.path.join(tmp, "plan.json"))
        src = "本次现采"
    times = np.array(times)

    print("素材 %s  %dx%d  %s" % (os.path.basename(a.video), W, H, mmss(dur)))
    print("采样方案：%s，共 %d 个采样点" % (src, len(times)))
    print("插针 %d 个（判定容差 %.1f 秒）" % (len(needles), a.tol))
    print()
    hit = 0
    rows = []
    for t in needles:
        if len(times) == 0:
            rows.append((t, None, None)); continue
        i = int(np.argmin(np.abs(times - t)))
        gap = float(abs(times[i] - t))
        ok = gap <= a.tol
        hit += ok
        rows.append((t, times[i], gap))
        print("  %s 针 @%s → 最近采样点 @%s  相距 %5.2fs  %s"
              % ("✓" if ok else "✗", mmss(t), mmss(times[i]), gap, "" if ok else "← 漏了"))
    print()
    print("命中 %d/%d = %.0f%%" % (hit, len(needles), 100.0 * hit / max(1, len(needles))))
    if a.json_out:
        json.dump({"video": os.path.abspath(a.video), "tol": a.tol, "plan": src,
                   "n_samples": len(times),
                   "needles": [{"t": t, "nearest": n, "gap": g,
                                "hit": bool(g is not None and g <= a.tol)}
                               for t, n, g in rows]},
                  open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("→ " + a.json_out)
    return 0 if hit == len(needles) else 1


def cmd_synthesize(a):
    """合成插针：往素材里插字卡或短镜头。"""
    W, H, dur = video_info(a.video)
    times = [float(x) for x in a.at.split(",") if x.strip()]
    if not times:
        sys.exit("--at 需要给至少一个插入时刻")
    vf = []
    for i, t in enumerate(times):
        txt = a.insert_text or ("NEEDLE%d" % (i + 1))
        vf.append("drawtext=text='%s':fontsize=%d:fontcolor=white:box=1:boxcolor=black@0.85:"
                  "boxborderw=12:x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,%.2f,%.2f)'"
                  % (txt, max(18, H // 12), t, t + a.insert_len))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", a.video,
           "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
           "-c:a", "copy", a.synthesize]
    r = _run(cmd)
    if not os.path.exists(a.synthesize):
        sys.exit("合成失败：\n" + r.stderr[-500:])
    print("✓ 已合成插入素材：" + a.synthesize)
    print("  插了 %d 处：%s（每处持续 %.1fs）"
          % (len(times), ", ".join(mmss(t) for t in times), a.insert_len))
    print("  接着跑：python3 verify.py needle %s --at %s --tol %.1f"
          % (a.synthesize, ",".join(str(t) for t in times), a.insert_len / 2 + 0.5))
    return 0


# --------------------------------------------------------------- 检验 B：留出帧

def _pick_holdout(dur, times, n, min_gap, seed):
    """随机挑 n 个时刻，避开已采样点 min_gap 秒。"""
    rnd = random.Random(seed)
    times = np.array(times) if len(times) else np.array([])
    picked, tries = [], 0
    while len(picked) < n and tries < n * 400:
        tries += 1
        t = rnd.uniform(0.5, max(0.6, dur - 0.5))
        if len(times) and float(np.min(np.abs(times - t))) < min_gap:
            continue
        if any(abs(t - p) < 2.0 for p in picked):
            continue
        picked.append(round(t, 2))
    return sorted(picked)


def cmd_holdout_gen(a):
    W, H, dur = video_info(a.video)
    times = load_plan(a.plan) if a.plan else []
    picked = _pick_holdout(dur, times, a.n, a.min_gap, a.seed)
    os.makedirs(a.out, exist_ok=True)
    d = {"video": os.path.abspath(a.video), "duration_s": dur, "size": [W, H],
         "plan": a.plan or "(未给，不去重)",
         "n_samples_reference": len(times), "min_gap_s": a.min_gap, "seed": a.seed,
         "frames": [{"t": t, "predict": "", "seen": "", "surprise": None} for t in picked],
         "how_to": ["1) 在每条的 predict 里写「你预计这一时刻画面里有什么」，" 
                    "不要先看图（先看就等于作弊）",
                    "2) 跑 reveal 出图",
                    "3) 在 seen 里写「实际看到了什么」",
                    "4) 跑 score 算意外率"]}
    p = os.path.join(a.out, "holdout.json")
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("留出帧已挑好：%d 个（避开采样点 %.1f 秒内，共参照 %d 个采样点）"
          % (len(picked), a.min_gap, len(times)))
    print("** 只给时刻、不给图 ** —— 请先写预测：")
    for f in d["frames"]:
        print("    t=%s" % mmss(f["t"]))
    print()
    print("预测写到：" + p + " 的每条 \"predict\" 字段")
    print("然后跑：python3 verify.py holdout reveal \"%s\"" % p)
    return 0


def cmd_holdout_reveal(a):
    d = json.load(open(a.holdout, encoding="utf-8"))
    outdir = os.path.dirname(os.path.abspath(a.holdout))
    missing = [f["t"] for f in d["frames"] if not (f.get("predict") or "").strip()]
    if missing and not a.force:
        print("⚠ 还有 %d 条没写预测：%s" % (len(missing), ", ".join(mmss(t) for t in missing[:8])))
        print("  先写预测再看图 —— 否则这个测验就没意义了。")
        print("  （确实要先看的，加 --force）")
        return 2
    fdir = os.path.join(outdir, "holdout_frames")
    os.makedirs(fdir, exist_ok=True)
    paths = []
    W, H = d["size"]
    for i, f in enumerate(d["frames"], 1):
        p = os.path.join(fdir, "h%02d.png" % i)
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % f["t"],
              "-i", d["video"], "-frames:v", "1", "-vf", "scale=320:-1", p])
        if os.path.exists(p):
            paths.append(p)
    # 拼成联络表
    cols = 5
    rows = (len(paths) + cols - 1) // cols
    if paths:
        sheet = os.path.join(outdir, "holdout_sheet.jpg")
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-pattern_type", "glob", "-i", os.path.join(fdir, "h*.png"),
              "-filter_complex", "tile=%dx%d" % (cols, rows), "-frames:v", "1", sheet])
        print("✓ 联络表：" + sheet)
    print("帧目录：" + fdir)
    print("现在去看图，然后在 holdout.json 的 \"seen\" 里写实际内容，再跑 score。")
    return 0


def cmd_holdout_score(a):
    d = json.load(open(a.holdout, encoding="utf-8"))
    n = len(d["frames"])
    no_pred = [f for f in d["frames"] if not (f.get("predict") or "").strip()]
    no_seen = [f for f in d["frames"] if not (f.get("seen") or "").strip()]
    if no_seen:
        print("⚠ 还有 %d 条没写 seen（实际看到什么）：" % len(no_seen))
        for f in no_seen[:6]:
            print("    t=%s" % mmss(f["t"]))
        return 2

    def norm(s):
        import re
        return re.sub(r"[\s，。、！？：；（）()【】\"'’‘·.,!?:;|-]+", "", (s or "").lower())

    su = 0
    print("留出帧测验 · 逐条对照")
    print("=" * 70)
    for i, f in enumerate(d["frames"], 1):
        a_, b_ = norm(f["predict"]), norm(f["seen"])
        if not a_ or not b_:
            sur = None
        else:
            # 简单重合判定：任一方的主要词出现在另一方里就算"预测到"
            overlap = 0
            for L in range(min(len(a_), len(b_)), 1, -1):
                found = False
                for s in range(0, len(a_) - L + 1):
                    if a_[s:s + L] in b_:
                        overlap = L; found = True; break
                if found:
                    break
            sur = not (overlap >= max(2, min(len(a_), len(b_)) // 3))
        f["surprise"] = sur
        if sur:
            su += 1
        mk = "?" if sur is None else ("意外" if sur else "猜到")
        print("%2d t=%-6s [%s]  预测「%s」 → 实际「%s」"
              % (i, mmss(f["t"]), mk, (f["predict"] or "")[:22], (f["seen"] or "")[:22]))
    print("=" * 70)
    scored = [f for f in d["frames"] if f["surprise"] is not None]
    print("意外 %d/%d = %.0f%%" % (su, len(scored),
                                 100.0 * su / max(1, len(scored))))
    print("（意外率 = 覆盖失败率：这些是我没看到、也没料到的内容）")
    if no_pred:
        print("注：有 %d 条当时没写预测，未计入" % len(no_pred))
    json.dump(d, open(a.holdout, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


def main():
    ap = argparse.ArgumentParser(description="给「我看懂了吗」一个可量化的检验")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("needle", help="插针检验：已知时刻的内容有没有被采到")
    p.add_argument("video")
    p.add_argument("--at", required=True, help="逗号分隔的插针时刻（秒）")
    p.add_argument("--tol", type=float, default=3.0, help="判定容差秒数（默认 3）")
    p.add_argument("--plan", default=None, help="已有的 plan.json；不给就现采")
    p.add_argument("--tau", default=None, help="现采时的 τ")
    p.add_argument("--max", type=int, default=200)
    p.add_argument("--json-out", default=None)
    p.set_defaults(fn=cmd_needle)

    p = sub.add_parser("synthesize", help="合成插针素材（往视频里插字卡）")
    p.add_argument("video")
    p.add_argument("--at", required=True, help="逗号分隔的插入时刻（秒）")
    p.add_argument("--insert-text", default=None, help="字卡文字（默认 NEEDLE1/2/…）")
    p.add_argument("--insert-len", type=float, default=1.5, help="每处持续秒数（默认 1.5）")
    p.add_argument("--synthesize", required=True, help="输出文件")
    p.set_defaults(fn=cmd_synthesize)

    p = sub.add_parser("holdout", help="留出帧测验")
    h = p.add_subparsers(dest="hcmd", required=True)
    q = h.add_parser("gen", help="挑留出帧（只给时刻，不出图）")
    q.add_argument("video"); q.add_argument("--plan", default=None)
    q.add_argument("--n", type=int, default=25); q.add_argument("--out", required=True)
    q.add_argument("--min-gap", type=float, default=5.0, help="距采样点至少这么远（默认 5s）")
    q.add_argument("--seed", type=int, default=42)
    q.set_defaults(fn=cmd_holdout_gen)
    q = h.add_parser("reveal", help="出图（先确认预测已写）")
    q.add_argument("holdout"); q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_holdout_reveal)
    q = h.add_parser("score", help="算意外率")
    q.add_argument("holdout")
    q.set_defaults(fn=cmd_holdout_score)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
