#!/usr/bin/env python3
"""subband.py —— 字幕带 OCR：把整条字幕轨一次性抠出来

为什么需要它
------------
"画面里的小字读不出来"是图版路线的硬伤：一张 5x5 图版每格只有 149px，
屏幕上的字幕/UI 文字糊成一团。但字幕类文字有两条可利用的性质：

  1. 位置固定 —— 字幕永远在同一条横带里
  2. 读一次就够 —— 不需要让 Agent 逐帧看图

所以正确做法不是"把字幕也拼进图版"，而是只 OCR 那一条带、再去重。
结果是**整条字幕轨，零图片 token**。

适用于：有硬字幕的视频（对白/唱词/解说）、新闻与综艺的下三分之一。
不适用于：没有字幕的视频 —— 本工具会明确说"没找到"，不会硬编。

关于"静态元素（台标/图卡）污染字幕轨"——**试过三种办法，全部有害，已默认关闭**
--------------------------------------------------------------------------
现象：新闻素材里静态的「新闻联播」台标被反复抽出，且 OCR 每次抖动成不同写法
      （XINWDN:LIANBO / CNWTNLIANBO / YINWTN:LIANBO …），把整条轨塞满。

试过并失败的三个方案（都在 --mask-static 里，默认关）：
  1. 文字框位置聚类 + 文本稳定度  → **完全没触发**。
     原因：RapidOCR 把台标和标题**并成了同一个框**（实测日志：
     `XINWTN:LIANBO 新闻联播 毒性之争引I发的PX"词条保卫战"`），
     所谓"同一位置的文字"里混着会变的标题，稳定度永远不够。
  2. 像素级遮罩（整片算时间维 σ） → **σ<3 的像素占 0%**，等于没遮。
     原因：那条素材有 31 个切点，整片统计量的是"镜头之间的差异"而不是"静 vs 动"。
     按窗口细分就露馅：全片 σ 中位 75.7，而 5s~8s 单镜头内 σ 中位 0.49、σ<3 占 86%。
  3. 按镜头遮罩 / 多数镜头投票 → **真的遮上了，但把字幕也遮掉了**。
     合成素材（只有变化字幕、无台标）实测：
        无遮罩           13/13 = 100%
        shot 模式        12 条，「到底是低毒还是剧毒」被截成「底是低毒还是居」
        global 多数票    13 条但 11/13 = 85%，仍截断
     原因无法调和：**某个像素在多数镜头里是背景、在少数镜头里是文字**，
     抹掉它就切断了文字。"台标"与"持续两三秒的字幕"在像素层面无法区分。

结论：**像素级静态遮罩在这类素材上做不安全。** 代码留着（--mask-static）供以后研究，
但默认关闭；真要用请先在没有台标的素材上确认不掉准确率。

可行的方向（未实现）：**文字级**处理 —— 对同一位置的多次读法求最长公共子串，
把反复出现的那段当静态前缀剥掉，保留会变的部分。这比像素方案更贴近实际失效点。

性能要点（重要）
----------------
proot 容器里**单次 OCR 要 2~15 秒**（热节流影响很大）。逐帧 OCR 一条 80 秒的视频
要 160 次调用 ≈ 800 秒，**根本跑不完**。所以本工具默认**拼批**：
把 8 个时刻的带图竖着拼成一张，一次 OCR 认完，按 y 坐标归位。
调用次数降一个数量级，实测 80 秒素材从 >900 秒降到约 100 秒。

用法
----
    python3 subband.py 视频.mp4 --out 输出目录          # 全自动
    python3 subband.py 视频.mp4 --band 300:360          # 手工指定带（像素，x 全宽）
    python3 subband.py 视频.mp4 --batch 12 --upscale 2  # 调参

产出
----
    subband.json   带位置 + 逐条字幕（起止时间、文本、置信度、抖动次数）
    subband.txt    人读的版式
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

import numpy as np


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def video_info(path):
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height",
              "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", path])
    vals = [x for x in r.stdout.strip().split("\n") if x.strip()]
    if len(vals) < 3:
        sys.exit("ffprobe 读取失败：" + path + "\n" + r.stderr[:300])
    return int(vals[0]), int(vals[1]), float(vals[2])


def grab_frame(path, t, out_png, crop=None, scale=1):
    """取 t 时刻的一帧。crop=(x,y,w,h)；内部会转成 ffmpeg 的 crop=w:h:x:y。"""
    vf = []
    if crop:
        vf.append("crop=%d:%d:%d:%d" % (crop[2], crop[3], crop[0], crop[1]))
    if scale and scale != 1:
        vf.append("scale=iw*%s:ih*%s:flags=lanczos" % (scale, scale))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", "%.3f" % t, "-i", path, "-frames:v", "1"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += [out_png]
    return _run(cmd).returncode == 0 and os.path.exists(out_png)


_OCR = None


def ocr(img_path, min_score=0.0):
    """返回 [{text, score, y, y0, y1, x0, x1}, ...]（图像坐标）。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    r = _OCR(img_path)
    res = r[0] if r and r[0] else []
    out = []
    for item in res:
        try:
            box, text, score = item[0], item[1], float(item[2])
        except Exception:
            continue
        if score < min_score:
            continue
        ys = [float(p[1]) for p in box]
        xs = [float(p[0]) for p in box]
        out.append({"text": text, "score": score,
                    "y": sum(ys) / len(ys), "y0": min(ys), "y1": max(ys),
                    "x0": min(xs), "x1": max(xs)})
    return out


# --------------------------------------------------------------- 找字幕带

def find_band(video, W, H, dur, sample_fps=0.5, max_samples=40):
    """采样若干帧做全帧 OCR，找"文字反复出现的 y 区间"。

    用直方图 + 滑窗，而不是"假设在底部" —— 台标在角上、时间戳可能在顶部，
    位置并不固定；滑窗还能自动排除只在个别帧出现的偶然文字。
    """
    n = int(min(max_samples, max(3, dur * sample_fps)))
    ts = np.linspace(0.0, max(0.0, dur - 0.05), n)
    tmpd = tempfile.mkdtemp(prefix="subband_")
    boxes = []
    for i, t in enumerate(ts):
        p = os.path.join(tmpd, "s%d.png" % i)
        if not grab_frame(video, float(t), p):
            continue
        boxes.extend(ocr(p))
        if os.path.exists(p):
            os.unlink(p)
    if not boxes:
        return None, {"n_probe_frames": n, "n_text_boxes": 0,
                      "reason": "整片未检出任何文字（这条素材大概没有硬字幕）"}
    centers = np.array([b["y"] for b in boxes], dtype=float)
    heights = np.array([b["y1"] - b["y0"] for b in boxes], dtype=float)
    med_h = float(np.median(heights)) if len(heights) else 20.0
    win = max(med_h * 4.0, H * 0.08)
    best, best_lo = -1, 0.0
    for lo in np.arange(0.0, max(1.0, H - win * 0.5), max(1.0, win / 8)):
        c = int(((centers >= lo) & (centers <= lo + win)).sum())
        if c > best:
            best, best_lo = c, float(lo)
    inside = [b for b in boxes if best_lo <= b["y"] <= best_lo + win]
    if inside:
        y0 = max(0.0, min(b["y0"] for b in inside) - med_h * 0.6)
        y1 = min(float(H), max(b["y1"] for b in inside) + med_h * 0.6)
    else:
        y0, y1 = best_lo, best_lo + win
    info = {"n_probe_frames": n, "n_text_boxes": len(boxes),
            "band_in_boxes": best, "band_coverage": round(best / len(boxes), 3),
            "median_text_height_px": round(med_h, 1)}
    return {"y0": round(y0, 1), "y1": round(y1, 1), "x0": 0.0, "x1": float(W)}, info


# --------------------------------------------------------------- 去重合并

def _norm(s):
    return re.sub(r"[\s，。、！？：；（）()【】“”\"'’‘·.,!?:;|]+", "", s or "")


def _sim(a, b):
    """归一化后的相似度（1 - 编辑距离/较长长度）。"""
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if abs(la - lb) / max(la, lb) > 0.5:
        return 0.0
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[lb] / max(la, lb)


def merge(rows, sim_thresh=0.7, max_gap=1.2):
    """逐时刻 OCR → 字幕轨。

    相邻时刻相似 → 延长同一条（抗 OCR 抖动）；中间空太久 → 断开另起。
    每条取"出现次数最多的读法"当代表。
    """
    entries = []
    for r in rows:
        txt = (r["text"] or "").strip()
        if not txt:
            continue
        if entries:
            e = entries[-1]
            if r["t"] - e["t1"] <= max_gap and _sim(txt, e["text"]) >= sim_thresh:
                e["t1"] = r["t"]
                e["hits"] += 1
                e["_all"].append(txt)
                if r["score"] > e["score"]:
                    e["text"], e["score"] = txt, r["score"]
                continue
        entries.append({"t0": r["t"], "t1": r["t"], "text": txt, "score": r["score"],
                        "hits": 1, "_all": [txt]})
    for e in entries:
        c = Counter(_norm(x) for x in e["_all"])
        if c:
            top = c.most_common(1)[0][0]
            for x in e["_all"]:
                if _norm(x) == top:
                    e["text"] = x
                    break
        e["duration"] = round(e["t1"] - e["t0"], 2)
        e["variants"] = len(set(_norm(x) for x in e["_all"]))
        e.pop("_all")
    return entries


def mmss(t):
    t = float(t)
    return "%d:%02d" % (int(t // 60), int(t % 60))


def _ocr_stack(video, idxs, fps, by, W, bh, upscale, tmpd, batch_tag, min_score,
               mask_of=None):
    """把一批时刻的带图竖拼成一张再 OCR，按 y 归位。返回 {i: [items]}"""
    from PIL import Image
    imgs = []
    for i in idxs:
        p = os.path.join(tmpd, "b%d.png" % i)
        im = None
        if grab_frame(video, i / fps, p, crop=(0, by, W, bh), scale=upscale):
            if mask_of is not None:
                apply_mask(p, mask_of(i))
            try:
                im = Image.open(p).convert("RGB")
                im.load()
            except Exception:
                im = None
        if os.path.exists(p):
            os.unlink(p)
        imgs.append(im)
    if not any(imgs):
        return {}
    sep = 6
    W2 = max(im.width for im in imgs if im is not None)
    H2 = sum(im.height for im in imgs if im is not None) + sep * (len(idxs) - 1)
    stack = Image.new("RGB", (W2, max(1, H2)), (255, 0, 255))
    bands, y = [], 0
    for i, im in zip(idxs, imgs):
        if im is None:
            continue
        stack.paste(im, (0, y))
        bands.append((i, y, y + im.height))
        y += im.height + sep
    sp = os.path.join(tmpd, "stack_%s.png" % batch_tag)
    stack.save(sp)
    its = ocr(sp, min_score)
    os.unlink(sp)
    per = {}
    for it in its:
        for i, ya, yb in bands:
            if ya <= it["y"] < yb:
                per.setdefault(i, []).append(it)
                break
    return per


# ------------------------------------------------- 静态元素遮罩（按镜头）

def shot_masks(video, W, dur, by, bh, upscale, tmpd,
               cut_thresh=18.0, min_shot=4, thresh=6.0, max_frames=200,
               mode="shot"):
    """**按镜头**算静态像素遮罩 —— 本工具最关键的一处修正。

    实测教训（为什么不能整片算）
    ---------------------------
    第一版按「全片 24 帧」算时间维标准差，结果 σ 中位 75.7、σ<3 的像素占 0% ——
    看起来"整个画面都在变"，方法像是失效了。按窗口细分就露馅了：

        全片        σ 中位 75.73   σ<3 占  0.0%
        5s~8s 内    σ 中位  0.49   σ<3 占 86.0%   ← 单镜头内

    那条素材有 31 个切点。**整片统计量的是"镜头之间的差异"，不是"静 vs 动"。**

    做法：抓一批带图 → 相邻帧差找切点 → 分镜 → 每个镜头内部算时间维 σ，
    σ<thresh 的像素即静态（台标/固定图卡）；OCR 前把这些像素抹成平色。
    镜头帧数少于 min_shot 就不遮罩，避免误判。

    返回 shots = [{t0, t1, n_frames, mask|None, static_fraction|None}, ...]
    """
    import numpy as _np
    from PIL import Image
    mf = 200 if max_frames is None else int(max_frames)
    k = max(12, min(mf, int(dur * 2) if dur > 0 else 24))
    ts = _np.linspace(0.5, max(0.6, dur - 0.5), k) if dur > 1 else _np.linspace(0, 1, 12)
    frames, kt = [], []
    for i, t in enumerate(ts):
        p = os.path.join(tmpd, "smf%d.png" % i)
        if not grab_frame(video, float(t), p, crop=(0, by, W, bh), scale=upscale):
            continue
        try:
            im = Image.open(p).convert("L")
            im.load()
            frames.append(_np.asarray(im, dtype=_np.float32))
            kt.append(float(t))
        except Exception:
            pass
        if os.path.exists(p):
            os.unlink(p)
    if len(frames) < min_shot:
        return []
    cuts = [0]
    for i in range(1, len(frames)):
        d = float(_np.abs(frames[i] - frames[i - 1]).mean())
        if d > cut_thresh or (kt[i] - kt[i - 1]) > 3.0:
            cuts.append(i)
    cuts.append(len(frames))
    shots = []
    for si in range(len(cuts) - 1):
        a_, b_ = cuts[si], cuts[si + 1]
        idx = list(range(a_, b_))
        t0, t1 = kt[a_], kt[min(b_, len(kt) - 1)]
        if len(idx) < min_shot:
            shots.append({"t0": round(t0, 2), "t1": round(t1, 2), "n_frames": len(idx),
                          "mask": None, "static_fraction": None})
            continue
        A = _np.stack([frames[i] for i in idx], 0)
        m = A.std(axis=0) < float(thresh)
        shots.append({"t0": round(t0, 2), "t1": round(t1, 2), "n_frames": len(idx),
                      "mask": m, "static_fraction": round(float(m.mean()), 3)})
    if mode == "global":
        # 只有「在多数镜头里都静态」的像素才抹掉 —— 这是区分「持续叠加的台标」
        # 与「持续两三秒的字幕」的关键：
        #   台标在几乎每个镜头里都不变 → 多数票通过 → 该抹
        #   字幕每句文字不同，同一像素在不同镜头里取值不同 → 少数票 → 不该抹
        got = [x["mask"] for x in shots if x.get("mask") is not None]
        if got:
            import numpy as _np
            vote = _np.mean(_np.stack([g.astype(_np.float32) for g in got], 0), axis=0)
            gm = vote >= 0.8
            for x in shots:
                if x.get("mask") is not None:
                    x["mask"] = gm
                    x["static_fraction"] = round(float(gm.mean()), 3)
    return shots


def mask_at(shots, t):
    if not shots:
        return None
    for sh in shots:
        if sh["t0"] <= t <= sh["t1"]:
            return sh.get("mask")
    return min(shots, key=lambda sh: min(abs(t - sh["t0"]), abs(t - sh["t1"]))).get("mask")


def apply_mask(img_path, mask, fill=127):
    if mask is None:
        return False
    import numpy as _np
    from PIL import Image
    try:
        im = Image.open(img_path).convert("RGB")
        a = _np.asarray(im).copy()
        if mask.shape[:2] != a.shape[:2]:
            return False
        a[mask] = fill
        Image.fromarray(a).save(img_path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="字幕带 OCR：把整条字幕轨一次抠出来")
    ap.add_argument("video")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--band", default="auto", help="字幕带 y0:y1（像素）；默认 auto 自动检测")
    ap.add_argument("--fps", type=float, default=2.0, help="带内 OCR 帧率（默认 2）")
    ap.add_argument("--upscale", type=int, default=1, help="OCR 前放大倍数（默认 1；实测放大到 2 不提升准确率，纯浪费）")
    ap.add_argument("--batch", type=int, default=8, help="每批拼几张带图一起 OCR（默认 8）")
    ap.add_argument("--sample-fps", type=float, default=0.5, help="自动找带时的采样帧率")
    ap.add_argument("--sim", type=float, default=0.7, help="合并两条字幕的相似度阈值")
    ap.add_argument("--max-gap", type=float, default=1.2, help="间隔超过此秒数即断开")
    ap.add_argument("--min-score", type=float, default=0.4, help="带内 OCR 的最低置信度")
    ap.add_argument("--mask-static", action="store_true", default=False,
                    help="⚠ 实验性且**实测有害**：像素级静态遮罩（抹掉台标/固定图卡）。"
                         "合成素材实测把 100% 打成 85%（字幕被截断），默认关闭。详见文件头说明。")
    ap.add_argument("--no-mask-static", dest="mask_static", action="store_false",
                    help="关闭静态遮罩（默认）")
    ap.add_argument("--mask-thresh", type=float, default=6.0,
                    help="镜头内时间维标准差低于此值算静像素（默认 6.0）")
    ap.add_argument("--cut-thresh", type=float, default=18.0,
                    help="判切点的帧差阈值（默认 18.0；越小切得越碎）")
    ap.add_argument("--mask-mode", choices=["shot", "global"], default="global",
                    help="shot=镜头内静态就抹（会把持续几秒的字幕也抹掉，实测有害）；"
                         "global=要在多数镜头里都静态才抹（默认，能区分台标与字幕）")
    a = ap.parse_args()

    out = a.out or os.path.splitext(a.video)[0] + "_subband"
    os.makedirs(out, exist_ok=True)
    W, H, dur = video_info(a.video)
    print("视频 %s  %dx%d  %s" % (os.path.basename(a.video), W, H, mmss(dur)))

    # 1) 定带
    if a.band == "auto":
        band, info = find_band(a.video, W, H, dur, a.sample_fps)
        if band is None:
            print("✗ 没找到字幕带：" + info.get("reason", ""))
            print("  这条素材大概没有硬字幕 —— 别走字幕带路线，"
                  "改走 ASR，或者（歌曲类）联网取歌词。")
            with open(os.path.join(out, "subband.json"), "w", encoding="utf-8") as f:
                json.dump({"video": os.path.abspath(a.video), "duration_s": dur,
                           "band": None, "entries": [], "band_info": info},
                          f, ensure_ascii=False, indent=1)
            return 2
        print("字幕带  y %.0f-%.0f（高 %.0fpx）  覆盖 %.0f%% 的检出框  文字中位高 %.0fpx"
              % (band["y0"], band["y1"], band["y1"] - band["y0"],
                 info["band_coverage"] * 100, info["median_text_height_px"]))
    else:
        y0, y1 = (float(x) for x in a.band.split(":"))
        band = {"y0": y0, "y1": y1, "x0": 0.0, "x1": float(W)}
        info = {"reason": "手工指定"}

    # 2) 分批 OCR 那条带
    bh = max(8, int(round(band["y1"] - band["y0"])))
    by = int(max(0, min(H - bh, round(band["y0"]))))
    n = max(1, int(dur * a.fps))
    nb = (n + a.batch - 1) // a.batch
    print("分批 OCR：%sfps × %d 个时刻，每批 %d 张 → %d 次调用（带内 %dx%d，放大 %d×）"
          % (a.fps, n, a.batch, nb, W, bh, a.upscale))
    tmpd = tempfile.mkdtemp(prefix="subband_")

    # 按镜头的静态遮罩（台标/固定图卡会被抹掉再 OCR）
    shots = []
    if a.mask_static:
        shots = shot_masks(a.video, W, dur, by, bh, a.upscale, tmpd,
                           a.cut_thresh, 4, a.mask_thresh, None, a.mask_mode)
        got = [x["static_fraction"] for x in shots if x.get("static_fraction") is not None]
        if shots:
            print("静态遮罩（按镜头）：切成 %d 段，%d 段做了遮罩，镜头内静像素占比中位 %.0f%%"
                  % (len(shots), len(got),
                     (sorted(got)[len(got)//2] if got else 0) * 100))
            for r in shots[:6]:
                if r.get("static_fraction") is not None:
                    print("   镜头 %5.1fs~%5.1fs  %2d 帧  静像素 %.0f%%"
                          % (r["t0"], r["t1"], r["n_frames"], r["static_fraction"] * 100))
        else:
            print("静态遮罩：样本不足，跳过")

    def _mask_of(i):
        return mask_at(shots, i / a.fps) if shots else None

    rows = []
    for b, b0 in enumerate(range(0, n, a.batch)):
        idxs = list(range(b0, min(n, b0 + a.batch)))
        per = _ocr_stack(a.video, idxs, a.fps, by, W, bh, a.upscale, tmpd, str(b),
                         a.min_score, _mask_of if shots else None)
        for i in idxs:
            got = per.get(i)
            if got:
                got.sort(key=lambda x: x["x0"])
                rows.append({"t": round(i / a.fps, 2),
                             "text": " ".join(x["text"] for x in got),
                             "score": max(x["score"] for x in got)})
        print("  … 批 %d/%d  已得 %d 个有字时刻" % (b + 1, nb, len(rows)))

    # 3) 合并
    entries = merge(rows, a.sim, a.max_gap)
    print("→ %d 个有字时刻，合并为 %d 条字幕" % (len(rows), len(entries)))

    result = {"video": os.path.abspath(a.video), "duration_s": round(dur, 2),
              "size": [W, H], "band": band, "band_info": info,
              "params": {"fps": a.fps, "upscale": a.upscale, "batch": a.batch,
                         "sim": a.sim, "max_gap": a.max_gap, "min_score": a.min_score},
              "stats": {"n_ocr_steps": n, "n_ocr_calls": nb, "n_text_moments": len(rows),
                        "n_entries": len(entries),
                        "total_text_seconds": round(sum(e["duration"] for e in entries), 1)},
              "shots": [{k: v for k, v in sh.items() if k != "mask"} for sh in shots],
              "entries": entries}
    with open(os.path.join(out, "subband.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    lines = ["字幕轨  %s  %dx%d  %s" % (os.path.basename(a.video), W, H, mmss(dur)),
             "字幕带  y %.0f-%.0f   %d 条" % (band["y0"], band["y1"], len(entries)), ""]
    for e in entries:
        lines.append("[%s→%s] (%4.1fs ×%-2d 置信%.2f) %s"
                     % (mmss(e["t0"]), mmss(e["t1"]), e["duration"], e["hits"],
                        e["score"], e["text"]))
    txt = "\n".join(lines)
    with open(os.path.join(out, "subband.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print()
    print(txt[:2500])
    print("\n输出目录: " + out)


if __name__ == "__main__":
    main()
