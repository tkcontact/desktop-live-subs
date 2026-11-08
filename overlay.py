"""画面下の最前面・半透明の字幕ウィンドウ (原文と訳文の 2 段)

新しい字幕が来ても直前の字幕は消さず、薄い色で上に残す (読み切れないうちに消えないように)。

クリック透過のときはマウス操作がすべて下のウィンドウに抜ける。透過を切ると枠が出て、
ドラッグで移動、右下の ◢ でサイズ変更、右クリックでメニュー。位置・大きさ・文字サイズは設定に保存する。
"""

import ctypes
import queue
import sys
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from ctypes import wintypes

from settings import Settings

BG = "#101010"
EDIT_BORDER = "#4a90d9"
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_LAYERED = 0x20, 0x80000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32")
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND


class Overlay:
    def __init__(self, settings: Settings, on_menu: dict[str, Callable[[], None]]):
        if sys.platform == "win32":
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高 DPI 環境で文字がぼやけないように
            except (AttributeError, OSError):
                pass

        self.settings = settings
        self.root = root = tk.Tk()
        root.title("Desktop Live Subs")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", settings["alpha"])
        root.configure(bg=BG, highlightthickness=0, highlightbackground=EDIT_BORDER, highlightcolor=EDIT_BORDER)

        size = settings["font_size"]
        self.status_font = tkfont.Font(family="Yu Gothic UI", size=max(9, round(size * 0.45)))
        self.src_font = tkfont.Font(family="Segoe UI", size=round(size * 0.6))
        self.ja_font = tkfont.Font(family="Yu Gothic UI", size=size, weight="bold")
        self.old_ja_font = tkfont.Font(family="Yu Gothic UI", size=round(size * 0.8))
        self.status = tk.Label(root, fg="#ffb347", bg=BG, font=self.status_font, justify="center")
        self.src = tk.Label(root, fg="#bbbbbb", bg=BG, font=self.src_font, justify="center")
        self.ja = tk.Label(root, fg="#ffffff", bg=BG, font=self.ja_font, justify="center")
        self.ja.pack(side="bottom", fill="both", expand=True, padx=16, pady=(0, 8))
        # 直前の字幕 (古い順)。(原文のラベル, 訳文のラベル)
        self.history = [
            (tk.Label(root, fg="#707070", bg=BG, font=self.src_font, justify="center"),
             tk.Label(root, fg="#9a9a9a", bg=BG, font=self.old_ja_font, justify="center"))
            for _ in range(settings["history"])
        ]
        self._current = ("", "")
        self._past: list[tuple[str, str]] = []
        self.grip = tk.Label(root, text="◢", fg="#888888", bg=BG, cursor="size_nw_se")
        self._status_text = ""
        self._apply_layout()

        self.base_h = self._fit_height()  # 文字が収まらないときだけこれより高くする
        self._restore_geometry()

        # 子ウィジェット上のクリックも root のバインドに届く。グリップだけは "break" で止めてリサイズに使う
        root.bind("<ButtonPress-1>", self._start_move)
        root.bind("<B1-Motion>", self._move)
        root.bind("<ButtonRelease-1>", lambda e: self._save_geometry())
        self.grip.bind("<ButtonPress-1>", self._start_resize)
        self.grip.bind("<B1-Motion>", self._resize)
        self.grip.bind("<ButtonRelease-1>", lambda e: (self._save_geometry(), "break")[1])
        root.bind("<Configure>", self._on_configure)

        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="文字を大きく", command=lambda: self.zoom(+2))
        menu.add_command(label="文字を小さく", command=lambda: self.zoom(-2))
        menu.add_command(label="原文の表示を切り替え", command=lambda: self.set_show_source(not settings["show_source"]))
        menu.add_command(label="クリック透過にする", command=lambda: self.set_click_through(True))
        menu.add_separator()
        for label, fn in on_menu.items():
            menu.add_command(label=label, command=fn)
        root.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        self._calls: queue.Queue[Callable[[], None]] = queue.Queue()
        self._events: queue.Queue[tuple[bool, str, str]] = queue.Queue()
        self._clear_job = None
        # Tk は表示するまで外側のウィンドウを作らない。透過などのスタイルは表示してから外側に当てる
        root.update()
        self._apply_window_state()
        self._poll()
        self._keep_on_top()

    # --- どのスレッドから呼んでもよいもの ---

    def show(self, src: str, ja: str, new: bool = False) -> None:
        """new=True なら新しい字幕として出し、今の字幕は上の履歴に送る。False なら今の字幕を書き換える。"""
        self._events.put((new, src, ja))

    def clear(self) -> None:
        self.call(self._clear)

    def set_status(self, text: str) -> None:
        self.call(lambda: self._set_status(text))

    def call(self, fn: Callable[[], None]) -> None:
        """fn を Tk のスレッドで実行する"""
        self._calls.put(fn)

    # --- Tk のスレッドで呼ぶもの ---

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        self._save_geometry()
        self.root.quit()

    def set_click_through(self, on: bool) -> None:
        self.settings["click_through"] = on
        self.settings.save()
        self._apply_window_state()

    def set_visible(self, on: bool) -> None:
        self.settings["visible"] = on
        self.settings.save()
        self._apply_window_state()

    def set_show_source(self, on: bool) -> None:
        self.settings["show_source"] = on
        self.settings.save()
        self._apply_layout()
        self.base_h = self._fit_height()
        self._fit()

    def zoom(self, step: int) -> None:
        size = max(10, self.ja_font.cget("size") + step)
        self.settings["font_size"] = size
        self.ja_font.configure(size=size)
        self.old_ja_font.configure(size=round(size * 0.8))
        self.src_font.configure(size=round(size * 0.6))
        self.status_font.configure(size=max(9, round(size * 0.45)))
        self.base_h = self._fit_height()
        self._fit()
        self._save_geometry()

    # --- 内部 ---

    def _hwnd(self) -> int:
        # 枠なしのウィンドウでは wm frame が内側の子ウィンドウを返すので、トップレベルを辿る
        return _user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT

    def _apply_window_state(self) -> None:
        s = self.settings
        if not s["visible"]:
            self.root.withdraw()
            return
        self.root.deiconify()
        if sys.platform == "win32":
            h = self._hwnd()
            style = _user32.GetWindowLongW(h, GWL_EXSTYLE) | WS_EX_LAYERED
            style = style | WS_EX_TRANSPARENT if s["click_through"] else style & ~WS_EX_TRANSPARENT
            _user32.SetWindowLongW(h, GWL_EXSTYLE, style)
        # 透過を切っているときは枠とリサイズ用のグリップを出して、動かせることが分かるように
        if s["click_through"]:
            self.root.configure(highlightthickness=0)
            self.grip.place_forget()
        else:
            self.root.configure(highlightthickness=2)
            self.grip.place(relx=1.0, rely=1.0, anchor="se")
            self.grip.lift()

    def _apply_layout(self) -> None:
        """上から 状態 → 直前の字幕 (原文・訳文) → 今の原文 → 今の訳文 の順に並べる。空のものは出さない。"""
        for w in (self.status, self.src, *(w for pair in self.history for w in pair)):
            w.pack_forget()
        top = dict(side="top", fill="x", padx=16, before=self.ja)
        if self._status_text:
            self.status.pack(pady=(6, 0), **top)
        for (src_label, ja_label), (src, ja) in zip(self.history[len(self.history) - len(self._past):], self._past):
            if self.settings["show_source"] and src:
                src_label.pack(pady=(6, 0), **top)
            if ja:
                ja_label.pack(pady=(0, 0), **top)
        if self.settings["show_source"]:
            self.src.pack(pady=(8, 0), **top)

    def _set_status(self, text: str) -> None:
        if text == self._status_text:
            return
        self._status_text = text
        self.status.configure(text=text)
        self._apply_layout()
        self._fit()

    def _poll(self) -> None:
        while not self._calls.empty():
            try:
                self._calls.get_nowait()()
            except Exception:
                import logging

                logging.getLogger(__name__).exception("字幕ウィンドウの更新に失敗しました")
        changed = False
        while not self._events.empty():
            new, src, ja = self._events.get_nowait()
            if new and any(self._current):
                self._past = (self._past + [self._current])[-len(self.history):] if self.history else []
            self._current = (src, ja)
            changed = True
        if changed:
            self._render()
            self._schedule_clear()
        self.root.after(50, self._poll)

    def _render(self) -> None:
        src, ja = self._current
        self.src.configure(text=src)
        self.ja.configure(text=ja)
        for (src_label, ja_label), (s, j) in zip(self.history[len(self.history) - len(self._past):], self._past):
            src_label.configure(text=s)
            ja_label.configure(text=j)
        self._apply_layout()
        self._fit()

    def _clear(self) -> None:
        self._current = ("", "")
        self._past = []
        self._render()

    def _schedule_clear(self) -> None:
        if self._clear_job:
            self.root.after_cancel(self._clear_job)
            self._clear_job = None
        sec = self.settings["clear_after_sec"]
        if sec:
            self._clear_job = self.root.after(int(sec * 1000), self._clear)

    def _keep_on_top(self) -> None:
        """ほかの最前面ウィンドウ (ゲームのランチャーなど) に隠されないよう、ときどき最前面に戻す"""
        if sys.platform == "win32" and self.settings["visible"]:
            _user32.SetWindowPos(self._hwnd(), HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.root.after(2000, self._keep_on_top)

    def _fit_height(self) -> int:
        """原文 2 行 + 訳文 2 行 + 直前の字幕が収まる高さ"""
        h = 2 * self.ja_font.metrics("linespace") + 24
        if self.settings["show_source"]:
            h += 2 * self.src_font.metrics("linespace")
        # 直前の字幕は 1 行ずつ見込む (長ければ _fit で伸ばす)
        per_entry = self.old_ja_font.metrics("linespace") + (self.src_font.metrics("linespace") if self.settings["show_source"] else 0)
        return h + len(self.history) * per_entry

    def _fit(self) -> None:
        """文字が収まらなければ下端を固定したまま上に伸ばし、収まるようになれば base_h に戻す"""
        self.root.update_idletasks()
        need = sum(w.winfo_reqheight() + 8 for w in self.root.pack_slaves()) + 8
        h = max(self.base_h, need)
        cur_h = self.root.winfo_height()
        if h != cur_h:
            bottom = self.root.winfo_y() + cur_h
            self.root.geometry(f"{self.root.winfo_width()}x{h}+{self.root.winfo_x()}+{bottom - h}")

    def _restore_geometry(self) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = int(sw * 0.6), self.base_h
        x, bottom = (sw - w) // 2, sh - sh // 10
        g = self.settings["geometry"]
        if g and self._on_screen(*g):
            x, bottom, w, h = g
            self.base_h = h
        self.root.geometry(f"{w}x{h}+{x}+{bottom - h}")

    def _on_screen(self, x: int, bottom: int, w: int, h: int) -> bool:
        """保存した位置が今の画面 (複数モニターを含む) の中にあるか"""
        if sys.platform != "win32":
            return True
        m = ctypes.windll.user32.GetSystemMetrics
        vx, vy, vw, vh = m(76), m(77), m(78), m(79)  # SM_XVIRTUALSCREEN など
        return vx <= x + w // 2 <= vx + vw and vy <= bottom - h // 2 <= vy + vh

    def _save_geometry(self) -> None:
        r = self.root
        self.settings["geometry"] = [r.winfo_x(), r.winfo_y() + r.winfo_height(), r.winfo_width(), self.base_h]
        self.settings.save()

    def _start_move(self, e: tk.Event) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _move(self, e: tk.Event) -> None:
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _start_resize(self, e: tk.Event) -> str:
        self._resize_from = (e.x_root, e.y_root, self.root.winfo_width(), self.root.winfo_height())
        return "break"

    def _resize(self, e: tk.Event) -> str:
        x0, y0, w0, h0 = self._resize_from
        self.base_h = max(60, h0 + e.y_root - y0)
        self.root.geometry(f"{max(300, w0 + e.x_root - x0)}x{self.base_h}")
        return "break"

    def _on_configure(self, e: tk.Event) -> None:
        if e.widget is self.root:
            for label in (self.status, self.src, self.ja, *(w for pair in self.history for w in pair)):
                label.configure(wraplength=e.width - 40)
