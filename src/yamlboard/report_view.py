"""A report as the board shows it: parameters, filters, views, chart, pivot and detail, all its controls.

The board's Reports page and the dev app's Report page both call ``show``, so a report under test looks as its
readers will see it.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from yamlboard import charts, export, settings, times
from yamlboard import query as q
from yamlboard.board import db_error, fetch, resolved_columns, shown_errors
from yamlboard.charts import measure_label
from yamlboard.engine import DB_ERRORS, dialect_of, engine_for
from yamlboard.schema import ChartType, FieldDef, FieldType, Parameter, Report

PAGE_SIZES = settings.PAGE_SIZES
DEFAULT_PAGE_SIZE = 50
VIEWS = settings.VIEWS
CUSTOM = settings.CUSTOM  # a range entered as two dates
MAX_SEGMENTS = 4  # more grains than this: a dropdown
BASIC_VIEWS = ("Chart", "Detail")  # the pivot and the SQL need the advanced mode
# The view's main choices go in the page address, so a link reopens them; every report-scoped widget keeps its
# value for the session, so a report left and opened again is as it was, and an export holds them all.
URL = {"bind": "query-params"}
KEEP = {"persist_state": "session"}
OP_LABELS = {"in": "is any of", "not_in": "is none of", "between": "between", "not_between": "is not between",
             "is_null": "is empty", "not_null": "is not empty"}


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
        type="tertiary", help="Save this view to a file: views, chart, grouping, measure, grains, colours, filters, "
        "period, detail columns, sort. Not custom dates: a period such as *Last 7 days* ends when the view is opened.",
    )
    with tools.popover(":material/folder_open:", help="Open a saved view", type="tertiary"):
        st.file_uploader("Open a saved view", type=["json"], key=f"settings_file:{st.session_state.get('settings_upload', 0)}",
                         on_change=import_settings)
    if report.description:
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
    st.session_state.setdefault(filters_key, [])
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

        Dates and numbers take a range, other fields a list of values, any field can match empty (NULL)
        values; each one can be negated.
        """
        key = f"{rid}:new:{field.name}"
        ranged = field.type.temporal or field.type is FieldType.NUMBER
        row = st.container(horizontal=True)
        exclude = row.segmented_control("Keep", ["Include", "Exclude"], default="Include", key=f"{key}:not",
                                        label_visibility="collapsed") == "Exclude"
        match = row.segmented_control("Match", ["Range" if ranged else "Values", "Empty"],
                                      default="Range" if ranged else "Values", key=f"{key}:match",
                                      label_visibility="collapsed")
        if match is None:
            return None
        if match == "Empty":
            return q.Filter(field.name, "not_null" if exclude else "is_null")
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


    def add_filter(new: q.Filter) -> None:
        """Append ``new``, or merge its values into the list filter of the same kind on its field."""
        for i, f in enumerate(filters):
            if f.op in ("in", "not_in") and (f.field, f.op) == (new.field, new.op):
                filters[i] = q.Filter(f.field, f.op, tuple(dict.fromkeys(f.values + new.values)))
                return
        filters.append(new)


    filterable = [f.name for f in fields.values() if f.filterable]
    title = lambda name: fields[name].title
    title_of = lambda name: fields[name].title if name in fields else str(name)
    with filter_slot.popover(f":material/filter_alt: {len(filters)}" if filters else ":material/filter_alt:",
                             help="Filters", type="tertiary", key=f"{rid}:filters_open"):  # keyed: stays open
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
    CHART_NAMES = {ChartType.COLUMN_STACKED: "Stacked column", ChartType.BAR_STACKED: "Stacked bar"}

    # One row of controls, each as wide as its content needs; it wraps on a narrow screen.
    top = st.container(horizontal=True, vertical_alignment="bottom")
    picked = top.segmented_control(  # one key per mode: the basic one has no pivot to keep
        "View", VIEWS if advanced else BASIC_VIEWS, selection_mode="multi",
        default=["Pivot"] if advanced and not view.rows else ["Chart"],
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
    rows: list[q.Group] = []
    columns: list[q.Group] = []
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
        default_m = q.Measure(view.measures[0].field, view.measures[0].aggregation).alias
        measure = measures[top.selectbox(
            "Measure", list(measures), index=list(measures).index(default_m) if default_m in measures else 0,
            format_func=lambda a: measure_label(report, measures[a]), key=f"{rid}:measure", width=240, **URL,
        )]
        grains = {}
        for f in [fields[n] for n in (row_picked, col_picked) if n and fields[n].grains]:
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
        if col_picked and col_picked != row_picked:
            columns = [q.Group(col_picked, grains.get(col_picked))]
        elif col_picked:
            st.caption(f"{title(col_picked)} is already the row group: not split on columns.")

    if not modes:
        st.info("Pick a view. Pick several to stack them.", icon=":material/view_agenda:")
        st.stop()


    # --- results: one card per result, its title and downloads on top, its SQL below ------------

    def card_header(heading: str, subtitle: str, df: pd.DataFrame, name: str, slot: Any,
                    tools: Callable[[Any], pd.DataFrame] | None = None) -> pd.DataFrame:
        """The card's title and subtitle on the left, its tools and downloads on the right.

        ``tools`` draws more icons in the header and returns the frame to show and download.
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
            target = st.selectbox(title(dim), values, key=value_key)

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


    def aggregate_views() -> None:
        stmt = q.aggregate(report, dialect, params, filters, rows, columns, [measure], limit=q.MAX_GROUPS + 1,
                           columns_map=cmap)
        with st.spinner("Running query…"):
            df = fetch(url, stmt.sql, stmt.binds)
        truncated = len(df) > q.MAX_GROUPS
        df = df.head(q.MAX_GROUPS)
        # PostgreSQL returns sum and avg of integers as NUMERIC, which pandas keeps as Decimal objects.
        df = df.assign(**{measure.alias: pd.to_numeric(df[measure.alias])})
        x, color = (rows[0] if rows else None), (columns[0] if columns else None)
        heading = charts.title(report, measure, x and x.field, x and x.grain, color and color.field, color and color.grain)
        charted = "Chart" in modes and x is not None
        dim, values = (charts.legend(df, kind, report, x.field, color and color.field, x.grain, color and color.grain)
                       if charted else (None, []))

        def pick_colors(slot: Any) -> pd.DataFrame:
            with slot.popover(":material/palette:", help="Colours", type="tertiary", key=f"{rid}:colors_open"):
                color_picker(dim, values)
            return df

        with st.container(border=True):
            card_header(heading, f"{len(df):,} groups" + (f" · {len(filters)} filters" if filters else ""), df,
                        "aggregate", st, pick_colors if charted else None)
            if truncated:
                st.warning(f"More than {q.MAX_GROUPS:,} groups: showing the first {q.MAX_GROUPS:,}. "
                           "Use a coarser grain or a filter.", icon=":material/warning:")
            if "Chart" in modes:
                if not rows and not columns:
                    value = df[measure.alias].iloc[0] if len(df) else None
                    st.metric(measure_label(report, measure), "–" if value is None or pd.isna(value) else f"{value:,}")
                elif not rows:
                    st.info("Pick a row group to draw a chart.", icon=":material/bar_chart:")
                else:
                    mine = st.session_state.get(f"{rid}:colors", {}).get(dim or charts.SERIES)
                    st.altair_chart(charts.build(df, kind, report, x.field, measure, color and color.field,
                                                 x.grain, color and color.grain, mine))
            if "Pivot" in modes:
                index = [g.field for g in rows]
                if columns and index:
                    # One row per group already; pivot_table would drop the NULL groups the chart shows.
                    pivot = df.set_index([*index, columns[0].field])[measure.alias].unstack(columns[0].field)
                    pivot.columns = ["(empty)" if pd.isna(c) else str(c) for c in pivot.columns]
                    st.dataframe(pivot)
                else:
                    st.dataframe(df, hide_index=True,
                                 column_config={f: fields[f].title for f in df.columns if f in fields})
            show_sql(stmt, df)


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
        sort_key, desc_key = f"{rid}:sort", f"{rid}:desc"
        with st.container(border=True), shown_errors():
            header = st.container()
            # Their widgets are in the header's sort popover, drawn once the rows are in: read their state first.
            if st.session_state.get(sort_key) not in fields:  # a field the YAML dropped
                st.session_state.pop(sort_key, None)
            order_field = st.session_state.get(sort_key)
            desc = bool(st.session_state.get(desc_key)) and order_field is not None
            size = st.session_state.get(f"{rid}:page_size", DEFAULT_PAGE_SIZE)  # its widget is on the pager line
            count_stmt = q.count(report, dialect, params, filters, columns_map=cmap)
            with st.spinner("Counting rows…"):
                total = int(fetch(url, count_stmt.sql, count_stmt.binds)["n"].iloc[0])
            pages = max(1, -(-total // size))
            page = min(max(st.session_state.get(page_key, 1), 1), pages)  # a new filter can shrink the result
            st.session_state[page_key] = page
            stmt = q.detail(report, dialect, params, filters, order_by=order_field,
                            descending=desc, limit=size, offset=(page - 1) * size, columns_map=cmap)
            with st.spinner("Running query…"):
                df = fetch(url, stmt.sql, stmt.binds)

            def detail_tools(slot: Any) -> pd.DataFrame:
                label = ":material/sort:" + (f" {title(order_field)} " + (":material/arrow_downward:" if desc
                                             else ":material/arrow_upward:") if order_field else "")
                with slot.popover(label, help="Sort every row, not only this page", type="tertiary",
                                  key=f"{rid}:sort_open"):  # keyed: stays open when its label changes
                    st.selectbox("Sort by", list(fields), index=None, format_func=title, placeholder="Unsorted",
                                 key=sort_key, width=240, **KEEP)
                    st.toggle("Descending", key=desc_key, disabled=order_field is None, **KEEP)
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
                return df[picked or shown]

            df = card_header("Detail", f"{len(filters)} filters" if filters else "", df, "detail", header, detail_tools)
            st.dataframe(df, hide_index=True, column_config={f.name: f.title for f in fields.values()})
            if total > size:  # st.dataframe sorts in the browser, on the rows it holds
                st.caption("A click on a column header sorts this page only; :material/sort: sorts every row.")
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


    if aggregated:
        with shown_errors():
            aggregate_views()
    if "Detail" in modes:
        detail_view()
