"""File sources: formats, paths, and log lines cut by a pattern, run on DuckDB."""

import duckdb
import pytest

from yamlboard import files
from yamlboard.schema import FieldType

ACCESS = """\
127.0.0.1 - frank [10/Oct/2026:13:55:36 +0200] "GET /a.gif HTTP/1.0" 200 2326 "http://ex.com/" "Mozilla/4.08"
10.0.0.2 - - [10/Oct/2026:13:56:01 +0200] "POST /login HTTP/1.1" 302 - "-" "curl/8.0"
garbage line, with 'quote" and comma
10.0.0.3 - - [10/Oct/2026:14:00:00 +0000] "GET / HTTP/1.1" 404 120
"""


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("SET TimeZone = 'UTC'")
    return c


@pytest.fixture
def access(tmp_path):
    p = tmp_path / "it's access.log"                  # a quote in the path
    p.write_text(ACCESS)
    return str(p)


@pytest.mark.parametrize(("name", "fmt"), [("a.csv", "csv"), ("a.TSV", "csv"), ("a.jsonl", "json"),
                                           ("a.json.gz", "json"), ("a.parquet", "parquet"), ("app.log", "log"),
                                           ("syslog", "log")])
def test_guess_format(name, fmt):
    assert files.guess_format(name) is files.Format(fmt)


def test_resolve(tmp_path, monkeypatch):
    (tmp_path / "a.log").write_text("x\n")
    monkeypatch.chdir(tmp_path)
    assert files.resolve(" a.log ") == str(tmp_path / "a.log")
    assert files.resolve("*.log") == str(tmp_path / "*.log")   # a glob stays a glob
    with pytest.raises(FileNotFoundError):
        files.resolve("b.log")
    with pytest.raises(IsADirectoryError):
        files.resolve(str(tmp_path))
    with pytest.raises(ValueError, match="no file"):
        files.resolve("  ")


def test_structured_files(tmp_path, con):
    (tmp_path / "a.csv").write_text("region,amount\nNorth,1.5\nSouth,2\n")
    (tmp_path / "a.jsonl").write_text('{"region": "North", "n": 1}\n{"region": "South", "n": 2}\n')
    for name, fmt in (("a.csv", files.Format.CSV), ("a.jsonl", files.Format.JSON)):
        rows = con.sql(files.select_sql(str(tmp_path / name), fmt)).fetchall()
        assert [r[0] for r in rows] == ["North", "South"]


def test_groups():
    assert files.groups(r"^(\d+) (?P<user>\w+)(?:x)?") == [None, "user"]
    with pytest.raises(ValueError, match="no named group"):
        files.groups(r"^(\d+)")
    with pytest.raises(ValueError, match="invalid pattern"):
        files.groups(r"(?P<a>")
    with pytest.raises(ValueError, match="_g"):
        files.groups(r"(?P<_g1>x)")


def test_combined_log(access, con):
    fmt = files.LOG_FORMATS["combined"]
    assert con.sql(files.match_sql(access, fmt.pattern)).fetchone() == (4, 3)
    df = con.sql(files.log_sql(access, fmt.pattern, fmt.columns)).df()
    assert list(df.columns)[:4] == ["client", "user", "time", "method"]
    assert df["time"].dt.hour.tolist() == [11, 11, 14]          # +0200 read as an offset, shown in UTC
    assert df["status"].tolist() == [200, 302, 404]
    assert df["bytes"].isna().tolist() == [False, True, False]  # '-' is no size, not an error
    assert df["agent"].isna().tolist() == [False, False, True]  # the common format has no agent
    assert con.sql(files.unmatched_sql(access, fmt.pattern)).fetchall() == [("garbage line, with 'quote\" and comma",)]


def test_unnamed_groups_are_left_out(access, con):
    sql = files.log_sql(access, r"^(\S+) \S+ (?P<user>\S+) \[(?P<time>[^\]]+)\] \"(\S+) (?P<path>\S+)",
                        {"time": files.Column("time", FieldType.TIMESTAMP, "%d/%b/%Y:%H:%M:%S %z")})
    df = con.sql(sql).df()
    assert list(df.columns) == ["user", "time", "path"]
    assert df["path"].tolist() == ["/a.gif", "/login", "/"]


def test_python_log_and_iso_times(tmp_path, con):
    p = tmp_path / "yamlboard.log"
    p.write_text("2026-10-02 10:11:12,345 INFO yamlboard run 1 on duckdb\nSELECT 1\n"
                 "2026-10-02 10:11:12,400 WARNING yamlboard run 1 failed\n")
    fmt = files.LOG_FORMATS["python"]
    df = con.sql(files.log_sql(str(p), fmt.pattern, fmt.columns)).df()
    assert df["level"].tolist() == ["INFO", "WARNING"]          # the SQL line is left out
    assert df["time"].dt.microsecond.tolist() == [345000, 400000]


def test_head(access):
    assert files.head(access, 2)[1].startswith("10.0.0.2")


def test_listing(tmp_path):
    (tmp_path / "b.csv").write_text("a\n1\n")
    (tmp_path / "A.log").write_text("x\n")
    (tmp_path / ".hidden").write_text("")
    (tmp_path / "z_dir").mkdir()
    (tmp_path / "broken").symlink_to(tmp_path / "missing")
    df = files.listing(tmp_path)
    assert df["name"].tolist() == ["z_dir/", "A.log", "b.csv"]   # directories first, then by name
    assert df["kind"].tolist() == ["dir", "log", "csv"]
    assert df["size"].tolist()[1:] == [2, 4]
    assert ".hidden" in files.listing(tmp_path, hidden=True)["name"].tolist()


def test_brackets_are_a_name_not_a_pattern(tmp_path):
    log = tmp_path / "[prod] app.log"
    log.write_text("one\ntwo\n")
    assert files.resolve(str(log)) == str(log)
    assert files.head(str(log)) == ["one", "two"]
    assert files.head(str(tmp_path / "*.log")) == ["one", "two"]   # a glob still matches
    with pytest.raises(FileNotFoundError):
        files.head(str(tmp_path / "*.csv"))
