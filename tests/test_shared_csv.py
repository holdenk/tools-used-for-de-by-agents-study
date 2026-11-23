"""The tally round trip and the shared long-format observations CSV."""
import csv
import errno
import json
import os
from pathlib import Path

import pytest
from conftest import make_result

import agent_bakeoff as ab

OUT = ab.OutDir("out", "0123456789abcdef")


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
    assert ab.run_id(a, OUT) == ab.run_id(json.loads(json.dumps(a)), OUT)
    assert ab.run_id(a, OUT) != ab.run_id(b, OUT)


def test_out_dir_id_persists_moves_and_differs(tmp_path: Path) -> None:
    a, b = tmp_path / "m1" / "bakeoff", tmp_path / "m2" / "bakeoff"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    first = ab.out_dir(a)
    assert first.name == "bakeoff" and ab.out_dir(a) == first
    assert ab.out_dir(b).id != first.id  # same name, different folder
    moved = tmp_path / "renamed"
    a.rename(moved)
    assert ab.out_dir(moved).id == first.id
    assert [f.name for f in moved.iterdir()] == [".bakeoff-id"]  # no temp files left behind


def test_identical_old_runs_in_same_named_out_dirs_stay_distinct(tmp_path: Path) -> None:
    """Two machines both using the default `bakeoff` dir, syncing tallies from before `started` existed whose
    rows match on every field (a Cursor run reports no API time, cost or usage)."""
    old = {k: v for k, v in make_result().items() if k not in ("started", "api_ms", "cost_usd", "usage")}
    shared = tmp_path / "observations.csv"
    assert ab.sync_shared(shared, [old], ab.OutDir("bakeoff", "machine-1")) == (1, 0)
    assert ab.sync_shared(shared, [old], ab.OutDir("bakeoff", "machine-2")) == (1, 0)
    assert ab.sync_shared(shared, [old], ab.OutDir("bakeoff", "machine-2")) == (0, 0)


@pytest.mark.parametrize("answer", [{"components": 42}, {"components": [{"name": "pyspark"}], "languages": 42},
                                    {"components": "pyspark", "languages": {"Python": "3.12"}}])
def test_malformed_answers_are_recorded_not_fatal(tmp_path: Path, answer: dict[str, object]) -> None:
    stream = json.dumps({"type": "result", "is_error": False, "result": json.dumps(answer)})
    summary = ab.summarize(stream, tmp_path, "ask")
    rows = ab.observations({**make_result(style="ask"), **summary}, OUT)
    assert rows and {row["kind"] for row in rows} >= {"run", "prompt"}


def test_observations(tmp_path: Path) -> None:
    r = make_result(libs=("Spark", "tests"), preferred=("Spark", "Kafka"), tool_versions={"Spark": "3.5.1"})
    r["answer"] = {"components": [{"name": "Apache Spark", "version": "3.5.1", "role": "engine\nmain"}],
                   "languages": ["Python 3.12"], "rationale": "Because."}
    rows = ab.observations(r, OUT)
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
    assert ab.sync_shared(shared, results, OUT) == (2, 0)
    assert ab.sync_shared(shared, results, OUT) == (0, 0)
    assert ab.sync_shared(shared, [*results, make_result(rep=3)], OUT) == (1, 0)
    with shared.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len({row["run_id"] for row in rows}) == 3
    assert sum(1 for _ in shared.open()) == len(rows) + 1  # one physical line per row, plus the header


def test_failed_export_leaves_no_partial_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "observations.csv"
    first, second = make_result(rep=1), make_result(rep=2)
    ab.sync_shared(shared, [first], OUT)
    before = shared.read_bytes()

    def half_then_disk_full(fd: int, data: bytes) -> None:
        os.write(fd, data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ab, "_write_all", half_then_disk_full)
    with pytest.raises(OSError):
        ab.append_shared(shared, ab.observations(second, OUT))
    monkeypatch.undo()
    assert shared.read_bytes() == before  # nothing of the failed run is left behind
    assert ab.sync_shared(shared, [first, second], OUT) == (1, 0)  # so the next sync adds it whole


def test_non_ascii_prompt_round_trips(tmp_path: Path) -> None:
    shared = tmp_path / "observations.csv"
    r = {**make_result(), "prompt": "PM2.5 above 75 µg/m³, TMAX in °C"}
    ab.sync_shared(shared, [r], OUT)
    with shared.open(newline="", encoding="utf-8") as f:
        [prompt] = [row for row in csv.DictReader(f) if row["kind"] == "prompt"]
    assert prompt["value"] == r["prompt"]
