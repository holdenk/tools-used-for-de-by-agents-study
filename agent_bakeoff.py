#!/usr/bin/env python3
"""Run every prompt in prompts/ against each Cursor model (then Cortex Code and Claude Code).

Each (task, model) pair gets a fresh directory <out>/<task>/<model>/, runs with
--yolo and stream-json so we can report which tools it used, and is inspected
for the languages, libraries and pinned versions it picked. Writes
<out>/report.md and <out>/results.json, and keeps a CSV tally that a rerun
restores from, so no (task, model) pair runs twice.

A task is just a prompt file: prompts/<task>.md. Add a file to add a task.
"""
import argparse
import collections
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
LEGACY_TASK = "poi_s2"  # tallies/results from before tasks existed were all this prompt


def load_tasks(spec, prompts_dir):
    """task name -> prompt. spec: comma list of task names or prompt file paths; default every prompts/*.md."""
    if spec:
        files = [Path(s) if s.endswith((".md", ".txt")) else prompts_dir / f"{s}.md" for s in spec.split(",")]
    else:
        files = sorted(f for f in prompts_dir.glob("*.md") if f.name.lower() != "readme.md")
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        sys.exit(f"no such prompt file(s): {', '.join(missing)}")
    return {f.stem: f.read_text() for f in files}


def list_models():
    if not shutil.which("agent"):
        print("warning: Cursor `agent` CLI not on PATH; running only the non-Cursor extras", file=sys.stderr)
        return []
    out = subprocess.run(["agent", "--list-models"], capture_output=True, text=True, check=True).stdout
    return [m.group(1) for m in re.finditer(r"^(\S+) - ", out, re.M)]


# Non-Cursor CLIs; both emit Claude-style stream-json (tool_use content blocks).
EXTRAS = {
    "cortex": lambda prompt: ["cortex", "-p", prompt, "--output-format", "stream-json",
                              "--dangerously-allow-all-tool-calls"],
    "claude": lambda prompt: ["claude", "-p", "--dangerously-skip-permissions",
                              "--output-format", "stream-json", "--verbose", prompt],
}


LANGS = {".py": "Python", ".scala": "Scala", ".java": "Java", ".sql": "SQL", ".go": "Go",
         ".rs": "Rust", ".js": "JS", ".ts": "TS", ".kt": "Kotlin", ".r": "R", ".sh": "Shell",
         ".cpp": "C++", ".cc": "C++", ".cs": "C#"}
# regex over source + dependency files -> label. One list for every task so labels compare across tasks.
LIBS = {
    # compute engines
    r"pyspark|org\.apache\.spark|spark-sql|spark-core": "Spark",
    r"\breadStream\b|\bwriteStream\b|spark-sql-kafka": "Spark Structured Streaming",
    r"apache_beam|org\.apache\.beam": "Beam",
    r"pyflink|org\.apache\.flink|\bflink\b": "Flink",
    r"\bdask\b": "Dask", r"\bray\b[.\s]*(init|remote|data)|import ray": "Ray",
    r"polars": "polars", r"duckdb": "DuckDB", r"pandas": "pandas", r"pyarrow": "pyarrow", r"numpy": "numpy",
    r"\bbytewax\b": "Bytewax", r"\bfaust\b": "Faust", r"\bquixstreams\b": "Quix Streams",
    r"risingwave": "RisingWave", r"\bksql": "ksqlDB",
    # streaming transport + state
    r"kafka": "Kafka", r"kafka\.streams|\bKStream\b": "Kafka Streams", r"kinesis": "Kinesis",
    r"\bpulsar\b": "Pulsar", r"redpanda": "Redpanda", r"\bredis\b": "Redis", r"rocksdb": "RocksDB",
    # storage / warehouses / table formats
    r"delta-spark|deltalake|io\.delta|format\(\s*[\"']delta[\"']": "Delta Lake", r"iceberg": "Iceberg",
    r"\bhudi\b": "Hudi", r"snowflake": "Snowflake", r"bigquery": "BigQuery", r"psycopg|postgres": "Postgres",
    r"sqlalchemy": "SQLAlchemy", r"sqlite3?\b": "SQLite", r"boto3|s3a://|s3://": "S3", r"gcsfs|gs://": "GCS",
    # orchestration / scheduling
    r"airflow": "Airflow", r"dagster": "Dagster", r"prefect": "Prefect", r"\bdbt\b": "dbt",
    r"crontab|\bcron\b|@hourly": "cron", r"kubernetes|k8s": "Kubernetes", r"dataproc|emr\b": "managed Spark",
    # data quality / validation / alerting
    r"great_expectations|great-expectations": "Great Expectations", r"soda[-_.]core|from soda\b": "Soda",
    r"pydeequ|\bdeequ\b": "Deequ", r"pandera": "Pandera", r"pydantic": "pydantic", r"whylogs": "whylogs",
    r"evidently": "Evidently", r"\bscipy\b": "scipy", r"statsmodels": "statsmodels",
    r"slack_sdk|hooks\.slack\.com": "Slack", r"pagerduty": "PagerDuty", r"prometheus": "Prometheus",
    # geo
    r"sedona": "Sedona", r"\bs2sphere\b": "s2sphere", r"s2geometry|\bs2cell\b|\bpywraps2\b": "s2geometry",
    r"com\.google\.common\.geometry|S2RegionCoverer|S2CellId": "S2 Java/Scala",
    r"\bs2\b.*(rust|crate)|^s2\s*=|\bs2-?geo\b|\bs2::": "s2 (Rust)",
    r"\bs2geo\b|golang/geo": "s2 (Go)", r"\bh3\b": "H3", r"shapely": "shapely", r"geopandas": "geopandas",
    # ML / LLM / synthetic data
    r"sklearn|scikit-learn": "scikit-learn", r"xgboost": "XGBoost", r"lightgbm": "LightGBM",
    r"\btorch\b|pytorch": "PyTorch", r"\bonnx": "ONNX", r"mlflow": "MLflow",
    r"\btransformers\b": "HF transformers", r"from datasets import|import datasets\b": "HF datasets",
    r"huggingface_hub": "HF Hub", r"\bpeft\b": "PEFT", r"\btrl\b": "TRL", r"vllm": "vLLM",
    r"\bopenai\b": "OpenAI SDK", r"\banthropic\b": "Anthropic SDK", r"litellm": "LiteLLM",
    r"langchain": "LangChain", r"llama_index|llama-index": "LlamaIndex", r"\bdspy\b": "DSPy",
    r"ollama": "Ollama", r"tiktoken": "tiktoken", r"\bfaker\b": "Faker", r"\bsdv\b": "SDV",
    r"presidio": "Presidio", r"spacy": "spaCy", r"datasketch|minhash": "MinHash dedupe",
    r"rapidfuzz|thefuzz|fuzzywuzzy": "fuzzy matching",
    # ingest / parsing
    r"pypdf|pdfplumber|pdfminer|pymupdf|\bfitz\b": "PDF parsing", r"beautifulsoup|\bbs4\b": "BeautifulSoup",
    r"\blxml\b": "lxml", r"\brequests\b": "requests", r"\bhttpx\b": "httpx", r"scrapy": "Scrapy",
    r"tenacity|backoff": "retry lib",
    r"pytest|unittest|scalatest|junit": "tests",
}
SKIP = {".git", ".cursor", ".venv", "venv", "node_modules", "__pycache__", "target", ".pytest_cache",
        "build", "dist", "site-packages", ".bsp", ".idea", ".gradle"}  # build output + vendored deps


DEP_FILES = {"requirements.txt", "pyproject.toml", "build.sbt", "pom.xml", "build.gradle",
             "build.gradle.kts", "Cargo.toml", "go.mod", "package.json"}
PINS = [  # each yields (name, version)
    r"""^\s*["']?([A-Za-z][\w.\-]*)(?:\[[\w,]+\])?\s*(?:==|>=|~=|<=|<|>)\s*([\w.*]+)""",  # pip
    r'"[\w.\-]+"\s*%%?\s*"([\w.\-]+)"\s*%\s*"([\w.\-]+)"',                              # sbt
    r"<artifactId>([\w.\-]+)</artifactId>\s*<version>([^<]+)</version>",                # maven dep
    r"<([\w.\-]*version)>(\d[^<]*)</",                                                  # maven prop
    r"""["']([\w.\-]+:[\w.\-]+):(\d[\w.\-]*)["']""",                                    # gradle
    r'^\s*([\w\-]+)\s*=\s*"(\d[\w.\-]*)"',                                              # Cargo
    r"^\s*([\w./\-]+)\s+(v\d[\w.\-]*)",                                                 # go.mod
    r'"(@?[\w./\-]+)"\s*:\s*"[\^~]?(\d[\w.\-]*)"',                                      # package.json
]


def project_files(workdir):
    return [f for f in workdir.rglob("*") if f.is_file() and not SKIP & set(f.relative_to(workdir).parts)]


def pinned_versions(workdir):
    pins = {}
    for f in project_files(workdir):
        if f.name in DEP_FILES:
            text = f.read_text(errors="ignore")
            for rx in PINS:
                for name, ver in re.findall(rx, text, re.M):
                    pins.setdefault(name.lower(), ver)
    return pins


SPARK_VERSION_RX = [  # first hit wins; Python, then Scala/Java, then free-form (README, Dockerfile, submit scripts)
    r"""pyspark(?:\[[\w,]+\])?\s*(?:==|>=|~=|<=|<|>)\s*([\d][\w.]*)""",
    r"""['"]pyspark['"]\s*,?\s*(?:version\s*=\s*)?['"]?\s*[\^~=<>]*\s*(\d[\w.]*)""",
    r"""org\.apache\.spark["']?\s*%+\s*["']spark-[\w\-]+["']\s*%\s*["'](\d[\w.\-]*)["']""",  # sbt
    r"""org\.apache\.spark:spark-[\w\-.]+:(\d[\w.\-]*)""",                                   # gradle / --packages
    r"""<spark[\w.\-]*version>(\d[^<]*)</""",                                                # maven property
    r"""\bspark_?[vV]ersion\s*(?::=|=|:)\s*["'](\d[\w.\-]*)["']""",                          # sbt val / TOML / yaml
    r"""(?i)\bspark[\s\-_]*(?:version\s*)?v?(\d\.\d+\.\d+)""",                               # README / Dockerfile prose
]


def spark_version(workdir):
    text = ""
    for f in project_files(workdir):
        if f.name in DEP_FILES or f.name == "Dockerfile" or f.suffix in (".md", ".sh", ".yaml", ".yml", ".sbt", ".cfg", ".txt"):
            text += f.read_text(errors="ignore") + "\n"
    for rx in SPARK_VERSION_RX:
        m = re.search(rx, text)
        if m:
            return m.group(1)
    return None


def inspect_project(workdir):
    langs, libs = collections.Counter(), set()
    for f in project_files(workdir):
        lang = LANGS.get(f.suffix.lower())
        try:
            text = f.read_text(errors="ignore") if f.stat().st_size < 1_000_000 else ""
        except OSError:
            continue
        if lang:
            langs[lang] += text.count("\n") + 1
        if lang or f.name in DEP_FILES or f.name == "Dockerfile":
            libs.update(label for rx, label in LIBS.items() if re.search(rx, text, re.I | re.M))
    return dict(langs.most_common()), sorted(libs)


def summarize(stream, workdir):
    """Cursor: `started` tool_call events (key = tool). Others: tool_use blocks."""
    tools, shell_cmds, result = collections.Counter(), [], {}
    for line in stream.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "tool_call" and ev.get("subtype") == "started":
            for name, body in ev["tool_call"].items():
                if name.endswith("ToolCall"):
                    tools[name[: -len("ToolCall")]] += 1
                    if name == "shellToolCall":
                        shell_cmds.append(body.get("args", {}).get("command", ""))
        elif ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools[c["name"].lower()] += 1
                    if c["name"].lower() in ("bash", "shell"):
                        shell_cmds.append(c.get("input", {}).get("command", ""))
        elif ev.get("type") == "result":
            result = ev
    langs, libs = inspect_project(workdir)
    return {
        "languages": langs,
        "libraries": libs,
        "versions": pinned_versions(workdir),
        "spark_version": spark_version(workdir) if "Spark" in libs else None,
        "tools": dict(tools.most_common()),
        "shell_commands": shell_cmds,
        "ok": bool(result) and not result.get("is_error", True),
        "api_ms": result.get("duration_api_ms"),
        "cost_usd": result.get("total_cost_usd"),
        "usage": result.get("usage", {}),
        "files": len(project_files(workdir)),
        "final": (result.get("result") or "")[-600:],
    }


def run_one(task, model, root, prompt, timeout):
    wd = root / task / model
    shutil.rmtree(wd, ignore_errors=True)  # partial dir from a crashed run
    wd.mkdir(parents=True)
    if model in EXTRAS:
        cmd = EXTRAS[model](prompt)
    else:
        cmd = ["agent", "-p", "--yolo", "--trust", "--model", model,
               "--output-format", "stream-json", "--workspace", str(wd), prompt]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=wd, capture_output=True, text=True, timeout=timeout)
        stream, err, status = p.stdout, p.stderr, "exit %d" % p.returncode
    except subprocess.TimeoutExpired as e:
        stream = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, status = "", "TIMEOUT"
    (wd.parent / f"{model}.stream.jsonl").write_text(stream)
    return {"task": task, "model": model, "status": status, "wall_s": round(time.time() - t0),
            **summarize(stream, wd), "stderr": err[-400:]}


def primary_language(r):
    return next(iter(r["languages"]), "-")


def report(results):
    tasks = list(dict.fromkeys(r["task"] for r in results))
    by_task = {t: [r for r in results if r["task"] == t] for t in tasks}
    lines = ["# Agent DE bakeoff", "", f"tasks: {', '.join(tasks)}", ""]

    # cross-task views first: what each model reached for, and how popular each library is per task
    models = list(dict.fromkeys(r["model"] for r in results))
    cell = {(r["task"], r["model"]): r for r in results}
    lines += ["## Primary language by model and task", "",
              "| model | " + " | ".join(tasks) + " |", "|---" * (len(tasks) + 1) + "|"]
    for m in models:
        lines.append(f"| {m} | " + " | ".join(
            primary_language(cell[t, m]) if (t, m) in cell else "" for t in tasks) + " |")
    counts = {t: collections.Counter(lib for r in by_task[t] for lib in r["libraries"]) for t in tasks}
    libs = sorted({lib for c in counts.values() for lib in c}, key=lambda l: -sum(c[l] for c in counts.values()))
    lines += ["", "## Library use by task (models using it / models run)", "",
              "| library | " + " | ".join(tasks) + " |", "|---" * (len(tasks) + 1) + "|"]
    for lib in libs:
        lines.append(f"| {lib} | " + " | ".join(
            f"{counts[t][lib]}/{len(by_task[t])}" if counts[t][lib] else "" for t in tasks) + " |")

    for t in tasks:
        lines += ["", f"## {t}", "",
                  "| model | status | wall s | files | languages (LOC) | libraries | Spark | pinned versions |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in by_task[t]:
            langs = ", ".join(f"{k} {v}" for k, v in r["languages"].items()) or "-"
            pins = ", ".join(f"{k} {v}" for k, v in list(r["versions"].items())[:12]) or "-"
            lines.append(f"| {r['model']} | {r['status']} | {r['wall_s']} | {r['files']} "
                         f"| {langs} | {', '.join(r['libraries']) or '-'} "
                         f"| {('Spark ' + (r['spark_version'] or '(unpinned)')) if 'Spark' in r['libraries'] else 'no'} "
                         f"| {pins} |")
        for r in by_task[t]:
            lines += ["", f"### {t} / {r['model']}", f"languages: {r['languages']}", f"libraries: {r['libraries']}",
                      f"versions: {r['versions']}",
                      f"agent tools: {r['tools']}", f"usage: {r['usage']}"]
            lines += [f"- `{c[:160]}`" for c in r["shell_commands"][:15]]
            if r["final"]:
                lines += ["", "final message tail:", "", "> " + r["final"].replace("\n", "\n> ")]
            if r["stderr"].strip():
                lines += ["", "stderr tail:", "", "    " + r["stderr"].strip().replace("\n", "\n    ")]
    return "\n".join(lines) + "\n"


PLAIN = ("task", "model", "status")  # every other column is JSON-encoded


def load_tally(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = [{k: v if k in PLAIN else (json.loads(v) if v else None) for k, v in row.items()}
                for row in csv.DictReader(f)]
    for r in rows:
        r["task"] = r.get("task") or LEGACY_TASK
    return rows


def write_tally(path, results):
    """Rewrite the whole tally (it's small) so a column added later never misaligns old rows."""
    fields = list(dict.fromkeys(k for r in results for k in r))
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: v if k in PLAIN else json.dumps(v) for k, v in r.items()})
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="bakeoff",
                    help="run root; each (task, model) gets a fresh <out>/<task>/<model>. Rerun with the same dir to resume")
    ap.add_argument("--csv", help="running tally (default <out>/tally.csv); (task, model) pairs already in it are skipped")
    ap.add_argument("--tasks", help="comma-separated task names (prompts/<name>.md) or prompt file paths; default all")
    ap.add_argument("--prompts-dir", default=str(PROMPTS_DIR), help="where task prompts live (default %(default)s)")
    ap.add_argument("--models", help="comma-separated; default is every model from `agent --list-models`")
    ap.add_argument("--match", help="regex filter on the model list (e.g. 'sonnet|opus')")
    ap.add_argument("--no-extras", action="store_true", help="skip the cortex + claude runs at the end")
    ap.add_argument("--timeout", type=int, default=3600, help="per-run seconds")
    ap.add_argument("--dry-run", action="store_true", help="print the (task, model) pairs that would run")
    a = ap.parse_args()

    tasks = load_tasks(a.tasks, Path(a.prompts_dir))
    root = Path(a.out).resolve()
    tally = Path(a.csv) if a.csv else root / "tally.csv"
    results = load_tally(tally)
    done = {(r["task"], r["model"]) for r in results}

    models = a.models.split(",") if a.models else list_models()
    if not a.no_extras and not a.models:
        models += list(EXTRAS)
    if a.match:
        models = [m for m in models if re.search(a.match, m)]
    todo = [(t, m) for t in tasks for m in models]
    if a.dry_run:
        for t, m in todo:
            print(f"{t}\t{m}" + ("\t(done)" if (t, m) in done else ""))
        return 0

    root.mkdir(parents=True, exist_ok=True)
    for i, (t, m) in enumerate(todo, 1):
        if (t, m) in done:
            print(f"[{i}/{len(todo)}] {t} / {m} already in {tally}, skipping", flush=True)
            continue
        print(f"[{i}/{len(todo)}] {t} / {m} ...", flush=True)
        r = run_one(t, m, root, tasks[t], a.timeout)
        print(f"    {r['status']} {r['wall_s']}s langs={list(r['languages'])} libs={r['libraries']}", flush=True)
        results.append(r)
        write_tally(tally, results)
        (root / "results.json").write_text(json.dumps(results, indent=1))
        (root / "report.md").write_text(report(results))
    print(f"\nDONE. {root}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
