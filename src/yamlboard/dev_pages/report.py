"""Edit a report YAML and check it: does it load, do its fields match its SQL, and the report as the board shows
it, with every control (chart type, grains, filters, detail…)."""

from __future__ import annotations

import streamlit as st

from yamlboard import dev_state, page_parts, report_view
from yamlboard.schema import Report, ReportError, parse_report

state = st.session_state
editor, checks = st.columns(2)

with editor:
    yaml_text = st.text_area("Report YAML", value=state.yaml_text, height=560, key=f"yaml_editor:{state.yaml_version}",
                             placeholder="Upload a file in the sidebar, or draft one from the Query page.")
    state.yaml_text = yaml_text

if not yaml_text.strip():
    checks.info("Upload a report YAML in the sidebar, or draft one from a query on the Query page.",
                icon=":material/description:")
    st.stop()

try:
    report: Report = parse_report(yaml_text, "report")
except ReportError as e:
    checks.error(str(e).removeprefix("report: ").replace("; ", "\n\n"), icon=":material/error:")
    st.stop()


with checks:
    fields = report.field_map
    st.success(f"**{report.title}** (`{report.id}`) loads: {len(fields)} fields, "
               f"{len(report.parameters)} parameters.", icon=":material/check_circle:")
    row = st.container(horizontal=True)
    row.download_button("Download", yaml_text, f"{report.id}.yml", "application/x-yaml", icon=":material/download:")
    if row.button("Test its SQL", icon=":material/terminal:"):
        dev_state.set_sql(report.sql.strip(), {p.name: p.type for p in report.parameters})
        st.switch_page(dev_state.page("query"))

    page_parts.fields_table(report)

    url = state.url
    if url is None:
        st.info("Connect to a database to see the report.", icon=":material/link_off:")
        st.stop()
    st.caption(f"Below: the report as `yamlboard run` shows it, on the connection in the sidebar, whatever the "
               f"report's datasource (`{report.datasource}`).")

# The report as its readers will see it: the board's own view, every control included, full width.
st.divider()
advanced = st.sidebar.toggle("Advanced mode", key="advanced", help="Adds the pivot view and the SQL of each result.")
report_view.show(report, url, {report.id: report}, advanced, defaults=state.tested_params)
