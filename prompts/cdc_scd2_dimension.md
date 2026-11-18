---
prefer: Spark, Delta Lake
---
Build a pipeline that turns change-data-capture events from an OLTP database
into a slowly changing dimension (SCD Type 2) table in a data lake.

- Input: Debezium-style JSON change events (before / after images, op =
  c/u/d/r, source commit timestamp and LSN) for a `customers` table, landing
  under a path or S3/GCS prefix (or on a Kafka topic), plus an initial full
  snapshot.
- Scale: hundreds of millions of customers in the dimension and tens of
  millions of change events a day, with bursts after bulk updates in the
  source. Don't assume any step fits on one machine.
- Output: a `dim_customer` table with surrogate key, natural key, the tracked
  attributes, valid_from / valid_to, is_current and a deleted flag, plus a
  current-state view. A few columns are Type 1 (overwritten in place on every
  version); the rest are Type 2.
- Handle out-of-order and duplicate events (order by LSN, not arrival time),
  several changes to the same key in one batch, deletes and re-inserts,
  updates that don't touch tracked columns (no new version), and a column
  added to the source table.
- Each batch is atomic, reprocessing a batch is idempotent, and the dimension
  can be rebuilt from the snapshot plus the full change history.
- Include a synthetic snapshot + change-event generator, tests that check the
  history of tricky keys (same-batch updates, late events, delete then
  re-insert), a README with the run / schedule command, and RUN the tests
  before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
