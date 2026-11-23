"""Library, language and version detection, for build-style workspaces and ask-style answers."""
from pathlib import Path

import pytest

import agent_bakeoff as ab


@pytest.mark.parametrize(("filename", "text", "want"), [
    ("requirements.txt", "pyspark==3.5.1\nraincloud[build,duckdb] @ git+https://github.com/spiraldb/raincloud@v0.3.2\n",
     {"raincloud": "0.3.2", "pyspark": "3.5.1"}),
    ("requirements.txt", "raincloud @ git+ssh://git@github.com/spiraldb/raincloud.git@22e1c77#egg=raincloud\n",
     {"raincloud": "22e1c77"}),
    ("requirements.txt", "-e git+https://github.com/spiraldb/raincloud.git@abc123def#egg=raincloud\n",
     {"raincloud": "abc123def"}),
    ("pyproject.toml", '[project]\ndependencies = [\n  "raincloud[build] @ git+https://github.com/spiraldb/raincloud@v0.3.2",'
                       '\n  "duckdb==1.5.5",\n]\n', {"raincloud": "0.3.2", "duckdb": "1.5.5"}),
    ("pyproject.toml", '[tool.uv.sources]\nraincloud = { git = "https://github.com/spiraldb/raincloud", tag = "v0.3.2" }\n',
     {"raincloud": "0.3.2"}),
    ("Cargo.toml", '[dependencies]\nvortex = { git = "https://github.com/spiraldb/vortex", tag = "0.86.1" }\nserde = "1.0"\n',
     {"vortex": "0.86.1", "serde": "^1.0"}),
    ("pyproject.toml", '[tool.uv.sources]\nraincloud = { git = "https://github.com/spiraldb/raincloud", rev = "deadbeef12" }\n',
     {"raincloud": "deadbeef12"}),                                         # a commit hash is a pin
    ("requirements.txt", "pyspark>=3.5.1, <4\n", {"pyspark": ">=3.5.1,<4"}),
    ("requirements.txt", "pyspark==3.5.*\n", {"pyspark": "==3.5.*"}),  # a wildcard is a range, not a pin
])
def test_pins(workdir: Path, filename: str, text: str, want: dict[str, str]) -> None:
    (workdir / filename).write_text(text)
    pins = ab.pinned_versions(workdir)
    assert {k: pins.get(k) for k in want} == want


@pytest.mark.parametrize("text", [
    "raincloud @ git+https://github.com/spiraldb/raincloud\n",          # no ref
    "raincloud @ git+ssh://git@github.com/spiraldb/raincloud.git\n",     # the @ is the ssh user, not a ref
    "raincloud @ git+https://github.com/spiraldb/raincloud@main\n",     # a branch can move
    "git+https://github.com/spiraldb/raincloud.git@release-next#egg=raincloud\n",
    '[tool.uv.sources]\nraincloud = { git = "https://github.com/spiraldb/raincloud", branch = "develop" }\n',
    '[tool.uv.sources]\nraincloud = { git = "https://github.com/spiraldb/raincloud", rev = "develop" }\n',
])
def test_git_dependency_without_a_fixed_ref_is_unpinned(workdir: Path, text: str) -> None:
    (workdir / ("pyproject.toml" if "uv" in text else "requirements.txt")).write_text(text)
    assert "raincloud" not in ab.pinned_versions(workdir)


def test_maven_property_versions_resolve(workdir: Path) -> None:
    (workdir / "pom.xml").write_text(
        "<project><properties><spark.version>3.5.1</spark.version></properties><dependencies><dependency>"
        "<groupId>org.apache.spark</groupId><artifactId>spark-sql_2.13</artifactId><version>${spark.version}</version>"
        "</dependency></dependencies></project>")
    (workdir / "Job.scala").write_text("import org.apache.spark.sql.SparkSession\n")
    assert ab.inspect_project(workdir)[1] == ["Spark"]
    assert ab.spark_version(ab.docs_text(workdir), ab.pinned_versions(workdir)) == "3.5.1"


def test_build_style_labels_and_versions(workdir: Path) -> None:
    (workdir / "requirements.txt").write_text(
        "raincloud[build] @ git+https://github.com/spiraldb/raincloud@v0.3.2\nvortex-data==0.86.1\n")
    (workdir / "job.py").write_text("import raincloud\nds = raincloud.load('tpcgen-rs-tpch-sf1-lineitem', format='vortex')\n")
    langs, libs = ab.inspect_project(workdir)
    assert list(langs) == ["Python"]
    assert libs == ["Raincloud", "Vortex"]  # a Raincloud TPC-H slug is not the TPC-H generator
    pins, docs = ab.pinned_versions(workdir), ab.docs_text(workdir)
    assert ab.tool_version("Raincloud", pins, docs, None) == "0.3.2"
    assert ab.tool_version("Vortex", pins, docs, None) == "0.86.1"


def test_skips_vendored_dirs(workdir: Path) -> None:
    (workdir / ".venv" / "lib").mkdir(parents=True)
    (workdir / ".venv" / "lib" / "x.py").write_text("import pyspark\n")
    assert ab.inspect_project(workdir) == ({}, [])


def test_ask_answer() -> None:
    ans = {"languages": [{"name": "Python", "version": "3.12.7"}, "Scala 2.13"],
           "components": [{"name": "Apache Spark", "version": "4.0.1", "role": "engine"},
                          {"name": "spark-sql-kafka-0-10", "version": "3.5.1", "role": "connector"},
                          {"name": "raincloud", "version": "v0.3.2", "role": "data"},
                          {"name": "BeautifulSoup4 + lxml", "version": "4.12.3 / 5.3.0", "role": "parsing"},
                          {"name": "tpchgen-cli", "version": "3.0.0 (or 2.0)", "role": "gen"}]}
    assert ab.valid_answer(ans)
    langs, libs, tool_versions, _pins, lang_versions = ab.inspect_answer(ans)
    assert list(langs) == ["Python", "Scala"] and lang_versions == {"Python": "3.12.7", "Scala": "2.13"}
    assert {"Spark", "Kafka", "Raincloud", "BeautifulSoup", "lxml", "TPC-H generator"} <= set(libs)
    # the connector hits two labels, so Spark's version comes from the component that is only Spark
    assert tool_versions["Spark"] == "4.0.1" and tool_versions["Raincloud"] == "0.3.2"
    assert tool_versions["lxml"] == "5.3.0"
    assert "TPC-H generator" not in tool_versions  # "3.0.0 (or 2.0)" names an alternative, so no version


@pytest.mark.parametrize(("text", "want"), [
    ("Here you go:\n```json\n{\"components\": [{\"name\": \"x\"}]}\n```", {"components": [{"name": "x"}]}),
    ('{"components": [{"name": "x"}]} trailing words', {"components": [{"name": "x"}]}),
    ("no json here", None),
])
def test_parse_answer(text: str, want: object) -> None:
    assert ab.parse_answer(text) == want


@pytest.mark.parametrize("ans", [None, {}, {"components": []}, {"components": ["pyspark"]}, {"components": [{"name": " "}]}])
def test_invalid_answers(ans: object) -> None:
    assert not ab.valid_answer(ans)


def test_summarize_ask_stream(workdir: Path) -> None:
    stream = "\n".join([
        '{"type": "system", "subtype": "init", "model": "claude-x"}',
        '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}',
        '{"type": "result", "is_error": false, "result": "{\\"components\\": [{\\"name\\": \\"pyspark\\", '
        '\\"version\\": \\"3.5.1\\"}]}"}',
    ])
    s = ab.summarize(stream, workdir, "ask")
    assert s["ok"] and s["served_model"] == "claude-x" and s["tools"] == {"bash": 1}
    assert s["libraries"] == ["Spark"] and s["spark_version"] == "3.5.1"


def test_summarize_invalid_answer(workdir: Path) -> None:
    s = ab.summarize('{"type": "result", "is_error": false, "result": "I would use Spark."}', workdir, "ask")
    assert s["invalid_answer"] and not s["ok"] and s["libraries"] == []
