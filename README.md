# yamlboard

Declare a SQL report in a YAML file; get a chart and a table you can filter and aggregate,
on the fields the report allows, and nothing else.

```bash
uv sync                                  # or: pip install -e .   (add [postgres] for PostgreSQL)
uv run yamlboard run examples/reports    # opens the Streamlit app
uv run yamlboard dev --url data/w.duckdb # browse a database, test a query, draft a report (see Dev app)
```

- **The YAML is the contract.** Each field says whether it can be filtered, grouped on rows or columns,
  bucketed by time (minute, hour, day, …), or aggregated (sum, avg, median, …). The app offers only those,
  and the query builder rejects anything else, even when called directly.
- **Everything runs in the database.** The report's SQL becomes a subquery; filters become a `WHERE` clause,
  groups a `GROUP BY` clause. Values are always bound parameters. Nothing is loaded into memory beyond the
  aggregated result or one page of rows.
- **DuckDB and PostgreSQL** today (DuckDB also reads Parquet and CSV files). A new database means one small
  class in `dialects.py`.

## A report

```yaml
id: sales
title: Sales
datasource: default              # a name from datasources.yml
parameters:                      # bound into the SQL as :name
  - {name: start, type: date, required: true, label: From}
  - {name: end,   type: date, required: true, label: To}
ranges:                          # shown as one period: Last month, Last year, … or two dates
  - {start: start, end: end}
rules:
  - {param: start, other: end, rule: max_days, value: 366}
sql: |
  select order_id, order_date, region, amount
  from orders
  where order_date between :start and :end
result_columns:                  # the columns of the SQL result, and what users may do with each
  - name: order_date
    type: date
    filterable: true
    groupable_rows: true
    grains: [month, week, day]   # the time buckets users can pick
  - name: region
    filterable: true
    groupable: true              # rows and columns
    colors: {North: "#2a78d6", South: "#eb6834"}  # a value's colour in a chart; users can change it
  - name: amount
    type: number
    filterable: true
    measures: [sum, avg, median]
  - name: order_id
    measures: [count_distinct]
view:                            # what the report opens on
  rows: [order_date]
  grains: {order_date: month}
  columns: [region]
  measures: [{field: amount, aggregation: sum}]
  chart: line
```

Full examples: [examples/reports/](examples/reports/).

### Reference

| Key | Meaning |
|---|---|
| `id`, `title`, `description` | Identity. The sidebar lists reports by folder, then title: a report in a sub-directory of the reports directory shows as `audit - Cost` (`audit/deep - Cost` two levels down). Hidden directories are skipped |
| `active` | `false` hides the report (default `true`) |
| `datasource` | Name of a connection in `datasources.yml` (default `default`) |
| `sql` | The query. Its columns are the result columns; `:name` binds a parameter (write `cast(:name as date)`, not `:name::date`) |
| `parameters[]` | `name`, `type`, `label`, `required`, `default` |
| `ranges[]` | `start`, `end`: two date parameters entered as one range. `field`: apply the range to that date or timestamp field instead of writing the parameters in the SQL (see [Filters](#filters)) |
| `rules[]` | `param`, `other`, `rule`, `value`. Rules: `min`, `max`, `min_length`, `max_length`, `pattern`, `max_days` / `max_hours`: the longest span from `param` to `other`, a guard that keeps a query from scanning too much data (checked in the UI and again before any SQL runs) |
| `result_columns[]` | The columns of the SQL result, and what users may do with each (`fields` before 0.2, still read): `name`, `type`, `label`, `visible`, `filterable`, `groupable_rows`, `groupable_columns`, `groupable` (both), `grains`, `measures`, `exclude`, `colors` |
| `view` | `rows`, `columns`, `measures`, `grains`, `chart`, `color`. The board groups rows by one field and splits columns by one: it opens on the first of each, and the first measure |
| `rights` | Accepted, not enforced yet: every report is visible to everyone |
| `formats` | Downloads offered: `CSV`, `JSON`, `XLSX` (or `Excel`); default all three |
| `readme` | HTML shown under *About this report* |

Types: `string`, `integer`, `number`, `boolean`, `date`, `timestamp` (dates and timestamps: ISO-8601, UTC).
Grains: `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year`, or `all`: minute to year on a `timestamp`,
day to year on a `date`. Which one a chart opens on: see [Time grains](#time-grains).
Measures: `count` and `count_distinct` on any field; `sum`, `avg`, `min`, `max`, `median` on `integer` and
`number` fields only. A row count (`*`) is always available.
Charts: `column`, `column-stacked`, `bar`, `bar-stacked`, `line`, `area`, `pie`, `donut`. A pie or a donut has
one dimension, its slices: the board hides *Split columns by* and drops the split.
Colours: `colors` on a result column gives a value its colour wherever it splits a chart or is a pie slice, keyed
by the value as text (`CPU: "#00cc00"`, `"(empty)"` for NULL, a period such as `2026-03` on a date);
`view.color` colours a chart with one series. `#rrggbb` or `#rgb`. A value without one takes the palette's
colour of its place in the legend. Users override either on the board.

`yamlboard validate DIR` checks every file: unknown or unused `:parameters`, duplicate names, a view that uses a
field in a way the field does not allow, grains on a non-date field, a sum or a max on a non-numeric field.

### Legacy keys

Files written with the older French key names load unchanged:

| Legacy | Canonical |
|---|---|
| `mapping` | `name` |
| `actif` | `active` |
| `right` | `rights` |
| `customParameters` | `parameters` |
| `customParametersFormType` (`DATE_RANGE`, `paramsId`, `optionalParamsId`) | `ranges` (`start`, `end`) |
| `customParametersValidators` (`DAY_BETWEEN`, `HOUR_BETWEEN`, …) | `rules` (`max_days`, `max_hours`, …) |
| `filtrage` | `filterable` |
| `agregationLigne`, `agregationColone` | `groupable_rows`, `groupable_columns` |
| `affichageDefaut` | `visible` |
| `availableGroupingDateType` (`MINUTES`, `HEURES`, `JOUR`, `SEMAINE`, `MOIS`, `TRIMESTRE`, `ANNEE`) | `grains` |
| `slice` (`rows`, `columns`, `measures` with `uniqueName`) and `options.chart.type` | `view` |
| Types `Mnemo`, `UUID` | `string` |

Keys yamlboard doesn't use (`dashboard`, `async`, `dateTimePattern`, …) are ignored.

## Filters

Three kinds, each with its negation (the only operator in this version):

| Filter | Fields | SQL |
|---|---|---|
| `in` / `not_in` | any; the app offers it on text, integer and boolean fields | `col IN (…)` / `col NOT IN (…)` |
| `between` / `not_between` | the app offers it on date, timestamp and number fields; either bound may be left open | `col BETWEEN … AND …` / `NOT (…)` |
| `is_null` / `not_null` | any (*Empty* in the app) | `col IS NULL` / `col IS NOT NULL` |

How they combine:

- Filters on **different fields** must all match (`AND`).
- On **one field**, inclusions are alternatives: `region in (North)` plus `region in (South)` is
  `region IN (North, South)`, and an `in` next to a `between` on the same field is `(… IN … OR … BETWEEN …)`.
  Exclusions all apply: `in (a, b, c)` plus `not_in (b)` keeps `a` and `c`.
- **Empty values**: `is_null` is an inclusion like any other (*empty or in March* is
  `(ts IS NULL OR ts BETWEEN …)`), `not_null` an exclusion. *(empty)* in a list does the same. Without them, an
  exclusion keeps NULL rows (`NULL NOT IN (…)` alone would drop them silently).
- **Timestamps and days**: a date as the upper bound on a `timestamp` field keeps the whole of that day
  (`col < day + 1`): `col BETWEEN '2026-03-01' AND '2026-03-01'` would keep only the rows at midnight.
  A value with a time of day is taken as written (see [Dates, times and UTC](#dates-times-and-utc)).

The same trap sits in report SQL: `where ts between :start and :end` with two date parameters loses the end
day after midnight. Let yamlboard apply the range instead:

```yaml
parameters:
  - {name: start, type: date, required: true}
  - {name: end,   type: date, required: true}
ranges:
  - {start: start, end: end, field: sample_time}   # the SQL does not mention :start or :end
```

The range is then a `WHERE` around the report's SQL, which the database pushes into it. Keep the parameters in
the SQL when it aggregates or limits rows before the range should apply.

## Dates, times and UTC

The backend works in UTC only, and every date or time it accepts is ISO-8601. The UI labels its date pickers
*(UTC)*; the rules below hold for the library and the command line just the same.

- **In.** A day is `YYYY-MM-DD`. A timestamp is `YYYY-MM-DDTHH:MM[:SS[.ffffff]]` followed by `Z` or `±HH:MM`,
  and is converted to UTC: `2026-03-01T01:00:00+02:00` is `2026-02-28T23:00:00Z`. A time without a zone,
  `01/03/2026`, `2026-3-1` or a space instead of `T` are refused with an error naming the parameter. A day given
  to a timestamp is its midnight UTC. Python `datetime` objects are converted to UTC; naive ones are taken as UTC.
- **Inside.** Values are bound as UTC, and every database session runs with `TimeZone = 'UTC'`: `current_date`,
  `now()`, `date_trunc('day', …)` and `timestamptz` columns all mean UTC, whatever the server's zone. Store
  `timestamp` (without zone) columns in UTC; yamlboard cannot tell otherwise.
- **Out.** Result timestamps are UTC. The CSV and JSON downloads write `YYYY-MM-DDTHH:MM:SSZ` and
  `YYYY-MM-DD`; the chart's time axis is UTC, not the browser's zone.

## Time grains

A date grouped on rows or columns is cut into periods by a grain, one of the field's `grains`. Its default
follows the period the result covers: the **finest grain with at most 60 periods**, else the coarsest allowed.
With `grains: all`:

| Period shown | Default grain |
|---|---|
| up to 1 hour | minute |
| up to 2½ days | hour |
| up to 2 months | day |
| up to about 14 months | week |
| up to 5 years | month |
| up to 15 years | quarter |
| longer | year |

Periods are counted on the calendar, not by length: 60 days from mid-January touch 3 months, and a week
counts once it is touched (ISO weeks, Monday to Sunday).

The period shown is the report's date range on that field (`ranges` with its `field`), else its range
written in the SQL (`ranges` without a `field`), narrowed by a *between* filter on the field when it is
that field's only inclusion. With no such range, or an open end, the default is the `view` grain, else the
field's first grain.

On the board, a new period (a new range, or a filter on that date) resets the grain to its default; a grain
you pick holds until then. The grain control's tooltip shows the period it was chosen from. `default_view`
and the dev and lite previews use the same default.

## Datasources

`datasources.yml`, next to the reports (or `--datasources FILE`): one SQLAlchemy URL per name. `${VAR}` is
read from the environment, so no password is written in the file. Quote the URLs.

```yaml
default: "duckdb:///data/warehouse.duckdb"
files: "duckdb:///:memory:"      # then: sql: select * from 'data/*.parquet'
pg: "postgresql+psycopg://reporting:${PGPASSWORD}@db:5432/warehouse"
```

A DuckDB file is opened for each statement and closed after it: an open connection locks the file against
every other process, so the board never keeps a writer (a loader, a notebook) out between queries.

Connect with a **read-only account**. yamlboard rolls back every transaction, but the report SQL comes from
the YAML file and runs as written.

## Names and case

A field `name` matches its SQL column **ignoring case**: `orderDate` in the YAML finds `ORDERDATE`, `orderdate`
or a quoted `"OrderDate"`, whatever the database does with unquoted names. Before querying, yamlboard reads
the real column names (a query that returns no rows) and reports a field with no column, or with two columns
that differ only by case.

Output keys (the table, the CSV and JSON downloads) are **case-sensitive**: always the `name` exactly as written
in the YAML. A measure is named `<aggregation>_<name>`, e.g. `sum_amount`.

## The board

`yamlboard run` shows the reports. The chart and the rows get the room:

- **Title line**: the report's name, then its icons: *Filters* (with the number active; the active filters
  are listed under the title), *About this report*, *Save this view* and *Open a saved view* (below).
- **Controls**, on one line: **View**: *Chart* and *Detail* (*Pivot* too in advanced mode). Pick several to stack them, in that order: a
  chart with its rows below.
- **Parameters** (sidebar): a range is a **period ending now** (*Last hour*, *Last day*, *Last 7 days*,
  *Last month*, *Last 3 months*, *Last year*) or *Custom dates*. A period is kept by its name and turned into
  two bounds on every run, so it always ends now: never two dates that go stale. On `date` parameters it runs
  from the start's day to today (UTC), and *Last hour* is not offered; on two `timestamp` parameters it runs to
  the current minute, which the queries of that minute share in the cache (*Run* moves it on). Only the periods
  the report's rules allow are offered; it opens on the longest one a `max_days` / `max_hours` rule allows,
  else on *Last 7 days*. The caption under it shows the bounds used.
- **Group rows by** one field, **Split columns by** another (not for a pie or a donut), a **Measure**, and a grain
  for each date, picked from the period shown (see [Time grains](#time-grains)).
- **Detail** pages the rows. Under the table, on one line: *Rows per page* (1, 5, 10, 50 by default, 200), the
  arrows with *Page n of N* between them, and the number of rows found. A click on a column header sorts the
  rows of that page only (the table sorts in the browser); the *Sort* icon in the card's header sorts every
  row in the database, by one field, ascending or descending, and shows that field. The *Columns* icon picks
  the columns shown and their order (the arrows move one left or right); a download holds the page shown,
  with those columns in that order.
- **Advanced mode** (sidebar, off by default) adds the *Pivot* view and the *SQL* of each result, with its
  run id.
- **Results** sit in cards: a title built from the choices (*Sum of Amount by Order date (month), split by
  Region*), the number of groups or rows, a *Download* menu on the right. Bars and slices of a date take one
  band per period (`2026-03`, `2026-W10`, `2026-Q1`); lines and areas keep a time axis, one tick per period.
  Series colours avoid red, which the app keeps for errors, unless the report or you pick it: the *Colours*
  icon on a chart changes the colour of one series (a value of the split, a pie slice, or the single series),
  over the report's `colors`: one click on a quick colour (the palette, red, dark red, bright green, grey), or any
  colour in the picker (click its swatch again to apply it); *Reset colours* goes back to them.
- **Theme**: `yamlboard run` and `dev` start Streamlit on a light theme, navy (`#174D98`) for the selected
  controls and blue-grey (`#EEF3F8`) for the sidebar, the colours of the icon. A `STREAMLIT_THEME_*` variable or
  a `--theme.*` option overrides them (a `.streamlit/config.toml` does not: Streamlit ranks the environment
  above the file).
- **Downloads**: CSV and JSON (dates as ISO-8601 UTC strings), Excel (real dates and numbers, in UTC).
- **The page address** holds the report, the chart type, the row and column groups and the measure
  (`?report=Sales&sales:chart=Donut&sales:row=Region`: the labels shown, and `?report=audit - Cost` for a report in a folder), so a bookmark or a shared link reopens them. The rest
  stays out of the URL, which browsers and proxies limit in length.
- **Saved views** (title line): *Save this view* writes the report's choices to a JSON file (views, chart,
  groups, measure, grains, colours, filters, period, detail columns and their order, sort, page size); *Open a
  saved view* applies one and opens its report, so you can come back to a view later or hand it to someone.
  A period is saved by name, so *Last 7 days* ends when the view is opened; custom dates and other parameters
  are not saved. An opened view is checked against the report as it is now: what it no longer allows is
  skipped, and listed. Each report keeps its choices for the session when you switch to another and back.

## Dashboards

The *Dashboards* page (top of the board, next to *Reports*) shows a grid of charts, each a report seen through
a saved view: its chart, groups, measure, grains, colours, filters and period. Only the charts: no detail, no
pivot. A dashboard is a JSON file in the reports directory's `dashboards/` (and its sub-directories, listed as
*folder - name*, as reports are):

```json
{
  "yamlboard_dashboard": 1,
  "name": "Overview",
  "description": "Optional, under the name.",
  "grid": {"columns": 2, "height": 300},
  "cells": [
    {"view": "views/sales-by-region.json"},
    {"report": "sales", "title": "Orders by product", "view": {"chart": "donut", "row": "product", "measure": "count"}},
    {"report": "ash-activity", "title": "Database activity by wait class", "width": 2}
  ]
}
```

| Key | Meaning |
|---|---|
| `name`, `description` | The page's title and the line under it; no name: the file's |
| `grid.columns` | Charts per row, 1 to 6 (default 2) |
| `grid.height` | Each chart's height in pixels, 150 to 1200 (default 320) |
| `cells` | The charts, filling the grid row by row |
| `cells[].view` | A saved view file (*Save this view*), by its path from the dashboard's directory, or one report's part of such a file written in; none: the report's own view |
| `cells[].report` | The report's `id`; taken from the view file when the cell names one |
| `cells[].title` | The chart's title (default: the report's) |
| `cells[].width` | How many grid columns the chart takes (default 1); a cell that no longer fits its row starts the next one |

A cell's view is checked as an opened view is: what the report no longer allows is skipped, and a badge on the
cell lists it. A cell that cannot be drawn (no such report, a missing view file) says why, and the others are
drawn. A period ends when the dashboard is shown; custom dates are not saved, so a cell saved with them opens on
the report's default period. The *Open on the Reports page* icon (↗) of each cell opens its view on the *Reports* page.

Two icons on the title line. *Make a dashboard* (+) takes a name, the charts per row and saved view files, in
the order of the grid, and shows the dashboard at once, its views written in. *Open a dashboard file* (folder)
shows one from your disk; its views must be written in, since a file from your disk cannot point at others. A
dashboard shown that way is in no file of `dashboards/` yet: listed as *(not saved)*, it has a *Save* (download)
icon on its title line; put the file in `dashboards/` to list it for everyone. A view file is read only inside the
dashboards' directory. The example is
[`examples/reports/dashboards/overview.json`](examples/reports/dashboards/overview.json).

## Run log

Logging is compulsory. Each statement run on a database gets a UUID. The `yamlboard` logger writes it with the
connection (password hidden), the SQL and its binds, then the rows and the time; a failed run is a `WARNING`.

- **Where:** `logs/` in the directory you start yamlboard from (git-ignored), or another directory with
  `--log-dir DIR` (`YAMLBOARD_LOG_DIR`). The board writes `yamlboard.log`, the dev app `yamlboard-dev.log`,
  rotated at 10 MB, five old files kept. The records go to stderr too.
- **No log, no app:** before starting, yamlboard creates the directory if it is missing, checks its write
  permission and opens the log file. If any step fails, it prints why and exits with code 2.
- `YAMLBOARD_LOG_LEVEL=WARNING` keeps only the failures.

A run that fails (a missing database, a lock, a SQL error) shows the database's message and its run id in place
of the result, not a traceback.

In advanced mode, the *SQL* expander under a result shows its run id. A result read from the 10-minute cache
shows the id of the run that produced it.

```text
2026-10-02 11:14:06,780 INFO yamlboard run 1293ca53-… on duckdb:///:memory:
SELECT … LIMIT 50
-- binds: {'start': datetime.date(2025, 10, 1), 'end': datetime.date(2026, 10, 2)}
2026-10-02 11:14:06,783 INFO yamlboard run 1293ca53-…: 50 rows in 0.004 s
```

## Access

Not handled yet: every report is visible to everyone who can open the app, so put it behind your SSO.
`rights` in a report is read and kept for later.

## Command line

```bash
yamlboard validate reports/                     # exit 1 if a file does not load
yamlboard sql reports/sales.yml -p start=2026-01-01 -p end=2026-06-30 --dialect postgresql
yamlboard run reports/ [--datasources FILE] [--log-dir DIR] [streamlit options]
yamlboard dev [--url CONNECTION | --file PATH] [--yaml REPORT.yml] [--log-dir DIR] [streamlit options]
yamlboard lite site/                            # write yamlboard lite, a static site (see below)
```

## Dev app

`yamlboard dev` is the report writer's tool, a local web page in three tabs. Its source is a database or a
file (see [File sources](#file-sources)).

- **Schema**: the tables and views the connection can see; select one for its columns and types,
  then *Query this table*. With a file, the **File** tab replaces it.
- **Query**: run any SQL on its first rows (100 by default, up to 10,000). `:name` binds get a type and a
  value. Each column shows the field type it would get. *Draft the YAML* writes a report from the query:
  one field per column, every `:name` a required parameter.
- **Report**: edit a report YAML (drafted, or uploaded in the sidebar), see whether it loads and what each
  field allows, *Download* the file. *Test its SQL* sends the report's query back to the Query tab. Under the
  editor, the report **as `yamlboard run` shows it**, on the connection in the sidebar: parameters (opening on
  the values tested on the Query tab), filters, views, chart type, grains, colours, detail, saved views, and the
  *Advanced mode* toggle in the sidebar. What you see there is what the report's readers will see.

The draft's permissions follow the column types: text and booleans filter and group; dates and
timestamps group by grain; numbers sum, average and so on; an integer named `id` or `*_id` only counts
distinct values. Review them before publishing the file.

The connection, in the sidebar or with `--url`:

| You type | Opens |
|---|---|
| `duckdb:///data/w.duckdb`, `postgresql+psycopg://user:${PGPASSWORD}@host/db` | that SQLAlchemy URL, as written |
| `jdbc:postgresql://host:5432/db?user=rep&password=${PGPASSWORD}` | `postgresql+psycopg://rep:…@host:5432/db` |
| `jdbc:duckdb:/data/w.duckdb`, `data/w.duckdb` | the file, **read-only** (`jdbc:duckdb:` alone: in memory) |

The dev app runs whatever SQL is typed in. Every statement is rolled back, but connect with a read-only
account all the same, and keep the app on your own machine: it is not for report readers. It listens on
`localhost` only; `--server.address` overrides that, with a warning, since whoever reaches it runs SQL,
reads this machine's files and, through `${VAR}` in a typed URL, its environment variables.

### File sources

*Source: File* in the sidebar, or `--file PATH`, reads a file on this machine with an in-memory DuckDB.
Type its path (Enter opens it) or browse with the folder icon: the app's machine, from the opened file's
directory.
A glob (`/var/log/app/*.log`) reads every match as one; `.gz` files are read too. The format comes from the
extension, or is chosen:

- **CSV, JSON (array or one object per line), Parquet**: the *File* tab shows the columns, their field types and
  the first rows; *Query this file* starts a query with `read_csv_auto`, `read_json_auto` or `read_parquet`.
- **A log** (any other file): one row per line, cut into columns by a pattern. Pick a format (Apache / Nginx
  combined or common, syslog RFC 5424 or RFC 3164, Python logging, which yamlboard's own log uses, or ISO time,
  level, message), or write a *Custom pattern*. Each named group `(?P<name>…)` becomes a column; give it a
  type (text, integer, number, timestamp) and, for a timestamp, a `strptime` format (empty: ISO 8601, UTC
  when it has no offset). The tab shows how many lines match, a few that don't, and the first rows. A value
  that doesn't convert is empty, not an error.

The query is plain DuckDB SQL holding the file's absolute path, so a report drafted from it runs on the board
on a `duckdb:///:memory:` datasource, as long as the file stays there. Patterns run on RE2: no lookaround and no
backreference. A line that doesn't match is left out, so a multi-line entry (a stack trace, a SQL statement)
keeps its first line only.

### yamlboard lite: in the browser

`yamlboard lite DIR` writes the dev app's file side as a static site, for local log and data analysis with
nothing to install: open the page, drop a file in. It runs on [stlite](https://github.com/whitphx/stlite)
(Streamlit on Pyodide) and DuckDB-WASM, entirely in the browser tab, so **the file never leaves the machine**.
The `lite` workflow publishes it on GitHub Pages; locally, `python -m http.server -d DIR` serves it (a browser
will not run it from `file://`).

It has the same *File*, *Query* and *Report* tabs, on CSV, JSON, Parquet, logs and DuckDB database files (a
*Schema* tab then lists their tables). What differs:

- **No database server.** A browser cannot open a PostgreSQL connection; a DuckDB file is opened read-only.
- **A copy, not a path.** The page reads its own copy of the file, so *Draft the YAML* asks for the file's path
  on your machine and writes that into the report; the *Report* tab maps it back to the copy to run the view.
  No glob over several files: one file at a time.
- **Memory.** The tab holds the file several times over (up to 1 GB per file is accepted); a log of a few
  hundred MB is the practical limit.
- **Versions.** Streamlit 1.62 and DuckDB 1.4 (DuckDB-WASM 1.32), pinned in `lite/build.py`; first load fetches
  them from jsDelivr (about 10 s), then the browser caches them.

## Library

```python
from yamlboard import query as q
from yamlboard.engine import Workspace, columns_for, dialect_of, engine_for, run
from yamlboard.schema import Aggregation, Grain

ws = Workspace.open("reports")
report = ws.reports["sales"]
engine = engine_for(ws.url(report))
params = {"start": "2026-01-01", "end": "2026-06-30"}
stmt = q.aggregate(report, dialect_of(engine), params,
                   filters=[q.Filter("region", "in", ("North", None))],
                   rows=[q.Group("order_date", Grain.MONTH)],
                   measures=[q.Measure("amount", Aggregation.SUM)],
                   columns_map=columns_for(engine, report, params))   # match names ignoring case
df = run(engine, stmt)
```

Drafting a report from a query, without the dev app:

```python
from yamlboard import discover

engine = engine_for(discover.to_url("jdbc:duckdb:data/w.duckdb"))
sql = "select * from sales.orders"
print(discover.to_yaml(discover.draft_report(sql, discover.preview(engine, sql), "Orders")))
```

## Development

```bash
uv run pytest                                              # DuckDB only (a temporary .duckdb file), no server
YAMLBOARD_TEST_PG_URL=postgresql+psycopg://user:pw@localhost/db uv run pytest tests/test_postgres.py
```

## License

MIT
