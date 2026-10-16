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


def _ocr_stack(video, idxs, fps, by, W, bh, upscale, tmpd, batch_tag, min_score):
    """把一批时刻的带图竖拼成一张再 OCR，按 y 归位。返回 {i: [items]}"""
    from PIL import Image
    imgs = []
    for i in idxs:
        p = os.path.join(tmpd, "b%d.png" % i)
        im = None
        if grab_frame(video, i / fps, p, crop=(0, by, W, bh), scale=upscale):
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
    rows = []
    for b, b0 in enumerate(range(0, n, a.batch)):
        idxs = list(range(b0, min(n, b0 + a.batch)))
        per = _ocr_stack(a.video, idxs, a.fps, by, W, bh, a.upscale, tmpd, str(b), a.min_score)
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
