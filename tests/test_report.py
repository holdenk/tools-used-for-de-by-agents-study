"""Repeats, convergence metrics and the markdown report."""
import math
from collections import Counter

from conftest import make_result

import agent_bakeoff as ab


def test_jaccard_and_effective_choices() -> None:
    assert ab.jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == 1 / 3
    assert ab.jaccard(frozenset(), frozenset()) == 1.0
    assert ab.effective_choices(Counter({"x": 4})) == 1.0
    assert math.isclose(ab.effective_choices(Counter({"x": 1, "y": 1, "z": 1, "w": 1})), 4.0)


def test_tests_label_is_not_part_of_a_stack() -> None:
    assert ab.stack(make_result(libs=("Spark", "tests"))) == frozenset({"Spark"})


def test_mean_similarity_within_and_across_models() -> None:
    runs = [make_result(model=m, rep=rep, libs=libs) for m, rep, libs in [
        ("m1", 1, ("DuckDB",)), ("m1", 2, ("DuckDB",)),
        ("m2", 1, ("DuckDB",)), ("m2", 2, ("Spark",))]]
    assert ab.mean_similarity(runs, same_model=True) == "0.50"  # m1 pair 1.0, m2 pair 0.0
    assert ab.mean_similarity(runs, same_model=False) == "0.50"  # 2 of the 4 cross pairs match
    assert ab.mean_similarity(runs[:1], same_model=True) == "-"


def test_tally_cell() -> None:
    assert ab.tally_cell(["Spark 3.5.1"]) == "Spark 3.5.1"
    assert ab.tally_cell(["a", "b", "a"]) == "a ×2; b ×1"


def test_report_with_repeats() -> None:
    results = []
    for rep in (1, 2, 3):
        results.append(make_result(task="small", model="m1", rep=rep, libs=("DuckDB", "pandas", "tests")))
        results.append(make_result(task="large", model="m1", rep=rep, libs=("Spark", "pandas", "tests")))
        results.append(make_result(task="large", variant="prefer-spark", model="m1", rep=rep, libs=("Spark",),
                                   preferred=("Spark",), tool_versions={"Spark": "3.5.1" if rep < 3 else "4.0.1"}))
    results.append(make_result(task="large", variant="prefer-spark", model="m2", libs=("Spark",), preferred=("Spark",),
                               invalid=True))
    text = ab.report(results)
    assert "| m1 | Spark 3.5.1 ×2; Spark 4.0.1 ×1 |" in text
    assert "| m2 | INVALID ANSWER |" in text
    assert "| m1 #2 |" in text and "### large / float / m1 #3" in text
    assert "| small / float | 3 | 1 | DuckDB, pandas (3/3) | 1.0 | 1.00 | - | Python (3/3) |" in text
    # invalid answers stay out of the stats: 3 valid prefer runs, not 4
    assert "| large / prefer-spark | 3 | 1 | Spark (3/3) |" in text
    assert "Used by at least half the float runs of every task (build, 2 tasks): pandas" in text
