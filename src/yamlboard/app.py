"""The board: reports to filter and group on the fields they allow, and dashboards of saved views' charts.

Run with ``yamlboard run REPORTS_DIR``. Every interaction becomes SQL on the source database
(see ``query.py``); results are cached for ten minutes per statement. The pages are in ``board_pages/``.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from yamlboard import logs, ui

ASSETS = Path(__file__).with_name("assets")
PAGES = Path(__file__).with_name("board_pages")
st.set_page_config(page_title="yamlboard", page_icon=str(ASSETS / "yamlboard.png"), layout="wide")
ui.logo(str(ASSETS / "yamlboard.svg"))
try:
    logs.setup("yamlboard.log")
except logs.LogDirError as e:  # logging is compulsory: no log, no app
    st.error(f"{e}. Fix it, or set another directory with `--log-dir`.", icon=":material/error:")
    st.stop()

st.navigation([
    st.Page(PAGES / "report.py", title="Reports", icon=":material/table_chart:", default=True),
    st.Page(PAGES / "dashboard.py", title="Dashboards", icon=":material/dashboard:"),
], position="top").run()
