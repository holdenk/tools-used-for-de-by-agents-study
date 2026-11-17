---
prefer: Spark
---
Build an entity resolution pipeline that deduplicates and links customer
records from several source systems that share no common ID.

- Input: customer records from three sources (CRM, e-commerce accounts,
  in-store loyalty program), each with its own schema: names (nicknames,
  typos, transliterations), emails, phones in mixed formats, postal addresses
  and dates of birth, all partially missing.
- Scale: about 500 million records that should resolve to roughly 200 million
  people, so comparing every pair is impossible. Don't assume any step fits on
  one machine.
- Standardize the fields, generate candidate pairs with blocking, score pairs
  (probabilistic or learned, your choice, but explain the thresholds), and
  cluster matches into entities transitively without letting a few bad links
  chain huge clusters together.
- Output: a record -> entity id mapping, a golden record per entity with
  survivorship rules (e.g. most recent or most trusted source per field), and
  the match evidence for audit. Entity ids stay stable across reruns when the
  underlying records haven't changed, and new records can be resolved
  incrementally.
- Include a synthetic data generator with known ground truth (duplicates with
  controlled corruption), tests, an evaluation that reports pair and cluster
  precision / recall / F1 against the ground truth, a README with the cluster
  submit command, and RUN the tests before you stop.
{tooling}
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
