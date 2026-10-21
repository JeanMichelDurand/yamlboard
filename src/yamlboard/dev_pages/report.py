"""Edit a report YAML and check it: does it load, do its fields match its SQL, and the report as the board shows
it, with every control (chart, map, detail, full screen…).

The YAML and the page follow each other: the fields table, the map's fields and the report's controls show the
YAML (an edit of its ``view`` section sets the controls), *Update the YAML* writes the table's permissions, the
map's fields and the controls' view back into it. The map's fields apply to the report below at once, before they
are written. *Save* writes the YAML to a file. Full screen leaves the report alone on the page, as on the board."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from yamlboard import dev_state, discover, page_parts, report_view, settings
from yamlboard.schema import Report, ReportError, parse_report

state = st.session_state
KEEP = {"persist_state": "session"}  # kept while full screen hides them


def focused(text: str) -> bool:
    """The report in ``text`` is full screen (``report_view``): the editor and the checks are left out."""
    try:
        return bool(state.get(f"{parse_report(text, 'report').id}:focus"))
    except ReportError:
        return False


focus = focused(state.yaml_text)
editor, checks = st.columns(2) if not focus else (None, st.container())
if editor:
    with editor:
        yaml_text = st.text_area("Report YAML", value=state.yaml_text, height=560,
                                 key=f"yaml_editor:{state.yaml_version}",
                                 placeholder="Upload a file in the sidebar, or draft one from the Query page.")
        state.yaml_text = yaml_text
else:
    yaml_text = state.yaml_text

if not yaml_text.strip():
    checks.info("Upload a report YAML in the sidebar, or draft one from a query on the Query page.",
                icon=":material/description:")
    st.stop()

try:
    report: Report = parse_report(yaml_text, "report")
except ReportError as e:
    checks.error(str(e).removeprefix("report: ").replace("; ", "\n\n"), icon=":material/error:")
    st.stop()

rid = report.id


def view_signature(r: Report) -> str:
    return r.view.model_dump_json()


# A view section edited by hand (or another file opened): the controls open on it, as the board would. Grains
# follow the period shown, so they are dropped and picked again.
seen, signature = state.get(f"view_sig:{rid}"), view_signature(report)
if seen is not None and seen != signature:
    state.update(settings.view_keys(report))
    for key in [k for k in state if str(k).startswith((f"{rid}:grain:", f"{rid}:grain_default:"))]:
        del state[key]
    (state.get(f"{rid}:colors") or {}).pop(settings.SERIES, None)  # the view's colour, else the palette's
state[f"view_sig:{rid}"] = signature


def map_section(r: Report, slot: Any | None) -> dict[str, str] | None:
    """The ``map`` section the page applies: the YAML's, with the fields picked here; None for no map. ``slot``:
    where the pickers go; None reads what they kept (full screen hides them)."""
    yaml_map = r.map.model_dump(exclude_defaults=True) if r.map else None
    numbers = [f.name for f in r.fields if f.type.numeric]
    sig = hash(str(yaml_map))  # a map edited in the YAML: the pickers open on it
    roles = {"lat": ("Latitude", numbers), "lon": ("Longitude", numbers), "label": ("Label", [f.name for f in r.fields])}
    picked = {}
    for role, (label, options) in roles.items():
        key = f"map:{r.id}:{role}:{sig}"
        default = (yaml_map or {}).get(role)
        if slot is not None:
            choices = [None, *options]  # None as an option: a pick taken back, no map
            picked[role] = slot.selectbox(
                label, choices, index=choices.index(default) if default in options else 0, key=key,
                format_func=lambda n: "None" if n is None else r.field_map[n].title, width=200, **KEEP,
                help="The field a single place shows" if role == "label" else "A number field, in WGS84 degrees")
        else:
            picked[role] = state.get(key, default)
    if not (picked["lat"] and picked["lon"]):
        return None
    out = {k: v for k, v in (yaml_map or {}).items() if k not in roles} | {"lat": picked["lat"], "lon": picked["lon"]}
    return out | ({"label": picked["label"]} if picked["label"] else {})


def editor_text() -> str:
    """The editor's text in a button's callback: an edit made just before the click is not in ``yaml_text`` yet."""
    return state.get(f"yaml_editor:{state.yaml_version}", state.yaml_text)


def save_file() -> None:
    path = Path(state.yaml_path or f"{rid}.yml").expanduser().resolve()
    try:
        path.write_text(editor_text(), encoding="utf-8")
    except OSError as e:
        state["save_msg"] = ("error", f"Not saved: {dev_state.error_text(e)}")
        return
    state["save_msg"] = ("success", f"Saved to `{path}`.")


yaml_map = report.map.model_dump(exclude_defaults=True) if report.map else None
if focus:
    placed = map_section(report, None)
    url = state.url
else:
    with checks:
        fields = report.field_map
        st.success(f"**{report.title}** (`{rid}`) loads: {len(fields)} fields, "
                   f"{len(report.parameters)} parameters.", icon=":material/check_circle:")
        row = st.container(horizontal=True, vertical_alignment="bottom")
        row.text_input("File", key="yaml_path", placeholder=f"{rid}.yml", persist_state="session",
                       help="Where *Save* writes the YAML, on this machine; relative to the directory the dev app runs "
                            "in. An existing file is replaced.")
        row.button("Save", icon=":material/save:", on_click=save_file, help="Write the YAML to the file")
        row.download_button("Download", yaml_text, f"{rid}.yml", "application/x-yaml", icon=":material/download:")
        if row.button("Test its SQL", icon=":material/terminal:"):
            dev_state.set_sql(report.sql.strip(), {p.name: p.type for p in report.parameters})
            st.switch_page(dev_state.page("query"))
        if msg := state.pop("save_msg", None):
            getattr(st, msg[0])(msg[1], icon=":material/save:")

        with st.expander("Map", icon=":material/map:", expanded=report.map is not None):
            st.caption("The fields that place a row. The report below takes them at once (pick *Map* in its *View*); "
                       "*Update the YAML* writes them.")
            placed = map_section(report, st.container(horizontal=True))
        edited = page_parts.fields_table(report, key=f"fields_editor:{hash(report.model_dump_json(include={'fields'}))}")
        if st.button("Update the YAML", icon=":material/sync_alt:", type="primary",
                     help="The table's permissions become the fields' (`result_columns`), the report's controls below its "
                          "`view`: chart, row and column groups, measures (each one's chart and axis), grains, filters, "
                          "partial periods, the colour of a single series, empty cells, the axes; the map's fields its "
                          "`map`. Those sections are written again, their comments with them; the rest "
                          "of the file stays as typed. Not written: the period, the views picked, the detail's options."):
            text, edits = yaml_text, page_parts.field_edits(edited)
            if edits != page_parts.field_edits(page_parts.field_frame(report)):  # untouched: the section as typed
                text = discover.with_fields(text, edits)
            if placed != yaml_map:
                text = discover.with_section(text, "map", placed)
            text = discover.with_view(text, settings.view_of(state, report))
            try:
                new = parse_report(text, "report")
            except ReportError as e:  # a field unticked that the view groups or filters by, grains on a text…
                st.error("Not written: " + str(e).removeprefix("report: ").replace("; ", "\n\n"), icon=":material/error:")
            else:
                dev_state.set_yaml(text)
                state[f"view_sig:{new.id}"] = view_signature(new)  # the controls already show it
                state["update_msg"] = "The YAML is updated."
                st.rerun()
        if msg := state.pop("update_msg", None):
            st.success(msg, icon=":material/sync_alt:")

        url = state.url
        if url is None:
            st.info("Connect to a database to see the report.", icon=":material/link_off:")
            st.stop()
        st.caption(f"Below: the report as `yamlboard run` shows it, on the connection in the sidebar, whatever the "
                   f"report's datasource (`{report.datasource}`). Change its view there, then *Update the YAML*.")

# The report as its readers will see it: the board's own view, every control included, full width.
if not focus:
    st.divider()
advanced = st.sidebar.toggle("Advanced mode", key="advanced", help="Adds the pivot view and the SQL of each result.")
if url is None:
    st.stop()
shown = report
if placed != yaml_map:  # the map's fields picked above, tried before they are written
    try:
        shown = parse_report(discover.with_section(yaml_text, "map", placed), "report")
    except ReportError as e:
        st.error("Map not applied: " + str(e).removeprefix("report: "), icon=":material/error:")
report_view.show(shown, url, {rid: shown}, advanced, defaults=state.tested_params)
