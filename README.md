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
| `raincloud_tpch_mart_small` | TPC-H SF1 (8.7M rows) from Raincloud into a sales mart, on one small VM | Spark, Raincloud |
| `raincloud_tpch_mart_large` | The same mart at TPC-H SF100 (866M rows), on a cluster | Spark, Raincloud |
| `raincloud_air_quality_small` | Hourly Beijing air quality (420K rows) into daily AQI tables and pollution episodes | Spark, Raincloud |
| `raincloud_ghcn_climate_large` | NOAA GHCN-Daily (3.19B rows) into climate normals, anomalies and heat waves | Spark, Raincloud |

## Small vs large: the Raincloud tasks

The other tasks are all "doesn't fit on one machine". The `raincloud_*` tasks come
in small / large pairs, so we can see whether an agent's stack follows the data size.
Their inputs are real datasets from [Raincloud](https://github.com/spiraldb/raincloud),
so the sizes in the prompts come from its catalog rather than being made up.

- **TPC-H mart** (`_small` / `_large`): the prompts differ only in the input and
  scale bullets (SF1 on a 4 vCPU VM vs SF100 on a cluster). Both runs smoke-test on
  real SF1 data, so scale is the only variable.
- **Station time series** (`raincloud_air_quality_small` / `raincloud_ghcn_climate_large`):
  the same pipeline shape (quality flags, daily per-station rollups, baselines and
  anomalies, multi-day episodes) on two datasets about 7,600× apart in row count. The
  small one runs end to end on the real data; the large one runs only against
  fixtures, since building `ghcn-daily` means a multi-GB NOAA download.

All four name Spark and Raincloud in prefer mode, so a small task in prefer mode asks
how a Spark shop handles small data. Use `--prefer DuckDB,Raincloud` (or similar) to
ask the other way round. Raincloud isn't on PyPI, so agents usually pin it as a git
ref. The report records it from `raincloud @ git+…@<ref>`, bare `git+…@<ref>` lines
and `{ git = …, tag / rev = … }` tables (uv, Poetry, Cargo). A ref-less git
dependency or a branch shows as `unpinned`.

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
  (`languages: [{name, version}]`, `components: [{name, version, role}]`,
  `rationale`), with an exact version for every language and component. That answer
  goes through the same library and version detection, under an `ask-` variant
  (`ask-float`, `ask-prefer-spark+kafka`). The report marks those columns `[ask]`.
  What an agent says it would use and what it ends up using can differ, so
  `--style both` lets you compare the two.

Version caveat: for tools with a separate client library, the version recorded is
usually the client's (e.g. `confluent-kafka 2.6.1` shows up as `Kafka 2.6.1`).

## Repeats and convergence

One run per model shows what a model picked once, not what it usually picks.
`--repeats N` runs every (task, variant, model) N times. Repeat 1 runs everywhere
first, so a run cut short still covers every cell. The report then adds a
**Convergence** table per (task, variant):

- **top stack**: the most common exact library set, and its share of runs;
- **effective stacks**: 2^entropy of the library-set distribution (1 means every
  run picked the same set);
- **Jaccard within model / across models**: mean library-set overlap between
  repeats of one model, and between different models. If `across` is about as high
  as `within`, the models share a default rather than each having its own;
- **top answer**: the most common primary language (float) or preferred-tool
  versions (prefer).

It also lists the libraries that at least half the float runs of *every* task picked:
a default that doesn't depend on the task. The `tests` label is left out of all of
these, since every prompt asks for tests.

This borrows its method from Hamilton & Mimno, [*Elias in the Lighthouse, Again?
Diagnosing Low Diversity in LLM Stories*](https://arxiv.org/abs/2605.26492) (2026).
They sampled five near-identical story prompts 1,000 times each on four models and
found a handful of names, jobs and settings ("Elias", "keeper", "lighthouse") in
88.3% of 20,000 stories, at rates far above human-written fiction. They kept a word
only if at least half the models produced it. Our questions are the same: is there
an "Elias in the Lighthouse" stack or Spark version that agents keep landing on,
across models and tasks? How far is it from what the task needs and from what
people actually use?

Build runs are expensive, so for large N use `--style ask`, the same way the paper
used small models to afford more samples. Each run also records when it started
(`started`) and the model the CLI reported (`served_model`, e.g. what plain
`claude` resolved to), so a pin can later be compared against releases on that day
and against the model's training cutoff.

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

```bash
python3 agent_bakeoff.py --out bakeoff-ask --style ask --mode float --repeats 20 --tasks raincloud_tpch_mart_small,raincloud_tpch_mart_large
```

- `--models` takes Cursor model names plus `cortex` and `claude`. Use
  `claude:<model>` (e.g. `claude:haiku`) to pick the Claude Code model.

- `--mode float|prefer|both` (default both). `--prefer` overrides every selected
  task's `prefer:` header and gets its own variant (`prefer-flink+kafka`), so it
  doesn't collide with default-preference runs.
- Output goes to `<out>/<task>/<variant>/<model>/` (the agent's workspace) and
  `<out>/<task>/<variant>/<model>.stream.jsonl` (its raw stream). The variant is
  `float` or `prefer-<tools>`. Repeats after the first go to `<model>.rep<N>/`.
- `<out>/report.md` has the prefer-mode version tables, the float-mode tool tables,
  the convergence table, then per-(task, variant) details. With repeats, a pivot
  cell lists each distinct answer with its count (`Spark 3.5.1 ×3; Spark 4.0.1 ×1`).
  `<out>/results.json` has everything.
- `<out>/tally.csv` is the resume point: rerun with the same `--out` and any
  (task, variant, model, repeat) already in it is skipped, so raising `--repeats`
  adds only the new repeats. Rows from before `--repeats` count as repeat 1.
  Tallies from before tasks and modes existed load as `poi_s2 / legacy` (the old
  "Spark preferred" POI prompt).

Compare two runs (e.g. to see how stable a model's choices are):

```bash
python3 -I compare_runs.py bakeoff-run1 bakeoff-run2
```

It pairs runs by (task, variant, model, repeat). In prefer mode it compares the
preferred tools' versions; in float mode it compares primary language, Spark
version and library set.

## References

- Sil Hamilton and David Mimno. 2026. *Elias in the Lighthouse, Again? Diagnosing
  Low Diversity in LLM Stories.* arXiv:2605.26492. <https://arxiv.org/abs/2605.26492>
  The source of the repeated-sampling and convergence measures above.
- [Raincloud](https://github.com/spiraldb/raincloud), SpiralDB's harness for data
  management research datasets. The `raincloud_*` tasks take their inputs, sizes
  and schemas from its catalog.
