"""A report as the board shows it: parameters, filters, views, chart, pivot and detail, all its controls.

The board's Reports page and the dev app's Report page both call ``show``, so a report under test looks as its
readers will see it.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from yamlboard import board, charts, export, maps, settings, times
from yamlboard import query as q
from yamlboard.board import db_error, fetch, resolved_columns, shown_errors
from yamlboard.charts import measure_label
from yamlboard.engine import DB_ERRORS, dialect_of, engine_for
from yamlboard.schema import (
    MAX_DECIMALS,
    SERIES_CHARTS,
    AxisSide,
    ChartType,
    Empty,
    FieldDef,
    FieldType,
    Parameter,
    Report,
    Units,
)
from yamlboard.settings import NUMBER_STYLES
from yamlboard.sort_table import NumberFormat, sort_table

PAGE_SIZES = settings.PAGE_SIZES
DEFAULT_PAGE_SIZE = 50
VIEWS = settings.VIEWS
CUSTOM = settings.CUSTOM  # a range entered as two dates
MAX_SEGMENTS = 4  # more grains than this: a dropdown
BASIC_VIEWS = ("Chart", "Map", "Detail")  # the pivot and the SQL need the advanced mode; Map, a map section
# The view's main choices go in the page address, so a link reopens them; every report-scoped widget keeps its
# value for the session, so a report left and opened again is as it was, and an export holds them all.
URL = {"bind": "query-params"}
REFRESH_EVERY = [None, 30, 60, 300, 900]  # seconds; None: off


def refresh_label(every: int | None) -> str:
    return "Off" if every is None else f"{every} s" if every < 60 else f"{every // 60} min"


KEEP = {"persist_state": "session"}
EMPTY_LABELS = {Empty.GAPS: "Gaps", Empty.ZERO: "Zero", Empty.CONNECT: "Connect"}
UNIT_LABELS = {Units.NONE: "None", Units.THOUSANDS: "Thousands (k)", Units.MILLIONS: "Millions (M)",
               Units.BILLIONS: "Billions (bn)", Units.PERCENT: "Percent (0.1 = 10%)"}
# The hint of a column's search box in the detail, by field type (see query.search_filter).
SEARCH_HINTS = {FieldType.STRING: "Contains…", FieldType.INTEGER: "12, 10..20, >=5, <=9",
                FieldType.NUMBER: "1 234,5, 10..20, >=5", FieldType.DATE: "2026-03-01, a..b",
                FieldType.TIMESTAMP: "2026-03-01 (the day), a..b", FieldType.BOOLEAN: "true or false"}
OP_LABELS = {"in": "is any of", "not_in": "is none of", "between": "between", "not_between": "is not between",
             "is_null": "is empty", "not_null": "is not empty", "contains": "contains",
             "not_contains": "does not contain"}
# A filter added by a click on the chart lands in the closed Filters popover: its button pulses and wears a "NEW"
# badge, both fading out. Injected for one run only, so the animation plays once. KEY: the popover's key class.
# A sort set from a column header pulses the Sort button the same way, without the badge (PULSE_CSS).
PULSE_CSS = """<style>
.KEY button { position: relative; animation: yb-new-filter-pulse 1s ease-out 3; border-radius: 0.5rem; }
@keyframes yb-new-filter-pulse { 0% { box-shadow: 0 0 0 0 rgba(232, 89, 12, 0.7); }
                                 100% { box-shadow: 0 0 0 0.7rem rgba(232, 89, 12, 0); } }
</style>"""
NEW_FILTER_CSS = PULSE_CSS + """<style>
.KEY button::after {
  content: "NEW"; position: absolute; top: -0.55rem; right: -0.9rem; padding: 0 0.3rem; border-radius: 0.5rem;
  font-size: 0.6rem; font-weight: 700; line-height: 1rem; color: white; background: #e8590c;
  pointer-events: none; animation: yb-new-filter-badge 6s forwards;
}
@keyframes yb-new-filter-badge { 0%, 75% { opacity: 1; } 100% { opacity: 0; } }
</style>"""


# Full screen: one card (Chart, Map or Detail) over the whole window, under the title line and its Filters. The
# sidebar, the page header and the controls line go; CONTROLS: the controls line's key class.
FOCUS_CSS = """<style>
section[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], header[data-testid="stHeader"],
.CONTROLS { display: none !important; }
[data-testid="stMainBlockContainer"] { max-width: none; padding: 1rem 1.5rem; }
</style>"""
FOCUS_HEIGHT = "calc(100vh - 13rem)"  # the card's chart, map or table in full screen: the window less the lines above
FOCUS_TABLE_HEIGHT = "calc(100vh - 17rem)"  # the table's pager stays in sight below it
# A chart's height is in pixels: Streamlit's height="stretch" leaves it 0 high in a container set to the window's.
FOCUS_CHART_HEIGHT = 680
FOCUS_NAMES = {"Chart": "the chart", "Map": "the map", "Detail": "the rows"}


def key_class(key: str) -> str:
    """The CSS class Streamlit gives an element keyed ``key`` (its frontend's rule)."""
    return "st-key-" + re.sub(r"[^a-zA-Z0-9_-]", "-", key.strip())


# --- report and parameters ---------------------------------------------------


def show(report: Report, url: str, reports: Mapping[str, Report], advanced: bool,
         defaults: Mapping[str, Any] | None = None) -> None:
    """``report`` on the database at ``url``; ``reports``: those an opened view may name. Its parameters go in
    the sidebar, the rest in the main area. ``defaults``: parameter values over the report's own (the dev app's,
    tested on its Query page); one that does not fit its parameter is ignored."""
    rid = report.id

    def default_of(p: Parameter) -> Any:
        try:
            value = q.coerce(p.type, (defaults or {}).get(p.name))
        except (TypeError, ValueError):
            value = None
        return value if value is not None else q.coerce(p.type, p.default)

    def import_settings() -> None:
        """Apply an uploaded settings file before the widgets are drawn, then clear the uploader."""
        state = st.session_state
        upload = state.get(f"settings_file:{state.get('settings_upload', 0)}")
        state["settings_upload"] = state.get("settings_upload", 0) + 1
        if upload is None:
            return
        try:
            keys, skipped = settings.restore(upload.getvalue(), reports)
        except settings.SettingsError as e:
            state["settings_msg"] = ("error", f"{upload.name}: {e}")
            return
        state.update(keys)
        state["settings_msg"] = ("warning" if skipped else "success",
                                 f"Imported {upload.name}." + "".join(f"\n- skipped {s}" for s in skipped))


    def param_widget(p: Parameter) -> Any:
        key = f"{rid}:param:{p.name}"
        label = p.title + (" *" if p.required else "")
        default = default_of(p)
        if p.type.temporal:
            return st.date_input(label + " (UTC)", value=default or times.today(), key=key)
        if p.type is FieldType.INTEGER:
            return st.number_input(label, value=default, step=1, key=key)
        if p.type is FieldType.NUMBER:
            return st.number_input(label, value=default, key=key)
        if p.type is FieldType.BOOLEAN:
            return st.checkbox(label, value=bool(default), key=key)
        return st.text_input(label, value=default or "", key=key)


    raw_params: dict[str, Any] = {}
    if report.parameters:
        st.sidebar.subheader("Parameters")
        params = report.param_map
        # A range is a period ending now, kept by name and resolved on every run, or two dates (Custom). The period
        # choice sits outside the form, so Custom shows its dates at once.
        chosen = []
        for rg in report.ranges:
            start, end = params[rg.start], params[rg.end]
            days = settings.day_range(report, rg)
            offered = settings.periods(report, rg)
            options = [*offered, CUSTOM]
            period = st.sidebar.selectbox(
                f"{start.title} – {end.title}", options, index=options.index(settings.default_period(report, rg, offered)),
                format_func=lambda n: times.PERIODS[n][0] if n in times.PERIODS else "Custom dates",
                key=f"{rid}:period:{rg.start}", **KEEP,
            )
            bounds = None if period == CUSTOM else times.period_bounds(period, days)
            if bounds:
                lo, hi = bounds
                if day := times.frozen():
                    shown = f"{lo} to {hi}" if days else f"{lo:%Y-%m-%d %H:%M} to {hi:%Y-%m-%d %H:%M}"
                    st.sidebar.caption(f"{shown}, UTC: the board's clock is set to {day}")
                else:
                    st.sidebar.caption(f"{lo} to today, UTC" if days else f"{lo:%Y-%m-%d %H:%M} to now ({hi:%H:%M}), UTC")
            chosen.append((rg, bounds))
        with st.sidebar.form(f"{rid}:params", border=False):
            in_range = set()
            for rg, bounds in chosen:
                start, end = params[rg.start], params[rg.end]
                if bounds is None:
                    picked = st.date_input(f"{start.title} – {end.title} (UTC)", value=settings.default_range(report, start),
                                           key=f"{rid}:range:{rg.start}")
                    bounds = (picked[0] if len(picked) > 0 else None, picked[1] if len(picked) > 1 else None)
                raw_params[rg.start], raw_params[rg.end] = bounds
                in_range |= {rg.start, rg.end}
            for p in report.parameters:
                if p.name not in in_range:
                    raw_params[p.name] = param_widget(p)
            st.form_submit_button("Run", icon=":material/play_arrow:", type="primary",
                                  help="Runs again: a period ending now moves to the current minute.")

    # The title line: the report's name, then its tools as icons, so the chart and the rows get the room.
    head = st.container(horizontal=True, vertical_alignment="center")
    head.title(report.title, anchor=False, width="content")
    tools = head.container(horizontal=True, width="content", gap="small")
    filter_slot = tools.container(width="content")
    if report.readme:
        with tools.popover(":material/info:", help="About this report", type="tertiary"):
            st.html(report.readme)
    # Save this view to a file, open one again. Built on click, so the file holds the view as it is then (a sort
    # changed in the detail's fragment too). Streamlit builds it on a thread with no script context, where
    # st.session_state is empty: read the session's own state, captured here.
    session = get_script_run_ctx().session_state
    tools.download_button(
        ":material/bookmark:",
        lambda: settings.dumps(settings.export(session.filtered_state, {rid: report}, rid)),
        f"{report.filename or rid}-view.json", "application/json", key=f"{rid}:save_view", on_click="ignore",
        type="tertiary", help="Save this view to a file: views, chart, grouping, measures, grains, partial periods hidden, "
        "colours, the chart's format, filters, period, detail columns, sort. Not custom dates: a period such as *Last 7 days* ends when the view is opened.",
    )
    with tools.popover(":material/folder_open:", help="Open a saved view", type="tertiary"):
        st.file_uploader("Open a saved view", type=["json"], key=f"settings_file:{st.session_state.get('settings_upload', 0)}",
                         on_change=import_settings)
    focus_key = f"{rid}:focus"  # the view shown full screen, if any
    if report.description and not st.session_state.get(focus_key):
        st.caption(report.description)
    if msg := st.session_state.pop("settings_msg", None):
        getattr(st, msg[0])(msg[1], icon=":material/bookmark:")

    try:
        params = q.coerce_parameters(report, raw_params)
    except q.InvalidParameters as e:
        st.error("\n".join(f"- {p}" for p in e.problems), icon=":material/error:")
        st.stop()

    dialect = dialect_of(engine_for(url))
    fields = report.field_map


    try:
        cmap = resolved_columns(url, report.model_dump_json(), params)
    except q.ColumnMismatch as e:
        st.error(str(e), icon=":material/error:")
        st.stop()
    except DB_ERRORS as e:
        db_error(e)
        st.stop()


    # --- filters -----------------------------------------------------------------

    filters_key = f"{rid}:filters"
    st.session_state.setdefault(filters_key, settings.view_filters(report))
    filters: list[q.Filter] = st.session_state[filters_key]
    filters[:] = [f for f in filters if f.field in fields and fields[f.field].filterable]  # the YAML may have changed


    def describe(f: q.Filter) -> str:
        values = ", ".join("(empty)" if v is None else str(v) for v in f.values)
        if f.op in ("between", "not_between"):
            lo, hi = f.values
            values = f"{lo if lo is not None else '…'} and {hi if hi is not None else '…'}"
        return f"{fields[f.field].title} {OP_LABELS[f.op]} {values}".strip()


    def filter_builder(field: FieldDef) -> q.Filter | None:
        """Widgets for one new filter on ``field``; the filter once complete, else None.

        Dates and numbers take a range, other fields a list of values, a text field a text its values contain, any
        field can match empty (NULL) values; each one can be negated.
        """
        key = f"{rid}:new:{field.name}"
        ranged = field.type.temporal or field.type is FieldType.NUMBER
        row = st.container(horizontal=True)
        exclude = row.segmented_control("Keep", ["Include", "Exclude"], default="Include", key=f"{key}:not",
                                        label_visibility="collapsed") == "Exclude"
        texts = ["Contains"] if field.type is FieldType.STRING else []
        match = row.segmented_control("Match", ["Range" if ranged else "Values", *texts, "Empty"],
                                      default="Range" if ranged else "Values", key=f"{key}:match",
                                      label_visibility="collapsed")
        if match is None:
            return None
        if match == "Empty":
            return q.Filter(field.name, "not_null" if exclude else "is_null")
        if match == "Contains":
            text = st.text_input("Text", key=f"{key}:text", placeholder="Any case", width=260)
            return q.Filter(field.name, "not_contains" if exclude else "contains", (text,)) if text else None
        if ranged:
            row = st.container(horizontal=True)
            if field.type.temporal:  # UTC days; a timestamp keeps the whole "To" day
                lo = row.date_input("From (UTC)", value=None, key=f"{key}:lo")
                hi = row.date_input("To (UTC)", value=None, key=f"{key}:hi")
            else:
                lo = row.number_input("Min", value=None, key=f"{key}:lo")
                hi = row.number_input("Max", value=None, key=f"{key}:hi")
            if lo is not None and hi is not None and lo > hi:
                st.caption(":red[The start is after the end.]")
                return None
            op = "not_between" if exclude else "between"
            return q.Filter(field.name, op, (lo, hi)) if lo is not None or hi is not None else None

        limit = 500
        others = [f for f in filters if f.field != field.name]  # this field's own filters would hide its values
        stmt = q.distinct_values(report, dialect, params, field.name, others, limit=limit + 1, columns_map=cmap)
        with st.spinner("Loading values…"):
            values = fetch(url, stmt.sql, stmt.binds)[field.name].tolist()
        if len(values) > limit:
            st.caption(f"First {limit} values only: add another filter first to reach the others.")
            values = values[:limit]
        values = [None if pd.isna(v) else v for v in values]
        picked = st.multiselect("Values", values, format_func=lambda v: "(empty)" if v is None else str(v),
                                key=f"{key}:values")
        return q.Filter(field.name, "not_in" if exclude else "in", tuple(picked)) if picked else None


    def add_filter(new: q.Filter) -> bool:
        """Append ``new``, or merge its values into the list filter of the same kind on its field; False if it
        changed nothing."""
        if new in filters:
            return False
        for i, f in enumerate(filters):
            if f.op in ("in", "not_in") and (f.field, f.op) == (new.field, new.op):
                merged = q.Filter(f.field, f.op, tuple(dict.fromkeys(f.values + new.values)))
                filters[i] = merged
                return merged != f
        filters.append(new)
        return True


    filterable = [f.name for f in fields.values() if f.filterable]
    title = lambda name: fields[name].title
    title_of = lambda name: fields[name].title if name in fields else str(name)
    filters_open = f"{rid}:filters_open"
    added = st.session_state.pop(f"{rid}:filters_added", None)  # by a click on the chart, the run before
    if added:  # out of sight in the Filters popover: say so, and flag its button for a few seconds
        st.toast("Filter added: " + " · ".join(describe(f) for f in added if f.field in fields)
                 + ". Remove it in Filters.", icon=":material/filter_alt:")
        st.html(NEW_FILTER_CSS.replace("KEY", key_class(filters_open)))
    with filter_slot.popover(f":material/filter_alt: {len(filters)}" if filters else ":material/filter_alt:",
                             help="Filters", type="tertiary", key=filters_open):  # keyed: stays open
        for i, f in enumerate(filters):
            row = st.container(horizontal=True, vertical_alignment="center")
            row.markdown(describe(f))
            if row.button("Remove", icon=":material/close:", key=f"{rid}:rm:{i}:{f}", type="tertiary"):
                filters.pop(i)
                st.rerun()
        if filterable:
            field = st.selectbox("Add a filter on", filterable, index=None, format_func=title,
                                 placeholder="Choose a field", key=f"{rid}:new_field", width=260)
            new = filter_builder(fields[field]) if field else None
            if st.button("Add filter", icon=":material/add:", disabled=new is None, key=f"{rid}:add"):
                add_filter(new)
                for k in [k for k in st.session_state if str(k).startswith(f"{rid}:new")]:
                    del st.session_state[k]  # start the next filter from blank widgets
                st.rerun()
            if len(filters) > 1:
                st.caption("Filters on different fields must all match. On one field, a row matches any inclusion "
                           "and no exclusion.")
        else:
            st.caption("This report has no filterable field.")
    if filters:  # what applies, readable with the expander closed
        st.caption(":material/filter_alt: " + " · ".join(describe(f) for f in filters))


    # --- views, grouping and measure ------------------------------------------------

    row_fields = [f.name for f in fields.values() if f.groupable_rows]
    col_fields = [f.name for f in fields.values() if f.groupable_columns]
    measures = settings.measures(report)
    view = report.view
    CHART_NAMES = {ChartType.COLUMN_STACKED: "Stacked column", ChartType.BAR_STACKED: "Stacked bar",
                   ChartType.COLUMN_PERCENT: "100% stacked column", ChartType.BAR_PERCENT: "100% stacked bar"}

    # One row of controls, each as wide as its content needs; it wraps on a narrow screen.
    top = st.container(horizontal=True, vertical_alignment="bottom", key=f"{rid}:controls")
    offered = [v for v in (VIEWS if advanced else BASIC_VIEWS) if v != "Map" or report.map]
    picked = top.segmented_control(  # one key per mode: the basic one has no pivot to keep
        "View", offered, selection_mode="multi",
        default=["Map"] if report.map and not view.rows else ["Pivot"] if advanced and not view.rows else ["Chart"],
        key=f"{rid}:mode:{'advanced' if advanced else 'basic'}", help="Pick several to stack them, top to bottom.", **KEEP,
    )
    modes = [v for v in VIEWS if v in picked]
    kind = None
    if "Chart" in modes:
        kind = top.selectbox(
            "Chart", list(ChartType), index=list(ChartType).index(view.chart), key=f"{rid}:chart", width=170, **URL,
            format_func=lambda c: CHART_NAMES.get(c, c.value.capitalize()),  # a selectbox shows no icon markup
        )
    aggregated = "Chart" in modes or "Pivot" in modes
    mapped = "Map" in modes

    def pick_measures() -> list[q.Measure]:
        return [measures[a] for a in top.multiselect(
            "Measures", list(measures), default=settings.default_measures(report), placeholder="Pick a measure",
            format_func=lambda a: measure_label(report, measures[a]), key=f"{rid}:measures", width=300, **URL,
            help="Several: one series each, unsplit (the split is left out); the chart's format (:material/tune:) "
                 "puts each on its own chart and axis. A map shows the first.",
        )]

    rows: list[q.Group] = []
    columns: list[q.Group] = []
    chosen: list[q.Measure] = []
    hide_partial = False
    if aggregated:
        row_picked = top.selectbox(
            "Group rows by", row_fields,
            index=row_fields.index(view.rows[0]) if view.rows and view.rows[0] in row_fields else None,
            format_func=title, placeholder="None", key=f"{rid}:row", width=200, **URL,
        )
        col_picked = None
        if not (kind and kind.one_dimension):  # a pie's only dimension is its slices: no split, and none kept
            col_picked = top.selectbox(
                "Split columns by", col_fields, index=col_fields.index(view.columns[0]) if view.columns else None,
                format_func=title, placeholder="None", key=f"{rid}:col", width=200, **URL,
            )
        chosen = pick_measures()
        grains = {}
        split_field = col_picked if len(chosen) < 2 else None  # several measures: no split
        for f in [fields[n] for n in (row_picked, split_field) if n and fields[n].grains]:
            # The default follows the period shown: a new period resets the grain to its default, a grain picked
            # for this period stays. Set through the state, not `default`, which a keyed widget reads only once.
            key, seen = f"{rid}:grain:{f.name}", f"{rid}:grain_default:{f.name}"
            auto = q.default_grain(report, f.name, params, filters)
            before = st.session_state.get(seen)
            st.session_state[seen] = auto
            if key not in st.session_state or (before is not None and before != auto):
                st.session_state[key] = auto
            span = q.timeframe(report, f.name, params, filters)
            tip = (f"Default: the finest grain with at most {times.AUTO_PERIODS} periods from {span[0]:%Y-%m-%d} "
                   f"to {span[1]:%Y-%m-%d}." if span else None)
            if len(f.grains) <= MAX_SEGMENTS:
                grains[f.name] = top.segmented_control(f"{f.title} by", f.grains, format_func=lambda g: g.value,
                                                       key=key, help=tip, **KEEP)
            else:  # minute to year would wrap the controls' line
                grains[f.name] = top.selectbox(f"{f.title} by", f.grains, format_func=lambda g: g.value.capitalize(),
                                               key=key, help=tip, width=130, **KEEP)
        rows = [q.Group(row_picked, grains.get(row_picked))] if row_picked else []
        if col_picked and len(chosen) > 1:
            st.caption(f"Several measures: not split by {title(col_picked)}.")
        elif col_picked and col_picked != row_picked:
            columns = [q.Group(col_picked, grains.get(col_picked))]
        elif col_picked:
            st.caption(f"{title(col_picked)} is already the row group: not split on columns.")
        if kind and kind.one_dimension and len(chosen) > 1:
            st.caption(f"A {kind.value} shows one measure: {measure_label(report, chosen[0])}.")
        if any(g.grain for g in rows + columns):
            st.session_state.setdefault(f"{rid}:hide_partial", view.hide_partial)  # not `value`: a sync sets it
            hide_partial = top.checkbox(
                "Hide partial periods", key=f"{rid}:hide_partial", **KEEP,
                help="Leave out the periods the result holds only part of: today, the first day of a period that "
                "starts at noon, a first or last period the data covers less than all the others (data from 14:00 on "
                "the first day, from midnight on the others). A partial period is lower for want of time, not of "
                "activity.",
            )

    elif mapped:
        chosen = pick_measures()

    every = None
    if advanced:  # for a log still being pulled (fetch --follow), or a table still being written
        every = top.selectbox("Auto-refresh", REFRESH_EVERY, format_func=refresh_label, key=f"{rid}:refresh_every",
                              width=130, help="Run the queries again on a timer, not from the cache", **KEEP)
        top.button(":material/refresh:", help="Run the queries again now, not from the cache", key=f"{rid}:refresh",
                   on_click=board.refresh, type="tertiary")

    if not modes:
        st.info("Pick a view. Pick several to stack them.", icon=":material/view_agenda:")
        st.stop()

    # Full screen: that view alone. Its controls are still drawn, hidden, so they keep their values.
    focus = st.session_state.get(focus_key)
    if focus not in modes:
        focus = st.session_state[focus_key] = None
    if focus:
        modes = [focus]
        st.html(FOCUS_CSS.replace("CONTROLS", key_class(f"{rid}:controls")))


    # --- results: one card per result, its title and downloads on top, its SQL below ------------

    def card_header(heading: str, subtitle: str, df: pd.DataFrame, name: str, slot: Any,
                    tools: Callable[[Any], pd.DataFrame] | None = None, view: str | None = None) -> pd.DataFrame:
        """The card's title and subtitle on the left, its tools and downloads on the right.

        ``tools`` draws more icons in the header and returns the frame to show and download. ``view``: the view the
        card's full-screen icon shows alone.
        """
        header = slot.container(horizontal=True, vertical_alignment="center", horizontal_alignment="distribute")
        text = header.container(gap=None)
        text.markdown(f"**{heading}**")
        if subtitle:
            text.caption(subtitle)
        icons = header.container(horizontal=True, width="content", gap="small", vertical_alignment="center")
        if tools:
            df = tools(icons)
        base = report.filename or report.id
        with icons.popover("Download", icon=":material/download:", type="tertiary"):
            for fmt in export.formats(report.formats):
                # Built on click, not on every rerun: an Excel file of 10,000 rows takes about half a second.
                st.download_button(export.LABELS[fmt], lambda fmt=fmt: export.render(df, fmt, sheet=name),
                                   f"{base}-{name}.{fmt.lower()}", export.MIME[fmt], icon=":material/download:",
                                   key=f"{rid}:{name}:{fmt.lower()}", on_click="ignore", width="stretch")
        if view:  # rerun the whole page, from the detail's fragment too
            full = st.session_state.get(focus_key) == view
            if icons.button(":material/fullscreen_exit:" if full else ":material/fullscreen:", key=f"{rid}:focus:{name}",
                            help="Leave full screen" if full else f"Full screen: {FOCUS_NAMES[view]} alone, the "
                            "filters still at hand", type="tertiary"):
                st.session_state[focus_key] = None if full else view
                st.rerun(scope="app")
        return df


    def show_sql(stmt: q.Statement, df: pd.DataFrame) -> None:
        if not advanced:
            return
        with st.expander("SQL", icon=":material/code:"):
            st.code(stmt.sql, language="sql")
            st.caption(f"Binds: {stmt.binds}")
            if run_id := df.attrs.get("run_id"):
                st.caption(f"Run `{run_id}` in the log (an earlier run when the result came from the cache).")


    def swatch(hex_color: str) -> str:
        """A pill's label: a square of the colour, as an inline SVG image (a pill shows images like icons)."""
        svg = f"<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'><rect width='16' height='16' rx='3' fill='{hex_color}'/></svg>"
        return f"![{hex_color}](data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()})"


    def color_picker(dim: str | None, values: list[str]) -> None:
        """Change the colour of one value of ``dim`` (of the single series without one); the others keep theirs."""
        key = dim or charts.SERIES
        mine: dict[str, dict[str, str]] = st.session_state.setdefault(f"{rid}:colors", {})
        hues = charts.palette(report, dim, values, mine.get(key))
        target = charts.SERIES
        if dim and not values:
            st.caption("No value to colour.")
            return
        if dim:
            value_key = f"{rid}:color_value:{dim}"
            if st.session_state.get(value_key) not in values:  # another period or filter: other values
                st.session_state.pop(value_key, None)
            target = st.selectbox("Measure" if dim == charts.MEASURE else title(dim), values, key=value_key)

        def store(widget: str, value: str) -> None:
            if st.session_state[widget]:  # a pill clicked off is None
                mine.setdefault(key, {})[value] = st.session_state[widget]

        # Keyed on the colour shown too: a reset or an opened view shows its colour, not the last one picked.
        picker = f"{rid}:color:{key}:{target}:{hues[target]}"
        st.pills("Quick colours", charts.QUICK_COLORS, format_func=swatch, key=picker + ":quick",
                 on_change=store, args=(picker + ":quick", target))
        st.color_picker("Any colour", hues[target], key=picker, on_change=store, args=(picker, target),
                        help="Pick, then click the swatch again to apply it.")
        if mine.get(key):
            st.caption("Changed: " + ", ".join("series" if v == charts.SERIES else v for v in mine[key]))
            st.button("Reset colours", icon=":material/format_color_reset:", key=f"{rid}:color_reset:{key}",
                      type="tertiary", on_click=lambda: mine.pop(key, None))


    def format_chart(shown: list[q.Measure], drawn: Mapping[str, ChartType]) -> None:
        """The chart's format: empty cells; with several measures, each one's chart and axis; each axis's bounds,
        scale and labels. Opens on the view's own (``view.empty``, ``view.measures``, ``view.axes``)."""
        state = st.session_state
        defaults = settings.format_keys(report)
        for m in shown:
            defaults.setdefault(f"{rid}:series_chart:{m.alias}", None)
            defaults.setdefault(f"{rid}:series_axis:{m.alias}", AxisSide.PRIMARY)
        for k, v in defaults.items():
            state.setdefault(k, v)  # not each widget's `value`: an opened view or a YAML edit sets them
        st.segmented_control(
            "Empty cells", list(Empty), format_func=EMPTY_LABELS.get, key=f"{rid}:empty", **KEEP,
            help="A period with no rows, or a split value missing at a row group. **Gaps**: nothing drawn, a line "
                 "breaks. **Zero**: a count or a sum reads 0 (no rows, nothing counted); an average, a min, a max or a "
                 "median stays a gap. **Connect**: a line joins the points either side.",
        )
        st.checkbox(
            "Trim empty periods", key=f"{rid}:trim_empty", **KEEP,
            help="The time axis runs from the first period with data to the last, not over the whole period shown: "
                 "a year asked for, 90 days of data, the chart shows the 90 days. Empty periods between keep their "
                 "format above.",
        )
        sides = [AxisSide.PRIMARY]
        if drawn:
            st.markdown(":material/functions: **Measures**")
            for m in shown:
                row = st.container(horizontal=True, vertical_alignment="bottom")
                if kind in charts.COMBO:
                    row.selectbox(measure_label(report, m), [None, *SERIES_CHARTS], key=f"{rid}:series_chart:{m.alias}",
                                  format_func=lambda c: "As the chart" if c is None else c.value.capitalize(),
                                  placeholder="As the chart", width=200, **KEEP)
                else:
                    row.markdown(measure_label(report, m))
                row.segmented_control("Axis", list(AxisSide), key=f"{rid}:series_axis:{m.alias}",
                                      format_func=lambda a: a.value.capitalize(), **KEEP)
            fmt, _ = settings.format_of(state, report)
            if {fmt.axis_of(m.alias) for m in shown} == set(AxisSide):
                sides.append(AxisSide.SECONDARY)
        for side in sides:
            st.markdown(f":material/straighten: **{side.value.capitalize()} axis**")
            if side is AxisSide.PRIMARY and kind in charts.PERCENT:
                st.caption("A 100% chart's axis runs from 0 to 100%.")
                continue
            k = f"{rid}:axis:{side.value}:"
            row = st.container(horizontal=True, vertical_alignment="bottom")
            row.number_input("Minimum", value=None, key=k + "min", placeholder="Auto", width=120, **KEEP)
            row.number_input("Maximum", value=None, key=k + "max", placeholder="Auto", width=120, **KEEP)
            row.checkbox("Log scale", key=k + "log", help="Zero and negative values are left out.", **KEEP)
            row = st.container(horizontal=True, vertical_alignment="bottom")
            row.selectbox("Display units", list(Units), format_func=UNIT_LABELS.get, key=k + "units", width=160, **KEEP)
            row.selectbox("Decimals", [None, *range(MAX_DECIMALS + 1)], key=k + "decimals", width=90,
                          format_func=lambda d: "Auto" if d is None else str(d), placeholder="Auto", **KEEP)
            row.text_input("Before", key=k + "prefix", max_chars=8, placeholder="$", width=75, **KEEP)
            row.text_input("After", key=k + "suffix", max_chars=8, placeholder=" €", width=75, **KEEP)
        for problem in settings.format_of(state, report)[1]:
            st.caption(f":red[{problem}: not applied.]")
        st.button("Reset the format", icon=":material/restart_alt:", key=f"{rid}:format_reset", type="tertiary",
                  on_click=lambda: state.update(defaults), help="Back to the view's format")


    def aggregate_views() -> None:
        if not chosen:
            st.info("Pick a measure.", icon=":material/functions:")
            return
        stmt = q.aggregate(report, dialect, params, filters, rows, columns, chosen, limit=q.MAX_GROUPS + 1,
                           columns_map=cmap)
        with st.spinner("Running query…"):
            df = fetch(url, stmt.sql, stmt.binds)
        truncated = len(df) > q.MAX_GROUPS
        df = df.head(q.MAX_GROUPS)
        # PostgreSQL returns sum and avg of integers as NUMERIC, which pandas keeps as Decimal objects.
        df = df.assign(**{m.alias: pd.to_numeric(df[m.alias]) for m in chosen})
        x, color = (rows[0] if rows else None), (columns[0] if columns else None)
        charted = "Chart" in modes and x is not None
        shown = chosen[:1] if kind and kind.one_dimension else chosen  # the chart's measures: a pie shows one
        fmt, _ = settings.format_of(st.session_state, report)
        # The chart's rows: the result and its empty cells (a day with no rows, a region with no Gizmo).
        chart_df, unfilled = df, False
        if charted:
            span = q.timeframe(report, x.field, params, filters) if x.grain and fields[x.field].type.temporal else None
            filled = charts.fill(df, kind, report, x.field, shown, color and color.field, x.grain, fmt.empty, span,
                                 trim=fmt.trim_empty)
            chart_df, unfilled = (df, True) if filled is None else (filled, False)
        hidden = 0
        if hide_partial:
            spans = {}
            for g in [g for g in rows + columns if g.grain and g.grain in q.PERIODS]:
                span_stmt = q.extents(report, dialect, params, g, filters, columns_map=cmap)
                spans[g] = fetch(url, span_stmt.sql, span_stmt.binds)

            def whole(frame: pd.DataFrame) -> pd.Series:
                keep = pd.Series(True, index=frame.index)
                for g, data_spans in spans.items():
                    keep &= q.complete_periods(frame[g.field], report, g.field, g.grain, params, filters,
                                               spans=data_spans)
                return keep

            chart_df = chart_df[whole(chart_df)]
            kept = whole(df)
            hidden = int((~kept).sum())
            df = df[kept]
        heading = charts.title(report, chosen, x and x.field, x and x.grain, color and color.field, color and color.grain)
        dim, values = (charts.legend(chart_df, kind, report, x.field, color and color.field, x.grain,
                                     color and color.grain, shown) if charted else (None, []))
        drawn = charts.marks(kind, shown, fmt) if charted else {}

        def chart_tools(slot: Any) -> pd.DataFrame:
            with slot.popover(":material/palette:", help="Colours", type="tertiary", key=f"{rid}:colors_open"):
                color_picker(dim, values)
            if not kind.one_dimension:
                with slot.popover(":material/tune:", help="Format the chart: empty cells, each measure's chart and "
                                  "axis, the axes' bounds, scale and labels", type="tertiary", key=f"{rid}:format_open"):
                    format_chart(shown, drawn)
            return df

        with st.container(border=True):
            card_header(heading, f"{len(df):,} groups" + (f" · {len(filters)} filters" if filters else "")
                        + (f" · {hidden:,} partial hidden" if hidden else ""), df,
                        "aggregate", st, chart_tools if charted else None, view="Chart" if "Chart" in modes else None)
            if truncated:
                st.warning(f"More than {q.MAX_GROUPS:,} groups: showing the first {q.MAX_GROUPS:,}. "
                           "Use a coarser grain or a filter.", icon=":material/warning:")
            if "Chart" in modes:
                if not rows and not columns:
                    tiles = st.container(horizontal=True)
                    for m in chosen:
                        value = df[m.alias].iloc[0] if len(df) else None
                        tiles.metric(measure_label(report, m), "–" if value is None or pd.isna(value) else f"{value:,}")
                elif not rows:
                    st.info("Pick a row group to draw a chart.", icon=":material/bar_chart:")
                else:
                    if unfilled:
                        st.caption(f"With its empty cells, the chart would pass {q.MAX_GROUPS:,} groups: they are "
                                   "not drawn. Use a coarser grain.")
                    mine = st.session_state.get(f"{rid}:colors", {}).get(dim or charts.SERIES)
                    split = None if kind.one_dimension else color
                    chart_key = f"{rid}:chart_pick"

                    def pick() -> None:  # a click on a mark or on the legend: a filter on the whole report
                        state = st.session_state[chart_key].selection
                        added = [f for name, value, grain in charts.picked(chart_df, report, state, x.field,
                                                                           split and split.field, x.grain,
                                                                           split and split.grain)
                                 if add_filter(f := q.pick_filter(fields[name], value, grain))]
                        if added:  # the next run flags the Filters button
                            st.session_state[f"{rid}:filters_added"] = added
                        st.session_state[f"{rid}:page"] = 1

                    clickable = [f for fs in charts.selections(kind, x.field, split and split.field, drawn).values()
                                 for f in fs]  # none on a line with one series
                    selectable = bool(clickable) and all(fields[f].filterable for f in clickable)
                    st.altair_chart(charts.build(chart_df, kind, report, x.field, shown, color and color.field,
                                                 x.grain, color and color.grain, mine, selectable=selectable, fmt=fmt),
                                    key=chart_key, on_select=pick if selectable else "ignore",
                                    height=FOCUS_CHART_HEIGHT if focus else "content")
                    if selectable:
                        st.caption("Click a column or a bar to filter the report on its row group." if drawn else
                                   "Click a bar, a slice, a line or a legend entry to filter the report on it.")
            if "Pivot" in modes:
                index = [g.field for g in rows]
                if columns and index:  # one measure: several are never split
                    # One row per group already; pivot_table would drop the NULL groups the chart shows.
                    pivot = df.set_index([*index, columns[0].field])[chosen[0].alias].unstack(columns[0].field)
                    pivot.columns = ["(empty)" if pd.isna(c) else str(c) for c in pivot.columns]
                    st.dataframe(pivot)
                else:
                    st.dataframe(df, hide_index=True,
                                 column_config={f: fields[f].title for f in df.columns if f in fields}
                                 | {m.alias: measure_label(report, m) for m in chosen})
            show_sql(stmt, df)


    def map_view() -> None:
        """The rows on a map: the database groups them by cell of a grid, the browser gathers cells by zoom."""
        if not chosen:
            st.info("Pick a measure.", icon=":material/functions:")
            return
        m = chosen[0]
        label = measure_label(report, m)
        named = label[:1].lower() + label[1:]  # in a sentence: "the average of Power (kW)"
        if m.aggregation not in q.MAP_AGGREGATIONS:
            st.info(f"A map cannot show the {named}: a cluster of places could not combine it. Pick a "
                    "count, a sum, an average, a min or a max.", icon=":material/map:")
            return
        levels_stmt = q.map_levels(report, dialect, params, filters, columns_map=cmap)
        with st.spinner("Placing the rows…"):
            levels = fetch(url, levels_stmt.sql, levels_stmt.binds).iloc[0].to_dict()
        size = q.map_grid(levels)
        stmt = q.map_cells(report, dialect, params, size, filters, m, columns_map=cmap)
        with st.spinner("Running query…"):
            cells = fetch(url, stmt.sql, stmt.binds)
        truncated = len(cells) > q.MAX_CELLS
        cells = cells.head(q.MAX_CELLS)
        total = int(pd.to_numeric(cells["yb_n"]).sum()) if len(cells) else 0
        where = f"{len(cells):,} places" if size == q.GRID[0] else f"{len(cells):,} cells of {size:g}°"
        subtitle = f"{total:,} rows in {where}" + (f" · {len(filters)} filters" if filters else "")
        shown = cells.rename(columns={"yb_lat": "lat", "yb_lon": "lon", "yb_n": "rows", "yb_s": "sum", "yb_c": "count",
                                      "yb_min": "min", "yb_max": "max", "yb_label": "label"})
        with st.container(border=True):
            card_header(f"{label} by place", subtitle, shown, "map", st, view="Map")
            if truncated:
                st.warning(f"More than {q.MAX_CELLS:,} cells even on a {size:g}° grid: showing {q.MAX_CELLS:,}. "
                           "Add a filter.", icon=":material/warning:")
            if len(chosen) > 1:
                st.caption(f"A map shows one measure: {label}.")
            measure = maps.map_measure(m.field, m.aggregation, label)
            maps.map_chart(cells, report.map, measure, key=f"{rid}:map", color=view.color or charts.PALETTE[0],
                           signature=f"{stmt.sql}|{sorted(stmt.binds.items(), key=lambda kv: kv[0])!r}",
                           height=FOCUS_HEIGHT if focus else maps.HEIGHT)
            sized = "Circles are sized by the" + (f" {named}." if measure.additive else
                                                  f" rows, coloured by the {named} (light: low, dark: high).")
            st.caption(f"{sized} Zoomed out, nearby places are gathered; zoom in, or click a group, to split it.")
            show_sql(stmt, shown)


    def turn_page(step: int) -> None:
        st.session_state[f"{rid}:page"] = st.session_state.get(f"{rid}:page", 1) + step


    def move_column(col: str, step: int) -> None:
        key = f"{rid}:detail_cols"
        cols = list(st.session_state.get(key) or [])
        if col in cols and 0 <= (i := cols.index(col)) + step < len(cols):
            cols[i], cols[i + step] = cols[i + step], cols[i]
            st.session_state[key] = cols


    @st.fragment  # the pager, the page size, the sort and the columns rerun this card only, not the chart above
    def detail_view() -> None:
        page_key = f"{rid}:page"
        order_key, sorted_key = f"{rid}:order", f"{rid}:sorted"  # the sort keys; set by a header click
        refilter_key = f"{rid}:refilter"
        if st.session_state.pop(refilter_key, False):  # a search added a filter: it applies to the whole report
            st.rerun(scope="app")
        with st.container(border=True), shown_errors():
            header = st.container()
            # (field, descending) pairs, first one first, less the fields the YAML dropped.
            order = st.session_state[order_key] = [(f, bool(d)) for f, d in st.session_state.get(order_key, [])
                                                   if f in fields]
            size = st.session_state.get(f"{rid}:page_size", DEFAULT_PAGE_SIZE)  # its widget is on the pager line
            count_stmt = q.count(report, dialect, params, filters, columns_map=cmap)
            with st.spinner("Counting rows…"):
                total = int(fetch(url, count_stmt.sql, count_stmt.binds)["n"].iloc[0])
            pages = max(1, -(-total // size))
            page = min(max(st.session_state.get(page_key, 1), 1), pages)  # a new filter can shrink the result
            st.session_state[page_key] = page
            stmt = q.detail(report, dialect, params, filters, order=order, limit=size, offset=(page - 1) * size, columns_map=cmap)
            with st.spinner("Running query…"):
                df = fetch(url, stmt.sql, stmt.binds)

            def set_order(new: list[tuple[str, bool]]) -> None:
                st.session_state.update({order_key: new, page_key: 1})

            def add_key() -> None:
                if field := st.session_state.pop(f"{rid}:sort_add", None):
                    set_order([*order, (field, False)])

            def detail_tools(slot: Any) -> pd.DataFrame:
                sort_open = f"{rid}:sort_open"
                if st.session_state.pop(sorted_key, False):  # set from a header, out of sight: flag the button
                    st.html(PULSE_CSS.replace("KEY", key_class(sort_open)))
                with slot.popover(f":material/sort: {len(order)}" if order else ":material/sort:",
                                  help="Sort every row. Or click a column's arrows: a click sorts by that column "
                                  "alone, a Shift+click adds it to the sort.", type="tertiary",
                                  key=sort_open):  # keyed: stays open when its label changes
                    for i, (f, d) in enumerate(order):
                        line = st.container(horizontal=True, vertical_alignment="center", gap="small")
                        line.markdown(f"{i + 1}. {title(f)}", width="stretch")
                        line.button("Descending" if d else "Ascending", key=f"{rid}:sort_dir:{f}",
                                    icon=":material/arrow_downward:" if d else ":material/arrow_upward:",
                                    on_click=set_order, args=([(g, not e if g == f else e) for g, e in order],),
                                    help="Click to reverse", type="tertiary")
                        moved = [order[:i - 1] + [order[i], order[i - 1]] + order[i + 1:],
                                 order[:i] + order[i + 1:i + 2] + [order[i]] + order[i + 2:]]
                        line.button(":material/keyboard_arrow_up:", key=f"{rid}:sort_up:{f}", disabled=i == 0,
                                    on_click=set_order, args=(moved[0],), help="Sort by it first", type="tertiary")
                        line.button(":material/keyboard_arrow_down:", key=f"{rid}:sort_down:{f}",
                                    disabled=i == len(order) - 1, on_click=set_order, args=(moved[1],),
                                    help="Sort by it later", type="tertiary")
                        line.button(":material/close:", key=f"{rid}:sort_rm:{f}", help="Remove",
                                    on_click=set_order, args=([o for o in order if o[0] != f],), type="tertiary")
                    st.selectbox("Then by" if order else "Sort by", [f for f in fields if f not in dict(order)],
                                 index=None, format_func=title, placeholder="Add a column", key=f"{rid}:sort_add",
                                 on_change=add_key, width=240)
                    if order:
                        st.button("Clear the sort", icon=":material/close:", key=f"{rid}:sort_clear",
                                  on_click=set_order, args=([],), type="tertiary")
                cols_key = f"{rid}:detail_cols"
                shown = [f.name for f in fields.values() if f.visible and f.name in df.columns] or list(df.columns)
                # The picked columns in their order, which the arrows change. Set through the state, not `default`,
                # so the arrows can rewrite it; the columns of a result that changed are dropped.
                st.session_state[cols_key] = [c for c in st.session_state.get(cols_key, shown) if c in df.columns]
                with slot.popover(":material/view_column:", help="Columns and their order", type="tertiary"):
                    picked = st.multiselect("Columns", list(df.columns), format_func=title_of, key=cols_key,
                                            placeholder="Pick the columns to show", **KEEP)
                    if not picked:
                        st.caption("No column picked: showing the default ones.")
                    for i, c in enumerate(picked):
                        line = st.container(horizontal=True, vertical_alignment="center", gap="small")
                        line.markdown(title_of(c), width="stretch")
                        line.button(":material/arrow_upward:", key=f"{rid}:col_up:{c}", help="Move left",
                                    disabled=i == 0, on_click=move_column, args=(c, -1), type="tertiary")
                        line.button(":material/arrow_downward:", key=f"{rid}:col_down:{c}", help="Move right",
                                    disabled=i == len(picked) - 1, on_click=move_column, args=(c, 1), type="tertiary")
                with slot.popover(":material/decimal_increase:", help="Number format", type="tertiary"):
                    st.selectbox("Separators", list(NUMBER_STYLES), key=f"{rid}:number_style", width=160, **KEEP)
                    st.number_input("Decimals", 0, MAX_DECIMALS, NumberFormat.decimals, key=f"{rid}:decimals",
                                    width=160, help="None on an integer column", **KEEP)
                return df[picked or shown]

            df = card_header("Detail", f"{len(filters)} filters" if filters else "", df, "detail", header, detail_tools,
                             view="Detail")
            number = NumberFormat(st.session_state.get(f"{rid}:decimals", NumberFormat.decimals),
                                  st.session_state.get(f"{rid}:number_style", NumberFormat.style))

            def search(name: str, text: str) -> None:  # a search under a header: a filter on every row
                try:
                    add_filter(q.search_filter(fields[name], text))
                except q.NotAllowed as e:
                    st.toast(f"{fields[name].title}: {e}", icon=":material/error:")
                    return
                st.session_state[page_key] = 1
                st.session_state[refilter_key] = True  # the chart and the filter list are outside this fragment

            sort_table(df, {f.name: f.title for f in fields.values()}, order, key=f"{rid}:table",
                       on_sort=lambda new: st.session_state.update({order_key: new, page_key: 1, sorted_key: True}),
                       height=FOCUS_TABLE_HEIGHT if focus else "560px",
                       number=number, integers={f.name for f in fields.values() if f.type is FieldType.INTEGER},
                       searches={c: SEARCH_HINTS[fields[c].type] for c in df.columns
                                 if c in fields and fields[c].filterable}, on_search=search)
            pager = st.container(horizontal=True, vertical_alignment="center", horizontal_alignment="center")
            pager.selectbox("Rows per page", PAGE_SIZES, index=PAGE_SIZES.index(DEFAULT_PAGE_SIZE), width=130,
                            format_func=lambda n: f"{n} / page", label_visibility="collapsed",
                            key=f"{rid}:page_size", on_change=lambda: st.session_state.update({page_key: 1}), **KEEP)
            pager.button(":material/chevron_left:", help="Previous page", key=f"{rid}:prev", disabled=page <= 1,
                         on_click=turn_page, args=(-1,), type="tertiary")
            pager.markdown(f"Page **{page:,}** of {pages:,}")
            pager.button(":material/chevron_right:", help="Next page", key=f"{rid}:next", disabled=page >= pages,
                         on_click=turn_page, args=(1,), type="tertiary")
            pager.caption(f"{total:,} rows found")
            show_sql(stmt, df)


    if "Chart" in modes or "Pivot" in modes:
        with shown_errors():
            aggregate_views()
    if "Map" in modes:
        with shown_errors():
            map_view()
    if "Detail" in modes:
        detail_view()
    board.auto_refresh(f"{rid}:page_run", every)
