---
prefer: Spark
---
Build a pipeline that sessionizes raw web clickstream events and produces
daily product analytics tables.

- Input: JSON clickstream events (event_id, anonymous_id, user_id once logged
  in, event_ts, event_type, page_url, referrer, utm params, user_agent, ip)
  landing hourly under a path or S3/GCS prefix, partitioned by arrival hour.
  Events can arrive up to 48 hours late and some are duplicated.
- Scale: about 5 billion events (several TB) a day with a heavy tail: bots and
  a few power users produce millions of events each. Don't assume any step
  fits on one machine.
- Sessions: 30-minute inactivity timeout, split at midnight UTC and when the
  campaign (UTM source) changes. Stitch anonymous_id to user_id once a visitor
  logs in, including their earlier anonymous sessions.
- Output, partitioned by date: a sessions table (session id, user, start / end,
  duration, page count, landing and exit page, traffic source), a daily funnel
  table (view -> add to cart -> checkout -> purchase), and bot-filtered daily
  active users. Filter bots by user agent and by behaviour.
- Late events update the affected days' outputs, and rerunning any day is
  idempotent.
- Include a synthetic event generator with known sessions, tests that check
  exact session boundaries (timeout, midnight, UTM change, login stitching,
  late and duplicate events), a README with the run / schedule command, and
  RUN the tests before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
