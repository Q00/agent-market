#!/usr/bin/env python3
"""Sample customers from the Nemotron-Personas-Korea parquet files (CC BY 4.0, nvidia/Nemotron-Personas-Korea).

    persona-pool.py <dir with train-*.parquet> <out.jsonl> [N=500] [seed=7]

Reads one random row group per file until N rows are picked, keeps the fields a customer needs, and writes JSONL.
Needs pyarrow (the market itself does not).
"""
import json
import random
import sys
from pathlib import Path

import pyarrow.parquet as pq

KEEP = ("uuid", "persona", "professional_persona", "family_persona", "culinary_persona", "hobbies_and_interests",
        "cultural_background", "sex", "age", "marital_status", "family_type", "housing_type", "occupation",
        "district", "province")


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 500
    rnd = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 7)
    files = sorted(src.glob("train-*.parquet"))
    picked = []
    while len(picked) < n:
        f = pq.ParquetFile(rnd.choice(files))
        rows = f.read_row_group(rnd.randrange(f.num_row_groups), columns=list(KEEP)).to_pylist()
        rnd.shuffle(rows)
        picked += [r for r in rows[:max(1, n // 20)] if r.get("age", 0) >= 19]
    with out.open("w", encoding="utf-8") as fh:
        for r in picked[:n]:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{out}: {min(n, len(picked))} personas")


if __name__ == "__main__":
    main()
