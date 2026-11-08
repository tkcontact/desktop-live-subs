"""Strata (OpenAI 互換 API) で日本語に訳す"""

import json
import logging
import re
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import openai
from openai import OpenAI

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You translate English audio playing on the user's PC (videos, games, voice chat) into Japanese subtitles.
Each user message is the next piece of speech, transcribed by speech recognition. \
It may contain recognition errors or be cut off mid-sentence; infer what was most likely said.
Reply with only the Japanese translation of the latest message: natural, casual spoken Japanese \
that follows the flow of the conversation and matches the speaker's tone, short enough to read as a subtitle.
Earlier messages are context only; never repeat or re-translate them.
If the message stops mid-sentence, translate only what is there and end it with "…".
Never leave English in the output except for names. No notes, quotes, romaji, or explanations."""

# 一度に訳す長さの上限 (溜まった文をまとめて訳すとき)
MAX_BATCH_CHARS = 600


@dataclass
class Profile:
    """profiles/<名前>.json: 説明文・Whisper 用の固有名詞・用語集 (英→日)"""

    name: str
    description: str = ""
    proper_nouns: list[str] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Profile":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path.stem,
            data.get("description", ""),
            list(data.get("proper_nouns", [])),
            dict(data.get("glossary", {})),
        )

    def whisper_prompt(self) -> str | None:
        return ", ".join(self.proper_nouns) + "." if self.proper_nouns else None


def list_profiles(directory: Path) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.json"))


class StrataUnavailable(Exception):
    """Strata に繋がらない・応答しない"""


def build_system_prompt(profile: Profile | None) -> str:
    prompt = SYSTEM_PROMPT
    if profile and profile.description:
        prompt += f"\n\nAbout what is playing:\n{profile.description}"
    if profile and profile.glossary:
        terms = "\n".join(f"- {en} → {ja}" for en, ja in profile.glossary.items())
        prompt += f"\n\nGlossary (always translate these terms this way):\n{terms}"
    return prompt


class Translator:
    def __init__(self, base_url: str, context: int = 4, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.client = OpenAI(base_url=self.base_url, api_key="strata", timeout=timeout, max_retries=0)
        self.model: str | None = None
        self.system_prompt = build_system_prompt(None)
        # 直前の (原文, 訳文) を会話履歴として渡し、話の流れと訳語の一貫性を保つ
        self.history: deque[tuple[str, str]] = deque(maxlen=context)

    def set_profile(self, profile: Profile | None) -> None:
        self.system_prompt = build_system_prompt(profile)
        self.history.clear()

    def healthy(self) -> bool:
        """Strata が応答を返せる状態か (/health と /v1/models)"""
        root = re.sub(r"/v1$", "", self.base_url)
        try:
            with urllib.request.urlopen(f"{root}/health", timeout=3) as r:
                if r.status != 200:
                    return False
            models = self.client.models.list(timeout=3).data
        except Exception:
            return False
        if models:
            self.model = models[0].id
        return bool(models)

    def translate(self, text: str, on_partial: Callable[[str], None] | None = None) -> tuple[str, float]:
        """(訳文, 最初の文字が届くまでの秒数) を返す。繋がらないときは StrataUnavailable。"""
        if self.model is None and not self.healthy():
            raise StrataUnavailable("Strata に接続できません")

        messages = [{"role": "system", "content": self.system_prompt}]
        for src, ja in self.history:
            messages += [{"role": "user", "content": src}, {"role": "assistant", "content": ja}]
        messages.append({"role": "user", "content": text})

        t0 = time.perf_counter()
        ttft = 0.0
        out = ""
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=256,
                reasoning_effort="none",  # 思考モードを切る (既定は high で、訳が出るまで数秒かかる)
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    if not out:
                        ttft = time.perf_counter() - t0
                    out += delta
                    if on_partial:
                        on_partial(out.strip())
        except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as e:
            self.model = None  # 復旧後に /v1/models から取り直す
            raise StrataUnavailable(str(e) or type(e).__name__) from e
        except openai.APIStatusError as e:
            if e.status_code in (429, 502, 503, 504):  # 起動中・満杯
                raise StrataUnavailable(f"HTTP {e.status_code}") from e
            raise
        out = out.strip()
        self.history.append((text, out))
        return out, ttft
