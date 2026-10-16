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
    来源齐备   至少两路「不同通道」的**可核实**来源，且全部解析通过
               ★ 注意：这只表示"来源给够了"，**不等于结论正确**
    待核       只有一路可核实来源
    待核(同通道) 两路都是同一通道（例如两次 OCR 读同一行）—— 不算两路独立
    仅记录     只有 net/manual 这类记录性来源 —— 程序没核实过任何东西
    推断       没有来源（check 会高亮，因为它最容易冒充事实）
    有坏源     有来源但至少一个解析不了（含写错类型的）——
               比「推断」更该警惕：你以为有依据，其实那个依据不存在

    「可核实」= visual（sheet/read/frame）或 text（ocr/asr/subband）。
    net 只是记了个网址（程序不会去访问），manual 只是人写的一句话，
    **两者都不计入独立性**。
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

# 哪些通道算「可核实」—— 只有这些才计入"独立性"。
# net 只是记了个网址（程序不会去访问），manual 只是人写的一句话，
# 两者都**不能**当独立证据用：早先把它们也算一路，导致
# 「图版 + manual:看起来是对的」被判「已确认」（2026-10-16 实测复现）。
VERIFIABLE = {"visual", "text"}


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
                    # 直接按样本自带的 sheet/row/col 匹配 —— plan.json 的样本里
                    # 本来就有这三个字段，根本不用算 k。
                    # 早先自己算 `(sheet-1)*25 + row*5 + col`，而 plan.json 的 k 是
                    # **从 1 开始**编的，于是第一格永远算成 0、查不到（实测复现）。
                    hit = [x for x in d.get("samples", [])
                           if x.get("sheet") == s["sheet"] and x.get("row") == s["row"]
                           and x.get("col") == s["col"]]
                    if hit:
                        extra = "该格对应 t=%.1fs" % hit[0]["t"]
                    else:
                        extra = "**该格在 plan.json 里找不到对应样本**（行/列越界？）"
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
            return True, "人工观察（**不计入独立性**）：" + s.get("note", "")[:50], None
        # 未知/写错的来源**不能**当作已核实 —— 早先这里返回 True，
        # 于是一串乱码也能把结论托成「已确认」（实测复现）。
        return False, "无法识别的来源类型「%s」—— 请按 sheet:/ocr:/asr:/net:/manual: 写" % t, None
    except Exception as e:
        return False, "解析出错：%s: %s" % (type(e).__name__, e), None


def status_of(sources):
    """判定状态。**严格区分四件事**（早先把它们混成一件，是设计错误）：

        1. 来源「能被程序解析」吗（文件在不在、条号越界没）
        2. 来源「算独立证据」吗（net/manual 不算 —— 程序没去核实过）
        3. 有没有「两路不同通道」的可核实来源
        4. **结论本身成不成立** —— 这一条本工具**无法判定**，
           所以状态里绝不出现"已确认"这种话，只说"来源齐备"。

    ★ 特别注意：`来源齐备` **不等于结论正确**。
      它只说明"你给出的可核实来源够两路且都解析通过"。
      来源内容是否真的支持这条结论，只能靠人或另一个模型复核。
    """
    parsed = [p for p in (parse_src(x) for x in sources) if p]
    if not parsed:
        return "推断", []
    results = [(p, *resolve(p)) for p in parsed]
    bad = [r for r in results if not r[1]]
    if bad:
        return "有坏源", results

    verifiable = [p for p, _, _, _ in results if p["family"] in VERIFIABLE]
    fams = set(p["family"] for p in verifiable)
    if not verifiable:
        # 只有 net/manual 这类记录性来源 —— 程序没核实过任何东西
        return "仅记录", results
    if len(fams) >= 2:
        return "来源齐备", results
    if len(verifiable) >= 2:
        # 两路都是同一通道（比如两次 OCR）—— 不算两路独立
        return "待核(同通道)", results
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
    mark = {"来源齐备": "✓", "待核": "·", "待核(同通道)": "·", "仅记录": "~",
            "推断": "?", "有坏源": "✗"}
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
    if cnt.get("来源齐备"):
        print()
        print("   注：`来源齐备` 只表示「可核实来源够两路且都解析通过」，")
        print("       **不代表结论正确** —— 来源内容是否真的支持该结论，本工具判不了。")


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
