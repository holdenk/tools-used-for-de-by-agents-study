---
prefer: Spark, Raincloud
---
Build a pipeline that turns daily weather-station observations into climate
normals, anomalies and heat waves.

- Input: the Raincloud (https://github.com/spiraldb/raincloud) dataset
  `ghcn-daily`: NOAA's Global Historical Climatology Network daily
  observations, one row per (station_id, date, element) with an integer
  `value` and GHCN's mflag / qflag / sflag columns. Elements include TMAX and
  TMIN (tenths of °C), PRCP (tenths of mm), SNOW and SNWD (mm), plus many
  rarer ones. A non-empty qflag means the value failed a NOAA quality check.
  The first two characters of station_id are the country code; station names
  and coordinates are in NOAA's separate ghcnd-stations.txt. In production the
  files come from our team's Raincloud mirror (`RAINCLOUD_MIRROR`, an `s3://`
  prefix); locally you can build them with Raincloud.
- Scale: 3.19 billion rows from about 125,000 stations, going back to 1763
  (5.3 GB as Parquet, 31 GB as Arrow in memory), and NOAA adds and revises
  recent days every day. The pipeline runs a full rebuild and a daily
  incremental on our shared cluster. Don't assume any step fits on one
  machine.
- Quality: flag (don't silently drop) values that failed NOAA QC, physically
  implausible values and days where TMIN > TMAX, with a reason code; only
  unflagged values feed the outputs.
- Output: a daily per-station table (TMAX, TMIN, PRCP, SNOW, SNWD in °C and
  mm, one row per station-day); monthly per-station summaries with a
  completeness flag when more than 5 days are missing; 1991-2020 daily normals
  per station, smoothed across days of the year, for stations with at least
  25 years of data in that window; daily TMAX / TMIN anomalies against those
  normals; heat waves: runs of 3 or more consecutive days where TMAX exceeds
  the station's 1991-2020 90th percentile for that calendar day, with start,
  end and peak; and a yearly mean anomaly per country.
- The daily incremental recomputes only what changed (revised values update
  the affected outputs), rerunning any day is idempotent, and a full rebuild
  produces the same results.
- Include tests on a small fixture with the same schema and known heat waves,
  failed-QC values and gaps, a README with the deploy / schedule command, and
  RUN the tests before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
