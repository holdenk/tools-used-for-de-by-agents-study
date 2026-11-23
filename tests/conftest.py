from pathlib import Path
from typing import Any

import pytest

Result = dict[str, Any]


def make_result(task: str = "t", variant: str = "float", model: str = "m1", rep: int = 1,
                libs: tuple[str, ...] = (), lang: str = "Python", tool_versions: dict[str, str] | None = None,
                preferred: tuple[str, ...] = (), invalid: bool = False, style: str = "build") -> Result:
    """A finished run, shaped like run_one's output."""
    tv = tool_versions or {}
    return {"task": task, "variant": variant, "style": style, "mode": "prefer" if preferred else "float",
            "preferred": list(preferred), "model": model, "rep": rep, "started": f"2026-11-23T00:00:0{rep}Z",
            "status": "exit 0", "wall_s": 10 + rep, "answer": None, "invalid_answer": invalid,
            "languages": {lang: 100}, "language_versions": {}, "libraries": sorted(libs), "tool_versions": tv,
            "versions": {}, "spark_version": tv.get("Spark"), "tools": {"bash": 2}, "shell_commands": ["ls"],
            "ok": not invalid, "api_ms": 1, "cost_usd": 0.01, "usage": {"output_tokens": 5}, "files": 3,
            "final": "", "stderr": "", "served_model": "model-x", "prompt_sha": "abc", "template_sha": "def",
            "prompt": "Build it.\nPlease.", "harness_commit": "0123"}


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d
