"""What every yamlboard app shows the same way: the logo."""

from __future__ import annotations

import streamlit as st

# st.logo's "large" is 2rem high, too small for an icon that is the brand: twice that, in the sidebar and in
# the header (where it goes when the sidebar is collapsed).
LOGO_CSS = """<style>
[data-testid="stSidebarLogo"], [data-testid="stHeaderLogo"] { height: 4rem; max-width: 100%; }
</style>"""


def logo(path: str) -> None:
    st.logo(path, size="large", icon_image=path)
    st.html(LOGO_CSS)
