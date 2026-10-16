#!/usr/bin/env python3
"""subband.py —— 字幕带 OCR：把整条字幕轨一次性抠出来

关于"静态元素（台标/图卡）污染字幕轨"——**功能已删除，原因见下**
--------------------------------------------------------------------------
现象：新闻素材里静态的「新闻联播」台标被反复抽出，且 OCR 每次抖动成不同写法
      （XINWDN:LIANBO / CNWTNLIANBO / YINWTN:LIANBO …），把整条轨塞满。

★ 2026-10-16 三方独立审查后的**订正**（我先前的归因有错，这里保留记录）：

订正 1：我说「RapidOCR 把台标和标题并成了同一个框」——**错**。
        实测 RapidOCR 返回的是独立框（台标 x 56-130、正文 x 144-598，完全不重叠），
        `return_word_box=True` 也可用。
        **并框是本文件自己干的**：早先主循环里 `" ".join(x["text"] for x in got)`
        把同一时刻所有框拼成一行，位置信息在那一步就丢了。这是**实现 bug**，
        不是 API 限制 —— 也是「按位置分轨」这条路被误判为走不通的真正原因。

订正 2：我说「像素级静态遮罩在这类素材上做不安全」——**话说反了**。
        实测（复刻本站的投票流程）真实素材上：
            台标区 vote 中位 0.77 → ≥0.8 抹掉台标 **9%**
            正文区 vote 中位 0.85 → ≥0.8 抹掉正文 **75%**
        而且分区 σ 是：台标「新闻联播」 **9.08**（有旋转地球/光效，是**动**的），
        新闻的下三分之一图文卡 **1.79**（是**静**的）。
        → **这类素材里"静止"恰恰是要保留内容的特征，用"时间维方差"当判据方向是反的。**
        另：合成素材上把阈值 0.8 改成 1.0 就能把 1/7 修到 6/7 —— 说明失败来自
        **那一版实现**（镜头内 σ + 80% 多数票 + 填 127 灰），不是"像素级原理上不行"。

订正 3：我据此默认关闭了遮罩，并说「实测有害」——**证据不对称**。
        我是用**没有静态元素**的合成素材测的，必然显示有害；
        在**目标场景**（有台标的真实素材）上净收益**从未量化**。

★ 结论：**该功能已整体删除**（不是关闭）。
  方向应该是「按位置分轨」而不是「按时间维方差抹像素」——
  即保留每个 OCR 框的坐标、按位置聚类成轨、每轨独立合并，最后在**输出层**
  标注哪些轨是持续元素。**非破坏性**：判错最坏是多标/漏标一条轨，
  不会像像素遮罩那样把笔画打洞、不可恢复。
  这条已记录为待办交给后续实现（见交付目录的交接文档）。
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
        for bx in ocr(p):
            bx["fi"] = i          # 记下「这个框来自第几帧」——选带要靠它
            boxes.append(bx)
        if os.path.exists(p):
            os.unlink(p)
    if not boxes:
        return None, {"n_probe_frames": n, "n_text_boxes": 0,
                      "reason": "整片未检出任何文字（这条素材大概没有硬字幕）"}
    centers = np.array([b["y"] for b in boxes], dtype=float)
    heights = np.array([b["y1"] - b["y0"] for b in boxes], dtype=float)
    med_h = float(np.median(heights)) if len(heights) else 20.0
    win = max(med_h * 4.0, H * 0.08)
    # ★ 判据修正（2026-10-16 审查发现）：
    #   原实现是**纯框计数** —— 同一帧里 20 个框和 20 帧里各 1 个框得分相同。
    #   于是「某一帧中部有一大块文字」（表格页/正文页/弹幕）就能把带拉到那里，
    #   **整条底部字幕轨被静默丢掉**。
    #   实测（审查员注入合成框）：中部 5 帧各 20 框(=100) vs 底部 20 帧各 1 框(=20)
    #   → 原实现选中 y 91-141（完全不含底部）。
    #   修法：主看**有多少个不同帧**在这个 y 区间里有字（这才是"反复出现"），
    #   框数只当平手时的次要依据。
    best, best_lo = (-1, -1), 0.0
    for lo in np.arange(0.0, max(1.0, H - win * 0.5), max(1.0, win / 8)):
        sel = [b for b in boxes if lo <= b["y"] <= lo + win]
        key = (len(set(b.get("fi", -1) for b in sel)), len(sel))
        if key > best:
            best, best_lo = key, float(lo)
    n_frames_in_band, n_boxes_in_band = best
    inside = [b for b in boxes if best_lo <= b["y"] <= best_lo + win]
    if inside:
        y0 = max(0.0, min(b["y0"] for b in inside) - med_h * 0.6)
        y1 = min(float(H), max(b["y1"] for b in inside) + med_h * 0.6)
    else:
        y0, y1 = best_lo, best_lo + win
    info = {"n_probe_frames": n, "n_text_boxes": len(boxes),
            "band_in_boxes": n_boxes_in_band,
            "band_frames": n_frames_in_band,
            "band_coverage": round(n_frames_in_band / max(1, n), 3),
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


def split_tracks(rows, n_moments, q=12, persist=0.30, y_tol=10.0):
    """把每个时刻的文字框**按位置聚类分轨**，标出「持续存在的轨」。

    为什么这么做（三方审查的共同结论）
    ----------------------------------
    早先「把同一时刻所有框 join 成一行文本」，那一步**把位置信息彻底丢了**。
    位置一丢就只能在文本层时序合并，于是台标被拆成十几条、且与正文拼在一行。
    `RapidOCR` 从来没有把台标和正文并成一个框（实测：台标 x 56-130、
    正文 x 144-598，完全不重叠）—— 并框是我们在 join 那一步自己干的。

    本函数**非破坏性**：不抹像素、不删文字，只按位置分轨打标签。

    ⚠️ 关键实现细节（踩过的坑）
      不能用「精确分桶」（把 x0 按 4px 取整当签名）：OCR 的框位置逐帧抖动
      （同一个台标 x0 会在 56/60/63/64 之间跳），精确分桶会把它切成十几条轨，
      每条只出现两三帧、持续性全都过不了阈值。实测这样跑出 **164 条轨、0 条持续、
      243 条条目**，比不做还糟。必须用**容差聚类**：与已有簇的中心足够近就并进去。

    真正管用的量是**持续性**：台标守在同一个位置贯穿全片（实测 19/25、15/25），
    真字幕每句换区间（3~5/25）。
    **抖动率单独不能用**：「新闻联播」抖动率 0.11，比真字幕 0.33 还低。

    ⚠️ **当前实现是未完成的，默认关闭**（--tracks 才启用）。
       实测缺陷：按位置聚类会把**不同长度的字幕**分到不同簇
       （「巨毒还是低毒」窄、「到底是低毒还是剧毒」宽），
       于是每条轨里装的都是不同的字幕，merge 一句都合并不了 ——
       合成素材从 13 条变成 **78 条**（每个时刻一条）。
       正确做法应该是：**只把「持续轨」（台标）分出去单独处理，
       其余全部走原来的合并路径**，而不是把所有框都拆成轨。
       这条已作为「待办 B」写进交接文档。
    """
    allb = []
    for mi, r in enumerate(rows):
        for b in r.get("boxes", []):
            allb.append({"mi": mi, "b": b})
    if not allb:
        return [], {"reason": "没有文字框"}

    # 容差聚类：按 y 分行、再按 x 邻近合并
    allb.sort(key=lambda z: (z["b"]["y0"], z["b"]["x0"]))
    clusters = []
    for it in allb:
        bb = it["b"]
        w = max(1.0, bb["x1"] - bb["x0"])
        placed = False
        for c in clusters:
            cw = max(1.0, c["x1"] - c["x0"])
            # y 中心接近 + x 区间重叠或近邻 + 宽度相近
            if (abs(bb["y0"] - c["y0"]) <= y_tol
                    and abs(bb["y1"] - c["y1"]) <= y_tol
                    and min(bb["x1"], c["x1"]) - max(bb["x0"], c["x0"]) > -q
                    and 0.5 <= w / cw <= 2.0):
                c["items"].append(it)
                c["x0"] = min(c["x0"], bb["x0"]); c["x1"] = max(c["x1"], bb["x1"])
                c["y0"] = min(c["y0"], bb["y0"]); c["y1"] = max(c["y1"], bb["y1"])
                c["cx"] = sum(x["b"]["x0"] for x in c["items"]) / len(c["items"])
                placed = True
                break
        if not placed:
            clusters.append({"x0": bb["x0"], "x1": bb["x1"], "y0": bb["y0"], "y1": bb["y1"],
                             "cx": bb["x0"], "items": [it]})

    from collections import Counter
    tracks = []
    for ci, c in enumerate(clusters):
        moms = sorted(set(x["mi"] for x in c["items"]))
        readings = [x["b"]["text"] for x in c["items"]]
        top, topn = Counter(readings).most_common(1)[0]
        tracks.append({
            "sig": ci,
            "x0": round(c["x0"], 1), "x1": round(c["x1"], 1),
            "y0": round(c["y0"], 1), "y1": round(c["y1"], 1),
            "n_moments": len(moms), "n_boxes": len(c["items"]),
            "persistence": round(len(moms) / max(1, n_moments), 3),
            "n_readings": len(set(readings)),
            "jitter": round(len(set(readings)) / max(1, len(c["items"])), 2),
            "mean_score": round(sum(x["b"]["score"] for x in c["items"]) / len(c["items"]), 2),
            "example": top, "top_reading_share": round(topn / max(1, len(c["items"])), 2),
            "moments": moms,
            "_boxes": [id(x["b"]) for x in c["items"]],
        })
    tracks.sort(key=lambda t: -t["n_moments"])
    for ti, t in enumerate(tracks):
        t["track_id"] = ti
        t["persistent"] = bool(t["persistence"] >= persist)
    info = {"n_tracks": len(tracks),
            "n_persistent": sum(1 for t in tracks if t["persistent"]),
            "persist_threshold": persist, "x_tolerance_px": q, "y_tolerance_px": y_tol}
    return tracks, info


def merge_per_track(rows, tracks, sim_thresh, max_gap):
    """每条轨各自合并，再汇总（轨之间互不干扰）。"""
    out = []
    for tk in tracks:
        want = set(tk.get("_boxes", []))
        sub = []
        for mi in tk["moments"]:
            r = rows[mi]
            got = [b for b in r.get("boxes", []) if id(b) in want]
            if got:
                got.sort(key=lambda z: z["x0"])
                sub.append({"t": r["t"], "text": " ".join(b["text"] for b in got),
                            "score": max(b["score"] for b in got)})
        for e in merge(sub, sim_thresh, max_gap):
            e["track"] = tk.get("track_id", 0)
            e["track_persistent"] = tk["persistent"]
            e["track_x"] = [tk["x0"], tk["x1"]]
            e["track_persistence"] = tk["persistence"]
            out.append(e)
    out.sort(key=lambda e: (e["t0"], e["track"]))
    return out


def merge(rows, sim_thresh=0.7, max_gap=1.2, bridge_gap=None):
    """逐时刻 OCR → 字幕轨。

    相邻时刻相似 → 延长同一条（抗 OCR 抖动）；中间空太久 → 断开另起。
    每条取"出现次数最多的读法"当代表。

    ★ bridge_gap：**修「OCR 漏检把同一句拆成多条」的 bug**。
      原实现在 `r["t"] - e["t1"] > max_gap` 时无条件新起一条，
      **不会再回头看"新起的这条是不是跟上一条其实是同一句"**。
      而 max_gap=1.2s 在 2fps 下只允许漏检 2 帧 —— OCR 漏检是常态
      （实测新闻素材 81 个时刻里只有 69 个有字），所以同句拆条是**系统性**的。
      修法：新起之前，先在最近 `bridge_gap` 秒内**回看**已有的条目，
      如果文本仍然相似，就接回那一条（而不是造一条重复的）。
      回看窗口限制了"同句隔很久又出现"被误并的可能。

      实测（构造同一句在 t=0/0.5 与 t=2.0/2.5、中间漏检 → gap 1.5s > 1.2）：
          修复前：拆成 2 条
          修复后：并为 1 条
    """
    if bridge_gap is None:
        bridge_gap = max_gap * 4.0
    entries = []
    bridged = 0
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
            # ── 回看：OCR 漏检造成的断裂，接回上一条而不是新起重复条目
            for back in entries[-6:][::-1]:
                if r["t"] - back["t1"] > bridge_gap:
                    break
                if _sim(txt, back["text"]) >= sim_thresh:
                    back["t1"] = r["t"]
                    back["hits"] += 1
                    back["_all"].append(txt)
                    back["bridged"] = back.get("bridged", 0) + 1
                    if r["score"] > back["score"]:
                        back["text"], back["score"] = txt, r["score"]
                    bridged += 1
                    e = None
                    break
            if e is None:
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
        if e.get("bridged"):
            e["note"] = "跨过 %d 次 OCR 漏检接回" % e["bridged"]
        e.pop("_all")
    if bridged:
        print("   （merge 回看：接回了 %d 处被 OCR 漏检打断的句子）" % bridged)
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
    ap.add_argument("--tracks", action="store_true",
                    help="⚠ 实验性且**未完成**：按位置分轨（想分离台标与正文用）。"
                         "当前实现在「字幕长度不一」的素材上会把本该合并的句子拆散"
                         "（实测合成素材 13 条 → 78 条），故默认关闭。"
                         "正确做法见交付目录的交接文档「待办 B」。")
    ap.add_argument("--track-q", type=int, default=4, help="位置量化像素（默认 4）")
    ap.add_argument("--track-persist", type=float, default=0.30,
                    help="位置占比 ≥ 此值判为「持续轨」（默认 0.30）")
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
        try:
            parts = a.band.split(":")
            if len(parts) != 2:
                raise ValueError("要写成 y0:y1，例如 --band 320:480")
            y0, y1 = float(parts[0]), float(parts[1])
        except ValueError as e:
            sys.exit("✗ --band 参数错误：%s（收到 %r）" % (e, a.band))
        if not (y1 > y0):
            sys.exit("✗ --band 的 y1 必须大于 y0（收到 %s）" % a.band)
        if y0 < 0 or y1 > H:
            sys.exit("✗ --band 超出画面高度：画面高 %d，收到 y %g-%g。"
                     "（这个错误以前不检查，会导致每帧 ffmpeg 都失败、"
                     "最后安静地输出「0 条字幕」并返回成功）" % (H, y0, y1))
        if (y1 - y0) < 8:
            sys.exit("✗ --band 太窄（%g 像素），无法 OCR" % (y1 - y0))
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
        per = _ocr_stack(a.video, idxs, a.fps, by, W, bh, a.upscale, tmpd, str(b),
                         a.min_score)
        for i in idxs:
            got = per.get(i)
            if got:
                got.sort(key=lambda x: x["x0"])
                # **保留每个框的位置** —— 早先这里只留 join 后的文本，
                # 位置一丢就再也分不开台标与正文（三方审查共同指出的根因）。
                rows.append({"t": round(i / a.fps, 2), "boxes": got,
                             "text": " ".join(x["text"] for x in got),
                             "score": max(x["score"] for x in got)})
        print("  … 批 %d/%d  已得 %d 个有字时刻" % (b + 1, nb, len(rows)))

    # ★ 全失败保护（2026-10-16 审查发现）：
    #   原实现里如果所有帧都抓不到（crop 非法、ffmpeg 失败等），
    #   会安静地输出「0 条字幕」并**返回退出码 0** ——
    #   使用者会把它读成"这条视频没有字幕"，而不是"我参数给错了"。
    #   对比：cumsample.py 在 ffmpeg 失败时是 raise 的，这里以前漏了。
    if not rows:
        print()
        print("✗ 一个时刻都没 OCR 到文字 —— 这**不**等于「这条视频没有字幕」。")
        print("  已检查 %d 个时刻，全部没结果。常见原因：" % n)
        print("    · --band 给的范围不对（当前 y %.0f-%.0f，画面高 %d）" % (by, by + bh, H))
        print("    · ffmpeg 取帧失败（路径/编码问题）")
        print("    · 这条素材确实没有硬字幕（那请用 ASR 或联网取词）")
        print("  写了一份空结果到输出目录，但**别把它当成结论**。")
        with open(os.path.join(out, "subband.json"), "w", encoding="utf-8") as f:
            json.dump({"video": os.path.abspath(a.video), "duration_s": dur,
                       "size": [W, H], "band": band, "band_info": info,
                       "error": "no_text_found", "entries": [],
                       "stats": {"n_ocr_steps": n, "n_text_moments": 0, "n_entries": 0}},
                      f, ensure_ascii=False, indent=1)
        return 3

    # 3) 先按位置分轨，再逐轨合并（非破坏性：只打标签，不删内容）
    tracks, track_info = [], None
    if a.tracks:
        tracks, track_info = split_tracks(rows, len(rows), a.track_q, a.track_persist)
        if tracks:
            print("位置分轨：%d 条轨，其中 %d 条判为「持续轨」（位置占比 ≥%.0f%%，疑似台标/固定图卡）"
                  % (track_info["n_tracks"], track_info["n_persistent"],
                     track_info["persist_threshold"] * 100))
            for tk in [t for t in tracks if t["persistent"]][:4]:
                print("   持续轨 x %.0f-%.0f 出现 %d/%d 时刻 (%.0f%%)  读法 %d 种  "
                      "置信 %.2f  例「%s」"
                      % (tk["x0"], tk["x1"], tk["n_moments"], len(rows),
                         tk["persistence"] * 100, tk["n_readings"],
                         tk["mean_score"], tk["example"][:22]))
            entries = merge_per_track(rows, tracks, a.sim, a.max_gap)
        else:
            entries = merge(rows, a.sim, a.max_gap)
    else:
        entries = merge(rows, a.sim, a.max_gap)
    print("→ %d 个有字时刻，合并为 %d 条字幕" % (len(rows), len(entries)))

    result = {"video": os.path.abspath(a.video), "duration_s": round(dur, 2),
              "size": [W, H], "band": band, "band_info": info,
              "params": {"fps": a.fps, "upscale": a.upscale, "batch": a.batch,
                         "sim": a.sim, "max_gap": a.max_gap, "min_score": a.min_score},
              "stats": {"n_ocr_steps": n, "n_ocr_calls": nb, "n_text_moments": len(rows),
                        "n_entries": len(entries),
                        "total_text_seconds": round(sum(e["duration"] for e in entries), 1)},
              "tracks": [{k: v for k, v in t.items() if k not in ("moments", "_boxes")} for t in tracks],
              "track_info": track_info,
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
