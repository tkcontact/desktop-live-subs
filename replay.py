"""logs の TSV の原文を、今の翻訳の設定で順に訳し直して、記録されている訳と並べる (プロンプトの調整用)

    python replay.py logs\\20261108-113621.tsv [--profile 名前]
"""

import argparse
import csv
import statistics
import sys
import time
from pathlib import Path

from settings import ROOT, Settings
from translator import Profile, Translator


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tsv", nargs="+")
    p.add_argument("--profile")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = Settings()
    tl = Translator(settings["strata_url"], settings["context"])
    if args.profile:
        tl.set_profile(Profile.load(ROOT / "profiles" / f"{args.profile}.json"))
    if not tl.healthy():
        raise SystemExit("Strata が応答しません")

    times, empty = [], 0
    for path in args.tsv:
        with open(Path(path), encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        for row in rows:
            t = time.perf_counter()
            ja, _ = tl.translate(row["src"])
            times.append(time.perf_counter() - t)
            empty += not ja
            print(f"{row['src']}\n  記録: {row['ja']}\n  今回: {ja}")
    print(f"\n{len(times)} 文, 空の訳 {empty}, 応答 中央値 {statistics.median(times):.2f}s 最大 {max(times):.2f}s")


if __name__ == "__main__":
    main()
