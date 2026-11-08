"""Whisper の VRAM 使用量を測り、Strata に空けておいてもらう量 (--vram-reserve-mib) を決める

    python vram.py --reserve   予約量 (MiB) を 1 行で出す。測ったことがなければ先に測る (start.cmd が使う)
    python vram.py --measure   測り直して結果を出す
    python vram.py --stress 120 [--audio 音声ファイル]
                               Whisper と Strata を同時に回し続け、VRAM の最大使用量とエラーを出す

Strata は起動時に空いている VRAM から予約量を引いた分をエキスパートのキャッシュに使う。
start.cmd は Strata → 字幕ツールの順に起動するので、予約量には Whisper の分を含める。
"""

import argparse
import json
import threading
import time
from datetime import datetime

import numpy as np

from settings import ROOT, Settings

RESULT = ROOT / "vram.json"
# Strata が既定で画面表示用に空けておく量 (16GB 以下で画面をつないだカード)
DESKTOP_RESERVE_MIB = 1536
MARGIN_MIB = 256


def gpu_used_mib(index: int = 0) -> int:
    import pynvml

    pynvml.nvmlInit()
    try:
        return pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(index)).used // 2**20
    finally:
        pynvml.nvmlShutdown()


class PeakSampler:
    """別スレッドで VRAM の使用量を見続け、最大値を覚える"""

    def __init__(self, interval: float = 0.05):
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        import pynvml

        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        while not self._stop.is_set():
            self.peak = max(self.peak, pynvml.nvmlDeviceGetMemoryInfo(h).used // 2**20)
            time.sleep(self.interval)
        pynvml.nvmlShutdown()


def measure(settings: Settings) -> dict:
    """Whisper を読み込んで数回文字起こしし、その間に増えた VRAM の最大値を測る"""
    from asr import Transcriber

    before = gpu_used_mib()
    t0 = time.perf_counter()
    with PeakSampler() as sampler:
        tr = Transcriber(settings["whisper_model"], settings["compute_type"], settings["beam_size"])
        load_sec = time.perf_counter() - t0
        # 中身は何でもよい (Whisper は 30 秒の窓で処理するので、エンコーダーの使用量は長さによらない)。
        # デコーダーもビームサーチで動くよう、雑音の強さを変えて何回か流す
        rng = np.random.default_rng(0)
        times = []
        for level in (0.02, 0.1, 0.3):
            audio = (rng.standard_normal(16000 * int(settings["max_chunk_sec"])) * level).astype(np.float32)
            t = time.perf_counter()
            tr.transcribe(audio)
            times.append(time.perf_counter() - t)
        after = gpu_used_mib()
    del tr
    return {
        "whisper_mib": max(sampler.peak, after) - before,
        "idle_after_mib": after - before,
        "before_mib": before,
        "load_sec": round(load_sec, 2),
        "transcribe_sec": [round(t, 2) for t in times],
        "model": settings["whisper_model"],
        "compute_type": settings["compute_type"],
        "beam_size": settings["beam_size"],
        "measured_at": datetime.now().isoformat(timespec="seconds"),
    }


STRESS_SENTENCES = [
    "Okay, so welcome back to the channel.",
    "Today we are going to talk about how to make a really good cup of coffee at home.",
    "Honestly, I was surprised how much of a difference it made when I tried it for the first time.",
    "Let me know in the comments what you think, and I'll see you in the next one.",
]


def stress(settings: Settings, seconds: float, audio_path: str | None) -> dict:
    """Whisper の文字起こしと Strata の翻訳を同時に回し続け、VRAM の最大使用量とエラーを調べる"""
    import statistics

    from asr import Transcriber
    from faster_whisper import decode_audio
    from translator import Translator

    tl = Translator(settings["strata_url"])
    if not tl.healthy():
        raise SystemExit("Strata が応答しません。先に Strata を起動してください")
    tr = Transcriber(settings["whisper_model"], settings["compute_type"], settings["beam_size"])
    n = 16000 * int(settings["max_chunk_sec"])
    audio = decode_audio(audio_path)[:n] if audio_path else np.random.default_rng(0).standard_normal(n).astype(np.float32) * 0.1

    stop = threading.Event()
    asr_times, llm_times, errors = [], [], []

    def asr_loop():
        while not stop.is_set():
            t = time.perf_counter()
            try:
                tr.transcribe(audio)
            except Exception as e:
                errors.append(f"whisper: {e}")
            asr_times.append(time.perf_counter() - t)

    def llm_loop():
        i = 0
        while not stop.is_set():
            t = time.perf_counter()
            try:
                tl.translate(STRESS_SENTENCES[i % len(STRESS_SENTENCES)])
            except Exception as e:
                errors.append(f"strata: {e}")
            llm_times.append(time.perf_counter() - t)
            i += 1

    import pynvml

    pynvml.nvmlInit()
    total = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0)).total // 2**20
    pynvml.nvmlShutdown()
    with PeakSampler() as sampler:
        threads = [threading.Thread(target=f, daemon=True) for f in (asr_loop, llm_loop)]
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        for t in threads:
            t.join(timeout=60)

    def med(xs):
        return round(statistics.median(xs), 2) if xs else None

    return {
        "seconds": seconds,
        "vram_peak_mib": sampler.peak,
        "vram_total_mib": total,
        "vram_min_free_mib": total - sampler.peak,
        "whisper_runs": len(asr_times),
        "whisper_median_sec": med(asr_times),
        "strata_requests": len(llm_times),
        "strata_median_sec": med(llm_times),
        "strata_max_sec": round(max(llm_times), 2) if llm_times else None,
        "errors": errors[:10],
        "error_count": len(errors),
    }


def load_result(settings: Settings) -> dict | None:
    try:
        r = json.loads(RESULT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 測ったときと Whisper の設定が違えば測り直す
    same = r.get("model") == settings["whisper_model"] and all(r.get(k) == settings[k] for k in ("compute_type", "beam_size"))
    return r if same else None


def save_result(result: dict) -> None:
    RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")


def reserve_mib(settings: Settings, result: dict) -> int:
    return DESKTOP_RESERVE_MIB + result["whisper_mib"] + MARGIN_MIB + int(settings["strata_extra_reserve_mib"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--reserve", action="store_true")
    g.add_argument("--measure", action="store_true")
    g.add_argument("--stress", type=float, metavar="秒", help="Whisper と Strata を同時に回し続けて VRAM を調べる")
    p.add_argument("--audio", help="--stress で Whisper に流す音声ファイル (省略時は雑音)")
    args = p.parse_args()
    settings = Settings()
    if args.stress:
        print(json.dumps(stress(settings, args.stress, args.audio), ensure_ascii=False, indent=2))
        return
    result = None if args.measure else load_result(settings)
    if result is None:
        result = measure(settings)
        save_result(result)
    if args.reserve:
        print(reserve_mib(settings, result))
    else:
        print(json.dumps({**result, "strata_reserve_mib": reserve_mib(settings, result)}, indent=2))


if __name__ == "__main__":
    main()
