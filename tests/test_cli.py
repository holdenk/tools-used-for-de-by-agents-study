"""End to end through the CLI, with a fake `claude` on PATH standing in for a real agent."""
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = """#!{python}
import json
answer = {{"languages": [{{"name": "Python", "version": "3.12.7"}}],
          "components": [{{"name": "pyspark", "version": "4.0.1", "role": "engine"}},
                         {{"name": "raincloud", "version": "v0.3.2", "role": "data"}}],
          "rationale": "Small data;\\nline two."}}
for ev in [{{"type": "system", "subtype": "init", "model": "claude-fake"}},
           {{"type": "result", "is_error": False, "result": json.dumps(answer), "total_cost_usd": 0.01}}]:
    print(json.dumps(ev))
"""


@pytest.fixture
def fake_path(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def bakeoff(tmp_path: Path, path: str, *args: str) -> str:
    cmd = [sys.executable, str(REPO / "agent_bakeoff.py"), "--out", str(tmp_path / "out"), "--style", "ask",
           "--models", "claude", "--tasks", "raincloud_air_quality_small",
           "--shared-csv", str(tmp_path / "observations.csv"), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "PATH": path}, check=True)
    return p.stdout


def test_repeats_resume_and_shared_csv(tmp_path: Path, fake_path: str) -> None:
    bakeoff(tmp_path, fake_path, "--repeats", "2")
    assert bakeoff(tmp_path, fake_path, "--repeats", "2").count("skipping") == 4
    out = bakeoff(tmp_path, fake_path)  # the default, 3, adds only the third repeat
    assert out.count("skipping") == 4 and out.count(" ...") == 2

    runs = json.loads((tmp_path / "out" / "results.json").read_text())
    assert sorted((r["variant"], r["rep"]) for r in runs) == sorted(
        (v, rep) for v in ("ask-float", "ask-prefer-spark+raincloud") for rep in (1, 2, 3))
    assert all(r["served_model"] == "claude-fake" and r["libraries"] == ["Raincloud", "Spark"] for r in runs)
    [rep3] = [r for r in runs if r["variant"] == "ask-float" and r["rep"] == 3]
    saved = tmp_path / "out" / "raincloud_air_quality_small" / "ask-float" / "claude.rep3.prompt.txt"
    assert saved.read_text() == rep3["prompt"]

    with (tmp_path / "observations.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len({row["run_id"] for row in rows}) == 6
    marker = (tmp_path / "out" / ".bakeoff-id").read_text().strip()
    assert {(row["out"], row["out_id"]) for row in rows} == {("out", marker)}
    prompts = [row for row in rows if row["kind"] == "prompt"]
    assert len(prompts) == 6 and all(row["name"] == row["prompt_sha"] for row in prompts)
    assert "## Convergence" in (tmp_path / "out" / "report.md").read_text()


def test_dry_run_and_print_prompts() -> None:
    script = str(REPO / "agent_bakeoff.py")
    dry = subprocess.run([sys.executable, script, "--dry-run", "--models", "claude", "--shared-csv", ""],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    tasks = len(list((REPO / "prompts").glob("*.md")))
    assert len(dry) == tasks * 2 * 3  # float + prefer, 3 repeats by default
    shown = subprocess.run([sys.executable, script, "--print-prompts", "--style", "ask", "--mode", "prefer"],
                           capture_output=True, text=True, check=True).stdout
    assert shown.count("===== ") == tasks and "Tooling: use Spark" in shown


def test_compare_runs(tmp_path: Path) -> None:
    def result(rep: int, libs: list[str]) -> dict[str, object]:
        return {"task": "t", "variant": "float", "model": "m", "rep": rep, "preferred": [], "libraries": libs,
                "languages": {"Python": 1}, "spark_version": None, "tool_versions": {}}
    for name, libs in (("a", ["DuckDB"]), ("b", ["DuckDB", "Kafka"])):
        (tmp_path / name).mkdir()
        (tmp_path / name / "results.json").write_text(json.dumps([result(1, ["DuckDB"]), result(2, libs)]))
    out = subprocess.run([sys.executable, "-I", str(REPO / "compare_runs.py"), str(tmp_path / "a"), str(tmp_path / "b")],
                         capture_output=True, text=True, check=True).stdout
    assert "| t | float | m | 2 | Python | Python | no Spark | no Spark | - | Kafka |" in out
    assert "identical: 1; different: 1" in out
