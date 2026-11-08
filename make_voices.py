"""読み上げの「日本語ネイティブの声」の見本 (voices/male.wav, voices/female.wav と文字起こしの .txt) を作る

Qwen3-TTS の VoiceDesign (1.7B) に声の説明を渡して読ませ、候補を何本か作って、Whisper でいちばん正しく
聞き取れたものを残す。一度作ればよい (VRAM を 4.5GB ほど使うので、字幕ツールは止めておく)。

    python make_voices.py [male|female ...] [--candidates 3]

自分で用意した声を使うなら、voices/<male|female>.wav (5〜10 秒) とその文字起こしの .txt を置き換える。
"""

import argparse
import re
import sys
import time
import types
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "voices"
MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"

SPECS = {
    "male": (
        "A Japanese man in his early 30s, a native speaker of standard Tokyo Japanese. Natural, relaxed and friendly "
        "conversational tone, medium-low pitch, clear articulation, moderate speed.",
        "えっと、今日はちょっと面白い話があるんだよね。最初は全然信じてなかったんだけど、実際にやってみたら本当にうまくいってさ。",
    ),
    "female": (
        "A Japanese woman in her late 20s, a native speaker of standard Tokyo Japanese. Natural, bright and friendly "
        "conversational tone, medium pitch, clear articulation, moderate speed.",
        "ねえ、聞いて。昨日ちょっと面白いことがあったの。最初はびっくりしたんだけど、話してみたらすごくいい人だったんだよね。",
    ),
}


def _chars(text: str) -> str:
    return re.sub(r"[\s、。！？!?,.「」…ー]", "", text)


def cer(ref: str, hyp: str) -> float:
    a, b = _chars(ref), _chars(hyp)
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (ca != cb))
    return d[len(b)] / max(len(a), 1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("names", nargs="*", metavar="male|female")
    p.add_argument("--candidates", type=int, default=3)
    args = p.parse_args()
    args.names = args.names or list(SPECS)
    if bad := set(args.names) - set(SPECS):
        p.error(f"知らない声: {', '.join(bad)}")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.modules.setdefault("sox", types.ModuleType("sox"))  # tts.py と同じ (import 時に cmd を起動するのを避ける)

    import torch
    from qwen_tts import Qwen3TTSModel

    t = time.perf_counter()
    model = Qwen3TTSModel.from_pretrained(MODEL, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
    print(f"{MODEL} を読み込みました ({time.perf_counter() - t:.0f} 秒)")
    made: dict[str, list[tuple[np.ndarray, int]]] = {}
    for name in args.names:
        instruct, text = SPECS[name]
        made[name] = []
        for i in range(args.candidates):
            torch.manual_seed(i)
            wavs, sr = model.generate_voice_design(text=text, instruct=instruct, language="Japanese")
            made[name].append((wavs[0].astype(np.float32), sr))
            print(f"{name} 候補 {i + 1}: {len(wavs[0]) / sr:.1f} 秒")
    del model
    torch.cuda.empty_cache()

    import asr  # noqa: F401  (cublas の DLL を PATH に足す)
    import soxr
    from faster_whisper import WhisperModel

    whisper = WhisperModel("large-v3-turbo", device="cuda", compute_type="int8")
    VOICES.mkdir(exist_ok=True)
    for name, cands in made.items():
        text = SPECS[name][1]
        scored = []
        for i, (x, sr) in enumerate(cands):
            segs, _ = whisper.transcribe(soxr.resample(x, sr, 16000), language="ja", beam_size=5)
            heard = "".join(s.text for s in segs)
            scored.append((cer(text, heard), i))
            print(f"{name} 候補 {i + 1}: CER {scored[-1][0]:.2f} | {heard}")
        best = min(scored)[1]
        x, sr = cands[best]
        x = x / max(1e-4, float(np.abs(x).max())) * 0.9
        sf.write(VOICES / f"{name}.wav", x, sr, subtype="PCM_16")
        (VOICES / f"{name}.txt").write_text(text + "\n", encoding="utf-8")
        print(f"→ voices/{name}.wav (候補 {best + 1})")


if __name__ == "__main__":
    main()
