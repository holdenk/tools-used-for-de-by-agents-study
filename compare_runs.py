#!/usr/bin/env python3
"""Compare two agent_bakeoff.py runs, per (task, variant, model, repeat): python3 -I compare_runs.py RUN1 RUN2

prefer-mode runs compare the versions of the preferred tools; float (and legacy) runs
compare primary language (with its version, when an ask run gave one), Spark yes/no+version,
and the library set.
"""
import json
import sys
from pathlib import Path
from typing import Any

Result = dict[str, Any]
Key = tuple[str, str, str, str]  # (task, variant, model, repeat)


def load(d: str) -> dict[Key, Result]:
    out = {}
    for r in json.loads((Path(d) / "results.json").read_text()):
        r.setdefault("preferred", [])  # results from before tasks / modes existed
        r.setdefault("tool_versions", {})
        out[(r.get("task") or "poi_s2", r.get("variant") or "legacy", r["model"], str(r.get("rep") or 1))] = r
    return out


def versions(r: Result) -> str:
    if r.get("invalid_answer"):
        return "INVALID ANSWER"
    if r["preferred"]:
        return ", ".join(f"{t} {r['tool_versions'].get(t) or 'unpinned'}" if t in r["libraries"] else f"{t} NOT USED"
                         for t in r["preferred"])
    return "Spark " + (r["spark_version"] or "unpinned") if "Spark" in r["libraries"] else "no Spark"


def lang(r: Result) -> str:
    first = next(iter(r["languages"]), None)
    ver = (r.get("language_versions") or {}).get(first)
    return f"{first} {ver}" if first and ver else first or "-"


def main(d1: str, d2: str) -> None:
    a, b = load(d1), load(d2)
    same = diff = 0
    print("| task | variant | model | repeat | lang run1 | lang run2 | versions run1 | versions run2 "
          "| libs only in 1 | libs only in 2 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for key in sorted(a.keys() & b.keys()):
        x, y = a[key], b[key]
        o1, o2 = sorted(set(x["libraries"]) - set(y["libraries"])), sorted(set(y["libraries"]) - set(x["libraries"]))
        if x["preferred"]:  # prefer mode: we only care whether the preferred tools' versions moved
            o1 = o2 = []
            unchanged = versions(x) == versions(y)
        else:
            unchanged = (lang(x), versions(x), o1, o2) == (lang(y), versions(y), [], [])
        if unchanged:
            same += 1
            continue
        diff += 1
        print(f"| {' | '.join(key)} | {lang(x)} | {lang(y)} | {versions(x)} | {versions(y)} "
              f"| {', '.join(o1) or '-'} | {', '.join(o2) or '-'} |")
    print(f"\nidentical: {same}; different: {diff}; only in one run: {len(a.keys() ^ b.keys())}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
