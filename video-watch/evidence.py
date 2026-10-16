#!/usr/bin/env python3
"""evidence.py —— 证据账本：结论必须挂来源，来源必须可解析

为什么需要它
------------
我在分析 BV185pu6PEeF 时把年份 2014 说成了 2011。事后复盘：**根因不是看错，
而是"猜的"和"看到的"没有分开** —— 两者都被我当成事实说出口了。

这个工具的规则很简单：

  1. **每条结论都必须挂来源**，来源要**能被程序解析**（不是写个文件名就算数）
  2. **两路独立来源一致** → 标「已确认」
  3. **只有一路** → 标「待核」
  4. **没有来源** → 标「推断」—— 不允许悄悄混进事实里

"独立"的定义很重要：**同一类通道重复读两次不算两路**。
「图版上看到的」和「OCR 读出来的」是两路；两次 OCR 读同一行字是一路。

用法
----
    python3 evidence.py new 账本.json --subject "BV185pu6PEeF 分析"
    python3 evidence.py add 账本.json "词条被改的时间是 2014-03-30" --src sheet:目录:8:3:2 --src ocr:目录/subband.json:12
    python3 evidence.py add 账本.json "受访者叫毕啸天" --src ocr:目录/subband.json:31
    python3 evidence.py check 账本.json      # 核验来源是否真的能解析 + 定状态
    python3 evidence.py show 账本.json       # 出人读的报告

来源写法
--------
    sheet:<目录>:<版号>:<行>:<列>     图版的某一格（会自动算出对应时刻）
    read:<目录>:<文件>                read 面板裁切的某张图
    ocr:<json 文件>:<条号>            字幕轨/OCR 结果里的一条
    asr:<json 文件>:<段号>            ASR 结果里的一段
    net:<URL>                         外部来源（记下即可，程序没法核）
    manual:<说明>                     人工观察（记下即可）

状态含义
--------
    已确认   至少两路「不同通道」的来源，且**全部可解析**
    待核     只有一路来源，或两路同通道
    推断     没有来源（这条会被 check 高亮，因为它最容易冒充事实）
    有坏源   有来源但至少一个解析不了 —— 比「推断」更该警惕：
             这说明你以为有依据，其实那个依据不存在
"""

import argparse
import json
import os
import re
import sys

# 通道分类：只有跨通道的两路才算「两路独立」
FAMILY = {
    "sheet": "visual", "read": "visual", "frame": "visual",
    "ocr": "text", "asr": "text", "subband": "text",
    "net": "external", "manual": "manual",
}


def load(path):
    if not os.path.exists(path):
        sys.exit("账本不存在：" + path + "（先用 new 建一个）")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, d):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 解析来源

def parse_src(s):
    """'sheet:dir:8:3:2' → {type, family, ref, ...}"""
    parts = s.split(":")
    if not parts:
        return None
    t = parts[0].strip().lower()
    fam = FAMILY.get(t, "other")
    d = {"type": t, "family": fam, "raw": s}
    if t in ("sheet",) and len(parts) >= 5:
        d.update({"dir": parts[1], "sheet": int(parts[2]), "row": int(parts[3]), "col": int(parts[4])})
    elif t == "read" and len(parts) >= 3:
        d.update({"dir": parts[1], "file": ":".join(parts[2:])})
    elif t in ("ocr", "subband") and len(parts) >= 3:
        d.update({"file": parts[1] + (":" + parts[2] if False else ""), "idx": None})
        # ocr:<json>:<条号>  —— 但路径里可能带冒号，所以从右往左取条号
        if parts[-1].isdigit():
            d["idx"] = int(parts[-1])
            d["file"] = ":".join(parts[1:-1])
        else:
            d["file"] = ":".join(parts[1:])
    elif t == "asr" and len(parts) >= 3:
        d["idx"] = int(parts[-1]) if parts[-1].isdigit() else None
        d["file"] = ":".join(parts[1:-1]) if parts[-1].isdigit() else ":".join(parts[1:])
    elif t == "net":
        d["url"] = ":".join(parts[1:])
    elif t == "manual":
        d["note"] = ":".join(parts[1:])
    else:
        d["note"] = ":".join(parts[1:])
    return d


def resolve(s):
    """核验来源是否真的能解析。返回 (ok, 说明, 可选的补充信息)。"""
    t = s["type"]
    try:
        if t == "sheet":
            # 找 <dir>/sheet_XX.jpg
            pats = []
            if os.path.isdir(s["dir"]):
                pats = [f for f in os.listdir(s["dir"])
                        if re.match(r"sheet_%02d\.(jpg|png)$" % s["sheet"], f)]
            if not pats:
                return False, "找不到图版 sheet_%02d.* in %s" % (s["sheet"], s["dir"]), None
            extra = ""
            pj = os.path.join(s["dir"], "plan.json")
            if os.path.exists(pj):
                try:
                    d = json.load(open(pj, encoding="utf-8"))
                    k = (s["sheet"] - 1) * 25 + s["row"] * 5 + s["col"]
                    hit = [x for x in d.get("samples", []) if x.get("k") == k]
                    if hit:
                        extra = "该格对应 t=%.1fs" % hit[0]["t"]
                except Exception:
                    pass
            return True, "图版存在 " + pats[0] + ("，" + extra if extra else ""), extra
        if t == "read":
            p = os.path.join(s["dir"], s["file"])
            return (os.path.exists(p), ("文件存在 " + p) if os.path.exists(p) else "找不到 " + p, None)
        if t in ("ocr", "subband"):
            if not os.path.exists(s["file"]):
                return False, "找不到 " + s["file"], None
            d = json.load(open(s["file"], encoding="utf-8"))
            es = d.get("entries", [])
            i = s.get("idx")
            if i is None or i < 1 or i > len(es):
                return False, "条号 %s 越界（该文件共 %d 条）" % (i, len(es)), None
            e = es[i - 1]
            return True, "第 %d 条 [%.1f-%.1fs] %s" % (i, e["t0"], e["t1"], e["text"][:40]), e
        if t == "asr":
            if not os.path.exists(s["file"]):
                return False, "找不到 " + s["file"], None
            d = json.load(open(s["file"], encoding="utf-8"))
            segs = d.get("segments", [])
            i = s.get("idx")
            if i is None or i < 1 or i > len(segs):
                return False, "段号 %s 越界（共 %d 段）" % (i, len(segs)), None
            g = segs[i - 1]
            return True, "第 %d 段 [%.1f-%.1fs] %s" % (i, g["start"], g["end"], g["text"][:40]), g
        if t == "net":
            return True, "外部来源（程序无法核实）：" + s.get("url", "")[:60], None
        if t == "manual":
            return True, "人工观察：" + s.get("note", "")[:60], None
        return True, "未知类型，原样记录", None
    except Exception as e:
        return False, "解析出错：%s: %s" % (type(e).__name__, e), None


def status_of(sources):
    """按「通道数」定状态，并把每个来源的解析结果带上。"""
    parsed = [parse_src(x) for x in sources]
    parsed = [p for p in parsed if p]
    fams = set(p["family"] for p in parsed)
    results = [(p, *resolve(p)) for p in parsed]
    bad = [r for r in results if not r[1]]
    if not parsed:
        return "推断", results
    if bad:
        return "有坏源", results
    if len(fams) >= 2:
        return "已确认", results
    return "待核", results


# ------------------------------------------------------------------ 子命令

def cmd_new(a):
    d = {"subject": a.subject or "", "claims": []}
    save(a.ledger, d)
    print("✓ 建好账本：" + a.ledger)


def cmd_add(a):
    d = load(a.ledger)
    d["claims"].append({"id": "c%d" % (len(d["claims"]) + 1), "text": a.text,
                        "sources": list(a.src or [])})
    save(a.ledger, d)
    print("✓ 已加：" + a.text + "（来源 %d 个）" % len(a.src or []))


def cmd_check(a):
    d = load(a.ledger)
    n_bad = 0
    for c in d["claims"]:
        st, res = status_of(c["sources"])
        c["status"] = st
        if st == "有坏源":
            n_bad += 1
            print("✗ [%s] %s" % (c["id"], c["text"]))
            for p, ok, why, _ in res:
                if not ok:
                    print("      坏源 %s → %s" % (p["raw"], why))
        elif st == "推断":
            print("⚠ [%s] %s  ← 没有来源，标为「推断」，别当事实用" % (c["id"], c["text"]))
    save(a.ledger, d)
    print("核验完成：%d 条结论，%d 条有坏源" % (len(d["claims"]), n_bad))
    return 1 if n_bad else 0


def cmd_show(a):
    d = load(a.ledger)
    mark = {"已确认": "✓", "待核": "·", "推断": "?", "有坏源": "✗"}
    print("账本  %s" % d.get("subject", ""))
    print("=" * 66)
    for c in d["claims"]:
        st = c.get("status") or status_of(c["sources"])[0]
        print("%s [%s] %s" % (mark.get(st, " "), st, c["text"]))
        for s in c["sources"]:
            p = parse_src(s)
            ok, why, _ = resolve(p) if p else (False, "无法解析", None)
            print("      %s %s" % ("✓" if ok else "✗", why))
    print("=" * 66)
    from collections import Counter
    cnt = Counter((c.get("status") or status_of(c["sources"])[0]) for c in d["claims"])
    print("   " + "  ".join("%s %d" % (k, v) for k, v in cnt.items()))


def main():
    ap = argparse.ArgumentParser(description="证据账本：结论必须挂来源")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="建账本")
    p.add_argument("ledger")
    p.add_argument("--subject", default="")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("add", help="加一条结论")
    p.add_argument("ledger")
    p.add_argument("text")
    p.add_argument("--src", action="append", help="来源，可重复")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("check", help="核验来源 + 定状态")
    p.add_argument("ledger")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("show", help="出报告")
    p.add_argument("ledger")
    p.set_defaults(fn=cmd_show)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
