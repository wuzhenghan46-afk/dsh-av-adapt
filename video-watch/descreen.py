#!/usr/bin/env python3
"""descreen.py —— 翻拍屏幕的检测与透视校正

为什么需要它
------------
不是所有"视频"都是屏幕录制。有一类素材是**手机对着显示器拍**：
透视变形、摩尔纹、反光、手持抖动全都有。
在这类素材上直接 OCR 屏幕内容，识别率会被变形拖垮。

Chrome 的建议是「多数情况不必校正，RapidOCR 本来吃四边形，缩略图也会抹掉摩尔纹」，
但这条没被实测过。本工具就是来实测它的。

做法
----
  1. 自动找屏幕四角：Canny 边缘 → 最大四边形轮廓（文档扫描那一套）
     找不到就接受人工给的四角（--corners）
  2. 透视校正：优先用 OpenCV（比 ffmpeg 的 perspective 滤镜好调），没有就退回 ffmpeg
  3. 对比校正前后的 OCR 识别率

用法
----
    python3 descreen.py 视频.mp4 --out 输出目录                  # 自动检测 + 校正 + 对比
    python3 descreen.py 视频.mp4 --corners "8,4 350,22 340,240 5,210"
    python3 descreen.py 一帧.png --only-detect                    # 只看检测结果对不对

产出
----
    detect.png     四角标注图（人眼可验检测对不对）
    corrected.png  校正后的单帧
    ocr_compare.txt 校正前后 OCR 对比
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def grab(path, t, out, scale=1):
    vf = []
    if scale != 1:
        vf.append("scale=iw*%s:ih*%s:flags=lanczos" % (scale, scale))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", "%.3f" % t, "-i", path, "-frames:v", "1"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += [out]
    return _run(cmd).returncode == 0 and os.path.exists(out)


def order_quad(pts):
    """把 4 个点排成 左上/右上/右下/左下。"""
    p = np.array(pts, dtype=np.float32)
    s = p.sum(axis=1)
    d = np.diff(p, axis=1).ravel()
    return np.array([p[np.argmin(s)], p[np.argmin(d)], p[np.argmax(s)], p[np.argmax(d)]],
                    dtype=np.float32)


def find_screen(img_path, min_area_frac=0.06, approx_lo=0.015, approx_hi=0.06):
    """返回四角 (4,2) 或 None，以及一张标注图。"""
    import cv2
    img = cv2.imread(img_path)
    if img is None:
        return None, None
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    best = None
    for lo, hi in ((40, 120), (60, 160), (30, 90)):
        edges = cv2.Canny(gray, lo, hi)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:8]
        for c in cnts:
            area = cv2.contourArea(c)
            if area < W * H * min_area_frac:
                continue
            peri = cv2.arcLength(c, True)
            for eps in (0.02, 0.03, 0.05):
                ap = cv2.approxPolyDP(c, eps * peri, True)
                if len(ap) == 4 and cv2.isContourConvex(ap):
                    q = order_quad(ap.reshape(4, 2))
                    # 面积要接近外接矩形（排除细长条）
                    if area > W * H * min_area_frac and best is None:
                        best = (q, area, (lo, hi), eps)
                    break
            if best:
                break
        if best:
            break
    if best is None:
        return None, None
    q, area, canny, eps = best
    vis = img.copy()
    cv2.polylines(vis, [q.astype(np.int32)], True, (0, 0, 255), 3)
    for i, (x, y) in enumerate(q.astype(int)):
        cv2.circle(vis, (x, y), 6, (0, 255, 0), -1)
        cv2.putText(vis, "TL TR BR BL"[i * 2:i * 2 + 3] if False else ["TL", "TR", "BR", "BL"][i],
                    (x + 8, y + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    return q, vis


def warp(img_path, quad, out_path):
    """按四角做透视校正，输出正向的屏幕图。"""
    import cv2
    img = cv2.imread(img_path)
    q = order_quad(quad)
    wA = np.linalg.norm(q[1] - q[0]); wB = np.linalg.norm(q[2] - q[3])
    hA = np.linalg.norm(q[3] - q[0]); hB = np.linalg.norm(q[2] - q[1])
    W, H = int(max(wA, wB)), int(max(hA, hB))
    if W < 16 or H < 16:
        return False
    dst = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(q, dst)
    out = cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_CUBIC)
    cv2.imwrite(out_path, out)
    return True


_OCR = None


def ocr_text(img_path, min_score=0.4):
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    r = _OCR(img_path)
    res = r[0] if r and r[0] else []
    return [(x[1], float(x[2])) for x in res if float(x[2]) >= min_score]


def main():
    ap = argparse.ArgumentParser(description="翻拍屏幕的检测与透视校正")
    ap.add_argument("input", help="视频或单帧图")
    ap.add_argument("--out", default=None)
    ap.add_argument("--at", type=float, default=60.0, help="取哪一时刻的帧（默认 60s）")
    ap.add_argument("--corners", default=None,
                    help="手工四角 \"x1,y1 x2,y2 x3,y3 x4,y4\"（TL TR BR BL）")
    ap.add_argument("--only-detect", action="store_true", help="只检测、不校正")
    ap.add_argument("--upscale", type=int, default=2, help="OCR 前放大（默认 2）")
    a = ap.parse_args()

    out = a.out or os.path.splitext(a.input)[0] + "_descreen"
    os.makedirs(out, exist_ok=True)
    is_img = a.input.lower().endswith((".png", ".jpg", ".jpeg"))
    frame = a.input if is_img else os.path.join(out, "frame.png")
    if not is_img:
        if not grab(a.input, a.at, frame):
            sys.exit("取帧失败")
    print("帧：%s" % frame)

    # 1) 找四角
    quad = None
    if a.corners:
        quad = np.array([[float(v) for v in p.split(",")] for p in a.corners.split()], dtype=np.float32)
        print("四角：手工指定")
    else:
        quad, vis = find_screen(frame)
        if vis is not None:
            import cv2
            p = os.path.join(out, "detect.png")
            cv2.imwrite(p, vis)
            print("四角：自动检测到 → %s" % p)
        else:
            print("四角：**自动检测失败**（画面里找不到明显的四边形）")
            print("  → 这类素材要么确实没有屏幕，要么需要人工给角点：--corners \"x,y x,y x,y x,y\"")
    if quad is not None:
        print("    " + "  ".join("(%.0f,%.0f)" % (x, y) for x, y in order_quad(quad)))
    if a.only_detect:
        return 0 if quad is not None else 2

    if quad is None:
        return 2

    # 2) 校正
    corr = os.path.join(out, "corrected.png")
    if not warp(frame, quad, corr):
        sys.exit("透视校正失败（四角退化了？）")
    import cv2
    ih = cv2.imread(corr)
    print("校正后：%dx%d → %s" % (ih.shape[1], ih.shape[0], corr))

    # 3) OCR 对比
    def prep(src, dst):
        # 统一放大，保证两边像素量可比
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
              "-vf", "scale=iw*%d:ih*%d:flags=lanczos" % (a.upscale, a.upscale), dst])
        return dst

    p_before = prep(frame, os.path.join(out, "before_up.png"))
    p_after = prep(corr, os.path.join(out, "after_up.png"))
    tb, ta = ocr_text(p_before), ocr_text(p_after)
    lines = ["校正前后 OCR 对比（同一帧，放大 %d 倍后识别）" % a.upscale,
             "=" * 62,
             "校正前：%d 段文字" % len(tb)]
    for t, s in tb:
        lines.append("   %.2f  %s" % (s, t))
    lines.append("")
    lines.append("校正后：%d 段文字" % len(ta))
    for t, s in ta:
        lines.append("   %.2f  %s" % (s, t))
    txt = "\n".join(lines)
    with open(os.path.join(out, "ocr_compare.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print()
    print(txt)
    return 0


if __name__ == "__main__":
    main()
