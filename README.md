# tools-used-for-de-by-agents-study
A quick non-scientific study of which libraries and tools agents use for DE type problems.

Each task is a prompt in [`prompts/`](prompts/). `agent_bakeoff.py` runs every task
against every Cursor model (`agent --list-models`), then Cortex Code and Claude Code.
Each run gets a fresh directory, and the script records the languages, libraries,
pinned versions (and Spark version, if any) and agent tools each one used.

| task | prompt |
|---|---|
| `poi_s2` | Convert all of the Mapbox POI data into S2 bounding-box coverings on a cluster |
| `hourly_data_quality` | An hourly job that flags data quality issues in an `orders` event table |
| `cc_fraud_streaming` | A streaming job that looks for credit card fraud in card authorization events |
| `health_appeal_synthetic` | A synthetic training set for fine-tuning a health insurance appeal model, seeded from Washington State data |

To add a task, drop a new `prompts/<task>.md` in; the file name is the task name.

## Running

```bash
python3 agent_bakeoff.py --dry-run
```

```bash
python3 agent_bakeoff.py --out bakeoff --tasks cc_fraud_streaming,hourly_data_quality --match 'sonnet|opus'
```

- Output goes to `<out>/<task>/<model>/` (the agent's workspace) and
  `<out>/<task>/<model>.stream.jsonl` (its raw stream).
- `<out>/report.md` has primary language by model × task, library use per task,
  and a per-task table and per-model details. `<out>/results.json` has everything.
- `<out>/tally.csv` is the resume point: rerun with the same `--out` and any
  (task, model) pair already in it is skipped. Tallies from before tasks existed
  load as `poi_s2`.
- `--tasks` also takes paths to prompt files outside `prompts/`.

Compare two runs (e.g. to see how stable a model's choices are):

```bash
python3 -I compare_runs.py bakeoff-run1 bakeoff-run2
```
