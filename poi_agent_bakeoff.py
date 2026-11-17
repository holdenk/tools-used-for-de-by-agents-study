#!/usr/bin/env python3
"""Ask each Cursor model (then Cortex Code and Claude Code) to build a Mapbox POI -> S2 bounding box pipeline.

One model at a time, each in a fresh directory, --yolo, stream-json so we can
report which tools it used. Writes <out>/report.md and <out>/results.json, and appends each finished
model to a CSV tally that a rerun restores from, so no model runs twice.
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

PROMPT = """\
Build a data pipeline that converts ALL of the Mapbox POI data into S2 bounding
boxes, running in parallel on a cluster (Spark preferred; Beam/Dask/Ray are
fine if you justify it).

- Input: Mapbox POI records (GeoJSON / Parquet / CSV, point or polygon
  geometry). Support a path or S3/GCS prefix, so it scales past one machine.
- Output: one row per POI with poi id, the S2 cell covering of its bounding box
  (cell ids as tokens, configurable min/max level and max cells), written
  partitioned by a coarse S2 cell so downstream spatial joins are cheap.
- Handle antimeridian crossing, poles, null/invalid geometry (quarantine, don't
  crash), and skew (dense city cells).
- Include a small synthetic sample dataset, unit tests, a README with the
  cluster submit command, and RUN the tests before you stop.
- Work only in the current directory. Don't ask me questions; make decisions
  and note them in the README.
"""


def list_models():
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
# regex over source + dependency files -> label
LIBS = {
    r"pyspark|org\.apache\.spark|spark-sql|spark-core": "Spark",
    r"apache_beam|org\.apache\.beam": "Beam",
    r"\bdask\b": "Dask", r"\bray\b[.\s]*(init|remote|data)|import ray": "Ray",
    r"sedona": "Sedona", r"\bs2sphere\b": "s2sphere", r"s2geometry|\bs2cell\b|\bpywraps2\b": "s2geometry",
    r"com\.google\.common\.geometry|S2RegionCoverer|S2CellId": "S2 Java/Scala",
    r"\bs2\b.*(rust|crate)|^s2\s*=|\bs2-?geo\b|\bs2::": "s2 (Rust)",
    r"\bs2geo\b|golang/geo": "s2 (Go)", r"\bh3\b": "H3",
    r"shapely": "shapely", r"geopandas": "geopandas", r"pandas": "pandas", r"pyarrow": "pyarrow",
    r"polars": "polars", r"duckdb": "DuckDB", r"snowflake": "Snowflake", r"boto3|s3a://|s3://": "S3",
    r"airflow": "Airflow", r"kubernetes|k8s": "Kubernetes", r"dataproc|emr\b": "managed Spark",
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


def pinned_versions(workdir):
    pins = {}
    for f in workdir.rglob("*"):
        if f.name in DEP_FILES and not SKIP & set(f.relative_to(workdir).parts):
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
    for f in workdir.rglob("*"):
        if f.is_file() and not SKIP & set(f.relative_to(workdir).parts) and (
                f.name in DEP_FILES or f.name == "Dockerfile" or f.suffix in (".md", ".sh", ".yaml", ".yml", ".sbt", ".cfg", ".txt")):
            text += f.read_text(errors="ignore") + "\n"
    for rx in SPARK_VERSION_RX:
        m = re.search(rx, text)
        if m:
            return m.group(1)
    return None


def inspect_project(workdir):
    langs, libs = collections.Counter(), set()
    for f in workdir.rglob("*"):
        if not f.is_file() or SKIP & set(f.relative_to(workdir).parts):
            continue
        lang = LANGS.get(f.suffix.lower())
        try:
            text = f.read_text(errors="ignore") if f.stat().st_size < 1_000_000 else ""
        except OSError:
            continue
        if lang:
            langs[lang] += text.count("\n") + 1
        if lang or f.name in ("requirements.txt", "pyproject.toml", "build.sbt", "pom.xml",
                              "build.gradle", "Cargo.toml", "go.mod", "package.json", "Dockerfile"):
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
    files = [p for p in workdir.rglob("*") if p.is_file() and not SKIP & set(p.relative_to(workdir).parts)]
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
        "files": len(files),
        "final": (result.get("result") or "")[-600:],
    }


def run_one(model, root, prompt, timeout):
    wd = root / model
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
    r = summarize(stream, wd)
    r.update(model=model, status=status, wall_s=round(time.time() - t0), stderr=err[-400:])
    return r


def report(results):
    lines = ["# POI -> S2 bakeoff", "",
             "| model | status | wall s | files | languages (LOC) | libraries | Spark | pinned versions |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        langs = ", ".join(f"{k} {v}" for k, v in r["languages"].items()) or "-"
        pins = ", ".join(f"{k} {v}" for k, v in list(r["versions"].items())[:12]) or "-"
        lines.append(f"| {r['model']} | {r['status']} | {r['wall_s']} | {r['files']} "
                     f"| {langs} | {', '.join(r['libraries']) or '-'} "
                     f"| {('Spark ' + (r['spark_version'] or '(unpinned)')) if 'Spark' in r['libraries'] else 'no'} "
                     f"| {pins} |")
    for r in results:
        lines += ["", f"## {r['model']}", f"languages: {r['languages']}", f"libraries: {r['libraries']}",
                  f"versions: {r['versions']}",
                  f"agent tools: {r['tools']}", f"usage: {r['usage']}"]
        lines += [f"- `{c[:160]}`" for c in r["shell_commands"][:15]]
        if r["final"]:
            lines += ["", "final message tail:", "", "> " + r["final"].replace("\n", "\n> ")]
        if r["stderr"].strip():
            lines += ["", "stderr tail:", "", "    " + r["stderr"].strip().replace("\n", "\n    ")]
    return "\n".join(lines) + "\n"


PLAIN = ("model", "status")  # every other column is JSON-encoded


def load_tally(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return [{k: v if k in PLAIN else json.loads(v) for k, v in row.items()} for row in csv.DictReader(f)]


def append_tally(path, r):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(r))
        if new:
            w.writeheader()
        w.writerow({k: v if k in PLAIN else json.dumps(v) for k, v in r.items()})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="poi-bakeoff",
                    help="run root; each model gets a fresh subdir. Rerun with the same dir to resume")
    ap.add_argument("--csv", help="running tally (default <out>/tally.csv); models already in it are skipped")
    ap.add_argument("--models", help="comma-separated; default is every model from `agent --list-models`")
    ap.add_argument("--match", help="regex filter on the model list (e.g. 'sonnet|opus')")
    ap.add_argument("--no-extras", action="store_true", help="skip the cortex + claude runs at the end")
    ap.add_argument("--timeout", type=int, default=3600, help="per-model seconds")
    ap.add_argument("--prompt-file", help="override the built-in prompt")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    models = a.models.split(",") if a.models else list_models()
    if a.match:
        models = [m for m in models if re.search(a.match, m)]
    if not a.no_extras and not a.models:
        models += [m for m in EXTRAS if not a.match or re.search(a.match, m)]
    prompt = Path(a.prompt_file).read_text() if a.prompt_file else PROMPT
    if a.dry_run:
        print("\n".join(models))
        return 0

    root = Path(a.out).resolve()
    root.mkdir(parents=True, exist_ok=True)
    tally = Path(a.csv) if a.csv else root / "tally.csv"
    results = load_tally(tally)
    done = {r["model"] for r in results}
    for i, m in enumerate(models, 1):
        if m in done:
            print(f"[{i}/{len(models)}] {m} already in {tally}, skipping", flush=True)
            continue
        print(f"[{i}/{len(models)}] {m} ...", flush=True)
        r = run_one(m, root, prompt, a.timeout)
        print(f"    {r['status']} {r['wall_s']}s langs={list(r['languages'])} libs={r['libraries']}", flush=True)
        results.append(r)
        append_tally(tally, r)
        (root / "results.json").write_text(json.dumps(results, indent=1))
        (root / "report.md").write_text(report(results))
    print(f"\nDONE. {root}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
