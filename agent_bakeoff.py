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
(variant is `float` or `prefer-<tools>`, prefixed `ask-` for ask style; repeats past the first, from --repeats,
get <model>.rep<N>/), runs with --yolo and stream-json so we can
report which tools it used, and is inspected for languages, libraries and versions.
Writes <out>/report.md and <out>/results.json, and keeps a CSV tally that a rerun
restores from, so no (task, variant, model, repeat) runs twice.

A task is just a prompt file: prompts/<task>.md, with a `{tooling}` line the mode
fills in and an optional `prefer: Tool, Tool` front-matter header.
"""
import argparse
import collections
import csv
import json
import math
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
with the exact version you would pin (for a language, the runtime or compiler
version you would target). Reply with only a JSON object shaped like:

{"languages": [{"name": "<language name>", "version": "<exact version>"}, ...],
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
    r"\bvortex\b": "Vortex",
    # dataset catalogs / generators (Raincloud's TPC-H slugs say "tpcgen-rs", so they don't hit the generator label)
    r"\braincloud\b": "Raincloud", r"tpchgen|\bdbgen\b": "TPC-H generator",
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
# a version spec: optional operator, a version (wildcards allowed), then any ", <op> <version>" clauses
OP = r"(?:===|==|>=|~=|<=|!=|<|>|\^|~|=)"
SPEC = rf"{OP}?\s*v?\d[\w.\-+*]*(?:\s*,\s*{OP}\s*v?[\w.\-+*]+)*"
PINS = [  # each yields (name, version)
    rf"""^\s*["']?([A-Za-z][\w.\-]*)(?:\[[\w,]+\])?\s*((?:===|==|>=|~=|<=|!=|<|>)\s*v?\d[\w.\-+*]*(?:\s*,\s*{OP}\s*v?[\w.\-+*]+)*)""",  # pip
    r'"[\w.\-]+"\s*%%?\s*"([\w.\-]+)"\s*%\s*"([\w.\-]+)"',                              # sbt
    r"<artifactId>([\w.\-]+)</artifactId>\s*<version>([^<]+)</version>",                # maven dep
    r"<([\w.\-]+version)>(\d[^<]*)</",                                                  # maven prop (not bare <version>)
    r"""["']([\w.\-]+:[\w.\-]+):(\d[\w.\-]*)["']""",                                    # gradle
    rf'^\s*([\w\-]+)\s*=\s*(?:\{{[^}}\n]*?version\s*=\s*)?"({SPEC})"',                       # Cargo / poetry
    r"^\s*([\w./\-]+)\s+(v\d[\w.\-]*)",                                                 # go.mod
    r'"(@?[\w./\-]+)"\s*:\s*"([\^~]?\d[\w.\-]*)"',                                      # package.json
]
GIT_REF_END = r"""(?=[\s"'#;,]|$)"""
GIT_PINS = [  # each yields (name, git tag or commit): an exact pin, even in Cargo.toml. No @ref = unpinned
    rf"""^\s*["']?([A-Za-z][\w.\-]*)(?:\[[\w,\s]+\])?\s*@\s*git\+[^\s"'#;]*@([\w.\-]+){GIT_REF_END}""",  # name @ git+URL@ref
    rf"""^\s*(?:-e\s+)?["']?git\+[^\s"'#;]*/([\w.\-]+?)(?:\.git)?@([\w.\-]+){GIT_REF_END}""",          # bare git+URL@ref
    r'^\s*([\w.\-]+)\s*=\s*\{(?=[^}\n]*\bgit\s*=)[^}\n]*\b(?:tag|rev)\s*=\s*"([^"]+)"',               # { git = .., tag = .. }
]


def _spec(v, cargo=False):
    """Normalize a version spec so only an exact pin is shown as a bare version.

    '==3.5.1' -> '3.5.1'; '>= 3.5.1, <4' -> '>=3.5.1,<4'; '==3.5.*' stays (a wildcard is a range);
    'v2.10' -> '2.10'. In Cargo.toml a bare '0.40.0' means '^0.40.0' and '=0.40.0' is the exact pin.
    """
    v = re.sub(r"\s+", "", v)
    v = re.sub(r"^([=<>!~^]*)v(?=\d)", r"\1", v)
    if cargo and re.match(r"\d", v):
        return "^" + v
    exact = re.fullmatch(r"(?:===|==|=)?(\d[\w.\-+]*)", v)  # one exact version, no wildcard, no range
    return exact.group(1) if exact else v




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
    "Vortex": ["vortex-data", "vortex-spark", "vortex-jni", "vortex-array"], "TPC-H generator": ["tpchgen-cli"],
}


def project_files(workdir):
    return [f for f in workdir.rglob("*") if f.is_file() and not SKIP & set(f.relative_to(workdir).parts)]


def pinned_versions(workdir):
    pins = {}
    for f in project_files(workdir):
        if f.name in DEP_FILES:
            text = f.read_text(errors="ignore")
            for rx in GIT_PINS:  # before PINS: a git tag beats a Cargo `version =` on the same line
                for name, ref in re.findall(rx, text, re.M):
                    pins.setdefault(name.lower(), _spec(ref))
            for rx in PINS:
                for name, ver in re.findall(rx, text, re.M):
                    pins.setdefault(name.lower(), _spec(ver, cargo=f.name == "Cargo.toml"))
            if f.name == "pom.xml":  # every <properties> entry, so ${spark.ver}-style refs resolve too
                for block in re.findall(r"<properties>(.*?)</properties>", text, re.S):
                    for name, val in re.findall(r"<([\w.\-]+)>([^<]+)</\1>", block):
                        pins.setdefault(name.lower(), val.strip())
    # Maven / Gradle deps often say ${flink.version}; resolve those from the <properties> pins
    for name, ver in pins.items():
        ref = re.fullmatch(r"\$\{([\w.\-]+)\}", ver.strip())
        if ref:
            pins[name] = pins.get(ref.group(1).lower(), ver)
    return {k: v for k, v in pins.items() if not v.startswith("$")}


def docs_text(workdir):
    """Dependency files plus free-form places versions get written down (README, Dockerfile, submit scripts)."""
    return "\n".join(f.read_text(errors="ignore") for f in project_files(workdir)
                     if f.name in DEP_FILES or f.name == "Dockerfile"
                     or f.suffix in (".md", ".sh", ".yaml", ".yml", ".sbt", ".cfg", ".txt"))


SPARK_VERSION_RX = [  # first hit wins; Python, then Scala/Java, then free-form (README, Dockerfile, submit scripts)
    rf"""pyspark(?:\[[\w,]+\])?\s*((?:===|==|>=|~=|<=|!=|<|>)\s*v?\d[\w.\-+*]*(?:\s*,\s*{OP}\s*v?[\w.\-+*]+)*)""",
    rf"""['"]pyspark['"]\s*,?\s*(?:version\s*=\s*)?['"]?\s*({SPEC})""",
    r"""org\.apache\.spark["']?\s*%+\s*["']spark-[\w\-]+["']\s*%\s*["'](\d[\w.\-]*)["']""",  # sbt
    r"""org\.apache\.spark:spark-[\w\-.]+:(\d[\w.\-]*)""",                                   # gradle / --packages
    r"""<groupId>org\.apache\.spark</groupId>\s*<artifactId>[^<]+</artifactId>\s*<version>([^<]+)</version>""",  # maven dep
    r"""<spark[\w.\-]*version>(\d[^<]*)</""",                                                # maven property
    r"""\bspark_?[vV]ersion\s*(?::=|=|:)\s*["'](\d[\w.\-]*)["']""",                          # sbt val / TOML / yaml
    r"""(?i)\bspark[\s\-_]*(?:version\s*)?v?(\d\.\d+\.\d+)""",                               # README / Dockerfile prose
]


def spark_version(text, pins=None):
    if pins and pins.get("pyspark"):  # a parsed dependency pin beats any pattern over prose
        return pins["pyspark"]
    for rx in SPARK_VERSION_RX:
        for m in re.finditer(rx, text):
            ver = m.group(1).strip()
            ref = re.fullmatch(r"\$\{([\w.\-]+)\}", ver)
            if ref:  # <version>${spark.version}</version>
                ver = (pins or {}).get(ref.group(1).lower(), "")
            if ver and not ver.startswith("$"):
                return _spec(ver)
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


def valid_answer(ans):
    """An ask answer counts as an observation only if every component is an object with a name."""
    comps = ans.get("components") if isinstance(ans, dict) else None
    return (isinstance(comps, list) and bool(comps)
            and all(isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip() for c in comps))


def split_component(c):
    """'BeautifulSoup4 + lxml' at '4.12.3 / 5.3.0' -> one component each, when names and versions pair up 1:1."""
    # separators inside "(...)" describe parts of one library ("Ray (core + Ray Data)"), so don't split there
    names = re.split(r"\s+(?:\+|/|and)\s+(?![^()]*\))", str(c["name"]))
    vers = re.split(r"\s*/\s*|\s*;\s*", str(c.get("version") or ""))
    if len(names) > 1 and len(names) == len(vers):
        return [{**c, "name": n, "version": v} for n, v in zip(names, vers)]
    if len(names) > 1:  # several libraries, versions that don't pair up: keep the libraries, drop the versions
        return [{**c, "name": n, "version": ""} for n in names]
    return [c]


def answer_version(text):
    """A version from an ask answer -> normalized spec, or None. The whole string must be one spec plus an
    optional "(note)"; "3.5.1 || 4.0.0", prose, or a note naming an alternative ("3.5.1 (or 4.0.0)") stays None."""
    ver = re.fullmatch(rf"({SPEC})(?:\s*\(([^)]*)\))?", str(text or "").strip())
    if not ver or (ver.group(2) and re.search(r"\bor\b|\|\||\beither\b|\balternativ", ver.group(2), re.I)):
        return None
    return _spec(ver.group(1))


def answer_languages(entries):
    """({language: None}, {language: version}, [text per entry]) from an ask answer's `languages`.
    Entries are {"name", "version"} objects, or bare strings like "Scala 2.13" (answers from before the
    trailer asked for language versions, or models that ignore the shape)."""
    langs, versions, texts = {}, {}, []
    for entry in entries or []:
        if isinstance(entry, dict):
            name, ver = str(entry.get("name") or ""), answer_version(entry.get("version"))
            texts.append(" ".join(str(v) for v in entry.values()))
        else:
            name, ver = str(entry), None
            texts.append(name)
        if not name.strip():
            continue
        if not ver:  # "Java 17 (Eclipse Temurin)": the version follows the name
            rest = re.match(r"\s*[A-Za-z][\w+#]*\s+(.*)", name)
            ver = answer_version(rest.group(1)) if rest else None
        lang = language_name(name)
        langs.setdefault(lang, None)
        if ver:
            versions.setdefault(lang, ver)
    return langs, versions, texts


def inspect_answer(ans):
    """(languages, libraries, tool_versions, pins, language_versions) from an ask-style answer,
    using the same LIBS labels."""
    comps = [c for c in ans.get("components") or [] if isinstance(c, dict) and c.get("name")]
    comps = [part for c in comps for part in split_component(c)]
    libs, candidates = set(), collections.defaultdict(list)
    for i, c in enumerate(comps):
        name = str(c["name"])
        ver = answer_version(c.get("version"))
        # code regexes catch `pyspark` / `confluent-kafka`; the bare label catches prose like "Apache Spark"
        hits = {label for rx, label in LIBS.items()
                if re.search(rx, name, re.I) or re.search(rf"\b{re.escape(label)}\b", name, re.I)}
        libs |= hits
        for label in hits:
            if ver:
                candidates[label].append((len(hits), i, ver))
    # A component that hits several labels is usually a connector (spark-sql-kafka, flink-connector-kafka),
    # so for each label prefer a component that hits only that label, then the first one listed.
    tool_versions = {label: min(cs)[2] for label, cs in candidates.items()}
    langs, lang_versions, lang_texts = answer_languages(ans.get("languages"))
    # "Spark SQL", "SQL (Spark SQL)" or "Java (runtime for Spark)" as a language still means the stack uses Spark
    if any(re.search(r"\bspark\b", t, re.I) for t in lang_texts):
        libs.add("Spark")
    libs = sorted(libs)
    pins = {str(c["name"]).lower(): str(c["version"]) for c in comps if c.get("version")}
    return langs, libs, tool_versions, pins, lang_versions


def summarize(stream, workdir, style="build"):
    """Cursor: `started` tool_call events (key = tool). Others: tool_use blocks."""
    tools, shell_cmds, result, served = collections.Counter(), [], {}, None
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
        elif ev.get("type") == "system" and ev.get("subtype") == "init" and ev.get("model"):
            served = served or ev["model"]  # what actually ran, e.g. when `claude` uses its default model
        elif ev.get("type") == "result":
            result = ev
    answer, invalid = None, False
    if style == "ask":
        answer = parse_answer(result.get("result") or "")
        invalid = not valid_answer(answer)
        langs, libs, tool_versions, pins, lang_versions = ({}, [], {}, {}, {}) if invalid else inspect_answer(answer)
        spark_v = tool_versions.get("Spark")
    else:
        langs, libs = inspect_project(workdir)
        lang_versions = {}  # build style doesn't detect language versions yet
        pins, docs = pinned_versions(workdir), docs_text(workdir)
        spark_v = spark_version(docs, pins) if "Spark" in libs else None
        tool_versions = {lib: v for lib in libs if (v := tool_version(lib, pins, docs, spark_v))}
    return {
        "answer": answer,  # ask style: the parsed JSON (None if it didn't answer in JSON)
        "invalid_answer": invalid,  # ask style: no usable JSON; not an observation, kept out of the stats
        "languages": langs,
        "language_versions": lang_versions,  # ask style: language -> version it said it would target
        "libraries": libs,
        "tool_versions": tool_versions,  # LIBS label -> version, for every detected library we found a version for
        "versions": pins,
        "spark_version": spark_v,
        "tools": dict(tools.most_common()),
        "shell_commands": shell_cmds,
        "ok": bool(result) and not result.get("is_error", True) and not invalid,
        "api_ms": result.get("duration_api_ms"),
        "cost_usd": result.get("total_cost_usd"),
        "usage": result.get("usage", {}),
        "files": len(project_files(workdir)),
        "final": (result.get("result") or "")[-600:],
        "served_model": served,  # from the stream's init event, when the CLI reports one
    }


def run_dir_name(model, rep):
    """Repeat 1 keeps the plain model name, so tallies and dirs from before --repeats line up."""
    return re.sub(r"[^\w.\-]", "_", model) + ("" if rep == 1 else f".rep{rep}")


def run_one(run, model, root, timeout, rep=1):
    wd = root / run["task"] / run["variant"] / run_dir_name(model, rep)
    shutil.rmtree(wd, ignore_errors=True)  # partial dir from a crashed run
    wd.mkdir(parents=True)
    prompt = run["prompt"]
    if is_extra(model):
        cmd = extra_cmd(model, prompt)
    else:
        cmd = ["agent", "-p", "--yolo", "--trust", "--model", model,
               "--output-format", "stream-json", "--workspace", str(wd), prompt]
    t0 = time.time()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))  # for version lag vs releases at run time
    try:
        p = subprocess.run(cmd, cwd=wd, capture_output=True, text=True, timeout=timeout)
        stream, err, status = p.stdout, p.stderr, "exit %d" % p.returncode
    except subprocess.TimeoutExpired as e:
        stream = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, status = "", "TIMEOUT"
    (wd.parent / f"{wd.name}.stream.jsonl").write_text(stream)
    summary = summarize(stream, wd, run["style"])
    if summary["invalid_answer"]:
        status += ", invalid answer"
    return {"task": run["task"], "variant": run["variant"], "style": run["style"], "mode": run["mode"],
            "preferred": run["preferred"], "model": model, "rep": rep, "started": started, "status": status,
            "wall_s": round(time.time() - t0),
            **summary, "stderr": err[-400:]}


def language_label(r, lang):
    """'Scala 2.13' when the run gave a language version, else just 'Scala'."""
    ver = (r.get("language_versions") or {}).get(lang)
    return f"{lang} {ver}" if ver else lang


def primary_language(r):
    if r.get("invalid_answer"):
        return "INVALID ANSWER"
    lang = next(iter(r["languages"]), None)
    return language_label(r, lang) if lang else "-"


def preferred_cell(r):
    """'Spark 3.5.1, Kafka unpinned' / 'vLLM NOT USED' for a prefer-mode run."""
    if r.get("invalid_answer"):
        return "INVALID ANSWER"
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


def run_label(r):
    rep = r.get("rep") or 1
    return r["model"] + ("" if rep == 1 else f" #{rep}")


def tally_cell(values):
    """One repeat: its value. Several: 'Spark 3.5.1 ×3; Spark 4.0.0 ×1', most common first."""
    if len(values) == 1:
        return values[0]
    return "; ".join(f"{v} ×{n}" for v, n in collections.Counter(values).most_common())


# --- convergence: do runs keep landing on the same stack? (cf. "Elias in the Lighthouse, Again?",
# arXiv 2605.26492: the same prompt, sampled many times, collapses onto a few defaults)
NOT_A_CHOICE = {"tests"}  # every prompt asks for tests, so this label says nothing about the stack


def stack(r):
    return frozenset(r["libraries"]) - NOT_A_CHOICE


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 1.0


def mean_similarity(runs, same_model):
    """Mean pairwise Jaccard of library sets, over pairs from the same model (repeats) or from different models."""
    sims = [jaccard(stack(x), stack(y)) for i, x in enumerate(runs) for y in runs[i + 1:]
            if (x["model"] == y["model"]) == same_model]
    return f"{sum(sims) / len(sims):.2f}" if sims else "-"


def effective_choices(counter):
    """2^entropy: 1.0 when every run made the same choice, n when n choices were equally common."""
    n = sum(counter.values())
    return 2 ** -sum(c / n * math.log2(c / n) for c in counter.values()) if n else 0.0


def modal(counter):
    value, count = counter.most_common(1)[0]
    return value, f"{count}/{sum(counter.values())}"


def convergence(groups, by_group, valid):
    lines = ["## Convergence: how often runs land on the same stack", "",
             "Over valid runs, ignoring the `tests` label. **Top stack**: the most common exact library set. "
             "**Effective stacks**: 2^entropy of the library-set distribution (1 = every run picked the same "
             "set). **Jaccard**: mean pairwise library-set overlap between repeats of one model, and between "
             "runs of different models; if `across` is close to `within`, the models share a default. "
             "**Top answer**: the most common primary language (float) or preferred-tool versions (prefer).", "",
             "| task / variant | runs | models | top stack | effective stacks | Jaccard within model "
             "| Jaccard across models | top answer |", "|---|---|---|---|---|---|---|---|"]
    for g in groups:
        rs = valid[g]
        if not rs:
            continue
        stacks = collections.Counter(stack(r) for r in rs)
        top, share = modal(stacks)
        answer = collections.Counter(preferred_cell(r) if r["preferred"] else primary_language(r) for r in rs)
        a, a_share = modal(answer)
        lines.append(f"| {g[0]} / {g[1]} | {len(rs)} | {len({r['model'] for r in rs})} "
                     f"| {', '.join(sorted(top)) or '-'} ({share}) | {effective_choices(stacks):.1f} "
                     f"| {mean_similarity(rs, True)} | {mean_similarity(rs, False)} | {a} ({a_share}) |")
    # task-independent defaults: libraries at least half the float runs of every task picked
    for style in dict.fromkeys(by_group[g][0].get("style", "build") for g in groups):
        floating = [g for g in groups if by_group[g][0]["mode"] == "float"
                    and by_group[g][0].get("style", "build") == style and valid[g]]
        if len(floating) < 2:
            continue
        share = lambda lib, g: sum(lib in stack(r) for r in valid[g]) / len(valid[g])
        libs = {lib for g in floating for r in valid[g] for lib in stack(r)}
        core = sorted(lib for lib in libs if all(share(lib, g) >= 0.5 for g in floating))
        lines += ["", f"Used by at least half the float runs of every task ({style}, {len(floating)} tasks): "
                      + (", ".join(core) or "none")]
    return lines + [""]


def report(results):
    groups = list(dict.fromkeys((r["task"], r["variant"]) for r in results))
    by_group = {g: [r for r in results if (r["task"], r["variant"]) == g] for g in groups}
    valid = {g: [r for r in rs if not r.get("invalid_answer")] for g, rs in by_group.items()}  # stats skip these
    models = list(dict.fromkeys(r["model"] for r in results))
    cells = collections.defaultdict(list)  # (task, variant, model) -> its runs, one per repeat
    for r in sorted(results, key=lambda r: r.get("rep") or 1):
        cells[(r["task"], r["variant"], r["model"])].append(r)
    shown = lambda g, m, fn: tally_cell([fn(r) for r in cells[(*g, m)]]) if cells.get((*g, m)) else ""
    lines = ["# Agent DE bakeoff", "", f"tasks: {', '.join(dict.fromkeys(t for t, _ in groups))}", ""]

    prefer = [g for g in groups if by_group[g][0]["mode"] == "prefer"]
    if prefer:
        lines += ["## Prefer mode: versions of the tools we asked for", "",
                  "`NOT USED` means the agent ignored the instruction; `unpinned` means it used the tool "
                  "without a version we could find; a range like `>=3.5` is shown as a range. `INVALID ANSWER` "
                  "(ask style) means the reply had no usable JSON; those runs are left out of the counts.", ""]
        lines += pivot(models, prefer, lambda g: f"{col(g, by_group[g])} ({', '.join(by_group[g][0]['preferred'])})",
                       lambda g, m: shown(g, m, preferred_cell))
        spread = collections.defaultdict(collections.Counter)
        for g in prefer:
            for r in valid[g]:
                for t in r["preferred"]:
                    spread[t + (" [ask]" if r.get("style") == "ask" else "")][r["tool_versions"].get(t, "unpinned") if t in r["libraries"] else "NOT USED"] += 1
        lines += ["", "### Version spread across all prefer-mode runs", ""]
        lines += [f"- **{t}**: " + ", ".join(f"{v} ×{n}" for v, n in c.most_common()) for t, c in spread.items()]
        lines.append("")

    floating = [g for g in groups if by_group[g][0]["mode"] == "float"]
    if floating:
        lines += ["## Float mode: primary language", ""]
        lines += pivot(models, floating, lambda g: col(g, by_group[g]), lambda g, m: shown(g, m, primary_language))
        counts = {g: collections.Counter(lib for r in valid[g] for lib in r["libraries"]) for g in floating}
        libs = sorted({lib for c in counts.values() for lib in c}, key=lambda l: -sum(c[l] for c in counts.values()))
        lines += ["", "## Float mode: library use by task (runs using it / runs)", "",
                  "| library | " + " | ".join(col(g, by_group[g]) for g in floating) + " |", "|---" * (len(floating) + 1) + "|"]
        for lib in libs:
            lines.append(f"| {lib} | " + " | ".join(
                f"{counts[g][lib]}/{len(valid[g])}" if counts[g][lib] else "" for g in floating) + " |")
        lines.append("")

    lines += convergence(groups, by_group, valid)

    for g in groups:
        rs = by_group[g]
        lines += [f"## {g[0]} / {g[1]}", "",
                  "| model | status | wall s | files | languages | libraries | preferred | tool versions |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in rs:
            langs = ", ".join(language_label(r, k) + ("" if v is None else f" ({v} LOC)")
                              for k, v in r["languages"].items()) or "-"
            tv = ", ".join(f"{k} {v}" for k, v in list(r["tool_versions"].items())[:12]) or "-"
            lines.append(f"| {run_label(r)} | {r['status']} | {r['wall_s']} | {r['files']} | {langs} "
                         f"| {', '.join(r['libraries']) or '-'} | {preferred_cell(r)} | {tv} |")
        for r in rs:
            lines += ["", f"### {g[0]} / {g[1]} / {run_label(r)}",
                      f"started: {r.get('started') or '?'}, served model: {r.get('served_model') or '?'}",
                      f"languages: {r['languages']}",
                      f"language versions: {r.get('language_versions') or {}}",
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
        r["rep"] = r.get("rep") or 1  # rows from before --repeats
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
    ap.add_argument("--repeats", type=int, default=1,
                    help="runs per (task, variant, model), to measure how consistent each model's choices are. "
                         "All repeat-1 runs go first, then repeat 2, and so on. Raising it on a resumed run adds "
                         "only the new repeats")
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
    done = {(r["task"], r["variant"], r["model"], r["rep"]) for r in results}

    models = a.models.split(",") if a.models else list_models()
    if not a.no_extras and not a.models:
        models += list(EXTRAS)
    bad = [m for m in models if m.startswith("cortex:")]
    if bad:
        sys.exit(f"only claude takes a :<model> suffix: {', '.join(bad)}")
    if a.match:
        models = [m for m in models if re.search(a.match, m)]
    if a.repeats < 1:
        sys.exit("--repeats must be at least 1")
    todo = [(run, m, rep) for rep in range(1, a.repeats + 1) for run in runs for m in models]
    if a.dry_run:
        for run, m, rep in todo:
            print(f"{run['task']}\t{run['variant']}\t{m}" + (f"\trep {rep}" if a.repeats > 1 else "")
                  + ("\t(done)" if (run["task"], run["variant"], m, rep) in done else ""))
        return 0

    root.mkdir(parents=True, exist_ok=True)
    for i, (run, m, rep) in enumerate(todo, 1):
        key = (run["task"], run["variant"], m, rep)
        label = f"{run['task']} / {run['variant']} / {m}" + (f" #{rep}" if rep > 1 else "")
        if key in done:
            print(f"[{i}/{len(todo)}] {label} already in {tally}, skipping", flush=True)
            continue
        print(f"[{i}/{len(todo)}] {label} ...", flush=True)
        r = run_one(run, m, root, a.timeout, rep)
        print(f"    {r['status']} {r['wall_s']}s langs={list(r['languages'])} libs={r['libraries']}"
              + (f" preferred: {preferred_cell(r)}" if r["preferred"] else ""), flush=True)
        results.append(r)
        write_tally(tally, results)
        done.add(key)
        (root / "results.json").write_text(json.dumps(results, indent=1))
        (root / "report.md").write_text(report(results))
    if results:  # also repairs exports when a resumed run had nothing left to do
        (root / "results.json").write_text(json.dumps(results, indent=1))
        (root / "report.md").write_text(report(results))
    print(f"\nDONE. {root}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
