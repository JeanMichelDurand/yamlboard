"""The Dashboards page: a grid of charts, each a report seen through a saved view (see ``dashboards.py``)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from yamlboard import charts, dashboards, settings
from yamlboard import query as q
from yamlboard.board import REPORT_PAGE, dashboards_dir, db_error, fetch, resolved_columns, shown_errors, workspace
from yamlboard.charts import measure_label
from yamlboard.dashboards import Cell, Dashboard, DashboardError
from yamlboard.engine import DB_ERRORS, dialect_of, engine_for
from yamlboard.schema import Report

UPLOADED = "\0opened"  # the option of a dashboard opened from the reader's disk; no file is named so

ws = workspace()
found, errors = dashboards.load_dir(dashboards_dir())
by_label = {d.label: d for d in found}
state = st.session_state


def show_opened(text: str | bytes, name: str) -> None:
    """Show a dashboard that is in no file of the dashboards' directory: opened from a disk, or just made."""
    try:
        state["dashboard_opened"] = dashboards.load(text, name=name)
    except DashboardError as e:
        state["dashboard_msg"] = f"{name}: {e}"
        return
    state["dashboard_json"] = text
    state["dashboard"] = UPLOADED


def open_file() -> None:
    """Read an uploaded dashboard, show it, then clear the uploader."""
    upload = state.get(f"dashboard_file:{state.get('dashboard_upload', 0)}")
    state["dashboard_upload"] = state.get("dashboard_upload", 0) + 1
    if upload is not None:
        show_opened(upload.getvalue(), upload.name.removesuffix(".json"))


def make(doc: dict) -> None:
    """Show the dashboard just made, then clear the form's files for the next one."""
    show_opened(settings.dumps(doc), doc["name"])
    state["new_dashboard_upload"] = state.get("new_dashboard_upload", 0) + 1


opened: Dashboard | None = state.get("dashboard_opened")
with st.sidebar:
    st.header("yamlboard", anchor=False)
    if errors:
        with st.expander(f"{len(errors)} dashboard(s) not loaded", icon=":material/error:"):
            for err in errors.values():
                st.caption(err)
    options = list(by_label) + ([UPLOADED] if opened else [])
    choice = st.selectbox("Dashboard", options, key="dashboard", bind="query-params",
                          format_func=lambda o: f"{opened.name} (not saved)" if o == UPLOADED and opened else o,
                          placeholder="No dashboard yet") if options else None
dashboard = opened if choice == UPLOADED else by_label.get(choice) if choice else None

# The title line: the dashboard's name, then its tools as icons, as on the Reports page.
head = st.container(horizontal=True, vertical_alignment="center")
head.title(dashboard.name if dashboard else "Dashboards", anchor=False, width="content")
tools = head.container(horizontal=True, width="content", gap="small")
if choice == UPLOADED:  # in no file of the dashboards' directory yet: save it
    tools.download_button(":material/download:", state["dashboard_json"], f"{opened.name}.json", "application/json",
                          on_click="ignore", type="tertiary", key="dashboard:save",
                          help=f"Save this dashboard to a file. Put it in `{dashboards_dir().name}/`, in the "
                          "reports' directory, to list it here for everyone; each cell can then take a `title` "
                          "and a `width`.")
with tools.popover(":material/folder_open:", help="Open a dashboard file", type="tertiary"):
    st.file_uploader("Open a dashboard file", type=["json"], on_change=open_file,
                     key=f"dashboard_file:{state.get('dashboard_upload', 0)}",
                     help="Its views must be written in: a file opened from your disk cannot point at others.")
with tools.popover(":material/add:", help="Make a dashboard from saved views", type="tertiary"):
    st.caption("Save the views with :material/bookmark: on the Reports page, then put them here, in the order "
               "of the grid: row by row.")
    name = st.text_input("Name", "My dashboard", key="new_dashboard:name")
    columns = st.number_input("Charts per row", 1, dashboards.MAX_COLUMNS, dashboards.DEFAULT_COLUMNS,
                              key="new_dashboard:columns")
    views = st.file_uploader("Saved views", type=["json"], accept_multiple_files=True,
                             key=f"new_dashboard:views:{state.get('new_dashboard_upload', 0)}")
    doc, skipped = dashboards.skeleton(name.strip() or "My dashboard",
                                       [(f.name, f.getvalue()) for f in views or []], int(columns))
    for s in skipped:
        st.caption(f":material/warning: {s}")
    st.button("Make the dashboard", icon=":material/dashboard:", type="primary", disabled=not doc["cells"],
              on_click=make, args=(doc,), key="new_dashboard:make")
if msg := state.pop("dashboard_msg", None):
    st.error(msg, icon=":material/error:")
if dashboard is None:
    st.info(f"No dashboard in `{dashboards_dir()}`. Make one from saved views with :material/add:, or open a "
            "dashboard file with :material/folder_open:.", icon=":material/dashboard:")
    st.stop()
if dashboard.description:
    st.caption(dashboard.description)


def open_in_board(report: Report, keys: dict) -> None:
    """The cell's view on the Reports page, as an opened view: chart, grouping, filters, colours, period."""
    state.update(keys)
    state["report"] = report.id
    st.switch_page(REPORT_PAGE)


def draw(cell: Cell, i: int) -> None:
    """One cell: its title, its chart and what of its view was skipped; an error in it stays in it."""
    report = ws.reports.get(cell.report or "")
    if cell.error or report is None or not report.active:
        st.markdown(f"**{cell.title or cell.report or 'Cell'}**")
        st.warning(cell.error or f"No report {cell.report!r}.", icon=":material/warning:")
        return
    plan = dashboards.plan(cell, report)
    try:
        params = q.coerce_parameters(report, plan.raw_params)
    except q.InvalidParameters as e:
        st.markdown(f"**{cell.title or report.title}**")
        st.error("\n".join(f"- {p}" for p in e.problems), icon=":material/error:")
        return
    plan = dashboards.with_grains(plan, report, params)

    header = st.container(horizontal=True, vertical_alignment="center", horizontal_alignment="distribute")
    text = header.container(gap=None)
    text.markdown(f"**{cell.title or report.title}**")
    text.caption(" · ".join(x for x in (dashboards.chart_title(plan, report), plan.period) if x))
    icons = header.container(horizontal=True, width="content", gap="small")
    if plan.skipped:
        icons.badge(f"{len(plan.skipped)} skipped", icon=":material/warning:", color="orange",
                    help="Skipped, the report no longer allows it:\n" + "\n".join(f"- {s}" for s in plan.skipped))
    if icons.button(":material/open_in_new:", key=f"dashboard:{i}:open", type="tertiary",
                    help="Open this view on the Reports page"):
        open_in_board(report, plan.keys)

    url = ws.url(report)
    try:
        cmap = resolved_columns(url, report.model_dump_json(), params)
    except q.ColumnMismatch as e:
        st.error(str(e), icon=":material/error:")
        return
    except DB_ERRORS as e:
        db_error(e)
        return
    rows, columns = plan.groups()
    stmt = q.aggregate(report, dialect_of(engine_for(url)), params, plan.filters, rows, columns, [plan.measure],
                       limit=q.MAX_GROUPS + 1, columns_map=cmap)
    with st.spinner("Running query…"):
        df = fetch(url, stmt.sql, stmt.binds)
    if len(df) > q.MAX_GROUPS:
        st.caption(f":material/warning: The first {q.MAX_GROUPS:,} groups only.")
    df = df.head(q.MAX_GROUPS)
    df = df.assign(**{plan.measure.alias: pd.to_numeric(df[plan.measure.alias])})
    if not rows:
        if columns:
            st.info("This view has no row group: no chart.", icon=":material/bar_chart:")
            return
        value = df[plan.measure.alias].iloc[0] if len(df) else None
        st.metric(measure_label(report, plan.measure), "–" if value is None or pd.isna(value) else f"{value:,}",
                  height=dashboard.height)
        return
    x, color = rows[0], columns[0] if columns else None
    dim, _ = charts.legend(df, plan.kind, report, x.field, color and color.field, x.grain, color and color.grain)
    chart = charts.build(df, plan.kind, report, x.field, plan.measure, color and color.field, x.grain,
                         color and color.grain, plan.colors.get(dim or charts.SERIES))
    st.altair_chart(chart.properties(height=dashboard.height))


for r, cells in enumerate(dashboards.grid_rows(dashboard.cells, dashboard.columns)):
    used = sum(c.width for c in cells)
    slots = st.columns([c.width for c in cells] + ([dashboard.columns - used] if used < dashboard.columns else []))
    for j, cell in enumerate(cells):
        with slots[j].container(border=True), shown_errors():
            draw(cell, r * dashboard.columns + j)
