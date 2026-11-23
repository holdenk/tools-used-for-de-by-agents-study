"""Nothing a run produced is cut or dropped: whole final messages, stderr, long values and nested usage counters,
plus the repair of runs recorded when only a final message's last 600 characters were kept."""
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_result

import agent_bakeoff as ab

REPO = Path(__file__).resolve().parent.parent
OUT = ab.OutDir("out", "0123456789abcdef")
# the old code kept only a final message's last 600 characters, which here start mid-word: "ocker compose up ..."
OLD_CUT = ("ocker compose up -d starts Kafka, Spark and the job.\n\n" + "Each stage is tested. " * 30)[:600]
LONG_FINAL = "Done. The pipeline is built. Run it with D" + OLD_CUT


def unescape(text: str) -> str:
    return text.replace("\\\\", "\0").replace("\\n", "\n").replace("\\r", "\r").replace("\0", "\\")


def stream_of(final: str) -> str:
    return json.dumps({"type": "result", "is_error": False, "result": final}) + "\n"


def test_summarize_keeps_the_whole_final_message(tmp_path: Path) -> None:
    summary = ab.summarize(stream_of(LONG_FINAL), tmp_path)
    assert summary["final"] == LONG_FINAL and summary["final_truncated"] is False


def test_tail_starts_on_a_whole_line_and_says_what_was_cut() -> None:
    text = "".join(f"line {i} of the log\n" for i in range(100))
    cut = ab.tail(text, 200, "t/v/claude.stderr.txt")
    head, kept = cut.split("\n", 1)
    assert head.startswith("[... ") and "all of it is in t/v/claude.stderr.txt" in head
    assert text.endswith(kept) and kept.startswith("line ") and len(kept) <= 200
    assert f"{len(text) - len(kept):,} earlier characters cut" in head
    assert ab.tail("short", 200, "x") == "short"
    one_line = "word " * 100  # no line break to start on: start on a whole word instead
    assert ab.tail(one_line, 52, "x").split("\n", 1)[1].startswith("word ")


def test_preview_marks_what_it_leaves_out() -> None:
    assert ab.preview("ls -la", 160) == ab.preview("ls -la\n", 160) == "ls -la"
    assert ab.preview("cat > a.py <<EOF\nprint(1)\nEOF", 160) == "cat > a.py <<EOF …"
    assert ab.preview("x" * 200, 160) == "x" * 160 + " …"


def test_tally_reads_back_fields_past_the_csv_default_limit(tmp_path: Path) -> None:
    """A build run that writes files through shell heredocs can put more than 128 KiB in one tally field."""
    r = {**make_result(), "shell_commands": ["cat > big.py <<'EOF'\n" + "x = 1\n" * 40_000 + "EOF"]}
    ab.write_tally(tmp_path / "tally.csv", [r])
    assert ab.load_tally(tmp_path / "tally.csv") == [r]


def test_observations_cut_nothing() -> None:
    long = "y" * 50_000
    r = {**make_result(), "final": LONG_FINAL, "shell_commands": [long],
         "usage": {"output_tokens": 5, "cache_creation": {"ephemeral_5m_input_tokens": 7}, "service_tier": "standard"},
         "answer": {"components": [{"name": "n" * 400, "version": "v" * 400, "role": long}], "rationale": long}}
    rows = ab.observations(r, OUT)
    facts = {(row["kind"], row["name"]): row for row in rows}
    assert unescape(facts[("response", "")]["value"]) == LONG_FINAL
    assert facts[("shell_command", "0")]["value"] == long and facts[("rationale", "")]["value"] == long
    assert facts[("answer_component", "n" * 400)]["version"] == "v" * 400
    assert facts[("usage", "cache_creation.ephemeral_5m_input_tokens")]["value"] == "7"
    assert ("usage", "service_tier") not in facts  # usage rows are counters


def test_a_known_truncated_final_is_not_exported_as_a_response() -> None:
    r = {**make_result(), "final": LONG_FINAL[-600:], "final_truncated": True}
    assert "response" not in {row["kind"] for row in ab.observations(r, OUT)}


def test_sync_adds_responses_to_runs_exported_without_them(tmp_path: Path) -> None:
    shared = tmp_path / "observations.csv"
    runs = [{**make_result(rep=rep), "final": f"answer {rep}"} for rep in (1, 2)]
    ab.append_shared(shared, [row for r in runs for row in ab.observations(r, OUT) if row["kind"] != "response"])
    assert ab.sync_shared(shared, runs, OUT) == (0, 2)
    assert ab.sync_shared(shared, runs, OUT) == (0, 0)
    with shared.open(newline="", encoding="utf-8") as f:
        responses = sorted(row["value"] for row in csv.DictReader(f) if row["kind"] == "response")
    assert responses == ["answer 1", "answer 2"]


def old_row(root: Path, final: str, stream: str | None, rep: int = 1) -> dict[str, object]:
    """A tally row from before whole final messages were kept, and the stream its run left on disk."""
    r = {k: v for k, v in make_result(rep=rep).items() if k != "final_truncated"}
    r["final"] = final[-ab.OLD_FINAL_CAP:]
    if stream is not None:
        d = root / str(r["task"]) / str(r["variant"])
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ab.run_dir_name(str(r['model']), rep)}.stream.jsonl").write_text(stream, encoding="utf-8")
    return r


def test_recover_finals(tmp_path: Path) -> None:
    restored, overwritten, no_stream, short = rows = [
        old_row(tmp_path, LONG_FINAL, stream_of(LONG_FINAL), rep=1),
        old_row(tmp_path, LONG_FINAL, stream_of("a later attempt said something else " * 30), rep=2),
        old_row(tmp_path, LONG_FINAL, None, rep=3),
        old_row(tmp_path, "All done.", None, rep=4)]
    assert restored["final"] == OLD_CUT  # what the old code recorded
    assert ab.recover_finals(rows, tmp_path) == 1
    assert restored["final"] == LONG_FINAL and restored["final_truncated"] is False
    assert overwritten["final_truncated"] is True and overwritten["final"] == LONG_FINAL[-600:]
    assert no_stream["final_truncated"] is True
    assert short["final_truncated"] is False and short["final"] == "All done."
    assert ab.recover_finals(rows, tmp_path) == 0  # every row is settled now


def test_report_shows_whole_final_messages_and_marks_previews() -> None:
    whole = {**make_result(rep=1), "final": LONG_FINAL, "shell_commands": [f"cmd {i}" for i in range(20)]}
    cut = {**make_result(rep=2), "final": LONG_FINAL[-600:], "final_truncated": True}
    text = ab.report([whole, cut])
    assert "final message:\n\n> Done. The pipeline is built. Run it with Docker compose up -d" in text
    assert "final message (only its last 600 characters were recorded):" in text
    assert "- `cmd 14`" in text and "- `cmd 15`" not in text
    assert "- … and 5 more shell command(s), all in results.json" in text


FAKE = """#!{python}
import json, sys
sys.stderr.buffer.write(b"\\xff\\xfe not UTF-8\\n" + b"".join(b"progress %d\\n" % i for i in range(1000)))
print(json.dumps({{"type": "result", "is_error": False, "result": {final!r}}}))
"""


def test_run_one_keeps_stderr_and_survives_bytes_that_are_not_utf8(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(FAKE.format(python=sys.executable, final=LONG_FINAL))
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    run = {"task": "t", "variant": "float", "style": "build", "mode": "float", "preferred": [], "prompt": "hi",
           "prompt_sha": "abc", "template_sha": "def"}
    r = ab.run_one(run, "claude", tmp_path / "out", timeout=60)
    assert r["final"] == LONG_FINAL
    saved = (tmp_path / "out" / "t" / "float" / "claude.stderr.txt").read_text(encoding="utf-8")
    assert saved.startswith("�� not UTF-8\n") and saved.endswith("progress 999\n")
    assert r["stderr"].startswith("[... ") and "all of it is in t/float/claude.stderr.txt" in r["stderr"]
    assert r["stderr"].endswith("progress 999\n") and "\nprogress " in r["stderr"]


ANSWER = {"languages": [{"name": "Python", "version": "3.12.7"}],
          "components": [{"name": "pyspark", "version": "4.0.1", "role": "engine"}],
          "rationale": "Docker compose brings up the whole stack. " * 30}


def test_a_rerun_restores_old_truncated_finals_everywhere(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = {"type": "result", "is_error": False, "result": json.dumps(ANSWER)}
    (bin_dir / "claude").write_text(f"#!{sys.executable}\nprint({json.dumps(result)!r})\n")
    (bin_dir / "claude").chmod(0o755)
    out, shared = tmp_path / "out", tmp_path / "observations.csv"
    cmd = [sys.executable, str(REPO / "agent_bakeoff.py"), "--out", str(out), "--style", "ask", "--models", "claude",
           "--tasks", "raincloud_air_quality_small", "--repeats", "1", "--shared-csv", str(shared)]
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
    whole = json.dumps(ANSWER)

    # roll the tally and shared CSV back to how the old code left them: 600-character tails, no response rows
    tally = ab.load_tally(out / "tally.csv")
    assert [r["final"] for r in tally] == [whole, whole]
    ab.write_tally(out / "tally.csv", [{**{k: v for k, v in r.items() if k != "final_truncated"},
                                       "final": r["final"][-600:]} for r in tally])
    with shared.open(newline="", encoding="utf-8") as f:
        rows = [row for row in csv.DictReader(f) if row["kind"] != "response"]
    with shared.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ab.SHARED_FIELDS)
        w.writeheader()
        w.writerows(rows)

    p = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
    assert "restored the whole final message of 2 earlier run(s)" in p.stdout
    assert f"added the whole final message of 2 earlier run(s) to {shared}" in p.stdout
    assert [r["final"] for r in ab.load_tally(out / "tally.csv")] == [whole, whole]
    assert "final message:\n\n> " + whole in (out / "report.md").read_text(encoding="utf-8")
    with shared.open(newline="", encoding="utf-8") as f:
        assert [row["value"] for row in csv.DictReader(f) if row["kind"] == "response"] == [whole, whole]
