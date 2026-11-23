"""Out-of-quota runs: detected only from the CLI's own errors, never recorded, paused on and retried."""
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import agent_bakeoff as ab

REPO = Path(__file__).resolve().parent.parent
LIMIT = "You've hit your session limit · resets 3pm (America/Los_Angeles)"


def events(*evs: dict[str, Any]) -> str:
    return "\n".join(json.dumps(ev) for ev in evs) + "\n"


def said(text: str, error: str | None = None) -> dict[str, Any]:
    """An assistant message; Claude Code sets `error` on the API-error message a run ends with."""
    ev: dict[str, Any] = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return {**ev, "error": error} if error else ev


def result(text: str, is_error: bool) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": is_error, "result": text}


@pytest.mark.parametrize(("stream", "stderr", "code", "want"), [
    (events(said(LIMIT, "rate_limit"), result(LIMIT, True)), "", 1, "hit your session limit"),  # Claude Code
    (events(said("Credit balance is too low", "billing_error"), result("Credit balance is too low", True)), "", 1,
     "Credit balance"),
    (events(result("Claude AI usage limit reached|1764000000", True)), "", 1, "usage limit reached"),  # older Claude
    ("", "Error: You've hit your usage limit for this month.\n", 1, "hit your usage limit"),  # stderr, no events
    ("API Error: 429 Too Many Requests\n", "", 1, "429"),  # non-JSON stdout
    (events({"type": "error", "message": "Monthly quota exceeded"}), "", 1, "quota"),
])
def test_quota_error_found(stream: str, stderr: str, code: int, want: str) -> None:
    reason = ab.quota_error(stream, stderr, code)
    assert reason is not None and want in reason


@pytest.mark.parametrize(("stream", "stderr", "code"), [
    # a finished run can talk about rate limits and quotas: it's the task, not the account
    (events(said("Kafka quotas and a rate limiter, per the usage limit spec"),
            result("Added a rate limit: 429 Too Many Requests goes to the DLQ", False)), "", 0),
    (events(said("I'll add a rate limiter for the card API")), "", None),  # timed out; the agent's own words
    (events(said("ok"))[:-1] + '\n{"type": "assistant", "message": {"content": [{"text": "a quota per', "", None),
    (events(result("Prompt is too long", True)), "", 1),
    (events(said("API Error: 500 Internal server error", "server_error"),
            result("API Error: 500 Internal server error", True)), "", 1),
    ("", "Approaching usage limit · resets 3pm\nTraceback: ConnectionResetError\n", 1),  # a warning, then a crash
    ("", "You've used 90% of your weekly limit\n", 1),
])
def test_quota_error_not_found(stream: str, stderr: str, code: int | None) -> None:
    assert ab.quota_error(stream, stderr, code) is None


def flaky(outcomes: list[str]) -> Any:
    """An attempt that stops on quota or returns a result, per call, repeating the last outcome."""
    calls = iter(outcomes + [outcomes[-1]] * 100)

    def attempt() -> dict[str, Any]:
        if next(calls) == "quota":
            raise ab.QuotaExhausted(LIMIT)
        return {"status": "exit 0"}
    return attempt


def test_retries_after_a_pause(capsys: pytest.CaptureFixture[str]) -> None:
    slept: list[float] = []
    r = ab.run_with_quota_retries(flaky(["quota", "quota", "ok"]), "[1/1] t", 10, 24, sleep=slept.append)
    assert r == {"status": "exit 0"}
    assert slept == [24 * 3600, 24 * 3600]
    out = capsys.readouterr().out
    assert out.count("OUT OF QUOTA") == 2 and "pausing 24h, until " in out
    assert "[1/1] t (quota retry 2/10) ..." in out and "giving up" not in out


def test_gives_up_after_the_retries(capsys: pytest.CaptureFixture[str]) -> None:
    slept: list[float] = []
    assert ab.run_with_quota_retries(flaky(["quota"]), "[1/1] t", 2, 0.5, sleep=slept.append) is None
    assert slept == [1800, 1800]
    out = capsys.readouterr().out
    assert out.count("OUT OF QUOTA") == 3 and "still out of quota after 2 quota retries; giving up" in out


def test_zero_retries_stops_at_once() -> None:
    slept: list[float] = []
    assert ab.run_with_quota_retries(flaky(["quota"]), "[1/1] t", 0, 24, sleep=slept.append) is None
    assert slept == []


# --- end to end, with a fake `claude` that follows FAKE_PLAN (one outcome per call; the last one repeats)
FAKE_CLAUDE = r"""
import json, os, sys
counter = os.environ["FAKE_COUNTER"]
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
plan = os.environ["FAKE_PLAN"].split(",")
init = {"type": "system", "subtype": "init", "model": "claude-fake"}
if plan[min(n, len(plan) - 1)] == "quota":
    msg = "You've hit your session limit, resets 3pm"
    for ev in [init, {"type": "assistant", "message": {"content": [{"type": "text", "text": msg}]}, "error": "rate_limit"},
               {"type": "result", "subtype": "success", "is_error": True, "result": msg}]:
        print(json.dumps(ev))
    sys.exit(1)
answer = {"languages": [{"name": "Python", "version": "3.12.7"}],
          "components": [{"name": "duckdb", "version": "1.5.5", "role": "engine"}], "rationale": "small data"}
for ev in [init, {"type": "result", "is_error": False, "result": json.dumps(answer), "total_cost_usd": 0.01}]:
    print(json.dumps(ev))
"""


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    fake.chmod(0o755)
    return bin_dir


def bakeoff(tmp_path: Path, fake_bin: Path, plan: str, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "calls").unlink(missing_ok=True)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "FAKE_PLAN": plan,
           "FAKE_COUNTER": str(tmp_path / "calls")}
    cmd = [sys.executable, str(REPO / "agent_bakeoff.py"), "--out", str(tmp_path / "out"), "--style", "ask",
           "--models", "claude", "--tasks", "raincloud_air_quality_small", "--repeats", "1", "--quota-wait", "0",
           "--shared-csv", str(tmp_path / "observations.csv"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def calls(tmp_path: Path) -> int:
    return int((tmp_path / "calls").read_text())


def recorded(tmp_path: Path) -> list[dict[str, str]]:
    with (tmp_path / "out" / "tally.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def shared_runs(tmp_path: Path) -> set[str]:
    with (tmp_path / "observations.csv").open(newline="") as f:
        return {row["run_id"] for row in csv.DictReader(f)}


def test_recovers_after_quota(tmp_path: Path, fake_bin: Path) -> None:
    p = bakeoff(tmp_path, fake_bin, "quota,quota,ok", "--mode", "float")
    assert p.returncode == 0, p.stderr
    assert p.stdout.count("OUT OF QUOTA") == 2 and "(quota retry 2/10) ..." in p.stdout
    assert calls(tmp_path) == 3
    [row] = recorded(tmp_path)  # only the run that got through
    assert row["status"] == "exit 0"
    assert len(shared_runs(tmp_path)) == 1


def test_gives_up_and_a_rerun_catches_up(tmp_path: Path, fake_bin: Path) -> None:
    p = bakeoff(tmp_path, fake_bin, "ok,quota", "--quota-retries", "1")
    assert p.returncode == ab.QUOTA_EXIT, p.stderr
    assert calls(tmp_path) == 3  # the float run, then the prefer run and its one retry
    assert "STOPPED, out of quota: 1 run(s) not done" in p.stdout
    [row] = recorded(tmp_path)
    assert row["variant"] == "ask-float"
    assert len(shared_runs(tmp_path)) == 1
    assert len(json.loads((tmp_path / "out" / "results.json").read_text())) == 1

    p = bakeoff(tmp_path, fake_bin, "ok")
    assert p.returncode == 0, p.stderr
    assert calls(tmp_path) == 1 and p.stdout.count("skipping") == 1
    assert sorted(row["variant"] for row in recorded(tmp_path)) == ["ask-float", "ask-prefer-spark+raincloud"]
    assert len(shared_runs(tmp_path)) == 2


def test_quota_error_then_a_hang_until_the_timeout(tmp_path: Path, fake_bin: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    (fake_bin / "claude").write_text(f"#!{sys.executable}\nimport sys, time\n"
                                     "print('Error: usage limit reached, retrying', file=sys.stderr, flush=True)\n"
                                     "time.sleep(30)\n")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    run = {"task": "t", "variant": "ask-float", "style": "ask", "prompt": "hi"}
    with pytest.raises(ab.QuotaExhausted, match="usage limit reached"):
        ab.run_one(run, "claude", tmp_path / "out", timeout=1)


def test_quota_stop_before_any_run_records_nothing(tmp_path: Path, fake_bin: Path) -> None:
    p = bakeoff(tmp_path, fake_bin, "quota", "--quota-retries", "0")
    assert p.returncode == ab.QUOTA_EXIT and calls(tmp_path) == 1
    assert "STOPPED, out of quota: 2 run(s) not done" in p.stdout
    assert not (tmp_path / "out" / "tally.csv").exists() and not (tmp_path / "observations.csv").exists()
