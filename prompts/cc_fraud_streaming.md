---
prefer: Spark, Kafka
---
Create a streaming job that looks for credit card fraud in real time.

- Input: a stream of card authorization events (JSON on a Kafka topic or
  similar) with txn_id, card_id, merchant_id, mcc, amount, currency, event_ts,
  lat/lon, country, card_present, device_id. Events arrive out of order and
  late, at tens of thousands per second at peak, so the job has to scale
  horizontally.
- Flag transactions, with reason codes, for at least: velocity (too many
  authorizations per card in a short window), amount spikes vs the card's own
  history, impossible travel between consecutive transactions, card testing
  (bursts of small authorizations across merchants), and a pluggable model
  score hook.
- Use event time with watermarks, keep per-card state bounded (TTL), make the
  alert output exactly-once or idempotent (dedupe key), and survive restarts
  via checkpointing.
- Output: an alerts topic/table (txn id, card id, score, reasons, event time,
  detection time) and a sink of every scored transaction for later model
  training.
- Include a local event generator that injects known fraud patterns, tests that
  run without a real cluster and assert injected fraud is caught and normal
  traffic isn't, a README with the deploy / submit command, and RUN the tests
  before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
