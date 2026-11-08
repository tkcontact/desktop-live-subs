"""訳した日本語を読み上げる

読み上げは Qwen3-TTS を CUDA Graph で速くした faster-qwen3-tts のボイスクローン。声は 3 通り (tts_voice):
- native: 日本語ネイティブの声 (voices/male.wav, female.wav を見本にする)。英語の発話の声の高さで男女を選ぶ
- clone: その文の英語の発話から声質 (話者の埋め込み) だけを取ってまねる。声は似るが少しなまる
- clone_icl: 英語の発話の音声と文字起こしの続きとして読ませる。抑揚やテンションまでまねるが、英語なまりになる
モデルは子プロセスで動かす (PyTorch は重く、生成中に録音や字幕ウィンドウのスレッドを待たせないように)。
読み上げはこのプロセスから鳴らすので、プロセス単位のループバック録音には入らない。

    python tts.py --worker <パイプ名> <モデル>   (Speaker が起動する子プロセス)
"""

import json
import logging
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import types
from collections import deque
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from pathlib import Path

import numpy as np

from sentences import VoiceRef

ROOT = Path(__file__).resolve().parent
TTS_RATE = 24000
MIN_REF_SEC = 1.5  # これより短い見本は声をまねにくいので、直前の見本を使う
REF_SILENCE_SEC = 0.5  # 見本の最後の音が読み上げの頭に混ざらないよう、見本の後ろに足す無音
MAX_PIECE_CHARS = 40  # 長い訳文は文ごとに分けて生成する (一度に生成する長さを抑える)
VOICES = ROOT / "voices"  # 日本語ネイティブの声の見本 (make_voices.py で作る)
NATIVE_VOICES = ("male", "female")
DUCK_FILE = ROOT / "ducked.json"  # 音量を下げている間だけある。落ちたときは次の起動で戻す
_KEY_ENV = "LIVE_SUBS_TTS_KEY"
log = logging.getLogger(__name__)


def split_pieces(text: str) -> list[str]:
    """句点などで区切り (かぎかっこの中では区切らない)、MAX_PIECE_CHARS を超えない範囲でまとめる"""
    sentences, cur, depth = [], "", 0
    for i, ch in enumerate(text):
        cur += ch
        if ch in "「『（(":
            depth += 1
        elif ch in "」』）)":
            depth = max(0, depth - 1)
        elif ch in "。！？!?" and depth == 0 and text[i + 1 : i + 2] not in tuple("。！？!?…」』）)"):
            sentences.append(cur)
            cur = ""
    sentences.append(cur)
    out: list[str] = []
    for s in (s.strip() for s in sentences):
        if s and out and len(out[-1]) + len(s) <= MAX_PIECE_CHARS:
            out[-1] += s
        elif s:
            out.append(s)
    return out


def median_f0(audio: np.ndarray, rate: int) -> float | None:
    """声の高さ (Hz) の中央値。声がはっきりしない (短い・小さい) ときは None"""
    if len(audio) < rate * 0.5:
        return None
    import librosa

    f0 = librosa.yin(audio, fmin=65, fmax=400, sr=rate, frame_length=2048, hop_length=512)
    rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=512)[0]
    n = min(len(f0), len(rms))
    f0, rms = f0[:n], rms[:n]
    voiced = f0[(rms > rms.max() * 0.25) & (f0 > 70) & (f0 < 380)]  # 大きめの音の部分だけ (無声音や BGM を避ける)
    return float(np.median(voiced)) if len(voiced) >= 10 else None


class _NativePicker:
    """英語の発話の声の高さで、ネイティブの声の男女を選ぶ。同じ人で行ったり来たりしないよう、切り替えに幅を持たせる"""

    def __init__(self):
        self.current: str | None = None

    def pick(self, audio: np.ndarray, rate: int) -> tuple[str, float | None]:
        f0 = median_f0(audio, rate)
        if f0 is not None:
            if self.current is None:
                self.current = "male" if f0 < 165 else "female"
            elif self.current == "male" and f0 > 180:
                self.current = "female"
            elif self.current == "female" and f0 < 150:
                self.current = "male"
        return self.current or "male", f0


def _max_tokens(text: str) -> int:
    # 12Hz のコーデックで、1 文字 0.2 秒 + 1.5 秒まで (止まらずに無音や意味のない音を続けるのを防ぐ)
    return int(12 * (len(text) * 0.2 + 1.5))


class TTSWorker:
    """読み上げモデルを動かす子プロセス。synthesize() で生成した音声を少しずつ受け取る。"""

    def __init__(self, model: str):
        self.model = model
        self.proc: subprocess.Popen | None = None
        self.conn = None
        self.info: dict = {}
        self.last: dict = {}

    def start(self, timeout: float = 600) -> dict:
        """子プロセスを起こし、モデルを読み込み終わるまで待つ。読み込みにかかった時間と VRAM を返す。"""
        key = secrets.token_bytes(16)
        address = rf"\\.\pipe\desktop-live-subs-tts-{os.getpid()}-{secrets.token_hex(4)}"
        listener = Listener(address, family="AF_PIPE", authkey=key)
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "--worker", address, self.model],
            env={**os.environ, _KEY_ENV: key.hex()},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        accepted = {}
        t = threading.Thread(target=lambda: accepted.setdefault("conn", listener.accept()), daemon=True)
        t.start()
        t.join(60)
        listener.close()
        if "conn" not in accepted:
            self.stop()
            raise RuntimeError("読み上げのプロセスが起動しませんでした")
        self.conn = accepted["conn"]
        if not self.conn.poll(timeout):
            self.stop()
            raise TimeoutError("読み上げのモデルを読み込めませんでした (時間切れ)")
        msg = self.conn.recv()
        if msg[0] != "ready":
            self.stop()
            raise RuntimeError(msg[1])
        self.info = msg[1]
        return self.info

    def synthesize(self, texts: list[str], ref: VoiceRef | None, voice: str = "native", native: str = "auto"):
        """texts を順に読んだ音声 (24kHz float32) を少しずつ返す。終わると self.last に所要時間などが入る。
        voice は native / clone / clone_icl、native はネイティブの声の男女 (auto / male / female)。
        途中でやめると子プロセスとのやりとりがずれるので、最後まで読み切ること。"""
        audio = np.asarray(ref.audio, np.float32) if ref else np.zeros(0, np.float32)
        self.conn.send(("say", texts, audio, ref.rate if ref else TTS_RATE, ref.text if ref else "", voice, native))
        while True:
            msg = self.conn.recv()
            if msg[0] == "chunk":
                yield msg[1]
            elif msg[0] == "done":
                self.last = msg[1]
                return
            else:
                raise RuntimeError(msg[1])

    def stop(self) -> None:
        if self.conn:
            try:
                self.conn.send(("quit",))
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def _worker(address: str, model_name: str) -> None:
    """子プロセス: モデルを読み込み、頼まれた文を読み上げた音声を返し続ける"""
    # pythonw では標準出力がなく、ライブラリの print や進捗表示が落ちるので捨て先を用意する
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / "logs" / "tts.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = Client(address, family="AF_PIPE", authkey=bytes.fromhex(os.environ.pop(_KEY_ENV)))
    try:
        # sox は 25Hz のモデルでしか使わない。import するだけで cmd を起動して探すので、空のモジュールで済ませる
        sys.modules.setdefault("sox", types.ModuleType("sox"))
        from faster_qwen3_tts import FasterQwen3TTS

        from vram import gpu_used_mib

        before = gpu_used_mib()
        t0 = time.perf_counter()
        model = FasterQwen3TTS.from_pretrained(model_name, max_seq_len=1024)
        model.warmup(prefill_len=100)
        natives = {}
        for name in NATIVE_VOICES:
            wav, txt = VOICES / f"{name}.wav", VOICES / f"{name}.txt"
            if wav.exists() and txt.exists():
                import soundfile as sf

                x, sr = sf.read(wav, dtype="float32")
                x = np.concatenate([x.mean(axis=1) if x.ndim > 1 else x, np.zeros(int(sr * REF_SILENCE_SEC), np.float32)])
                natives[name] = model.model.create_voice_clone_prompt(
                    ref_audio=(x, sr), ref_text=txt.read_text(encoding="utf-8").strip())
        info = {"load_sec": round(time.perf_counter() - t0, 1), "vram_mib": gpu_used_mib() - before,
                "voices": sorted(natives)}
        log.info("%s を読み込みました: %s", model_name, info)
        conn.send(("ready", info))
    except Exception as e:
        log.exception("読み上げのモデルを読み込めませんでした")
        conn.send(("error", f"読み上げのモデルを読み込めませんでした: {type(e).__name__}: {e}"))
        return

    picker = _NativePicker()

    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg[0] == "quit":
            return
        _, texts, audio, rate, ref_text, voice, native = msg
        try:
            t0 = time.perf_counter()
            # 見本の音量をそろえる (読み上げ中は元の音を下げているので、その間に録った見本は小さい)
            rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
            audio = np.clip(audio * (0.08 / rms), -1.0, 1.0) if rms > 1e-4 else audio
            if voice == "clone_icl" and len(audio) and ref_text:
                ref = np.concatenate([audio, np.zeros(int(rate * REF_SILENCE_SEC), np.float32)])
                prompt = model.model.create_voice_clone_prompt(ref_audio=(ref, rate), ref_text=ref_text)
                used = voice
            elif (voice == "clone" or not natives) and len(audio):
                prompt = model.model.create_voice_clone_prompt(ref_audio=(audio, rate), x_vector_only_mode=True)
                used = "clone"
            elif natives:
                name, f0 = (native, None) if native in natives else picker.pick(audio, rate)
                name = name if name in natives else next(iter(natives))
                prompt = natives[name]
                used = f"native:{name}" + (f" ({f0:.0f}Hz)" if f0 else "")
            else:
                raise RuntimeError("声の見本がありません (voices/ に male.wav などを置くか、Whisper 方式にしてください)")
            first, n = None, 0
            for text in texts:
                for chunk, sr, _ in model.generate_voice_clone_streaming(
                    text=text, language="Japanese", voice_clone_prompt=prompt,
                    max_new_tokens=_max_tokens(text), chunk_size=8,
                ):
                    if first is None:
                        first = time.perf_counter() - t0
                    x = np.asarray(chunk, np.float32).reshape(-1)
                    if sr != TTS_RATE:
                        import soxr

                        x = soxr.resample(x, sr, TTS_RATE)
                    conn.send(("chunk", x))
                    n += len(x)
            conn.send(("done", {"first_sec": first or 0.0, "gen_sec": time.perf_counter() - t0,
                                "audio_sec": n / TTS_RATE, "voice": used}))
        except Exception as e:
            log.exception("読み上げを生成できませんでした: %s", texts)
            conn.send(("error", f"{type(e).__name__}: {e}"))


# --- このプロセス以外のアプリの音声セッション (音量を下げる・鳴っているデバイスを調べる) ---


def _other_sessions():
    """(デバイス名, セッションの識別子, ISimpleAudioVolume, IAudioMeterInformation) を、すべての出力デバイスについて返す"""
    from pycaw.pycaw import (AudioUtilities, IAudioMeterInformation, IAudioSessionControl2,
                             ISimpleAudioVolume)

    from audio import _com_init

    _com_init()
    own = os.getpid()
    coll = AudioUtilities.GetDeviceEnumerator().EnumAudioEndpoints(0, 1)  # eRender, DEVICE_STATE_ACTIVE
    for i in range(coll.GetCount()):
        try:
            dev = AudioUtilities.CreateDevice(coll.Item(i))
            sessions = dev.AudioSessionManager.GetSessionEnumerator()
            for j in range(sessions.GetCount()):
                ctl = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
                pid = ctl.GetProcessId()
                if pid in (0, own):  # 0 はシステムの音
                    continue
                yield (dev.FriendlyName, f"{dev.id}|{ctl.GetSessionInstanceIdentifier()}",
                       ctl.QueryInterface(ISimpleAudioVolume), ctl.QueryInterface(IAudioMeterInformation))
        except Exception:
            log.debug("音声セッションを調べられませんでした", exc_info=True)


class Ducker:
    """読み上げている間、ほかのアプリの音量を level 倍に下げる。

    下げる前の音量は DUCK_FILE にも書いておき、落ちて戻せなかったときは次の起動で戻す。
    下げている間にユーザーが音量を変えたアプリは、戻さずにそのままにする。
    """

    def __init__(self, level: float = 0.3):
        self.level = level
        self.saved: dict[str, float] = {}
        self._lock = threading.Lock()

    def duck(self) -> None:
        with self._lock:
            if self.saved:
                return
            sessions = [(key, vol) for _, key, vol, _ in _other_sessions()]
            self.saved = {key: vol.GetMasterVolume() for key, vol in sessions}
            try:
                DUCK_FILE.write_text(json.dumps({"level": self.level, "sessions": self.saved}), encoding="utf-8")
            except OSError:
                log.warning("下げる前の音量を保存できませんでした", exc_info=True)
            self._ramp(sessions, (0.6, 0.3, 0.0))

    def restore(self) -> None:
        with self._lock:
            if not self.saved:
                return
            sessions = [(key, vol) for _, key, vol, _ in _other_sessions() if key in self.saved]
            sessions = [(k, v) for k, v in sessions if abs(v.GetMasterVolume() - self.saved[k] * self.level) < 0.02]
            self._ramp(sessions, (0.4, 0.7, 1.0))
            self.saved = {}
            DUCK_FILE.unlink(missing_ok=True)

    @classmethod
    def recover(cls) -> None:
        """前回落ちて下げたままになっている音量を戻す"""
        try:
            data = json.loads(DUCK_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        d = cls(data["level"])
        d.saved = data["sessions"]
        log.info("前回下げたままの音量を戻します (%d 個のアプリ)", len(d.saved))
        d.restore()

    def _ramp(self, sessions, steps) -> None:
        # 一気に変えると耳につくので、少しずつ変える (steps は元の音量に対する割合。0 で level 倍)
        for step in steps:
            for key, vol in sessions:
                try:
                    vol.SetMasterVolume(self.saved[key] * (self.level + (1 - self.level) * step), None)
                except Exception:
                    pass
            time.sleep(0.03)


class OutputPicker:
    """読み上げを鳴らすデバイスを選ぶ。

    tts_output が "auto" なら、ほかのアプリの音がいちばん鳴っているデバイス (= 元の音を聞いているデバイス)。
    "default" なら既定の出力デバイス、それ以外はその名前のデバイス。
    """

    def __init__(self, settings):
        self.settings = settings
        self.levels: dict[str, float] = {}
        self._stop = threading.Event()
        threading.Thread(target=self._watch, daemon=True, name="tts-meter").start()

    def stop(self) -> None:
        self._stop.set()

    def device_name(self) -> str | None:
        target = self.settings["tts_output"]
        if target == "default":
            return None
        if target != "auto":
            return target
        levels = dict(self.levels)
        if not levels or max(levels.values()) < 0.01:
            return None
        return max(levels, key=levels.get)

    def _watch(self) -> None:
        while not self._stop.is_set():
            if self.settings["tts_output"] == "auto":
                peaks: dict[str, float] = {}
                try:
                    for name, _, _, meter in _other_sessions():
                        peaks[name] = max(peaks.get(name, 0.0), meter.GetPeakValue())
                except Exception:
                    log.debug("音量のメーターを読めませんでした", exc_info=True)
                # 直近数秒でいちばん鳴っていたデバイスを覚えておく (無音の間に切り替わらないよう、ゆっくり減らす)
                for name in set(self.levels) | set(peaks):
                    self.levels[name] = max(self.levels.get(name, 0.0) * 0.9, peaks.get(name, 0.0))
            self._stop.wait(0.5)


class _Player:
    """読み上げの音声を出力デバイスに流す。鳴らしている間はほかのアプリの音量を下げる。"""

    IDLE_SEC = 0.8  # これだけ次が来なければデバイスを閉じて音量を戻す

    def __init__(self, settings, picker: OutputPicker, on_error):
        self.settings = settings
        self.picker = picker
        self.on_error = on_error
        self.ducker = Ducker(settings["tts_duck_level"])
        self.q: queue.Queue[np.ndarray | None] = queue.Queue()
        self._queued = 0
        self._lock = threading.Lock()
        self.device_name = ""
        self.active = False  # 出力デバイスを開いて鳴らしている (次を待っている短い間も含む)
        self._thread = threading.Thread(target=self._run, daemon=True, name="tts-play")
        self._thread.start()

    def feed(self, x: np.ndarray) -> None:
        if len(x):
            with self._lock:
                self._queued += len(x)
            self.q.put(x)

    def queued_sec(self) -> float:
        return self._queued / TTS_RATE

    def close(self) -> None:
        """鳴らし終えて音量を戻すまで待つ"""
        self.q.put(None)
        self._thread.join(5)

    def _run(self) -> None:
        import pyaudiowpatch as pyaudio
        import soxr

        from audio import _com_init

        _com_init()
        pa = pyaudio.PyAudio()
        stream = resampler = None
        channels = 1
        try:
            while True:
                try:
                    x = self.q.get(timeout=self.IDLE_SEC if stream else None)
                except queue.Empty:
                    self._close(stream, resampler, channels)
                    stream = None
                    self.active = False
                    continue
                if x is None:
                    break
                if stream is None:
                    try:
                        dev = self._device(pa, pyaudio)
                        rate, channels = int(dev["defaultSampleRate"]), min(2, dev["maxOutputChannels"])
                        stream = pa.open(format=pyaudio.paFloat32, channels=channels, rate=rate, output=True,
                                         output_device_index=dev["index"])
                        resampler = soxr.ResampleStream(TTS_RATE, rate, 1, dtype="float32")
                        self.device_name = dev["name"]
                        self.active = True
                    except Exception as e:
                        log.exception("読み上げの出力デバイスを開けませんでした")
                        self.on_error(f"読み上げの出力デバイスを開けませんでした: {e}")
                        self._done(len(x))
                        continue
                    if self.settings["tts_duck"]:
                        self.ducker.level = self.settings["tts_duck_level"]
                        self.ducker.duck()
                self._write(stream, resampler.resample_chunk(x * float(self.settings["tts_volume"])), channels)
                self._done(len(x))
        finally:
            self._close(stream, resampler, channels)
            self.active = False
            pa.terminate()

    def _done(self, n: int) -> None:
        with self._lock:
            self._queued -= n

    def _device(self, pa, pyaudio) -> dict:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        name = self.picker.device_name()
        if name:
            for i in range(wasapi["deviceCount"]):
                d = pa.get_device_info_by_host_api_device_index(wasapi["index"], i)
                if d["maxOutputChannels"] > 0 and not d.get("isLoopbackDevice") and d["name"] == name:
                    return d
        return pa.get_device_info_by_index(wasapi["defaultOutputDevice"])

    @staticmethod
    def _write(stream, y: np.ndarray, channels: int) -> None:
        if len(y):
            y = np.clip(y, -1.0, 1.0).astype(np.float32)
            stream.write((np.repeat(y[:, None], channels, axis=1) if channels > 1 else y).tobytes())

    def _close(self, stream, resampler, channels: int) -> None:
        if stream is not None:
            try:
                self._write(stream, resampler.resample_chunk(np.zeros(0, np.float32), last=True), channels)
                stream.stop_stream()  # 残りを鳴らし終えてから止まる
                stream.close()
            except Exception:
                log.debug("出力デバイスを閉じられませんでした", exc_info=True)
        self.ducker.restore()


@dataclass
class _Job:
    text: str
    ref: VoiceRef
    ended_at: float  # 元の発話が終わった時刻 (perf_counter)


class Speaker:
    """訳文を順に読み上げる。遅れてきたら少し速く読み、遅れすぎた文は読まずに飛ばす。"""

    def __init__(self, settings, on_error):
        self.settings = settings
        self.on_error = on_error
        self.worker = TTSWorker(settings["tts_model"])
        self.jobs: deque[_Job] = deque()
        self._cv = threading.Condition()
        self._stopped = False
        self._last_ref: VoiceRef | None = None
        self.picker = None
        self.player = None

    def start(self) -> dict:
        """モデルを読み込み終わるまで待つ (数十秒かかることがある)"""
        info = self.worker.start()
        self.picker = OutputPicker(self.settings)
        self.player = _Player(self.settings, self.picker, self.on_error)
        threading.Thread(target=self._run, daemon=True, name="tts").start()
        return info

    @property
    def playing(self) -> bool:
        return bool(self.player and self.player.active)

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self.jobs.clear()
            self._cv.notify()
        if self.player:
            self.player.close()
        if self.picker:
            self.picker.stop()
        self.worker.stop()

    def say(self, text: str, ref: VoiceRef | None, ended_at: float) -> None:
        if ref is None or ref.seconds < MIN_REF_SEC:
            ref = self._last_ref or ref  # 短すぎる見本 (「Yeah.」だけなど) より、直前の話し手の声を使う
        else:
            self._last_ref = ref
        if ref is None and self.settings["tts_voice"] != "native":
            log.debug("声の見本がないので読み上げません: %s", text)
            return
        with self._cv:
            self.jobs.append(_Job(text, ref, ended_at))
            self._cv.notify()

    def _run(self) -> None:
        from audiotsm import wsola

        s = self.settings
        while True:
            with self._cv:
                while not self.jobs and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                job = self.jobs.popleft()
                backlog = len(self.jobs)
            wait = self.player.queued_sec()  # 前の読み上げが鳴り終わるまで
            lag = time.perf_counter() + wait - job.ended_at
            if lag > s["tts_max_lag_sec"]:
                log.info("読み上げが %.1f 秒遅れているので飛ばします: %s", lag, job.text)
                continue
            # いつも tts_speed 倍で読み、前の読み上げがまだ鳴っていたり次が待っていたりする (詰まっている) ときは
            # さらに速く読んで追いつく。翻訳を待つ分の遅れ (3 秒前後) はいつもあるので数えない
            speed = min(s["tts_max_speed"], s["tts_speed"] + 0.15 * max(0.0, wait - 0.5) + 0.1 * backlog)
            tsm = wsola(1, speed=speed) if speed > 1.02 else None
            try:
                for x in self.worker.synthesize(split_pieces(job.text), job.ref, s["tts_voice"], s["tts_native"]):
                    self.player.feed(_stretch(tsm, x))
                if tsm:
                    self.player.feed(_stretch(tsm, None))
            except (EOFError, OSError) as e:
                log.exception("読み上げのプロセスが止まりました")
                self.on_error(f"読み上げが止まりました ({type(e).__name__})。トレイから入れ直してください")
                return
            except Exception as e:
                log.exception("読み上げに失敗しました: %s", job.text)
                self.on_error(f"読み上げに失敗しました: {e}")
                continue
            st = self.worker.last
            log.info("読み上げ 生成%5.2fs(初音%4.2fs) 音声%4.1fs ×%.2f 遅れ%4.1fs 待ち%d 声 %s → %s | %s",
                     st["gen_sec"], st["first_sec"], st["audio_sec"], speed, lag, backlog, st.get("voice"),
                     self.player.device_name or "-", job.text)


def _stretch(tsm, x: np.ndarray | None) -> np.ndarray:
    """速さを変える (x が None なら残りを出し切る)。tsm が None なら何もしない。"""
    if tsm is None:
        return x if x is not None else np.zeros(0, np.float32)
    from audiotsm.io.array import ArrayReader, ArrayWriter

    writer = ArrayWriter(1)
    if x is None:
        finished = False
        while not finished:
            _, finished = tsm.flush_to(writer)
    else:
        tsm.run(ArrayReader(x[np.newaxis, :].astype(np.float64)), writer, flush=False)
    return writer.data[0].astype(np.float32)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    _worker(sys.argv[2], sys.argv[3])
