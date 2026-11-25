"""Find review wording that the Consolidated Reason rules do not understand yet - from REAL reviews, no guessing.

Usage:   python review_phrase_finder.py "reports/REQ-.../boAt Bassheads 100 Wired Earphones.xlsx" [more.xlsx ...]

It re-reads every review in the Positive / Negative sheets, runs fk_reasons.classify_review on it and prints:
  1. reviews that end up as 'Generic Comments' even though they contain real wording,
  2. every review where the label in the workbook differs from what the current rules give (after you edit rules),
  3. the word pairs / triples that occur most often in the reviews listed in (1), with an example for each.
You then copy the exact wording from a real review into CUSTOM_PHRASES in fk_reasons.py (see the comments there)
and add that review to test_reasons.py as a regression case."""
import re
import sys
from collections import Counter

from openpyxl import load_workbook

from fk_reasons import GENERIC, classify_review

SHEETS = {"Positive Reviews": "Positive", "Negative Reviews": "Negative"}
STOP = set("the a an and or is was are it this that i my me to of for in on with at as be have has had very so too "
           "but not no".split())


def read_rows(path):
    wb = load_workbook(path, read_only=True, data_only=True)
    for sheet, sentiment in SHEETS.items():
        if sheet not in wb.sheetnames:
            continue
        rows = wb[sheet].iter_rows(values_only=True)
        header = None
        for row in rows:
            if header is None:
                if row and "Review Description" in row and "Consolidated Reason" in row:
                    header = {str(h): i for i, h in enumerate(row) if h is not None}
                continue
            if not row or row[header["Review Description"]] in (None, ""):
                continue
            g = lambda k: row[header[k]] if k in header and header[k] < len(row) and row[header[k]] is not None else ""
            yield sentiment, str(g("Short Review")), str(g("Review Description")), str(g("Consolidated Reason")), g("Ratings")


def main(paths):
    generic, changed, total = [], [], 0
    for path in paths:
        for sentiment, short, desc, label, rating in read_rows(path):
            total += 1
            text = f"{short}. {desc}"
            _detail, now = classify_review(text, sentiment)
            if now == GENERIC and len(re.findall(r"[a-z]{3,}", text.lower())) >= 6:
                generic.append((sentiment, rating, text))
            if now != label:
                changed.append((sentiment, text, label, now))
    print(f"{total} reviews read from {len(paths)} workbook(s)\n")
    print(f"=== 1. 'Generic Comments' reviews with real wording: {len(generic)} ===")
    for s, r, t in generic[:60]:
        print(f"[{s} {r}] {t[:220]}")
    print(f"\n=== 2. label in the workbook differs from the current rules: {len(changed)} ===")
    for s, t, old, new in changed[:40]:
        print(f"[{s}] {t[:160]}\n     workbook: {old}\n     now     : {new}")
    grams, example = Counter(), {}
    for _s, _r, t in generic:
        words = [w for w in re.findall(r"[a-z']+", t.lower())]
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                g = words[i:i + n]
                if g[0] in STOP or g[-1] in STOP:
                    continue
                key = " ".join(g)
                grams[key] += 1
                example.setdefault(key, t[:140])
    print("\n=== 3. wording that repeats in the Generic reviews (candidates - YOU decide the topic) ===")
    for g, c in grams.most_common(40):
        if c >= 2:
            print(f"{c:3d}x  {g}   e.g. {example[g]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
