"""画面下の最前面・半透明の字幕ウィンドウ (原文と訳文の 2 段)

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
        self.status = tk.Label(root, fg="#ffb347", bg=BG, font=self.status_font, justify="center")
        self.src = tk.Label(root, fg="#bbbbbb", bg=BG, font=self.src_font, justify="center")
        self.ja = tk.Label(root, fg="#ffffff", bg=BG, font=self.ja_font, justify="center")
        self.ja.pack(side="bottom", fill="both", expand=True, padx=16, pady=(0, 8))
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
        self._latest: tuple[str, str] | None = None
        self._clear_job = None
        # Tk は表示するまで外側のウィンドウを作らない。透過などのスタイルは表示してから外側に当てる
        root.update()
        self._apply_window_state()
        self._poll()
        self._keep_on_top()

    # --- どのスレッドから呼んでもよいもの ---

    def show(self, src: str, ja: str) -> None:
        self._latest = (src, ja)

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
        self.src.pack_forget()
        self.status.pack_forget()
        if self._status_text:
            self.status.pack(side="top", fill="x", padx=16, pady=(6, 0), before=self.ja)
        if self.settings["show_source"]:
            self.src.pack(side="top", fill="x", padx=16, pady=(8, 0), before=self.ja)

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
        if self._latest:
            src, ja = self._latest
            self._latest = None
            self.src.configure(text=src)
            self.ja.configure(text=ja)
            self._fit()
            self._schedule_clear()
        self.root.after(50, self._poll)

    def _schedule_clear(self) -> None:
        if self._clear_job:
            self.root.after_cancel(self._clear_job)
            self._clear_job = None
        sec = self.settings["clear_after_sec"]
        if sec:
            self._clear_job = self.root.after(int(sec * 1000), lambda: self.show("", ""))

    def _keep_on_top(self) -> None:
        """ほかの最前面ウィンドウ (ゲームのランチャーなど) に隠されないよう、ときどき最前面に戻す"""
        if sys.platform == "win32" and self.settings["visible"]:
            _user32.SetWindowPos(self._hwnd(), HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.root.after(2000, self._keep_on_top)

    def _fit_height(self) -> int:
        """原文 2 行 + 訳文 2 行が収まる高さ"""
        h = 2 * self.ja_font.metrics("linespace") + 24
        if self.settings["show_source"]:
            h += 2 * self.src_font.metrics("linespace")
        return h

    def _fit(self) -> None:
        """文字が収まらなければ下端を固定したまま上に伸ばし、収まるようになれば base_h に戻す"""
        self.root.update_idletasks()
        need = self.ja.winfo_reqheight() + 16
        if self.settings["show_source"]:
            need += self.src.winfo_reqheight()
        if self._status_text:
            need += self.status.winfo_reqheight() + 6
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
            for label in (self.status, self.src, self.ja):
                label.configure(wraplength=e.width - 40)
