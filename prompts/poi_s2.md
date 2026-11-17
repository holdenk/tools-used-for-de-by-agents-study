---
prefer: Spark
---
Build a data pipeline that converts ALL of the Mapbox POI data into S2 bounding
boxes, running in parallel on a cluster.

- Input: Mapbox POI records (GeoJSON / Parquet / CSV, point or polygon
  geometry). Support a path or S3/GCS prefix, so it scales past one machine.
- Output: one row per POI with poi id, the S2 cell covering of its bounding box
  (cell ids as tokens, configurable min/max level and max cells), written
  partitioned by a coarse S2 cell so downstream spatial joins are cheap.
- Handle antimeridian crossing, poles, null/invalid geometry (quarantine, don't
  crash), and skew (dense city cells).
- Include a small synthetic sample dataset, unit tests, a README with the
  cluster submit command, and RUN the tests before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
