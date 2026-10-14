#!/usr/bin/env python3
"""song-id.py —— 曲名识别的旧命令入口，转发到 video-watch skill 里的完整实现。

实现已统一到 <skill 目录>/bgm-id.py（多级兜底：整段识别 → 声道相减 → VAD 找空隙 → 人声分离），
本文件只保留旧命令入口，两个入口同一份实现。

定位 skill 的顺序：
  1. 环境变量 VW_PY（直接指向 bgm-id.py）
  2. 环境变量 VIDEO_WATCH_DIR（指向 skill 目录）
  3. 常见安装位置（~/.dsh/skills/video-watch、/root/.dsh/skills/video-watch …）
  4. 本文件同级目录下的 bgm-id.py

用法：python3 song-id.py <视频或音频> [更多文件...]
"""
import os
import runpy
import sys
from pathlib import Path


def find_bgmid() -> Path | None:
    env = os.environ.get("VW_PY")
    if env and Path(env).is_file():
        return Path(env)
    cands = []
    if os.environ.get("VIDEO_WATCH_DIR"):
        cands.append(Path(os.environ["VIDEO_WATCH_DIR"]) / "bgm-id.py")
    cands += [
        Path.home() / ".dsh" / "skills" / "video-watch" / "bgm-id.py",
        Path("/root/.dsh/skills/video-watch/bgm-id.py"),
        Path(__file__).resolve().parent / "bgm-id.py",
    ]
    return next((c for c in cands if c.is_file()), None)


target = find_bgmid()
if target is None:
    sys.exit(
        "找不到 bgm-id.py。请设 VW_PY=<bgm-id.py 的绝对路径>，"
        "或设 VIDEO_WATCH_DIR=<video-watch skill 目录> 后重试。"
    )
sys.argv[0] = str(target)
runpy.run_path(str(target), run_name="__main__")
