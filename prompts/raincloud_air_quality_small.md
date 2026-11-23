---
prefer: Spark, Raincloud
---
Build a pipeline that turns hourly monitoring-station readings into daily air
quality tables and pollution episodes.

- Input: the Raincloud (https://github.com/spiraldb/raincloud) dataset
  `uci-beijing-multi-site-air-quality`: hourly readings from 12 Beijing
  monitoring stations, March 2013 to February 2017. One row per (station,
  hour) with year, month, day, hour (Beijing local time), PM2.5, PM10, SO2,
  NO2, CO, O3 (µg/m³), TEMP, PRES, DEWP, RAIN, wind direction `wd`, wind speed
  `WSPM` and `station`. Readings are missing in places. In production the file
  comes from our team's Raincloud mirror (`RAINCLOUD_MIRROR`, an `s3://`
  prefix); locally you can build it with Raincloud.
- Scale: 420,768 rows (under 6 MB as Parquet). The pipeline reruns from
  scratch whenever the dataset is refreshed, on one small VM (2 vCPUs, 8 GB
  RAM).
- Quality: build real timestamps, make every missing station-hour explicit,
  and flag (don't silently drop) duplicates, physically implausible values and
  flat-lined sensors (the same reading for many hours in a row), with a reason
  code.
- Output, by Beijing local calendar day: a cleaned hourly table with the
  quality flags; a daily per-station table (mean and max PM2.5 and PM10, a
  completeness flag when fewer than 18 of 24 hours are valid, and the daily US
  EPA PM2.5 AQI and category, using the current breakpoints); daily mean PM2.5
  anomalies against each station's same-calendar-month baseline; and pollution
  episodes: runs of 3 or more consecutive days where the daily mean PM2.5
  exceeds 75 µg/m³ at a majority of stations, with start, end, peak and the
  stations affected.
- Rerunning is idempotent.
- Include tests on a small fixture with known gaps, duplicates, bad values and
  an injected episode, a README with the run / schedule command, RUN the
  tests, and run the whole pipeline once on the real dataset before you stop.
  If you can't build the data, say so in the README.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
