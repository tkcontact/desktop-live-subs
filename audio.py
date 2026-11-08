"""出力デバイスの音声を WASAPI ループバックで取り、VAD で発話ごとに切り出す

録る音声は 24kHz (読み上げで声をまねる見本にも使う)。VAD と Whisper には 16kHz にして渡す。
"""

import logging
import threading
import time
from collections import deque

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps

SAMPLE_RATE = 16000  # VAD と Whisper
CAPTURE_RATE = 24000  # 録音 (読み上げの声の見本)
log = logging.getLogger(__name__)


ALL, DEFAULT = "all", "default"  # 取得元: すべての出力デバイス / 既定の出力デバイス / それ以外はデバイス名


def _com_init() -> None:
    import comtypes

    try:
        comtypes.CoInitialize()  # 呼び出し元のスレッドで COM を使えるように (2 回目以降は何もしない)
    except OSError:
        pass


def output_devices() -> list[tuple[str, str]]:
    """使える出力デバイスの (ID, 名前)"""
    from pycaw.pycaw import AudioUtilities

    _com_init()
    coll = AudioUtilities.GetDeviceEnumerator().EnumAudioEndpoints(0, 1)  # eRender, DEVICE_STATE_ACTIVE
    out = []
    for i in range(coll.GetCount()):
        dev = AudioUtilities.CreateDevice(coll.Item(i))
        out.append((dev.id, dev.FriendlyName))
    return out


def _device_signature(target: str):
    """取得元の構成。変わったら開き直す (既定のデバイスの切り替え、USB の抜き差しなど)"""
    try:
        if target == DEFAULT:
            from pycaw.pycaw import AudioUtilities

            _com_init()
            dev = AudioUtilities.GetSpeakers()
            # pycaw の版によって IMMDevice そのものか、それを包んだ AudioDevice が返る
            return dev.GetId() if hasattr(dev, "GetId") else dev.id
        return tuple(sorted(output_devices()))
    except Exception:
        log.debug("出力デバイスの一覧を取得できませんでした", exc_info=True)
        return None


class _Stream:
    """1 つの出力デバイスのループバック。届いた音を 24kHz mono にして溜める。"""

    def __init__(self, pa, dev: dict, block_sec: float):
        import pyaudiowpatch as pyaudio
        import soxr

        self.name = dev["name"].removesuffix(" [Loopback]")
        rate, channels = int(dev["defaultSampleRate"]), dev["maxInputChannels"]
        resampler = soxr.ResampleStream(rate, CAPTURE_RATE, 1, dtype="float32")
        self.lock = threading.Lock()
        self.pending: list[np.ndarray] = []

        def callback(data, frames, time_info, status):
            x = np.frombuffer(data, np.float32).reshape(-1, channels).mean(axis=1)
            y = resampler.resample_chunk(x)
            with self.lock:
                self.pending.append(y)
            return (None, pyaudio.paContinue)

        self.stream = pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(rate * block_sec),
            stream_callback=callback,
        )
        log.info("ループバック録音を開始: %s (%d Hz, %d ch)", self.name, rate, channels)

    def take(self) -> np.ndarray | None:
        with self.lock:
            data, self.pending = self.pending, []
        return np.concatenate(data) if data else None

    def close(self) -> None:
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


class LoopbackAudio:
    """出力デバイスに流れている音を WASAPI ループバックで取り、24kHz mono の float32 で読む。

    target が "all" ならすべての出力デバイスの音を足し合わせる (アプリごとに出力先を変えていても拾える)。
    "default" なら既定の出力デバイスだけ、それ以外はその名前のデバイスだけ。
    WASAPI のループバックは何も再生されていない間はデータが来ないので、その間は無音で埋める
    (埋めないと、動画を止めた直後の発話が無音判定されずに切り出されないまま残る)。
    デバイスの構成が変わったら開き直す。
    """

    def __init__(self, target: str = ALL, block_sec: float = 0.1):
        self.target = target
        self.block_sec = block_sec
        self._pa = None
        self._streams: list[_Stream] = []
        self._signature = None
        self._last_check = 0.0
        self._last_data = 0.0
        self.device_name = ""

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        self._signature = _device_signature(self.target)
        self._pa = pa = pyaudio.PyAudio()
        loopbacks = list(pa.get_loopback_device_info_generator())
        if self.target == ALL:
            devs = loopbacks
        else:
            if self.target == DEFAULT:
                wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                name = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
            else:
                name = self.target
            # 同じ名前のデバイスが複数ある (同じ型のモニターが 2 台など) ときはまとめて録る
            devs = [d for d in loopbacks if d["name"].removesuffix(" [Loopback]") == name]
        if not devs:
            raise RuntimeError(f"出力デバイス「{self.target}」のループバックが見つかりません")
        self._streams = []
        for d in devs:
            try:
                self._streams.append(_Stream(pa, d, self.block_sec))
            except Exception as e:
                log.warning("%s のループバックを開けませんでした: %s", d["name"], e)
        if not self._streams:
            raise RuntimeError("ループバック録音を開始できるデバイスがありません")
        self.device_name = " + ".join(s.name for s in self._streams)
        self._last_check = self._last_data = time.perf_counter()

    def stop(self) -> None:
        for s in self._streams:
            s.close()
        self._streams = []
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    def read(self) -> np.ndarray:
        """block_sec 程度の音声を返す。どのデバイスも再生していない間は経過時間ぶんの無音を返す。"""
        time.sleep(self.block_sec)
        now = time.perf_counter()
        if now - self._last_check > 3.0:
            self._last_check = now
            sig = _device_signature(self.target)
            if sig is not None and sig != self._signature:
                log.info("出力デバイスの構成が変わったので開き直します")
                self.stop()
                self.start()
                return np.zeros(0, np.float32)

        chunks = [c for c in (s.take() for s in self._streams) if c is not None]
        if not chunks:
            gap = now - self._last_data
            if gap < self.block_sec * 2:
                return np.zeros(0, np.float32)
            self._last_data = now
            return np.zeros(int(CAPTURE_RATE * gap), np.float32)
        self._last_data = now
        if len(chunks) == 1:
            return chunks[0]
        # 複数のデバイスで同時に鳴っているときは足し合わせる (ふつうは 1 つしか鳴っていない)
        out = np.zeros(max(len(c) for c in chunks), np.float32)
        for c in chunks:
            out[: len(c)] += c
        return np.clip(out, -1.0, 1.0)


class FileAudio:
    """動作確認用: ローカルの音声/動画ファイルを ffmpeg で実時間で流す。LoopbackAudio と同じ使い方。"""

    def __init__(self, path: str, block_sec: float = 0.1, ffmpeg: str = "ffmpeg"):
        self.path = path
        self.block_sec = block_sec
        self.ffmpeg = ffmpeg
        self.proc = None
        self.device_name = path

    def start(self) -> None:
        import subprocess

        self.proc = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-i", self.path,
             "-vn", "-ac", "1", "-ar", str(CAPTURE_RATE), "-f", "s16le", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def read(self) -> np.ndarray:
        n = int(CAPTURE_RATE * self.block_sec)
        data = self.proc.stdout.read(n * 2)
        if not data:  # 終わったら無音を流し続ける
            time.sleep(self.block_sec)
            return np.zeros(n, np.float32)
        return np.frombuffer(data[: len(data) // 2 * 2], np.int16).astype(np.float32) / 32768.0


class Segmenter:
    """音声を溜めながら VAD をかけ、無音で区切れた発話を順に切り出す。

    バッファ全体に毎回 VAD をかけ直す単純な方式。バッファは発話 1 つ分 (最大 max_speech_s) 程度にしかならない。
    """

    def __init__(self, min_silence_ms: int = 500, max_speech_s: float = 8.0):
        self.vad = VadOptions(
            min_speech_duration_ms=250,
            min_silence_duration_ms=min_silence_ms,
            max_speech_duration_s=max_speech_s,
            speech_pad_ms=200,
        )
        self.buf = np.zeros(0, np.float32)
        self.start = 0  # buf の先頭が、受け取り始めてから何サンプル目か
        self.speaking = False  # バッファの末尾で発話が続いているか

    def reset(self, skipped: int = 0) -> None:
        """溜めた分 (と、渡さずに飛ばした skipped サンプル) を捨てる"""
        self.start += len(self.buf) + skipped
        self.buf = np.zeros(0, np.float32)
        self.speaking = False

    def feed(self, samples: np.ndarray, now: float) -> list[tuple[np.ndarray, float, int]]:
        """(発話の音声, 発話が終わった時刻, 発話の先頭が何サンプル目か) のリストを返す。
        now は samples の末尾を受け取った時刻 (perf_counter)。"""
        self.buf = np.concatenate([self.buf, samples])
        speeches = get_speech_timestamps(self.buf, self.vad)
        if not speeches:
            # 発話なし: 喋り出しを取りこぼさないよう直近 1 秒だけ残す
            self._drop(max(0, len(self.buf) - SAMPLE_RATE))
            self.speaking = False
            return []

        # 最後の発話がバッファ末尾まで続いていれば、まだ喋っている途中
        ongoing = speeches.pop() if speeches[-1]["end"] >= len(self.buf) else None
        self.speaking = ongoing is not None
        n = len(self.buf)
        chunks = [(self.buf[s["start"] : s["end"]], now - (n - s["end"]) / SAMPLE_RATE, self.start + s["start"])
                  for s in speeches]
        self._drop(ongoing["start"] if ongoing else speeches[-1]["end"])
        return chunks

    def _drop(self, n: int) -> None:
        self.buf = self.buf[n:]
        self.start += n


class _Ring:
    """直近 max_sec 秒の 24kHz の音声。受け取り始めてからのサンプル番号で切り出す。"""

    def __init__(self, max_sec: float = 30.0):
        self.max = int(CAPTURE_RATE * max_sec)
        self.blocks: deque[tuple[int, np.ndarray]] = deque()
        self.total = 0

    def append(self, x: np.ndarray) -> None:
        self.blocks.append((self.total, x))
        self.total += len(x)
        while self.blocks and self.blocks[0][0] + len(self.blocks[0][1]) < self.total - self.max:
            self.blocks.popleft()

    def slice(self, a: int, b: int) -> np.ndarray:
        parts = [x[max(0, a - s) : max(0, b - s)] for s, x in self.blocks if s < b and s + len(x) > a]
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


class Capture:
    """LoopbackAudio (または FileAudio) を別スレッドで読み続け、切り出した発話を on_chunk に渡す。

    on_chunk(16kHz の発話, 発話が終わった時刻, 同じ発話の 24kHz の音声)
    """

    def __init__(self, on_chunk, audio, min_silence_ms: int = 500, max_speech_s: float = 8.0):
        self.on_chunk = on_chunk
        self.segmenter = Segmenter(min_silence_ms, max_speech_s)
        self.ring = _Ring()
        self.audio = audio
        self.paused = threading.Event()
        self.muted = None  # True を返す間は録った音を捨てる (読み上げの音が入る取得元で、読み上げている間)
        self._stop = threading.Event()
        self._thread = None

    @property
    def speaking(self) -> bool:
        return self.segmenter.speaking

    def start(self) -> None:
        self.audio.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="capture")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.audio.stop()

    def _run(self) -> None:
        import soxr

        to16k = soxr.ResampleStream(CAPTURE_RATE, SAMPLE_RATE, 1, dtype="float32")
        ratio = CAPTURE_RATE / SAMPLE_RATE
        while not self._stop.is_set():
            try:
                samples = self.audio.read()
            except Exception:
                log.exception("音声の取得に失敗しました。2 秒後に開き直します")
                self.audio.stop()
                time.sleep(2)
                try:
                    self.audio.start()
                except Exception:
                    log.exception("音声デバイスを開けませんでした")
                continue
            if not len(samples):
                continue
            self.ring.append(samples)
            x = to16k.resample_chunk(samples)
            if self.paused.is_set() or (self.muted and self.muted()):
                self.segmenter.reset(len(x))
                continue
            for chunk, ended_at, start in self.segmenter.feed(x, time.perf_counter()):
                ref = self.ring.slice(int(start * ratio), int((start + len(chunk)) * ratio))
                self.on_chunk(chunk, ended_at, ref)
