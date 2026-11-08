"""Windows で流れている英語の音声をリアルタイムで日本語字幕にする常駐ツール

音声認識は 2 方式 (トレイで切り替え):
- whisper: PC で鳴っている音をループバックで録り、VAD で区切って faster-whisper で文字起こし
- livecaptions: Windows のライブキャプションの字幕テキストを UI Automation で読む
どちらも確定した文を Strata (OpenAI 互換 API) で日本語に訳し、最前面の字幕ウィンドウに出す。
読み上げをオンにすると、訳文を元の話し手の声をまねて読み上げる (tts.py)。
"""

import argparse
import csv
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

from audio import ALL, CAPTURE_RATE, SAMPLE_RATE, Capture, FileAudio, LoopbackAudio, output_devices
from hotkeys import Hotkeys
from livecaptions import LiveCaptionsSource
from overlay import Overlay
from sentences import FragmentMerger, RecentFilter, Unit, VoiceRef, pick_ref
from settings import ROOT, Settings
from tray import ASR_LABELS, Tray
from translator import _JAPANESE, MAX_BATCH_CHARS, Profile, StrataUnavailable, Translator, list_profiles

LOG_DIR = ROOT / "logs"
PROFILE_DIR = ROOT / "profiles"
STRATA_CMD = os.path.expandvars(r"%LOCALAPPDATA%\Strata\terminal\current\bin\strata.cmd")
STALE_SEC = 20.0  # Strata の復旧を待つ間に、これより古くなった文は訳さない
STATUS_ORDER = ("pause", "strata", "source", "tts", "vram", "notice")
log = logging.getLogger("live-subs")


class WhisperSource:
    """ループバック録音 → VAD → faster-whisper。短い断片は FragmentMerger でまとめる。"""

    name = "whisper"

    def __init__(self, app: "App"):
        self.app = app
        self.chunks: queue.Queue = queue.Queue()
        self.merger = FragmentMerger()
        self.paused = threading.Event()
        self._stop = threading.Event()
        self.capture = None
        self.transcriber = None

    @property
    def speaking(self) -> bool:
        # 文字起こし待ちの発話があるときも「続きが来る」とみなす
        return bool(self.capture and self.capture.speaking) or not self.chunks.empty()

    def start(self) -> None:
        self.transcriber = self.app.load_transcriber()
        self.transcriber.initial_prompt = self.app.whisper_prompt()
        s = self.app.settings
        if self.app.args.file:
            audio = FileAudio(self.app.args.file)
        elif s["capture_device"] == ALL:
            from process_loopback import ProcessLoopbackAudio

            audio = ProcessLoopbackAudio(CAPTURE_RATE)
        else:
            audio = LoopbackAudio(s["capture_device"])
        self.capture = Capture(lambda c, t, r: self.chunks.put((c, t, r)), audio, s["min_silence_ms"], s["max_chunk_sec"])
        self.capture.paused = self.paused
        try:
            self.capture.start()
        except Exception:
            if s["capture_device"] != ALL or self.app.args.file:
                raise
            log.warning("プロセス単位のループバックを使えないので、出力デバイスごとのループバックを足し合わせます", exc_info=True)
            self.capture.audio = audio = LoopbackAudio(ALL)
            self.capture.start()
        if isinstance(audio, LoopbackAudio):
            # デバイスのループバックには読み上げの音も入るので、読み上げている間は録らない (訳し直さないように)
            self.capture.muted = self.app.tts_playing
        log.info("Whisper 方式で開始 (入力: %s)", self.capture.audio.device_name)
        threading.Thread(target=self._asr_loop, daemon=True, name="asr").start()

    def stop(self) -> None:
        self._stop.set()
        if self.capture:
            self.capture.stop()

    def set_prompt(self, prompt: str | None) -> None:
        if self.transcriber:
            self.transcriber.initial_prompt = prompt

    def _asr_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk, ended_at, ref_audio = self.chunks.get(timeout=0.2)
            except queue.Empty:
                chunk = None
            units = []
            if chunk is not None and not self.paused.is_set():
                t0 = time.perf_counter()
                try:
                    text = self.transcriber.transcribe(chunk)
                except Exception:
                    log.exception("文字起こしに失敗しました")
                    text = ""
                asr_sec = time.perf_counter() - t0
                log.debug("ASR %.2fs (音声 %.1fs): %s", asr_sec, len(chunk) / SAMPLE_RATE, text)
                if text:
                    audio_sec = len(chunk) / SAMPLE_RATE
                    cut = audio_sec >= self.app.settings["max_chunk_sec"] - 0.5  # 無音で区切れず上限で切った
                    ref = VoiceRef(ref_audio, CAPTURE_RATE, text)  # 読み上げで声をまねる見本
                    units = self.merger.add(Unit(text, ended_at, audio_sec, asr_sec, cut=cut, ref=ref), time.perf_counter())
            if self.paused.is_set():
                self.merger.flush()
                continue
            units += self.merger.flush_due(time.perf_counter(), self.speaking)
            for u in units:
                self.app.submit(u)


class SpeechStream:
    """訳している途中の文を、文末まで確定したところから読み上げに回す (訳し終わるのを待たずに読み始める)"""

    def __init__(self, speaker, unit: Unit):
        self.speaker = speaker
        self.unit = unit
        self.job = None
        self.sent = ""  # 読み上げに回した部分

    def update(self, text: str) -> None:
        from tts import confirmed_prefix

        self._send(text[: confirmed_prefix(text)])

    def finish(self, text: str | None) -> None:
        """訳し終えた (text が None なら失敗した)。残りを回して終える"""
        if text:
            self._send(text)
        if self.job:
            self.job.end()

    def _send(self, prefix: str) -> None:
        # 訳し直しなどで食い違ったら、もう読んだ分はそのままにして続けない
        if not prefix.startswith(self.sent) or not _JAPANESE.search(prefix[len(self.sent):]):
            return
        if self.job is None:
            self.job = self.speaker.begin(self.unit.ref, self.unit.ended_at)
            if self.job is None:
                return
        self.job.add(prefix[len(self.sent):])
        self.sent = prefix


class App:
    def __init__(self, args: argparse.Namespace, settings: Settings):
        self.args = args
        self.settings = settings
        self.paused = False
        self.units: queue.Queue[Unit | None] = queue.Queue()
        self.filter = RecentFilter()
        self.source = None
        self.transcriber = None
        self.speaker = None  # 読み上げ (tts.Speaker)
        self._switch_lock = threading.Lock()
        self._whisper_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._status: dict[str, str] = {}

        self.translator = Translator(settings["strata_url"], settings["context"])
        self.profile = self._load_profile(settings["profile"])
        self.translator.set_profile(self.profile)

        self.overlay = Overlay(settings, {"一時停止 / 再開": self.toggle_pause, "終了": self.quit})
        self.tray = Tray(self)
        hk = settings["hotkeys"]
        self.hotkeys = Hotkeys(
            {
                "クリック透過の切り替え": (hk["click_through"], self.toggle_click_through),
                "表示の切り替え": (hk["visible"], self.toggle_visible),
                "一時停止の切り替え": (hk["pause"], self.toggle_pause),
            },
            on_error=lambda msg: self.status("notice", msg, clear_after=10),
        )

        LOG_DIR.mkdir(exist_ok=True)
        self.tsv_path = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.tsv"

    # --- 起動と終了 ---

    def run(self) -> None:
        self.tray.start()
        self.hotkeys.start()
        threading.Thread(target=self._translate_loop, daemon=True, name="translate").start()
        threading.Thread(target=self._vram_monitor, daemon=True, name="vram").start()
        self.switch_asr(self.settings["asr"])
        threading.Thread(target=self._recover_volume, daemon=True, name="recover").start()
        if self.settings["tts"]:
            threading.Thread(target=self._start_tts, daemon=True, name="tts-start").start()
        self.overlay.run()

    def quit(self) -> None:
        log.info("終了します")
        self.units.put(None)
        self.hotkeys.stop()
        self.tray.stop()
        if self.source:
            self.source.stop()
        if self.speaker:
            self.speaker.stop()
        if self.settings["stop_strata_on_exit"] and os.path.exists(STRATA_CMD):
            self.status("notice", "Strata を止めています…")
            subprocess.Popen(
                ["cmd", "/c", STRATA_CMD, "launch", "--stop", "--preset", self.settings["strata_preset"]],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        self.overlay.call(self.overlay.close)

    # --- 音声認識 ---

    def switch_asr(self, mode: str) -> None:
        self.settings["asr"] = mode
        self.settings.save()
        self.tray.refresh()
        threading.Thread(target=self._start_source, daemon=True, name="switch").start()

    def _start_source(self) -> None:
        with self._switch_lock:
            if self.source:
                self.source.stop()
                self.source = None
            mode = self.settings["asr"]
            self.status("source", f"{ASR_LABELS[mode]} を準備中…")
            try:
                if mode == "whisper":
                    src = WhisperSource(self)
                else:
                    src = LiveCaptionsSource(self.submit, lambda msg: self.status("source", msg))
                if self.paused:
                    src.paused.set()
                src.start()
            except Exception as e:
                log.exception("音声認識を開始できませんでした")
                self.status("source", f"音声認識を開始できませんでした: {e}")
                return
            self.source = src
            if mode == "whisper":
                self.status("source", "")
            self.status("notice", f"音声認識: {ASR_LABELS[mode]}", clear_after=4)

    def load_transcriber(self):
        """Whisper は一度だけ読み込み、方式を切り替えても持っておく"""
        with self._whisper_lock:
            return self._load_transcriber()

    def _load_transcriber(self):
        if self.transcriber is None:
            from asr import Transcriber
            from vram import gpu_used_mib

            s = self.settings
            self.status("source", "Whisper を読み込み中…")
            before = gpu_used_mib()
            self.transcriber = Transcriber(s["whisper_model"], s["compute_type"], s["beam_size"])
            log.info("Whisper %s (%s) を読み込みました: VRAM +%d MiB", s["whisper_model"], s["compute_type"], gpu_used_mib() - before)
        return self.transcriber

    # --- 読み上げ ---

    def _start_tts(self) -> None:
        from tts import Speaker

        with self._tts_lock:
            if self.speaker:
                return
            self.status("tts", "読み上げのモデルを読み込み中…")
            try:
                self._make_room_for_tts()
                speaker = Speaker(self.settings, lambda msg: self.status("tts", msg, clear_after=15))
                info = speaker.start()
            except Exception as e:
                log.exception("読み上げを開始できませんでした")
                self.status("tts", f"読み上げを開始できませんでした: {e}", clear_after=30)
                return
            log.info("読み上げを開始 (%s): %s", self.settings["tts_model"], info)
            if not self.settings["tts"]:  # 読み込み中にオフにされた
                speaker.stop()
                self.status("tts", "")
                return
            self.speaker = speaker
            self.status("tts", "")
            self.status("notice", "読み上げ: オン", clear_after=4)
            import vram

            free = vram.gpu_free_mib()
            log.info("読み上げを載せた後の VRAM の空き: %d MiB", free)
            if free < 300:
                self.status("tts", f"VRAM の空きが {free} MiB しかなく、読み上げと翻訳が遅くなります。"
                                   "start.cmd から起動し直してください", clear_after=30)
            if self.settings["asr"] != "whisper" and self.settings["tts_voice"] != "native":
                self.status("notice", "読み上げ: オン (声の見本がないので、Whisper 方式に切り替えるまで読みません)", clear_after=8)

    def _stop_tts(self) -> None:
        with self._tts_lock:
            if self.speaker:
                self.speaker.stop()
                self.speaker = None
            self.status("tts", "")
            self.status("notice", "読み上げ: オフ", clear_after=4)

    def _make_room_for_tts(self) -> None:
        """読み上げのモデルの分の VRAM が空いていなければ、Strata を予約量を増やして起動し直す"""
        import vram

        if self.settings["asr"] == "whisper":
            self.load_transcriber()  # Whisper を読み込み終えてから空きを見る (読み込み中だと空きが揺れる)
        need = vram.tts_mib(self.settings) + vram.MARGIN_MIB
        # 前に動いていた読み上げのプロセスが VRAM を返し終えていないことがあるので、少し待って見直す
        for _ in range(5):
            free = vram.gpu_free_mib()
            if free >= need:
                return
            time.sleep(1)
        if not self.translator.healthy():
            return
        args = vram.strata_args(self.settings, loaded=True)
        log.info("VRAM の空き %d MiB < 読み上げに要る %d MiB なので Strata を起動し直します: %s", free, need, " ".join(args))
        self.status("tts", "読み上げの分の VRAM を空けるため、Strata を起動し直しています (1 分ほど)…")
        r = subprocess.run(["cmd", "/c", STRATA_CMD, "launch", "--preset", self.settings["strata_preset"], *args,
                            "--restart", "--yes", "--no-tui"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=900)
        if r.returncode:
            log.warning("Strata を起動し直せませんでした: %s", r.stdout.decode("utf-8", "replace")[-2000:])
        # Strata が空きを見てキャッシュの大きさを決め終える前に読み上げのモデルを載せると、その分まで
        # 空きとして数えられて VRAM が溢れる (溢れた分はメインメモリに回されて、読み上げも翻訳も遅くなる)
        deadline = time.perf_counter() + 300
        while not self.translator.healthy() and time.perf_counter() < deadline:
            time.sleep(2)
        self.status("tts", "読み上げのモデルを読み込み中…")

    def _recover_volume(self) -> None:
        try:
            from tts import Ducker

            Ducker.recover()
        except Exception:
            log.warning("前回下げた音量を戻せませんでした", exc_info=True)

    def whisper_prompt(self) -> str | None:
        return self.profile.whisper_prompt() if self.profile else None

    def submit(self, unit: Unit) -> None:
        self.units.put(unit)

    # --- トレイ・ホットキーから (どのスレッドからでもよい) ---

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.source:
            (self.source.paused.set if self.paused else self.source.paused.clear)()
        self.status("pause", f"一時停止中 ({self.settings['hotkeys']['pause']} で再開)" if self.paused else "")
        log.info("一時停止" if self.paused else "再開")
        self.tray.refresh()

    def toggle_visible(self) -> None:
        self.overlay.call(lambda: (self.overlay.set_visible(not self.settings["visible"]), self.tray.refresh()))

    def toggle_click_through(self) -> None:
        self.overlay.call(lambda: (self.overlay.set_click_through(not self.settings["click_through"]), self.tray.refresh()))

    def toggle_source(self) -> None:
        self.overlay.call(lambda: (self.overlay.set_show_source(not self.settings["show_source"]), self.tray.refresh()))

    def set_capture_device(self, target: str) -> None:
        self.settings["capture_device"] = target
        self.settings.save()
        self.tray.refresh()
        if self.settings["asr"] == "whisper":
            threading.Thread(target=self._start_source, daemon=True, name="switch").start()

    def toggle_tts(self) -> None:
        self.settings["tts"] = not self.settings["tts"]
        self.settings.save()
        self.tray.refresh()
        threading.Thread(target=self._start_tts if self.settings["tts"] else self._stop_tts, daemon=True).start()

    def set_tts_voice(self, voice: str, native: str | None = None) -> None:
        self.settings["tts_voice"] = voice
        if native:
            self.settings["tts_native"] = native
        self.settings.save()
        self.tray.refresh()

    def toggle_duck(self) -> None:
        self.settings["tts_duck"] = not self.settings["tts_duck"]
        self.settings.save()
        self.tray.refresh()

    def set_tts_output(self, target: str) -> None:
        self.settings["tts_output"] = target
        self.settings.save()
        self.tray.refresh()

    def tts_playing(self) -> bool:
        return bool(self.speaker and self.speaker.playing)

    def output_device_names(self) -> list[str]:
        try:
            return list(dict.fromkeys(name for _, name in output_devices()))
        except Exception:
            log.warning("出力デバイスの一覧を取得できませんでした", exc_info=True)
            return []

    def profile_names(self) -> list[str]:
        return list_profiles(PROFILE_DIR)

    def set_profile(self, name: str | None) -> None:
        self.settings["profile"] = name
        self.settings.save()
        self.profile = self._load_profile(name)
        self.translator.set_profile(self.profile)
        if self.source:
            self.source.set_prompt(self.whisper_prompt())
        self.status("notice", f"プロファイル: {name or 'なし'}", clear_after=4)
        self.tray.refresh()

    def open_folder(self, name: str) -> None:
        (ROOT / name).mkdir(exist_ok=True)
        os.startfile(ROOT / name)

    def status(self, key: str, text: str, clear_after: float = 0) -> None:
        """字幕ウィンドウの上の行に出す状態・エラー"""
        self._status[key] = text
        self.overlay.set_status("\n".join(self._status[k] for k in STATUS_ORDER if self._status.get(k)))
        if clear_after:
            threading.Timer(clear_after, lambda: self._status.get(key) == text and self.status(key, "")).start()

    def _load_profile(self, name: str | None) -> Profile | None:
        if not name:
            return None
        try:
            p = Profile.load(PROFILE_DIR / f"{name}.json")
        except (OSError, ValueError) as e:
            log.error("プロファイル %s を読めませんでした: %s", name, e)
            self.settings["profile"] = None
            return None
        log.info("プロファイル %s: 固有名詞 %d 個、用語集 %d 語", name, len(p.proper_nouns), len(p.glossary))
        return p

    # --- 翻訳 ---

    def _translate_loop(self) -> None:
        with open(self.tsv_path, "a", newline="", encoding="utf-8") as f:
            tsv = csv.writer(f, delimiter="\t")
            tsv.writerow(["time", "asr", "audio_sec", "asr_sec", "parts", "llm_ttft", "llm_sec",
                          "src_delay", "delay", "backlog", "src", "ja"])
            strata_down = False
            while True:
                unit = self.units.get()
                if unit is None:
                    return
                # 訳すのが追いつかずに溜まったら、まとめて 1 回で訳す
                while not self.units.empty() and len(unit.text) < MAX_BATCH_CHARS:
                    nxt = self.units.get_nowait()
                    if nxt is None:
                        return
                    unit = Unit(f"{unit.text} {nxt.text}", nxt.ended_at, unit.audio_sec + nxt.audio_sec,
                                unit.asr_sec + nxt.asr_sec, unit.parts + nxt.parts, ref=pick_ref(unit.ref, nxt.ref))
                if self.paused:
                    continue
                if self.filter.seen(unit.text, time.perf_counter()):
                    log.info("訳済みの文なので飛ばします: %s", unit.text)
                    continue

                shown = time.perf_counter()
                self.overlay.show(unit.text, "…", new=True)
                t0 = time.perf_counter()
                while True:
                    speech = SpeechStream(self.speaker, unit) if self.speaker else None

                    def on_partial(s, u=unit, speech=speech):
                        self.overlay.show(u.text, s)
                        if speech:
                            speech.update(s)

                    try:
                        ja, ttft = self.translator.translate(unit.text, on_partial)
                        break
                    except StrataUnavailable as e:
                        if speech:
                            speech.finish(None)
                        if not strata_down:
                            log.warning("Strata に接続できません: %s", e)
                            strata_down = True
                        self.status("strata", "翻訳サーバー (Strata) に接続できません。復旧を待っています…")
                        self.overlay.show(unit.text, "")
                        unit = self._wait_for_strata(unit)
                        if unit is None:
                            break
                        shown = t0 = time.perf_counter()
                    except Exception as e:
                        log.exception("翻訳に失敗しました")
                        ja, ttft = f"[翻訳エラー] {e}", 0.0
                        break
                if speech:
                    speech.finish(ja if unit and not ja.startswith("[翻訳エラー]") else None)
                if unit is None:
                    continue
                if strata_down:
                    strata_down = False
                    self.status("strata", "")
                    log.info("Strata が復旧しました")

                done = time.perf_counter()
                self.overlay.show(unit.text, ja)
                backlog = self.units.qsize()
                mode = self.source.name if self.source else "-"
                log.info(
                    "[%s] 音声%4.1fs ASR%5.2fs 翻訳%5.2fs(初字%4.2fs) 遅れ%5.2fs 待ち%d | %s\n%14s→ %s",
                    mode, unit.audio_sec, unit.asr_sec, done - t0, ttft, done - unit.ended_at, backlog, unit.text, "", ja,
                )
                tsv.writerow([f"{datetime.now():%H:%M:%S}", mode, f"{unit.audio_sec:.1f}", f"{unit.asr_sec:.2f}",
                              unit.parts, f"{ttft:.2f}", f"{done - t0:.2f}", f"{shown - unit.ended_at:.2f}",
                              f"{done - unit.ended_at:.2f}", backlog, unit.text, ja])
                f.flush()

    def _wait_for_strata(self, unit: Unit) -> Unit | None:
        """Strata が応答するまで待つ。その間に来た文は原文だけ出して、最新の 1 つだけ残す。"""
        while not self.translator.healthy():
            try:
                nxt = self.units.get(timeout=2)
            except queue.Empty:
                continue
            if nxt is None:
                self.units.put(None)
                return None
            unit = nxt
            self.overlay.show(unit.text, "", new=True)
        return unit if time.perf_counter() - unit.ended_at < STALE_SEC else None

    def _vram_monitor(self) -> None:
        """VRAM の空きを 30 秒ごとにログに残し、残りわずかなら字幕ウィンドウに出す"""
        try:
            import pynvml

            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            log.warning("VRAM の使用量を読めません", exc_info=True)
            return
        while True:
            m = pynvml.nvmlDeviceGetMemoryInfo(h)
            free = m.free // 2**20
            log.debug("VRAM: 使用 %d / %d MiB (空き %d MiB)", m.used // 2**20, m.total // 2**20, free)
            self.status("vram", f"VRAM の空きが残りわずかです ({free} MiB)" if free < 256 else "")
            time.sleep(30)


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    handlers = [logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")]
    if sys.stderr:  # pythonw では None
        sys.stderr.reconfigure(errors="replace")
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "faster_whisper", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    p = argparse.ArgumentParser(description="Windows で流れている英語の音声をリアルタイムで日本語字幕にする")
    p.add_argument("--asr", choices=["whisper", "livecaptions"], help="音声認識の方式 (設定に保存される)")
    p.add_argument("--file", help="動作確認用: ループバックの代わりにローカルの音声/動画ファイルを実時間で流す")
    p.add_argument("--debug", action="store_true", help="文字起こしの結果などもログに出す")
    args = p.parse_args()

    setup_logging()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    settings = Settings()
    if args.asr:
        settings["asr"] = args.asr
    log.info("起動 (音声認識: %s, プロファイル: %s)", settings["asr"], settings["profile"])
    App(args, settings).run()


if __name__ == "__main__":
    main()
