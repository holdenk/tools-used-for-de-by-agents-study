---
prefer: Spark, Iceberg
---
Build a lakehouse over the full history of NYC TLC trip record data, with
bronze, silver and gold layers.

- Input: the public NYC Taxi & Limousine Commission trip records (yellow,
  green, FHV and high-volume FHV), published monthly as Parquet, plus the taxi
  zone lookup. Write a downloader that pulls months incrementally; if you
  can't reach the network, use a small hand-made sample in the same shapes and
  say so in the README.
- Scale: every month since 2009, a few billion trips and hundreds of GB, with
  schemas that drift across years and trip types (renamed columns, changed
  types, new fees). Don't assume any step fits on one machine.
- Bronze: the raw files as published, plus ingest metadata. Silver: one
  unified, typed trips table across all trip types and years, with data quality
  rules (negative fares, impossible durations or speeds, unknown zones,
  timestamps outside the file's month) that quarantine bad rows with a reason
  instead of dropping them. Gold: a star schema (trip fact; zone, date and
  vendor dims) and daily / zone-level aggregates for a dashboard.
- Adding a new month processes only that month, and restating a month (TLC
  republishes files) replaces it atomically.
- Include unit tests for schema unification and each quality rule, a README
  with the backfill and monthly-increment commands, and RUN the tests before
  you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
