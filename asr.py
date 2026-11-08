"""faster-whisper による文字起こし"""

import os
import re
import site

# CTranslate2 は cublas64_12.dll を PATH から探すので、pip で入れた nvidia-cublas-cu12 の DLL を見えるようにする
for _sp in site.getsitepackages():
    _dll_dir = os.path.join(_sp, "nvidia", "cublas", "bin")
    if os.path.isdir(_dll_dir):
        os.environ["PATH"] = _dll_dir + os.pathsep + os.environ["PATH"]

from faster_whisper import WhisperModel  # noqa: E402

# 無音や BGM から Whisper が出しがちな定型文 (記号・空白を除いて小文字で比較)
HALLUCINATIONS = {
    "you",
    "thankyou",
    "thankyouforwatching",
    "thanksforwatching",
    "pleasesubscribe",
    "subtitlesbytheamaraorgcommunity",
}


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


class Transcriber:
    def __init__(self, model: str = "large-v3-turbo", compute_type: str = "int8", beam_size: int = 5):
        self.model = WhisperModel(model, device="cuda", compute_type=compute_type)
        self.beam_size = beam_size
        self.initial_prompt: str | None = None  # プロファイルの固有名詞リスト

    def transcribe(self, audio) -> str:
        """英語として文字起こしする。ハルシネーションとみなしたものは空文字にする。"""
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=self.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=self.initial_prompt,
        )
        text = "".join(s.text for s in segments).strip()
        if _normalize(text) in HALLUCINATIONS:
            return ""
        # 無音に近い音声だと initial_prompt (固有名詞の羅列) をそのまま書き出すことがある
        if self.initial_prompt and len(text.split()) >= 4 and _normalize(text) in _normalize(self.initial_prompt):
            return ""
        return text
