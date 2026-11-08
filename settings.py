"""設定 (settings.json)。字幕ウィンドウの位置・大きさや、トレイで切り替えたものを次回も使う。"""

import json
import logging
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "settings.json"
log = logging.getLogger(__name__)

DEFAULTS = {
    "asr": "whisper",  # whisper | livecaptions
    # Whisper 方式で録る出力デバイス: "all" (すべてを足し合わせる) | "default" (既定の出力デバイス) | デバイス名
    "capture_device": "all",
    "profile": None,  # profiles/<名前>.json の名前。None はプロファイルなし
    "show_source": True,
    "click_through": True,
    "visible": True,
    "font_size": 24,
    "alpha": 0.8,
    "geometry": None,  # [x, 下端の y, 幅, 高さ]。None なら画面下の中央
    "history": 1,  # 今の字幕の上に薄く残す直前の字幕の数 (0 で残さない)
    "clear_after_sec": 12,  # 新しい字幕がこの秒数来なければ消す (0 で消さない)
    "hotkeys": {"click_through": "ctrl+alt+t", "visible": "ctrl+alt+h", "pause": "ctrl+alt+p"},
    # Windows の localhost は IPv6 を先に試して 2 秒ほど待たされるので IPv4 を直接指定する
    "strata_url": "http://127.0.0.1:8080/v1",
    "strata_preset": "live-subs",
    "stop_strata_on_exit": True,
    "strata_extra_reserve_mib": 0,  # GPU を使うゲームと併用するときなどに Strata の VRAM 予約へ足す量
    "context": 4,  # 文脈として渡す直前の文の数
    "whisper_model": "large-v3-turbo",
    "compute_type": "int8",
    "beam_size": 5,
    "min_silence_ms": 500,
    "max_chunk_sec": 8.0,
}


class Settings(dict):
    def __init__(self, path: Path = PATH):
        super().__init__(DEFAULTS)
        self.path = path
        self._lock = threading.Lock()
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.update({k: v for k, v in saved.items() if k in DEFAULTS})
                self["hotkeys"] = {**DEFAULTS["hotkeys"], **saved.get("hotkeys", {})}
            except (OSError, ValueError):
                log.exception("%s を読めなかったので既定の設定を使います", path)

    def save(self) -> None:
        with self._lock:
            try:
                self.path.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                log.exception("設定を保存できませんでした")
