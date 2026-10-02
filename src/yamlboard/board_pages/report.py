"""The Reports page: pick a report, then see it with every control the board gives (``report_view``)."""

from __future__ import annotations

import streamlit as st

from yamlboard import report_view
from yamlboard.board import reports_dir, workspace
from yamlboard.schema import Report

ws = workspace()
reports = sorted((r for r in ws.reports.values() if r.active), key=lambda r: (r.folder.lower(), r.title.lower()))

with st.sidebar:
    st.header("yamlboard", anchor=False)
    if ws.errors:
        with st.expander(f"{len(ws.errors)} report(s) not loaded", icon=":material/error:"):
            for err in ws.errors.values():
                st.caption(err)
    if not reports:
        st.info(f"No report to show in `{reports_dir()}`.")
        st.stop()
    by_id = {r.id: r for r in reports}
    rid = st.selectbox(
        "Report", list(by_id), format_func=lambda i: by_id[i].name, key="report", bind="query-params",
    )
    report: Report = by_id[rid]
    advanced = st.toggle("Advanced mode", key="advanced",
                         help="Adds the pivot view and the SQL of each result.")

report_view.show(report, ws.url(report), ws.reports, advanced)
