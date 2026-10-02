from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from yamlboard.engine import Workspace

EXAMPLES = Path(__file__).parents[1] / "examples" / "reports"
FIXTURES = Path(__file__).parent / "fixtures"

# 240 orders over 2026-01-01 .. 2026-04-30 (every 12 hours), 4 regions, every 7th unpaid, every 10th
# without a channel; 12 customers; a view on the North orders; an empty table in another schema.
WAREHOUSE_SQL = """
CREATE SCHEMA sales;
CREATE TABLE sales.customers AS
SELECT i AS customer_id, 'C' || lpad(CAST(i AS VARCHAR), 2, '0') AS customer_name,
       ['retail', 'pro'][1 + i % 2] AS segment
FROM range(1, 13) t(i);
CREATE TABLE sales.orders AS
SELECT i AS order_id,
       1 + i % 12 AS customer_id,
       CAST(TIMESTAMP '2026-01-01 08:00' + i * INTERVAL 12 HOUR AS DATE) AS order_date,
       TIMESTAMP '2026-01-01 08:00' + i * INTERVAL 12 HOUR AS "CreatedAt",
       ['North', 'South', 'East', 'West'][1 + i % 4] AS region,
       CASE WHEN i % 10 = 0 THEN NULL WHEN i % 2 = 0 THEN 'web' ELSE 'store' END AS channel,
       CAST(10 + i % 5 * 2.5 AS DECIMAL(10, 2)) AS amount,
       i % 7 <> 0 AS paid
FROM range(0, 240) t(i);
CREATE VIEW sales.v_orders_north AS SELECT * FROM sales.orders WHERE region = 'North';
CREATE TABLE main.notes (id INTEGER, body VARCHAR);
"""


@pytest.fixture(scope="session", autouse=True)
def log_dir(tmp_path_factory):
    """The apps' compulsory log, out of the repository."""
    mp = pytest.MonkeyPatch()
    d = tmp_path_factory.mktemp("logs")
    mp.setenv("YAMLBOARD_LOG_DIR", str(d))
    yield d
    mp.undo()


@pytest.fixture(scope="session")
def ws() -> Workspace:
    return Workspace.open(EXAMPLES)


@pytest.fixture
def sales_params() -> dict:
    today = date.today()
    return {"start": today - timedelta(days=365), "end": today}


@pytest.fixture(scope="session")
def warehouse_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("warehouse") / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute(WAREHOUSE_SQL)
    con.close()
    return path


@pytest.fixture(scope="session")
def warehouse_url(warehouse_file) -> str:
    """Read-only, as a reporting connection should be."""
    return f"duckdb:///{warehouse_file}?access_mode=read_only"


@pytest.fixture(scope="session")
def warehouse(warehouse_url):
    from yamlboard.engine import engine_for

    return engine_for(warehouse_url)
