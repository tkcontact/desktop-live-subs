"""Windows のライブキャプションの字幕テキストを UI Automation で読み、確定した文を取り出す

LiveCaptions-Translator (github.com/SakiRinn/LiveCaptions-Translator) と同じく、
LiveCaptionsDesktopWindow の CaptionsTextBlock 要素の Name を読む。
"""

import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes

from sentences import CaptionTracker, FragmentMerger, Unit

log = logging.getLogger(__name__)

EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "LiveCaptions.exe")
WINDOW_CLASS = "LiveCaptionsDesktopWindow"
TEXT_ID = "CaptionsTextBlock"

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
SW_MINIMIZE, SW_RESTORE = 6, 9
WM_CLOSE = 0x10

_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
if _user32:
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


class LiveCaptionsSource:
    """ライブキャプションを起動 (または既に開いているものに接続) して、確定した文を on_unit に渡す。

    ライブキャプションのウィンドウは最小化してタスクバーから隠す。止めるときは、こちらで起動したなら閉じ、
    もともと開いていたなら元に戻す。
    """

    name = "livecaptions"

    def __init__(self, on_unit, on_status, poll_sec: float = 0.1):
        self.on_unit = on_unit
        self.on_status = on_status
        self.poll_sec = poll_sec
        self.tracker = CaptionTracker()
        self.merger = FragmentMerger()
        self.paused = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._hwnd = None
        self._launched = False

    @property
    def speaking(self) -> bool:
        return time.perf_counter() - self.tracker.changed_at < self.tracker.idle_sec

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="livecaptions")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._release_window()

    def set_prompt(self, prompt: str | None) -> None:
        pass  # ライブキャプションには固有名詞を渡す手段がない

    def _run(self) -> None:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            text_ctrl = None
            while not self._stop.is_set():
                if text_ctrl is None:
                    text_ctrl = self._attach(auto)
                    if text_ctrl is None:
                        self._stop.wait(2)
                        continue
                try:
                    text = text_ctrl.Name or ""
                except Exception:
                    log.info("ライブキャプションのウィンドウが見つからなくなりました。開き直します")
                    text_ctrl = None
                    self._hwnd = None
                    continue

                now = time.perf_counter()
                if self.paused.is_set():
                    self.tracker.reset()
                    self.merger.flush()
                else:
                    got = self.tracker.update(text, now)
                    units = self.merger.add(Unit(got, now), now) if got else []
                    units += self.merger.flush_due(now, self.speaking)
                    for u in units:
                        self.on_unit(u)
                self._stop.wait(self.poll_sec)

    def _attach(self, auto):
        window = auto.WindowControl(searchDepth=1, ClassName=WINDOW_CLASS)
        if not window.Exists(0, 0):
            self.on_status("ライブキャプションを起動しています…")
            subprocess.Popen([EXE])
            self._launched = True
            if not window.Exists(10, 0.2):
                self.on_status("ライブキャプションを起動できませんでした")
                return None
        text_ctrl = window.Control(searchDepth=12, AutomationId=TEXT_ID)
        if not text_ctrl.Exists(5, 0.2):
            # 初回の同意や音声パックのダウンロードが済んでいないと字幕の欄がない
            self.on_status("ライブキャプションの字幕欄が見つかりません。ライブキャプションの初期設定 (英語の音声パック) を済ませてください")
            return None
        self._hwnd = window.NativeWindowHandle
        self._hide_window()
        self.tracker.reset()
        self.on_status("")
        log.info("ライブキャプションに接続しました (hwnd=%s, こちらで起動=%s)", self._hwnd, self._launched)
        return text_ctrl

    def _hide_window(self) -> None:
        h = self._hwnd
        if not h:
            return
        _user32.ShowWindow(h, SW_MINIMIZE)
        _user32.SetWindowLongW(h, GWL_EXSTYLE, _user32.GetWindowLongW(h, GWL_EXSTYLE) | WS_EX_TOOLWINDOW)

    def _release_window(self) -> None:
        h = self._hwnd
        if not h:
            return
        if self._launched:
            _user32.PostMessageW(h, WM_CLOSE, 0, 0)
        else:
            _user32.SetWindowLongW(h, GWL_EXSTYLE, _user32.GetWindowLongW(h, GWL_EXSTYLE) & ~WS_EX_TOOLWINDOW)
            _user32.ShowWindow(h, SW_RESTORE)
        self._hwnd = None
