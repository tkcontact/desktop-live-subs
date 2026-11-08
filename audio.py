"""既定の出力デバイスの音声を WASAPI ループバックで取り、VAD で発話ごとに切り出す"""

import logging
import queue
import threading
import time

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps

SAMPLE_RATE = 16000
log = logging.getLogger(__name__)


def default_output_id() -> str | None:
    """既定の出力デバイスの ID。切り替わりの検出に使う。"""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        try:
            comtypes.CoInitialize()  # 呼び出し元のスレッドで COM を使えるように (2 回目以降は何もしない)
        except OSError:
            pass
        dev = AudioUtilities.GetSpeakers()
        # pycaw の版によって IMMDevice そのものか、それを包んだ AudioDevice が返る
        return dev.GetId() if hasattr(dev, "GetId") else dev.id
    except Exception:
        log.debug("既定の出力デバイスを取得できませんでした", exc_info=True)
        return None


class LoopbackAudio:
    """既定の出力デバイスに流れている音を 16kHz mono の float32 で読む。

    WASAPI のループバックは何も再生されていない間はデータが来ないので、その間は無音で埋める
    (埋めないと、動画を止めた直後の発話が無音判定されずに切り出されないまま残る)。
    既定のデバイスが変わったら開き直す。
    """

    def __init__(self, block_sec: float = 0.1):
        self.block_sec = block_sec
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._pa = None
        self._stream = None
        self._resampler = None
        self._device_id = None
        self._last_check = 0.0
        self._last_data = 0.0
        self.device_name = ""

    def start(self) -> None:
        import pyaudiowpatch as pyaudio
        import soxr

        self._pa = pa = pyaudio.PyAudio()
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        dev = out if out.get("isLoopbackDevice") else None
        if dev is None:
            for lb in pa.get_loopback_device_info_generator():
                if out["name"] in lb["name"]:
                    dev = lb
                    break
        if dev is None:
            raise RuntimeError(f"出力デバイス「{out['name']}」のループバックが見つかりません")

        rate, channels = int(dev["defaultSampleRate"]), dev["maxInputChannels"]
        self._resampler = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")
        self._channels = channels

        def callback(data, frames, time_info, status):
            x = np.frombuffer(data, np.float32).reshape(-1, channels).mean(axis=1)
            self._q.put(x)
            return (None, pyaudio.paContinue)

        self._stream = pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(rate * self.block_sec),
            stream_callback=callback,
        )
        self._device_id = default_output_id()
        self._last_check = self._last_data = time.perf_counter()
        self.device_name = out["name"]
        log.info("ループバック録音を開始: %s (%d Hz, %d ch)", out["name"], rate, channels)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
        self._q = queue.Queue()

    def read(self) -> np.ndarray:
        """block_sec 程度の音声を返す。再生が止まっている間は経過時間ぶんの無音を返す。"""
        now = time.perf_counter()
        if now - self._last_check > 2.0:
            self._last_check = now
            dev_id = default_output_id()
            if dev_id and self._device_id and dev_id != self._device_id:
                log.info("既定の出力デバイスが変わったので開き直します")
                self.stop()
                self.start()
        try:
            x = self._q.get(timeout=self.block_sec * 3)
        except queue.Empty:
            gap = time.perf_counter() - self._last_data
            self._last_data = time.perf_counter()
            return np.zeros(int(SAMPLE_RATE * gap), np.float32)
        self._last_data = time.perf_counter()
        while not self._q.empty():
            x = np.concatenate([x, self._q.get_nowait()])
        return self._resampler.resample_chunk(x)


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
             "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def read(self) -> np.ndarray:
        n = int(SAMPLE_RATE * self.block_sec)
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
        self.speaking = False  # バッファの末尾で発話が続いているか

    def reset(self) -> None:
        self.buf = np.zeros(0, np.float32)
        self.speaking = False

    def feed(self, samples: np.ndarray, now: float) -> list[tuple[np.ndarray, float]]:
        """(発話の音声, 発話が終わった時刻) のリストを返す。now は samples の末尾を受け取った時刻 (perf_counter)。"""
        self.buf = np.concatenate([self.buf, samples])
        speeches = get_speech_timestamps(self.buf, self.vad)
        if not speeches:
            # 発話なし: 喋り出しを取りこぼさないよう直近 1 秒だけ残す
            self.buf = self.buf[-SAMPLE_RATE:]
            self.speaking = False
            return []

        # 最後の発話がバッファ末尾まで続いていれば、まだ喋っている途中
        ongoing = speeches.pop() if speeches[-1]["end"] >= len(self.buf) else None
        self.speaking = ongoing is not None
        n = len(self.buf)
        chunks = [(self.buf[s["start"] : s["end"]], now - (n - s["end"]) / SAMPLE_RATE) for s in speeches]
        if ongoing:
            self.buf = self.buf[ongoing["start"] :]
        else:
            self.buf = self.buf[speeches[-1]["end"] :]
        return chunks


class Capture:
    """LoopbackAudio (または FileAudio) を別スレッドで読み続け、切り出した発話を on_chunk に渡す。"""

    def __init__(self, on_chunk, audio=None, min_silence_ms: int = 500, max_speech_s: float = 8.0):
        self.on_chunk = on_chunk
        self.segmenter = Segmenter(min_silence_ms, max_speech_s)
        self.audio = audio or LoopbackAudio()
        self.paused = threading.Event()
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
            if self.paused.is_set():
                self.segmenter.reset()
                continue
            for chunk, ended_at in self.segmenter.feed(samples, time.perf_counter()):
                self.on_chunk(chunk, ended_at)
