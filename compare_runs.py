#!/usr/bin/env python3
"""Compare two agent_bakeoff.py runs, per (task, model): python3 -I compare_runs.py RUN1 RUN2"""
import json
import sys
from pathlib import Path

LEGACY_TASK = "poi_s2"  # results.json from before tasks existed


def load(d):
    return {(r.get("task") or LEGACY_TASK, r["model"]): r
            for r in json.loads((Path(d) / "results.json").read_text())}


def main(d1, d2):
    a, b = load(d1), load(d2)
    same = diff = 0
    print("| task | model | lang run1 | lang run2 | Spark run1 | Spark run2 | libs only in 1 | libs only in 2 |")
    print("|---|---|---|---|---|---|---|---|")
    for key in sorted(a.keys() & b.keys()):
        x, y = a[key], b[key]
        lang = lambda r: next(iter(r["languages"]), "-")
        sp = lambda r: (r["spark_version"] or "unpinned") if "Spark" in r["libraries"] else "no"
        o1, o2 = sorted(set(x["libraries"]) - set(y["libraries"])), sorted(set(y["libraries"]) - set(x["libraries"]))
        if (lang(x), sp(x), o1, o2) == (lang(y), sp(y), [], []):
            same += 1
            continue
        diff += 1
        print(f"| {key[0]} | {key[1]} | {lang(x)} | {lang(y)} | {sp(x)} | {sp(y)} "
              f"| {', '.join(o1) or '-'} | {', '.join(o2) or '-'} |")
    print(f"\nidentical (primary language, Spark yes/no+version, library set): {same}; different: {diff}; "
          f"only in one run: {len(a.keys() ^ b.keys())}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
