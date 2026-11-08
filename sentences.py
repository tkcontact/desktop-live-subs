"""翻訳に回す単位を決める: 短い断片をまとめる、ライブキャプションの確定部分を取り出す、同じ文を二度訳さない"""

import re
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

# 文末: . ! ? … の後に閉じ括弧・引用符が続いてもよい
_EOS = re.compile(r"[.!?…][\"')\]]*$")
_SPLIT = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")
_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "etc.", "jr.", "sr.", "e.g.", "i.e."}


def is_eos(word: str) -> bool:
    return bool(_EOS.search(word)) and word.lower() not in _ABBREV


def word_count(text: str) -> int:
    return len(text.split())


def normalize(text: str) -> str:
    """比較用: 小文字にして記号を落とす"""
    return " ".join(re.sub(r"[^\w' ]+", " ", text.lower()).split())


@dataclass
class Unit:
    """翻訳に回す 1 まとまり"""

    text: str
    ended_at: float  # 最後の発話が終わった (ライブキャプションでは文が確定した) 時刻。perf_counter
    audio_sec: float = 0.0
    asr_sec: float = 0.0
    parts: int = 1  # まとめた断片の数


class FragmentMerger:
    """短すぎる断片を次とまとめ、文として自然な単位で出す。

    - 文末で終わっていて min_words 語以上ならそのまま出す。
    - 文の途中で終わっていて末尾の未完の部分が短ければ、そこだけ次の断片を待つ。
    - 待っている断片は、発話が止んで hold_sec 経つか、max_hold_sec 経てば出す。
    """

    def __init__(self, min_words: int = 4, tail_words: int = 6, hold_sec: float = 1.2, max_hold_sec: float = 4.0):
        self.min_words = min_words
        self.tail_words = tail_words
        self.hold_sec = hold_sec
        self.max_hold_sec = max_hold_sec
        self.pending: Unit | None = None
        self.held_at = 0.0

    def add(self, unit: Unit, now: float) -> list[Unit]:
        if self.pending:
            p = self.pending
            unit = Unit(
                f"{p.text} {unit.text}",
                unit.ended_at,
                p.audio_sec + unit.audio_sec,
                p.asr_sec + unit.asr_sec,
                p.parts + unit.parts,
            )
            self.pending = None

        text = unit.text.strip()
        if _EOS.search(text):
            if word_count(text) >= self.min_words:
                return [unit]
            return self._hold(unit, now)

        sentences = _SPLIT.split(text)
        tail = sentences[-1]
        if word_count(tail) >= self.tail_words:
            return [unit]
        head = text[: len(text) - len(tail)].strip()
        if head and word_count(head) >= self.min_words:
            ready = Unit(head, unit.ended_at, unit.audio_sec, unit.asr_sec, unit.parts)
            self._hold(Unit(tail, unit.ended_at), now)
            return [ready]
        return self._hold(unit, now)

    def flush_due(self, now: float, speaking: bool) -> list[Unit]:
        if not self.pending:
            return []
        waited = now - self.held_at
        if waited >= self.max_hold_sec or (not speaking and waited >= self.hold_sec):
            return self.flush()
        return []

    def flush(self) -> list[Unit]:
        out = [self.pending] if self.pending else []
        self.pending = None
        return out

    def _hold(self, unit: Unit, now: float) -> list[Unit]:
        self.pending = unit
        self.held_at = now
        return []


class RecentFilter:
    """直近に訳した文と同じ (またはほぼ同じ) 文を弾く"""

    def __init__(self, window_sec: float = 30.0, size: int = 20, threshold: float = 0.9):
        self.window_sec = window_sec
        self.threshold = threshold
        self.recent: deque[tuple[float, str]] = deque(maxlen=size)

    def seen(self, text: str, now: float) -> bool:
        """訳済みなら True。未訳なら記録して False。"""
        key = normalize(text)
        if not key:
            return True
        for t, k in self.recent:
            if now - t <= self.window_sec and (k == key or SequenceMatcher(None, k, key).ratio() >= self.threshold):
                return True
        self.recent.append((now, key))
        return False


_TOKEN = re.compile(r"\n|[^\s]+")


def _key(token: str) -> str:
    return token if token == "\n" else re.sub(r"[^\w']", "", token.lower())


class CaptionTracker:
    """ライブキャプションの字幕テキスト (途中経過で書き換わる) から、確定した部分だけを順に取り出す。

    字幕テキストは末尾に語が増えながら直前の語が書き換わり、古い行は上から消えていく。
    前回のテキストと語単位で突き合わせて「どこまで出したか」を今回のテキスト上の位置に移し替え、
    その先で確定したものだけを返す。確定とみなすのは
    - 文末の記号の後に続きが出た (または stable_sec 変わらない) とき
    - 改行が入った (行が確定した) とき
    - idle_sec 変わらない (無音) とき: 文の途中でも出す
    """

    def __init__(self, stable_sec: float = 0.4, idle_sec: float = 1.2):
        self.stable_sec = stable_sec
        self.idle_sec = idle_sec
        self.reset()

    def reset(self) -> None:
        """次に受け取るテキストは既に出したものとして扱う (起動直後・再開時に古い字幕を訳さないように)"""
        self.tokens: list[str] = []
        self.keys: list[str] = []
        self.committed = 0  # tokens[:committed] は出した (または読み飛ばした)
        self.changed_at = 0.0
        self._baseline = True

    def update(self, text: str, now: float) -> str | None:
        """確定して新たに出す部分があれば返す"""
        tokens = _TOKEN.findall(text)
        if self._baseline or tokens != self.tokens:
            keys = [_key(t) for t in tokens]
            if self._baseline:
                committed = len(tokens)
                self._baseline = False
            elif keys == self.keys:
                committed = self.committed  # 記号や大文字小文字だけの書き換え
            else:
                committed = self._map_committed(keys)
            self.tokens, self.keys, self.committed = tokens, keys, committed
            self.changed_at = now

        end = self._confirmed_end(now)
        if end <= self.committed:
            return None
        out = " ".join(t for t in self.tokens[self.committed : end] if t != "\n")
        self.committed = end
        return out or None

    def _map_committed(self, keys: list[str]) -> int:
        c = self.committed
        if c == 0:
            return 0
        sm = SequenceMatcher(None, self.keys, keys, autojunk=False)
        if not any(size >= min(2, len(self.keys)) for _, _, size in sm.get_matching_blocks()):
            return 0  # まったく別のテキストになった (字幕が消えて新しく始まった)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal" and i1 <= c <= i2:
                return j1 + (c - i1)
            if tag in ("replace", "delete") and i1 < c <= i2:
                return j2  # 出した部分が書き換わった: 書き換え後も出したものとみなす
        return len(keys)

    def _confirmed_end(self, now: float) -> int:
        stable = now - self.changed_at
        end = self.committed
        for k in range(self.committed, len(self.tokens)):
            t = self.tokens[k]
            if t == "\n" or (is_eos(t) and (k + 1 < len(self.tokens) or stable >= self.stable_sec)):
                end = k + 1
        if end < len(self.tokens) and stable >= self.idle_sec:
            end = len(self.tokens)
        return end
