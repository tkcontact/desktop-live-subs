"""グローバルホットキー (Win32 の RegisterHotKey)。専用スレッドでメッセージループを回す。"""

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


def parse(spec: str) -> tuple[int, int]:
    """"ctrl+alt+t" → (修飾キー, 仮想キーコード)"""
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    mod = 0
    for m in mods:
        mod |= MODS[m]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit():
        vk = 0x70 + int(key[1:]) - 1  # VK_F1 から
    else:
        raise ValueError(f"ホットキーのキーが分かりません: {spec}")
    return mod, vk


class Hotkeys:
    def __init__(self, bindings: dict[str, tuple[str, Callable[[], None]]], on_error: Callable[[str], None]):
        """bindings: {名前: (キーの指定, 押されたときの処理)}"""
        self.bindings = bindings
        self.on_error = on_error
        self._thread_id = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="hotkeys").start()

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        callbacks = {}
        failed = []
        for i, (name, (spec, fn)) in enumerate(self.bindings.items(), start=1):
            try:
                mod, vk = parse(spec)
            except (KeyError, ValueError):
                failed.append(spec)
                continue
            if user32.RegisterHotKey(None, i, mod | MOD_NOREPEAT, vk):
                callbacks[i] = fn
                log.info("ホットキー %s: %s", spec, name)
            else:
                failed.append(spec)  # ほかのアプリが使っている
        if failed:
            self.on_error(f"ホットキーを登録できませんでした: {', '.join(failed)} (ほかのアプリが使用中か、指定が不正)")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in callbacks:
                try:
                    callbacks[msg.wParam]()
                except Exception:
                    log.exception("ホットキーの処理に失敗しました")
        for i in callbacks:
            user32.UnregisterHotKey(None, i)
