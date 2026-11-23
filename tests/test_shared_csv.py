"""The tally round trip and the shared long-format observations CSV."""
import csv
import json
from pathlib import Path

import pytest
from conftest import make_result

import agent_bakeoff as ab


def unescape(text: str) -> str:
    return text.replace("\\\\", "\0").replace("\\n", "\n").replace("\\r", "\r").replace("\0", "\\")


@pytest.mark.parametrize("text", ["plain", "a\nb", "c:\\new\\dir", "x\r\n\\n literal", ""])
def test_one_line_round_trips(text: str) -> None:
    encoded = ab.one_line(text)
    assert "\n" not in encoded and "\r" not in encoded
    assert unescape(encoded) == text


def test_tally_round_trip(tmp_path: Path) -> None:
    rows = [make_result(rep=1), make_result(rep=2, libs=("Spark",))]
    ab.write_tally(tmp_path / "tally.csv", rows)
    assert ab.load_tally(tmp_path / "tally.csv") == rows


def test_old_tally_rows_load_as_repeat_1(tmp_path: Path) -> None:
    row = {k: v for k, v in make_result().items() if k not in ("rep", "style")}
    ab.write_tally(tmp_path / "tally.csv", [row])
    [loaded] = ab.load_tally(tmp_path / "tally.csv")
    assert loaded["rep"] == 1 and loaded["style"] == "build"


def test_run_id_is_stable_and_distinct() -> None:
    a, b = make_result(rep=1), make_result(rep=2)
    assert ab.run_id(a) == ab.run_id(json.loads(json.dumps(a)))
    assert ab.run_id(a) != ab.run_id(b)


def test_observations(tmp_path: Path) -> None:
    r = make_result(libs=("Spark", "tests"), preferred=("Spark", "Kafka"), tool_versions={"Spark": "3.5.1"})
    r["answer"] = {"components": [{"name": "Apache Spark", "version": "3.5.1", "role": "engine\nmain"}],
                   "languages": ["Python 3.12"], "rationale": "Because."}
    rows = ab.observations(r, "bakeoff")
    assert all(set(row) == set(ab.SHARED_FIELDS) for row in rows)
    assert len({row["run_id"] for row in rows}) == 1
    facts = {(row["kind"], row["name"]): row for row in rows}
    assert facts[("library", "Spark")]["version"] == "3.5.1"
    assert facts[("preferred", "Spark")]["value"] == "1" and facts[("preferred", "Kafka")]["value"] == "0"
    assert facts[("answer_component", "Apache Spark")]["value"] == "engine\\nmain"
    assert unescape(facts[("prompt", "abc")]["value"]) == r["prompt"]
    assert facts[("run", "wall_s")]["value"] == "11" and facts[("usage", "output_tokens")]["value"] == "5"


def test_shared_csv_sync_is_idempotent(tmp_path: Path) -> None:
    shared = tmp_path / "data" / "observations.csv"
    results = [make_result(rep=1), make_result(rep=2)]
    assert ab.sync_shared(shared, results, "out") == 2
    assert ab.sync_shared(shared, results, "out") == 0
    assert ab.sync_shared(shared, [*results, make_result(rep=3)], "out") == 1
    with shared.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len({row["run_id"] for row in rows}) == 3
    assert sum(1 for _ in shared.open()) == len(rows) + 1  # one physical line per row, plus the header
