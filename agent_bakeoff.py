#!/usr/bin/env python3
"""Run every prompt in prompts/ against each Cursor model (then Cortex Code and Claude Code).

Two modes per task:
  float   - the agent picks its own stack; we mostly care WHICH tools it picks.
  prefer  - we name the tools (the task's `prefer:` header, or --prefer); we mostly
            care which VERSIONS it pins, and whether it actually used them.

Two styles:
  build   - the agent builds the whole thing; we inspect the code it wrote.
  ask     - cheaper: same prompt, but the agent only answers (as JSON) which languages,
            libraries and versions it WOULD use, and we inspect that answer.

Each (task, variant, model) gets a fresh directory <out>/<task>/<variant>/<model>/
(variant is `float` or `prefer-<tools>`, prefixed `ask-` for ask style), runs with --yolo and stream-json so we can
report which tools it used, and is inspected for languages, libraries and versions.
Writes <out>/report.md and <out>/results.json, and keeps a CSV tally that a rerun
restores from, so no (task, variant, model) runs twice.

A task is just a prompt file: prompts/<task>.md, with a `{tooling}` line the mode
fills in and an optional `prefer: Tool, Tool` front-matter header.
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
LEGACY = "legacy"  # tally/results rows from before modes existed (old POI prompt: "Spark preferred")
LEGACY_TASK = "poi_s2"
MODES = ("float", "prefer")
TOOLING = {  # replaces the {tooling} line; everything else in the prompt is identical between modes
    "float": "- Tooling: pick whatever languages, engines and libraries you think fit\n"
             "  best, and justify the choices in the README. Pin the versions of\n"
             "  everything you depend on.",
    "prefer": "- Tooling: use {tools}; that's our standard stack.\n"
              "  Pin the versions of everything you depend on.",
}


STYLES = ("build", "ask")
ASK = """{task}
---
Ignore the instructions above about writing code, tests or a README and about
running anything: don't create any files or run any commands. This is a planning
question only. Tell me exactly what you WOULD use to build the above: every
language, engine, library, service, scheduler, storage format and test tool,
with the exact version you would pin. Reply with only a JSON object shaped like:

{"languages": ["<language name>", ...],
 "components": [{"name": "<package or product>", "version": "<exact version>", "role": "<what it's for>"}, ...],
 "rationale": "<one short paragraph>"}
"""


def parse_prompt(text):
    """-> (prefer list, body). Front matter is a leading ---/--- block of `key: value` lines."""
    prefer = []
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if m:
        text = text[m.end():]
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "prefer":
                prefer = [t.strip() for t in v.split(",") if t.strip()]
    return prefer, text


def load_tasks(spec, prompts_dir):
    """task name -> (prefer, body). spec: comma list of task names or prompt file paths; default every prompts/*.md."""
    if spec:
        files = [Path(s) if s.endswith((".md", ".txt")) else prompts_dir / f"{s}.md" for s in spec.split(",")]
    else:
        files = sorted(f for f in prompts_dir.glob("*.md") if f.name.lower() != "readme.md")
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        sys.exit(f"no such prompt file(s): {', '.join(missing)}")
    return {f.stem: parse_prompt(f.read_text()) for f in files}


def join_tools(tools):
    return ", ".join(tools[:-1]) + " and " + tools[-1] if len(tools) > 1 else "".join(tools)


def render(body, mode, tools):
    line = TOOLING[mode].replace("{tools}", join_tools(tools))
    if "{tooling}" in body:
        return body.replace("{tooling}", line)
    return body.rstrip("\n") + "\n" + line + "\n"


def variant_name(style, mode, tools):
    v = "float" if mode == "float" else \
        "prefer-" + "+".join(re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") for t in tools)
    return "ask-" + v if style == "ask" else v


def list_models():
    if not shutil.which("agent"):
        print("warning: Cursor `agent` CLI not on PATH; running only the non-Cursor extras", file=sys.stderr)
        return []
    out = subprocess.run(["agent", "--list-models"], capture_output=True, text=True, check=True).stdout
    return [m.group(1) for m in re.finditer(r"^(\S+) - ", out, re.M)]


# Non-Cursor CLIs; both emit Claude-style stream-json (tool_use content blocks).
# `claude:<model>` (e.g. claude:haiku) picks the Claude Code model; plain `claude` uses its default.
EXTRAS = ("cortex", "claude")


def is_extra(spec):
    return spec.partition(":")[0] in EXTRAS


def extra_cmd(spec, prompt):
    name, _, model = spec.partition(":")
    if name == "claude":
        return ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
                *(["--model", model] if model else []), prompt]
    if model:
        raise ValueError(f"{spec}: only claude takes a :<model> suffix")
    return ["cortex", "-p", prompt, "--output-format", "stream-json", "--dangerously-allow-all-tool-calls"]


LANGS = {".py": "Python", ".scala": "Scala", ".java": "Java", ".sql": "SQL", ".go": "Go",
         ".rs": "Rust", ".js": "JS", ".ts": "TS", ".kt": "Kotlin", ".r": "R", ".sh": "Shell",
         ".cpp": "C++", ".cc": "C++", ".cs": "C#"}
# regex over source + dependency files -> label. One list for every task so labels compare across tasks.
LIBS = {
    # compute engines
    r"pyspark|org\.apache\.spark|spark-sql|spark-core|\bspark\s+sql\b": "Spark",
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
    # an import or a pip dependency, not Maven Shade's <transformers> tag
    r"^\s*(?:from|import)\s+transformers\b|^\s*[\"']?transformers[\"']?\s*(?:[=<>~!,\]]|$)": "HF transformers", r"from datasets import|import datasets\b": "HF datasets",
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
    # entity resolution / graphs / CDC / feature stores / query engines
    r"splink": "Splink", r"zingg": "Zingg", r"^\s*(?:from|import)\s+dedupe\b|^\s*[\"']?dedupe[\"']?\s*(?:[=<>~!,\]]|$)": "dedupe", r"recordlinkage": "recordlinkage",
    r"graphframes": "GraphFrames", r"networkx": "networkx", r"debezium": "Debezium", r"\bfeast\b": "Feast",
    r"hopsworks|\bhsfs\b": "Hopsworks", r"\btrino\b|presto": "Trino/Presto",
    r"chispa": "chispa", r"phonenumbers": "phonenumbers", r"usaddress|libpostal": "address parsing",
    r"jellyfish": "jellyfish", r"superset": "Superset",
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




LABELS = {v.lower(): v for v in LIBS.values()}
# label -> package / artifact / property names it gets pinned under (beyond the label itself and apache-<label>)
VERSION_ALIASES = {
    "Flink": ["apache-flink", "flink-streaming-java", "flink-clients", "flink.version"],
    "Kafka": ["confluent-kafka", "kafka-python", "kafka-python-ng", "aiokafka", "kafka-clients", "kafka.version"],
    "Kafka Streams": ["kafka-streams"],
    "Airflow": ["apache-airflow", "airflow.version"],
    "Beam": ["apache-beam", "beam-sdks-java-core", "beam.version"],
    "Great Expectations": ["great-expectations"], "Soda": ["soda-core"], "Deequ": ["pydeequ", "deequ"],
    "Delta Lake": ["delta-spark", "deltalake", "delta-core"], "Iceberg": ["pyiceberg", "iceberg-spark-runtime"],
    "Sedona": ["apache-sedona", "sedona-spark-shaded"], "PyTorch": ["torch"],
    "HF transformers": ["transformers"], "HF datasets": ["datasets"], "HF Hub": ["huggingface-hub"],
    "OpenAI SDK": ["openai"], "Anthropic SDK": ["anthropic"], "LangChain": ["langchain"],
    "Quix Streams": ["quixstreams"], "GraphFrames": ["graphframes"], "Splink": ["splink"],
}


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


def docs_text(workdir):
    """Dependency files plus free-form places versions get written down (README, Dockerfile, submit scripts)."""
    return "\n".join(f.read_text(errors="ignore") for f in project_files(workdir)
                     if f.name in DEP_FILES or f.name == "Dockerfile"
                     or f.suffix in (".md", ".sh", ".yaml", ".yml", ".sbt", ".cfg", ".txt"))


SPARK_VERSION_RX = [  # first hit wins; Python, then Scala/Java, then free-form (README, Dockerfile, submit scripts)
    r"""pyspark(?:\[[\w,]+\])?\s*(?:==|>=|~=|<=|<|>)\s*([\d][\w.]*)""",
    r"""['"]pyspark['"]\s*,?\s*(?:version\s*=\s*)?['"]?\s*[\^~=<>]*\s*(\d[\w.]*)""",
    r"""org\.apache\.spark["']?\s*%+\s*["']spark-[\w\-]+["']\s*%\s*["'](\d[\w.\-]*)["']""",  # sbt
    r"""org\.apache\.spark:spark-[\w\-.]+:(\d[\w.\-]*)""",                                   # gradle / --packages
    r"""<spark[\w.\-]*version>(\d[^<]*)</""",                                                # maven property
    r"""\bspark_?[vV]ersion\s*(?::=|=|:)\s*["'](\d[\w.\-]*)["']""",                          # sbt val / TOML / yaml
    r"""(?i)\bspark[\s\-_]*(?:version\s*)?v?(\d\.\d+\.\d+)""",                               # README / Dockerfile prose
]


def spark_version(text):
    for rx in SPARK_VERSION_RX:
        m = re.search(rx, text)
        if m:
            return m.group(1)
    return None


def _norm(name):
    """'org.apache.flink:flink-streaming-java_2.12' -> 'flinkstreamingjava'"""
    name = name.lower().split(":")[-1]
    return re.sub(r"[^a-z0-9]", "", re.sub(r"_2\.\d+$", "", name))


def tool_version(label, pins, docs, spark_v):
    """Best guess at the version an agent used for a LIBS label: dependency pins first, then prose."""
    if label in ("Spark", "Spark Structured Streaming"):
        return spark_v
    names = [label, "apache-" + label] + VERSION_ALIASES.get(label, [])
    wanted = {_norm(n) for n in names}
    for pin, ver in pins.items():
        if _norm(pin) in wanted:
            return ver
    for n in names:
        m = re.search(rf"(?i)\b{re.escape(n)}[\s\-_]*(?:version\s*)?v?(\d+\.\d+(?:\.\d+)?)\b", docs)
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


def parse_answer(text):
    """The JSON object from an ask-style final message (bare, or in a ```json fence), or None."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    if "{" in text:
        candidates.append(text[text.index("{"):])
    for c in candidates:
        try:
            ans, _ = json.JSONDecoder().raw_decode(c)  # ignores trailing junk after the object
        except ValueError:
            continue
        if isinstance(ans, dict):
            return ans
    return None


LANG_ALIASES = {**{v.lower(): v for v in LANGS.values()}, "bash": "Shell", "sh": "Shell", "python3": "Python",
                "pyspark": "Python", "javascript": "JS", "typescript": "TS", "golang": "Go"}


def language_name(text):
    """'Java 17 (Eclipse Temurin)' -> 'Java'; 'Bash scripts' -> 'Shell'; 'Spark SQL' -> 'Spark SQL';
    unknown -> its first word."""
    if re.match(r"\s*spark\s*sql", text, re.I):
        return "Spark SQL"
    if re.match(r"\s*(hive|ansi)\s*sql", text, re.I):
        return "SQL"
    word = (re.findall(r"[A-Za-z][\w+#]*", text) or [text])[0]
    return LANG_ALIASES.get(word.lower(), word)


def inspect_answer(ans):
    """(languages, libraries, tool_versions, pins) from an ask-style answer, using the same LIBS labels."""
    comps = [c for c in ans.get("components") or [] if isinstance(c, dict) and c.get("name")]
    libs, candidates = set(), collections.defaultdict(list)
    for i, c in enumerate(comps):
        name = str(c["name"])
        ver = re.match(r"v?(\d[\w.\-+]*)", str(c.get("version") or "").strip())  # skip "bundled with Spark"
        # code regexes catch `pyspark` / `confluent-kafka`; the bare label catches prose like "Apache Spark"
        hits = {label for rx, label in LIBS.items()
                if re.search(rx, name, re.I) or re.search(rf"\b{re.escape(label)}\b", name, re.I)}
        libs |= hits
        for label in hits:
            if ver:
                candidates[label].append((len(hits), i, ver.group(1)))
    # A component that hits several labels is usually a connector (spark-sql-kafka, flink-connector-kafka),
    # so for each label prefer a component that hits only that label, then the first one listed.
    tool_versions = {label: min(cs)[2] for label, cs in candidates.items()}
    langs = {language_name(str(l)): None for l in ans.get("languages") or []}
    # "Spark SQL", "SQL (Spark SQL)" or "Java (runtime for Spark)" as a language still means the stack uses Spark
    if any(re.search(r"\bspark\b", str(l), re.I) for l in ans.get("languages") or []):
        libs.add("Spark")
    libs = sorted(libs)
    pins = {str(c["name"]).lower(): str(c["version"]) for c in comps if c.get("version")}
    return langs, libs, tool_versions, pins


def summarize(stream, workdir, style="build"):
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
    answer = None
    if style == "ask":
        answer = parse_answer(result.get("result") or "")
        langs, libs, tool_versions, pins = inspect_answer(answer) if answer else ({}, [], {}, {})
        spark_v = tool_versions.get("Spark")
    else:
        langs, libs = inspect_project(workdir)
        pins, docs = pinned_versions(workdir), docs_text(workdir)
        spark_v = spark_version(docs) if "Spark" in libs else None
        tool_versions = {lib: v for lib in libs if (v := tool_version(lib, pins, docs, spark_v))}
    return {
        "answer": answer,  # ask style: the parsed JSON (None if it didn't answer in JSON)
        "languages": langs,
        "libraries": libs,
        "tool_versions": tool_versions,  # LIBS label -> version, for every detected library we found a version for
        "versions": pins,
        "spark_version": spark_v,
        "tools": dict(tools.most_common()),
        "shell_commands": shell_cmds,
        "ok": bool(result) and not result.get("is_error", True),
        "api_ms": result.get("duration_api_ms"),
        "cost_usd": result.get("total_cost_usd"),
        "usage": result.get("usage", {}),
        "files": len(project_files(workdir)),
        "final": (result.get("result") or "")[-600:],
    }


def run_one(run, model, root, timeout):
    wd = root / run["task"] / run["variant"] / re.sub(r"[^\w.\-]", "_", model)
    shutil.rmtree(wd, ignore_errors=True)  # partial dir from a crashed run
    wd.mkdir(parents=True)
    prompt = run["prompt"]
    if is_extra(model):
        cmd = extra_cmd(model, prompt)
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
    (wd.parent / f"{wd.name}.stream.jsonl").write_text(stream)
    return {"task": run["task"], "variant": run["variant"], "style": run["style"], "mode": run["mode"],
            "preferred": run["preferred"], "model": model, "status": status, "wall_s": round(time.time() - t0),
            **summarize(stream, wd, run["style"]), "stderr": err[-400:]}


def primary_language(r):
    return next(iter(r["languages"]), "-")


def preferred_cell(r):
    """'Spark 3.5.1, Kafka unpinned' / 'vLLM NOT USED' for a prefer-mode run."""
    return ", ".join(f"{t} {r['tool_versions'].get(t) or 'unpinned'}" if t in r["libraries"] else f"{t} NOT USED"
                     for t in r["preferred"]) or "-"


def pivot(rows, cols, header, cell):
    """Markdown table: one row per model, one column per (task, variant)."""
    lines = ["| model | " + " | ".join(header(c) for c in cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for m in rows:
        lines.append(f"| {m} | " + " | ".join(cell(c, m) for c in cols) + " |")
    return lines


def col(g, rs):
    return g[0] + (" [ask]" if rs[0].get("style") == "ask" else "")


def report(results):
    groups = list(dict.fromkeys((r["task"], r["variant"]) for r in results))
    by_group = {g: [r for r in results if (r["task"], r["variant"]) == g] for g in groups}
    models = list(dict.fromkeys(r["model"] for r in results))
    cell = {(r["task"], r["variant"], r["model"]): r for r in results}
    get = lambda g, m: cell.get((*g, m))
    lines = ["# Agent DE bakeoff", "", f"tasks: {', '.join(dict.fromkeys(t for t, _ in groups))}", ""]

    prefer = [g for g in groups if by_group[g][0]["mode"] == "prefer"]
    if prefer:
        lines += ["## Prefer mode: versions of the tools we asked for", "",
                  "`NOT USED` means the agent ignored the instruction; `unpinned` means it used the tool "
                  "without a version we could find.", ""]
        lines += pivot(models, prefer, lambda g: f"{col(g, by_group[g])} ({', '.join(by_group[g][0]['preferred'])})",
                       lambda g, m: preferred_cell(get(g, m)) if get(g, m) else "")
        spread = collections.defaultdict(collections.Counter)
        for g in prefer:
            for r in by_group[g]:
                for t in r["preferred"]:
                    spread[t + (" [ask]" if r.get("style") == "ask" else "")][r["tool_versions"].get(t, "unpinned") if t in r["libraries"] else "NOT USED"] += 1
        lines += ["", "### Version spread across all prefer-mode runs", ""]
        lines += [f"- **{t}**: " + ", ".join(f"{v} ×{n}" for v, n in c.most_common()) for t, c in spread.items()]
        lines.append("")

    floating = [g for g in groups if by_group[g][0]["mode"] == "float"]
    if floating:
        lines += ["## Float mode: primary language", ""]
        lines += pivot(models, floating, lambda g: col(g, by_group[g]), lambda g, m: primary_language(get(g, m)) if get(g, m) else "")
        counts = {g: collections.Counter(lib for r in by_group[g] for lib in r["libraries"]) for g in floating}
        libs = sorted({lib for c in counts.values() for lib in c}, key=lambda l: -sum(c[l] for c in counts.values()))
        lines += ["", "## Float mode: library use by task (models using it / models run)", "",
                  "| library | " + " | ".join(col(g, by_group[g]) for g in floating) + " |", "|---" * (len(floating) + 1) + "|"]
        for lib in libs:
            lines.append(f"| {lib} | " + " | ".join(
                f"{counts[g][lib]}/{len(by_group[g])}" if counts[g][lib] else "" for g in floating) + " |")
        lines.append("")

    for g in groups:
        rs = by_group[g]
        lines += [f"## {g[0]} / {g[1]}", "",
                  "| model | status | wall s | files | languages (LOC) | libraries | preferred | tool versions |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in rs:
            langs = ", ".join(k if v is None else f"{k} {v}" for k, v in r["languages"].items()) or "-"
            tv = ", ".join(f"{k} {v}" for k, v in list(r["tool_versions"].items())[:12]) or "-"
            lines.append(f"| {r['model']} | {r['status']} | {r['wall_s']} | {r['files']} | {langs} "
                         f"| {', '.join(r['libraries']) or '-'} | {preferred_cell(r)} | {tv} |")
        for r in rs:
            lines += ["", f"### {g[0]} / {g[1]} / {r['model']}", f"languages: {r['languages']}",
                      f"libraries: {r['libraries']}", f"tool versions: {r['tool_versions']}",
                      f"pinned: {r['versions']}", f"agent tools: {r['tools']}", f"usage: {r['usage']}"]
            if r.get("style") == "ask":
                lines.append(f"answer: {json.dumps(r.get('answer'))[:2000]}")
            lines += [f"- `{c[:160]}`" for c in r["shell_commands"][:15]]
            if r["final"]:
                lines += ["", "final message tail:", "", "> " + r["final"].replace("\n", "\n> ")]
            if r["stderr"].strip():
                lines += ["", "stderr tail:", "", "    " + r["stderr"].strip().replace("\n", "\n    ")]
        lines.append("")
    return "\n".join(lines)


PLAIN = ("task", "variant", "style", "mode", "model", "status")  # every other column is JSON-encoded


def load_tally(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = [{k: v if k in PLAIN else (json.loads(v) if v else None) for k, v in row.items()}
                for row in csv.DictReader(f)]
    for r in rows:  # rows written before tasks / modes existed
        r["task"] = r.get("task") or LEGACY_TASK
        r["variant"] = r.get("variant") or LEGACY
        r["mode"] = r.get("mode") or LEGACY
        r["style"] = r.get("style") or "build"
        r["preferred"] = r.get("preferred") or []
        r["tool_versions"] = r.get("tool_versions") or (
            {"Spark": r["spark_version"]} if r.get("spark_version") else {})
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


def canonical(tool):
    label = LABELS.get(tool.lower())
    if not label:
        print(f"warning: {tool!r} isn't a LIBS label, so the report can't tell whether it was used", file=sys.stderr)
    return label or tool


def plan_runs(tasks, styles, modes, prefer_override):
    runs = []
    for task, (prefer, body) in tasks.items():
        tools = prefer_override or [canonical(t) for t in prefer]
        if "prefer" in modes and not tools:
            print(f"warning: {task} has no `prefer:` header and no --prefer given; skipping prefer mode",
                  file=sys.stderr)
        for style in styles:
            for mode in modes:
                if mode == "prefer" and not tools:
                    continue
                used = tools if mode == "prefer" else []
                prompt = render(body, mode, used)
                runs.append({"task": task, "style": style, "mode": mode, "preferred": used,
                             "variant": variant_name(style, mode, used),
                             "prompt": ASK.replace("{task}", prompt.rstrip("\n")) if style == "ask" else prompt})
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="bakeoff",
                    help="run root; each run gets a fresh <out>/<task>/<variant>/<model>. Rerun with the same dir to resume")
    ap.add_argument("--csv", help="running tally (default <out>/tally.csv); runs already in it are skipped")
    ap.add_argument("--tasks", help="comma-separated task names (prompts/<name>.md) or prompt file paths; default all")
    ap.add_argument("--prompts-dir", default=str(PROMPTS_DIR), help="where task prompts live (default %(default)s)")
    ap.add_argument("--mode", choices=(*MODES, "both"), default="both",
                    help="float: agent picks the stack (we track tools). prefer: we name the stack (we track versions)")
    ap.add_argument("--style", choices=(*STYLES, "both"), default="build",
                    help="build: agent writes the code (we inspect it). ask: cheaper, agent only lists what it "
                         "would use, as JSON")
    ap.add_argument("--prefer", help="comma-separated tools for prefer mode, overriding every task's `prefer:` header")
    ap.add_argument("--models", help="comma-separated; default is every model from `agent --list-models` plus "
                                     "cortex and claude. claude:<model> (e.g. claude:haiku) picks the Claude model")
    ap.add_argument("--match", help="regex filter on the model list (e.g. 'sonnet|opus')")
    ap.add_argument("--no-extras", action="store_true", help="skip the cortex + claude runs at the end")
    ap.add_argument("--timeout", type=int, default=3600, help="per-run seconds")
    ap.add_argument("--dry-run", action="store_true", help="print the (task, variant, model) runs and exit")
    ap.add_argument("--print-prompts", action="store_true", help="print each rendered prompt and exit")
    a = ap.parse_args()

    modes = MODES if a.mode == "both" else (a.mode,)
    prefer = [canonical(t.strip()) for t in a.prefer.split(",") if t.strip()] if a.prefer else None
    styles = STYLES if a.style == "both" else (a.style,)
    runs = plan_runs(load_tasks(a.tasks, Path(a.prompts_dir)), styles, modes, prefer)
    if a.print_prompts:
        for run in runs:
            print(f"===== {run['task']} / {run['variant']} =====\n{run['prompt']}")
        return 0

    root = Path(a.out).resolve()
    tally = Path(a.csv) if a.csv else root / "tally.csv"
    results = load_tally(tally)
    done = {(r["task"], r["variant"], r["model"]) for r in results}

    models = a.models.split(",") if a.models else list_models()
    if not a.no_extras and not a.models:
        models += list(EXTRAS)
    bad = [m for m in models if m.startswith("cortex:")]
    if bad:
        sys.exit(f"only claude takes a :<model> suffix: {', '.join(bad)}")
    if a.match:
        models = [m for m in models if re.search(a.match, m)]
    todo = [(run, m) for run in runs for m in models]
    if a.dry_run:
        for run, m in todo:
            print(f"{run['task']}\t{run['variant']}\t{m}" + ("\t(done)" if (run["task"], run["variant"], m) in done else ""))
        return 0

    root.mkdir(parents=True, exist_ok=True)
    for i, (run, m) in enumerate(todo, 1):
        label = f"{run['task']} / {run['variant']} / {m}"
        if (run["task"], run["variant"], m) in done:
            print(f"[{i}/{len(todo)}] {label} already in {tally}, skipping", flush=True)
            continue
        print(f"[{i}/{len(todo)}] {label} ...", flush=True)
        r = run_one(run, m, root, a.timeout)
        print(f"    {r['status']} {r['wall_s']}s langs={list(r['languages'])} libs={r['libraries']}"
              + (f" preferred: {preferred_cell(r)}" if r["preferred"] else ""), flush=True)
        results.append(r)
        write_tally(tally, results)
        (root / "results.json").write_text(json.dumps(results, indent=1))
        (root / "report.md").write_text(report(results))
    print(f"\nDONE. {root}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
