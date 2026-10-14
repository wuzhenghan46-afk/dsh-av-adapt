#!/usr/bin/env python3
"""bgm-id.py —— 从视频/音频里识别背景音乐是什么曲子（带多级兜底）。

用法：
    python3 bgm-id.py <视频或音频> [更多文件...] [--no-fallback] [--strategy N]

策略链（成本递增，命中即止）—— 每一级的实测依据写在注释里：
  1. 整段直接识别（Shazam）
     实测能扛住「BGM 比人声低 15dB」的混合，且 5 秒素材就够。
     多数口播视频到这一级就结束。
  2. 声道相减（仅立体声）
     抵消中置人声的老办法，成本为零，对"人声居中、音乐铺两侧"的混音有效。
  3. VAD 找语音空隙 → 用纯音乐片段识别
     适合有自然停顿的口播。注意：连续不停顿的说话没有空隙可钻，
     这一级会直接跳过（实测 25 秒无间断口播 = 0 段空隙）。
  4. demucs 人声分离（可选依赖）→ 对伴奏轨识别
     最彻底，但要另装 PyTorch 与模型；检测不到 demucs 就跳过并提示。

另用 AcoustID 补充规范元数据（曲名/艺术家/专辑/厂牌，来自 MusicBrainz），
需要 application key：环境变量 ACOUSTID_KEY → ~/.dsh/acoustid-key.txt → 内置兜底。
注意 AcoustID 几乎不抗人声：实测同一首曲子干净时命中 94.5%，
混入说话后立刻无匹配。所以它在混合场景只是补充，主力永远是 Shazam。
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

FALLBACK_KEY = '1vOwZtEn'          # beets 项目公开在源码里的 key
MIN_GAP = 5.0                       # VAD 空隙短于这个秒数就不值得试（实测 Shazam 需要约 5s 素材才能识别）


# ── 基础工具 ────────────────────────────────────────────────────────────
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_audio_stream(src):
    """返回 (是否立体声, 时长秒)"""
    r = run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=channels', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', src])
    vals = [v for v in r.stdout.strip().split('\n') if v]
    ch = int(vals[0]) if vals and vals[0].isdigit() else 0
    dur = float(vals[-1]) if vals and vals[-1].replace('.', '').isdigit() else 0.0
    return ch >= 2, dur


def extract_audio(src, dst, mono=True):
    af = ['-ac', '1'] if mono else []
    r = run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
             '-vn', *af, '-ar', '44100', '-b:a', '128k', dst])
    return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


def cut(src, start, dur, dst):
    r = run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start),
             '-i', src, '-t', str(dur), dst])
    return r.returncode == 0 and os.path.exists(dst)


def pcm16k(src):
    """抽 16k 单声道 PCM 给 VAD 用。失败返回**空数组**，由 speech_gaps() 转成 None，
    上层据此区分「解码失败」与「确实没有空隙」两种情况。"""
    import numpy as np
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', src, '-f', 'f32le',
                        '-ac', '1', '-ar', '16000', '-'], capture_output=True)
    if r.returncode != 0:
        # 不打印 list 字面量（原来的 splitlines()[-1:] 会输出 ['…'] 这种形式）
        why = (r.stderr.decode('utf-8', 'ignore').strip().splitlines() or ['未知原因'])[-1][:120]
        print(f'    （音频解码失败，策略3 无法判断空隙：{why}）')
        return np.array([], dtype=np.float32)
    return np.frombuffer(r.stdout, dtype=np.float32)


# ── 各策略 ──────────────────────────────────────────────────────────────
async def shazam(path):
    """返回 track dict；失败时返回 {'_error': ...}。

    调用方必须检查 _error —— 把网络异常当成「没识别出来」会误导用户
    去怀疑素材（「可能是 UP 主自制 BGM」），而真实原因是网络（评审指出，
    本机网络本来就不稳，这条最容易带偏人）。
    """
    try:
        from shazamio import Shazam
    except ImportError:
        return {'_error': 'shazamio 未安装（python3 -m pip install --break-system-packages shazamio）'}
    try:
        out = await Shazam().recognize(path)
        return out.get('track') or {}
    except Exception as e:
        return {'_error': f'{type(e).__name__}: {e}'}


def vocal_cancel(src, dst):
    """声道相减：L-R，抵消居中人声。仅在立体声有意义。"""
    return run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                '-af', 'pan=mono|c0=0.5*c0-0.5*c1', '-ar', '44100', dst]).returncode == 0


def speech_gaps(audio_path, min_gap=MIN_GAP):
    """用 faster-whisper 的 VAD 找语音空隙，返回 [(start, end), ...]（秒）"""
    try:
        from faster_whisper.vad import get_speech_timestamps, VadOptions
    except ImportError:
        return None
    y = pcm16k(audio_path)
    if len(y) == 0:
        return None        # 解码失败：与「没有空隙（返回 []）」区分开
    if len(y) < 16000:
        return []
    dur = len(y) / 16000
    # speech_pad_ms 默认 400：VAD 会把每段语音两端各**外扩** 400ms，
    # 于是空隙被吃掉 0.8 秒 —— MIN_GAP=5.0 实际要求约 5.8 秒静音。
    # 这里显式归零，让「空隙长度」就是字面长度，MIN_GAP 的语义才准确。
    # 推理本身也要包住：VAD 首次运行会下模型，下载失败/OOM 不能把整个进程打崩。
    try:
        ts = get_speech_timestamps(y, VadOptions(speech_pad_ms=0), sampling_rate=16000)
    except Exception as e:
        print(f'    （VAD 推理失败：{type(e).__name__}: {str(e)[:120]}）')
        return None
    gaps, prev = [], 0.0
    for s in ts:
        st, en = s['start'] / 16000, s['end'] / 16000
        if st - prev >= min_gap:
            gaps.append((prev, st))
        prev = max(prev, en)
    if dur - prev >= min_gap:
        gaps.append((prev, dur))
    return sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)


def measure_peak(path):
    """用 ffmpeg volumedetect 拿峰值 dBFS（拿不到返回 None）"""
    r = run(['ffmpeg', '-hide_banner', '-i', path, '-af', 'volumedetect', '-f', 'null', '-'])
    for line in (r.stderr or '').splitlines():
        if 'max_volume:' in line:
            try:
                return float(line.split('max_volume:')[1].split('dB')[0].strip())
            except ValueError:
                return None
    return None


def demucs_available():
    """不查命令行：我们用库推理，只要 torch + demucs 能 import 即可"""
    try:
        import torch  # noqa: F401
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def separate_vocals(src, outdir):
    """用 demucs 分离出伴奏轨，返回 (路径, 错误说明)。

    不走 demucs 命令行：它保存结果依赖 torchaudio 的音频 I/O，而 torchaudio 2.9 起
    改要 TorchCodec，本机加载不了那个 .so。所以自己用 ffmpeg + soundfile 读写，
    只调 demucs 的模型推理（实测可行）。
    """
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
    except ImportError as e:
        return None, f'依赖不全（{e.name}）'
    tmp = os.path.join(outdir, '_dm_in.wav')
    r = run(['ffmpeg', '-y', '-v', 'error', '-i', src, '-ac', '2', '-ar', '44100', tmp])
    if r.returncode != 0:
        return None, 'ffmpeg 预处理失败'
    data, sr = sf.read(tmp, always_2d=True, dtype='float32')
    wav = torch.from_numpy(data.T)
    model = get_model('htdemucs')
    model.eval()
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std()
    with torch.no_grad():
        sources = apply_model(model, ((wav - mean) / (std + 1e-8))[None],
                              device='cpu', progress=False)[0]
    sources = sources * std + mean
    idx = {k: i for i, k in enumerate(model.sources)}
    accomp = sum(sources[idx[k]] for k in model.sources if k != 'vocals')
    out = os.path.join(outdir, 'accompaniment.wav')
    sf.write(out, accomp.detach().cpu().numpy().T, sr)
    return out, None


# ── AcoustID ────────────────────────────────────────────────────────────
def load_key():
    k = (os.environ.get('ACOUSTID_KEY') or '').strip()
    if k:
        return k, 'env ACOUSTID_KEY'
    p = os.path.expanduser('~/.dsh/acoustid-key.txt')
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            k = line.split('#')[0].strip()
            if k:
                return k, p
    return FALLBACK_KEY, 'beets 公开 key（兜底）'


def acoustid(path, key):
    if not shutil.which('fpcalc'):
        return None
    r = run(['fpcalc', '-json', path])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    d = json.loads(r.stdout)
    body = urllib.parse.urlencode({'client': key, 'meta': 'recordings releasegroups',
                                   'duration': int(d.get('duration') or 0),
                                   'fingerprint': d.get('fingerprint')}).encode()
    # 网络偶发抖动（实测遇到过 SSL handshake timeout），重试三次再报错
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    'https://api.acoustid.org/v2/lookup', data=body), timeout=25) as resp:
                return json.load(resp)
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    return {'status': 'error', 'error': {'message': f'{type(last).__name__}: {last}'}}


# ── 主流程 ──────────────────────────────────────────────────────────────
def show(track, tag):
    print(f'  ✓ [{tag}] {track.get("title")} — {track.get("subtitle")}')
    meta = {}
    for s in (track.get('sections') or []):
        for m in (s.get('metadata') or []):
            meta[m.get('title')] = m.get('text')
    extra = ' · '.join(f'{k}: {meta[k]}' for k in ('Album', 'Label', 'Released') if meta.get(k))
    if extra:
        print(f'      {extra}')
    if track.get('url'):
        print(f'      {track["url"]}')


async def handle(src, args, key, key_src):
    print(f'\n=== {os.path.basename(src)}')
    if not os.path.exists(src):
        print('  找不到文件')
        return
    # 收集各次 Shazam 调用的错误：最后要靠它区分「网络/服务问题」与「指纹库确实没有」
    net_errors = []

    async def ask(p):
        r = await shazam(p)
        if r.get('_error'):
            net_errors.append(r['_error'])
            return None
        return r or None
    stereo, dur = probe_audio_stream(src)
    with tempfile.TemporaryDirectory() as td:
        audio = os.path.join(td, 'audio.mp3')
        if not extract_audio(src, audio):
            print('  抽音轨失败')
            return
        print(f'  音轨 {dur:.1f}s{"（立体声）" if stereo else "（单声道）"}')

        # 策略 1
        if args.start <= 1:
            track = await ask(audio)
            if track and not track.get('_error'):
                show(track, '策略1 整段')
                return finish(audio, key, key_src)
            if args.no_fallback:
                print('  整段未识别（已禁用兜底）')
                return finish(audio, key, key_src)
            print('  整段未识别 → 启动兜底')
        else:
            print(f'  （--strategy {args.start}：跳过前 {args.start - 1} 级）')

        # 策略 2：声道相减
        if args.start > 2:
            print('    策略2 跳过：--strategy 指定从更高级开始')
        elif stereo:
            karaoke = os.path.join(td, 'karaoke.mp3')
            if not vocal_cancel(src, karaoke):
                print('    策略2 失败：声道相减转换没成功')
            else:
                t = await ask(karaoke)
                if t:
                    show(t, '策略2 声道相减')
                    return finish(audio, key, key_src)
                print('    策略2 声道相减后仍未识别')
        else:
            print('    策略2 跳过：单声道，没有可相减的声道差')

        # 策略 3：VAD 空隙
        if args.start > 3:
            gaps = None
            print('    策略3 跳过：--strategy 指定从更高级开始')
        else:
            gaps = speech_gaps(audio)
            if gaps is None:
                print('    策略3 跳过：未安装 faster-whisper（无法做 VAD）')
            elif not gaps:
                print(f'    策略3 跳过：没有 ≥{MIN_GAP}s 的语音空隙（说话太密）')
            else:
                print(f'    策略3 找到 {len(gaps)} 段空隙：' +
                      ', '.join(f'{a:.1f}-{b:.1f}s' for a, b in gaps[:4]))
                for start, end in gaps[:4]:
                    seg = os.path.join(td, f'gap_{int(start)}.mp3')
                    if not cut(audio, start, end - start, seg):
                        continue
                    t = await ask(seg)   # 必须走 ask()，否则策略3 的 _error 不进 net_errors
                    if t and not t.get('_error'):
                        show(t, f'策略3 空隙 {start:.1f}s 起')
                        return finish(audio, key, key_src)
                print('    策略3 各空隙均未识别')

        # 策略 4：人声分离
        if not demucs_available():
            print('    策略4 跳过：demucs/torch 未安装（跑 bash <skill 目录>/install-demucs.sh）')
        else:
            print('    策略4 人声分离中（CPU 推理约 8~10 倍实时，1 分钟素材要 8~10 分钟）…')
            try:
                stem, err = separate_vocals(audio, td)
            except Exception as e:
                # 模型下不下来、内存不够、推理报错都走这里；
                # 不捕获的话整个脚本会崩，连 AcoustID 和后面的文件都不跑了
                stem, err = None, f'{type(e).__name__}: {str(e)[:160]}'
            if err:
                # 技术性失败（模型下不下来、OOM、推理报错）同样不能归因于素材
                net_errors.append(f'人声分离失败：{err}')
                print(f'    策略4 失败：{err}')
            elif stem:
                # 只做保守增益：实测分离后伴奏可能只剩 -48dB，
                # 若猛提 30dB 以上，Shazam 会靠被放大的噪声**误匹配到完全无关的曲子**，
                # 比识别不出来更糟。所以限幅 + 结果必须人工复核。
                # 增益必须是「最多 +12dB」而不是无条件 +12dB：
                # 伴奏本来就录得响时再硬加 12dB 会削波，而削波本身就会制造误匹配
                peak = measure_peak(stem)
                gain = 0.0 if peak is None else max(0.0, min(12.0, -1.0 - peak))
                conv = os.path.join(td, 'accomp.mp3')
                r2 = run(['ffmpeg', '-y', '-v', 'error', '-i', stem,
                          '-af', f'volume={gain:.1f}dB', '-ar', '44100', '-b:a', '192k', conv])
                if r2.returncode != 0 or not os.path.exists(conv):
                    net_errors.append(f'伴奏轨转码失败（ffmpeg 退出码 {r2.returncode}）')
                    print(f'    策略4 失败：伴奏轨转码没成功（{r2.stderr.strip()[:120]}）')
                    return finish(audio, key, key_src)
                t = await ask(conv)
                if t:
                    show(t, '策略4 人声分离后')
                    print('      ⚠️ 分离后信噪比低，此结果**可能误匹配**（实测遇到过），')
                    print('         引用前请回原片复核，别直接当成结论。')
                    return finish(audio, key, key_src)
            print('    策略4 人声分离后仍未识别')

        if net_errors:
            # 有网络/服务/推理错误时绝不能说是素材的问题 —— 那是把用户往错方向带
            print('  ✗ 识别失败，但过程中出现了**技术性错误**（网络/服务/推理），不能归因于素材：')
            for e in dict.fromkeys(net_errors):        # 去重但保持顺序
                print(f'      · {e}')
            print('    网络恢复后重跑即可；不要据此判断「这曲子识别不出来」。')
        else:
            print('  ✗ 四级都没识别出来（指纹服务均正常响应，属素材层面：'
                  '可能是 UP 主自制 BGM / 被大幅变速 / 免版权曲）')
        finish(audio, key, key_src)


def finish(audio, key, key_src):
    res = acoustid(audio, key)
    if not res:
        return
    if res.get('status') != 'ok':
        msg = (res.get('error') or {}).get('message', '未知错误')
        print(f'  AcoustID: 查询失败（{msg}）· key 来自 {key_src}')
        return
    hits = [r for r in (res.get('results') or []) if r.get('recordings')]
    if not hits:
        print(f'  AcoustID: 无匹配（该曲未进 MusicBrainz 指纹库）· key 来自 {key_src}')
        return
    for r in hits:
        for rec in r['recordings']:
            arts = ' / '.join(a['name'] for a in rec.get('artists', [])) or '(未知)'
            print(f"  AcoustID ({r['score']*100:.0f}%): {arts} — {rec.get('title')}")


def preflight():
    """开跑前把依赖点一遍：缺件直接说清装什么，而不是等 import 抛堆栈"""
    # 硬依赖：只有 shazamio 与 ffmpeg/ffprobe。缺了它们主链路根本跑不了。
    missing = []
    try:
        __import__('shazamio')
    except ImportError:
        missing.append('shazamio → python3 -m pip install --break-system-packages shazamio')
    for exe in ('ffmpeg', 'ffprobe'):
        if not shutil.which(exe):
            missing.append(f'{exe} → apt-get install -y ffmpeg')
    if missing:
        print('✗ 缺少运行依赖：')
        for m in missing:
            print(f'    · {m}')
        return False

    # 软依赖：只影响个别策略，缺了要提示但**不能拦着不让跑**
    # （早先把它们当硬依赖，导致没装 soundfile 时连策略1 都不执行，与 README 自相矛盾）
    soft = []
    try:
        __import__('soundfile')
    except ImportError:
        soft.append('soundfile → 策略4 的人声分离需要：python3 -m pip install --break-system-packages soundfile')
    if not shutil.which('fpcalc'):
        soft.append('fpcalc → AcoustID 元数据补充需要：apt-get install -y libchromaprint-tools')
    if soft:
        print('提示：以下可选依赖缺失（只影响对应策略，主链路照跑）：')
        for m in soft:
            print(f'    · {m}')
    return True


async def main(argv):
    args = type('A', (), {'no_fallback': False, 'start': 1})()
    files = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--no-fallback':
            args.no_fallback = True
        elif a == '--strategy':
            i += 1
            if i >= len(argv) or not argv[i].isdigit() or not 1 <= int(argv[i]) <= 4:
                print('✗ --strategy 后面要跟 1~4 的整数（从第几级策略开始试）')
                return 2
            args.start = int(argv[i])
        elif a.startswith('-'):
            # 关键：未知选项必须报错。否则拼错的参数会被静默当成文件名忽略掉，
            # 用户以为参数生效了、其实没有（实测 --no-falback 就是这样被吞掉的）。
            print(f'✗ 未知参数：{a}')
            print('  可用：--no-fallback  --strategy 1..4')
            return 2
        else:
            files.append(a)
        i += 1
    if not files:
        print(__doc__)
        return 2
    if not preflight():
        return 3
    key, key_src = load_key()
    for f in files:
        await handle(f, args, key, key_src)
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main(sys.argv[1:])) or 0)
