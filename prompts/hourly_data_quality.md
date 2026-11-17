---
prefer: Spark, Airflow
---
Write an hourly job that flags data quality issues in a production event table,
and the schedule definition that runs it.

- Input: an append-only `orders` event table that lands every hour as Parquet
  under a path or S3/GCS prefix, partitioned by event date and hour. Columns:
  order_id, customer_id, event_ts, amount, currency, country, status,
  payment_method. The schema can drift without warning.
- Scale: billions of rows (several TB of Parquet) a day, with weeks of history
  behind the baselines, and each hourly run has to finish well inside its hour.
  Don't assume any step fits on one machine.
- Each run checks the latest completed hour (and can backfill any given hour)
  for: freshness / missing or late partitions, row-count anomalies vs the same
  hour in prior weeks, null and duplicate-key rates, schema drift,
  invalid / out-of-range values, and distribution shift in key columns vs a
  trailing baseline.
- Output: a durable table of flagged issues (run id, hour, check, severity,
  observed vs expected, sample offending keys) and an alert hook (a stub is
  fine) that fires on high severity. Thresholds are configurable and reruns of
  the same hour are idempotent.
- Include the scheduler definition, a synthetic sample dataset with injected
  issues, unit tests that assert every injected issue is flagged and clean
  hours are not, a README with the deploy / schedule steps, and RUN the tests
  before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
