"""Every task prompt parses, renders in every mode and names tools the report can detect."""
import pytest

import agent_bakeoff as ab

TASKS = ab.load_tasks(None, ab.PROMPTS_DIR)


def test_found_tasks() -> None:
    assert {"poi_s2", "raincloud_tpch_mart_small", "raincloud_ghcn_climate_large"} <= set(TASKS)


@pytest.mark.parametrize("task", sorted(TASKS))
def test_prompt(task: str) -> None:
    prefer, body, template_sha = TASKS[task]
    assert "{tooling}" in body and len(template_sha) == 12
    assert prefer, "every task names a stack for prefer mode"
    assert all(t.lower() in ab.LABELS for t in prefer), "prefer tools must be LIBS labels"
    for mode in ab.MODES:
        text = ab.render(body, mode, prefer if mode == "prefer" else [])
        assert "{tooling}" not in text and "- Tooling:" in text


def test_plan_runs_hashes() -> None:
    runs = ab.plan_runs({t: TASKS[t] for t in ("raincloud_tpch_mart_small",)}, ab.STYLES, ab.MODES, None)
    assert [r["variant"] for r in runs] == ["float", "prefer-spark+raincloud", "ask-float", "ask-prefer-spark+raincloud"]
    assert len({r["prompt_sha"] for r in runs}) == 4  # each variant's exact text differs
    assert len({r["template_sha"] for r in runs}) == 1
    assert all(r["prompt_sha"] == ab.sha(r["prompt"]) for r in runs)
    # ask style lists what the agent picks, not what its picks pull in
    assert all(("transitive" in r["prompt"]) == (r["style"] == "ask") for r in runs)


def test_tpch_pair_differs_only_in_input_and_scale() -> None:
    small = TASKS["raincloud_tpch_mart_small"][1].split("\n- ")
    large = TASKS["raincloud_tpch_mart_large"][1].split("\n- ")
    assert len(small) == len(large)
    differing = [s.split(":")[0] for s, big in zip(small, large, strict=True) if s != big]
    assert differing == ["Input", "Scale"]
