---
prefer: Spark, vLLM
---
Generate a synthetic training set for fine-tuning a model that writes health
insurance appeal letters, using Washington State data as the input.

- Input: public Washington State health insurance appeal data, such as the WA
  Office of the Insurance Commissioner's independent review organization (IRO)
  decisions, plus any other public WA data you think helps. Write a
  reproducible downloader + parser; if you can't reach the network, build the
  parser against a small hand-made sample in the same shape and say so in the
  README.
- Extract structured facts from each decision (denied service, diagnosis,
  denial reason category, plan / insurer type, outcome, reviewer rationale) and
  use them to seed synthetic examples: patient context + denial letter ->
  appeal letter, grounded in the reasoning that made overturned denials
  succeed. Generate with an LLM behind a pluggable interface, with a
  deterministic offline stub for tests.
- No real PHI: the source is de-identified, but scrub and verify anyway, and
  make sure no synthetic patient can be traced back to a single real case.
- Quality: exact and near-duplicate removal, filter low-quality or
  hallucinated generations (e.g. the treatment and diagnosis cited must match
  the seed), balance across denial categories and outcomes, and hold out an
  eval split with no source-case leakage between train and eval.
- It has to scale to regenerating tens of thousands of examples (batching,
  retries, rate limits, resumable).
- Output: train / eval JSONL in a common chat fine-tuning format and a dataset
  card (sources, licenses, counts, known biases and gaps).
- Include unit tests, a README with how to run it end to end, and RUN the
  tests before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
