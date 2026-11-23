# tools-used-for-de-by-agents-study
A quick non-scientific study of which libraries and tools agents use for DE type problems.

Each task is a prompt in [`prompts/`](prompts/). `agent_bakeoff.py` runs every task
against every Cursor model (`agent --list-models`), then Cortex Code and Claude Code.
Each run gets a fresh directory, and the script records the languages, libraries,
pinned versions and agent tools each one used.

| task | prompt | stack we name in prefer mode |
|---|---|---|
| `poi_s2` | Convert all of the Mapbox POI data into S2 bounding-box coverings on a cluster | Spark |
| `hourly_data_quality` | An hourly job that flags data quality issues in an `orders` event table | Spark, Airflow |
| `cc_fraud_streaming` | A streaming job that looks for credit card fraud in card authorization events | Spark, Kafka |
| `health_appeal_synthetic` | A synthetic training set for fine-tuning a health insurance appeal model, seeded from Washington State data | Spark, vLLM |
| `cdc_scd2_dimension` | Debezium-style CDC events into an SCD Type 2 customer dimension | Spark, Delta Lake |
| `clickstream_sessionization` | Sessionize web clickstream (late data, login stitching, bots) into sessions, funnel and DAU tables | Spark |
| `nyc_taxi_lakehouse` | Bronze / silver / gold lakehouse over the full NYC TLC trip history, with schema drift and quarantine | Spark, Iceberg |
| `entity_resolution` | Deduplicate and link customer records across three sources with no shared ID, with ground-truth evaluation | Spark |
| `point_in_time_features` | Point-in-time correct churn features: leakage-free backfill plus a daily incremental that matches it | Spark |

## Modes

Every task runs in two modes. The prompts are identical apart from one `Tooling:` line.

- **float**: "pick whatever languages, engines and libraries you think fit best".
  Here we care about **which tools** the agent picks. The report shows primary
  language and library use per task.
- **prefer**: "use Spark and Kafka; that's our standard stack". Here we care about
  **which versions** it pins, and whether it actually used what we asked for. The
  report shows one version cell per (model, task), e.g. `Spark 3.5.1, Kafka unpinned`
  or `vLLM NOT USED`, plus each tool's version spread across all runs.

Both modes ask the agent to pin dependency versions.

## Styles: build vs ask

- **build** (default): the agent builds the whole thing, runs the tests, and we
  inspect the code and dependency files it wrote. Slow and expensive, but it shows
  what the agent actually does.
- **ask** (`--style ask`): much cheaper. The agent gets the same task prompt, plus a
  trailer telling it not to write anything and to reply with only JSON
  (`languages`, `components: [{name, version, role}]`, `rationale`). That answer
  goes through the same library and version detection, under an `ask-` variant
  (`ask-float`, `ask-prefer-spark+kafka`). The report marks those columns `[ask]`.
  What an agent says it would use and what it ends up using can differ, so
  `--style both` lets you compare the two.

Version caveat: for tools with a separate client library, the version recorded is
usually the client's (e.g. `confluent-kafka 2.6.1` shows up as `Kafka 2.6.1`).

## Adding a task

Drop in `prompts/<task>.md`; the file name is the task name. Put a `{tooling}` line
where the mode's instruction should go, and optionally a front-matter header with
the preferred tools:

```
---
prefer: Spark, Kafka
---
Create a streaming job that ...
{tooling}
- Work only in the current directory. ...
```

Preferred tools should be library labels from `LIBS` in `agent_bakeoff.py`
(case-insensitive). Otherwise the report can't tell whether the agent used them.

## Running

```bash
python3 agent_bakeoff.py --dry-run
```

```bash
python3 agent_bakeoff.py --print-prompts --tasks cc_fraud_streaming
```

```bash
python3 agent_bakeoff.py --out bakeoff --mode prefer --tasks cc_fraud_streaming --match 'sonnet|opus'
```

```bash
python3 agent_bakeoff.py --out bakeoff --mode prefer --prefer Flink,Kafka --tasks cc_fraud_streaming
```

```bash
python3 agent_bakeoff.py --out bakeoff-ask --style ask --models claude:haiku,claude:sonnet
```

- `--models` takes Cursor model names plus `cortex` and `claude`. Use
  `claude:<model>` (e.g. `claude:haiku`) to pick the Claude Code model.

- `--mode float|prefer|both` (default both). `--prefer` overrides every selected
  task's `prefer:` header and gets its own variant (`prefer-flink+kafka`), so it
  doesn't collide with default-preference runs.
- Output goes to `<out>/<task>/<variant>/<model>/` (the agent's workspace) and
  `<out>/<task>/<variant>/<model>.stream.jsonl` (its raw stream). The variant is
  `float` or `prefer-<tools>`.
- `<out>/report.md` has the prefer-mode version tables, the float-mode tool tables,
  then per-(task, variant) details. `<out>/results.json` has everything.
- `<out>/tally.csv` is the resume point: rerun with the same `--out` and any
  (task, variant, model) already in it is skipped. Tallies from before tasks and
  modes existed load as `poi_s2 / legacy` (the old "Spark preferred" POI prompt).

Compare two runs (e.g. to see how stable a model's choices are):

```bash
python3 -I compare_runs.py bakeoff-run1 bakeoff-run2
```

In prefer mode it compares the preferred tools' versions; in float mode it compares
primary language, Spark version and library set.
