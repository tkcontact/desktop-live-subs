"""タスクトレイのアイコンとメニュー (pystray)"""

import pystray
from PIL import Image, ImageDraw, ImageFont

ASR_LABELS = {"whisper": "Whisper (GPU)", "livecaptions": "Windows ライブキャプション"}


def _icon_image(active: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 10, 62, 54), radius=10, fill=(40, 120, 220) if active else (110, 110, 110))
    try:
        font = ImageFont.truetype("YuGothB.ttc", 30)
    except OSError:
        font = ImageFont.load_default()
    d.text((32, 32), "字", fill="white", font=font, anchor="mm")
    return img


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = pystray.Icon("desktop-live-subs", _icon_image(True), "Desktop Live Subs", self._menu())

    def start(self) -> None:
        self.icon.run_detached()

    def stop(self) -> None:
        self.icon.stop()

    def refresh(self) -> None:
        """状態が変わったらメニューのチェックとアイコンを更新する"""
        self.icon.icon = _icon_image(not self.app.paused)
        self.icon.title = f"Desktop Live Subs - {ASR_LABELS[self.app.settings['asr']]}" + (" (一時停止中)" if self.app.paused else "")
        self.icon.menu = self._menu()
        self.icon.update_menu()

    def _menu(self) -> pystray.Menu:
        app, s = self.app, self.app.settings
        Item, Menu = pystray.MenuItem, pystray.Menu

        def asr_item(mode):
            return Item(ASR_LABELS[mode], lambda: app.switch_asr(mode), checked=lambda _: s["asr"] == mode, radio=True)

        def profile_item(name):
            return Item(name or "なし", lambda: app.set_profile(name), checked=lambda _: s["profile"] == name, radio=True)

        profiles = [profile_item(None), *(profile_item(n) for n in app.profile_names())]
        return Menu(
            Item("一時停止", lambda: app.toggle_pause(), checked=lambda _: app.paused, default=True),
            Item("字幕を表示", lambda: app.toggle_visible(), checked=lambda _: s["visible"]),
            Item("クリック透過", lambda: app.toggle_click_through(), checked=lambda _: s["click_through"]),
            Item("原文を表示", lambda: app.toggle_source(), checked=lambda _: s["show_source"]),
            Menu.SEPARATOR,
            Item("音声認識", Menu(asr_item("whisper"), asr_item("livecaptions"))),
            Item("プロファイル", Menu(*profiles, Menu.SEPARATOR,
                                     Item("一覧を読み直す", lambda: self.refresh()),
                                     Item("profiles フォルダを開く", lambda: app.open_folder("profiles")))),
            Menu.SEPARATOR,
            Item("ログのフォルダを開く", lambda: app.open_folder("logs")),
            Item("終了", lambda: app.quit()),
        )
