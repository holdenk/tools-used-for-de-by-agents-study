---
prefer: Spark
---
Build a pipeline that backfills point-in-time correct training features for a
customer churn model and keeps them up to date daily.

- Input: append-only tables under a path or S3/GCS prefix for subscription
  events (start, plan change, cancel), product usage events, support tickets
  and billing invoices. Every row has an event time and an ingestion time, and
  late or corrected records arrive with a later ingestion time. Labels are a
  table of (customer_id, as_of_date, churned_within_30d).
- Scale: 50 million customers, three years of history and tens of billions of
  usage events; training sets need features at billions of
  (customer, as_of_date) points. Don't assume any step fits on one machine.
- Features: rolling 7 / 30 / 90 day usage counts and trends, ticket counts,
  days since last login, current plan, recent payment failures, each computed
  as of the label date using only data that was available by then (by both
  event time and ingestion time), so nothing leaks from the future.
- Output: a feature table keyed by customer and date, a training set that
  joins features to labels, and a daily incremental job that adds the newest
  date without recomputing history. A backfill and the daily job must produce
  identical values for the same date.
- Include a synthetic data generator with planted leakage traps (late
  corrections, future-dated events), tests that prove no feature uses data
  unavailable at its as_of time and that backfill == incremental, a README with
  the backfill and daily commands, and RUN the tests before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
