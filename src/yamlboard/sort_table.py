"""The detail's table: a header click sorts every row in the database, not only the page shown; a search filters.

``st.dataframe`` sorts in the browser, on the rows it holds, and tells Python nothing: with the rows fetched one
page at a time, its header sort orders one page and reads as if it ordered them all. This table draws the page
it is given, and a header click calls back Python, which reruns the query sorted. Each header has two arrows,
ascending and descending; a click on one sorts by that column alone, a Shift+click adds the column to the sort (see
``next_order``). A click on the title goes ascending, descending, unsorted.
A search typed under a header (Enter) calls back Python too, which adds a filter on that column.
A custom component (Streamlit's v2), inline, no JavaScript library.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st

from yamlboard.settings import NUMBER_STYLES

HTML = """
<div class="wrap"><table><thead></thead><tbody></tbody></table></div>
"""

CSS = """
.wrap { max-height: 560px; overflow: auto; border: 1px solid var(--st-border-color-light);
        border-radius: var(--st-base-radius, 0.5rem); font-family: var(--st-font); font-size: 14px;
        color: var(--st-text-color); }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--st-border-color-light); white-space: nowrap;
         text-align: left; }
th { position: sticky; top: 0; background: var(--st-secondary-background-color); font-weight: 600;
     cursor: pointer; user-select: none; }
th:hover { color: var(--st-primary-color); }
th .arrows { margin-left: 4px; white-space: nowrap; }
th .arrows span { padding: 0 1px; opacity: 0.3; cursor: pointer; }
th .arrows span:hover { opacity: 0.8; color: var(--st-primary-color); }
th .arrows span.on { opacity: 1; color: var(--st-primary-color); }
th .rank { font-size: 0.7em; vertical-align: super; color: var(--st-primary-color); }
th .search { margin-left: 6px; opacity: 0.45; cursor: pointer; vertical-align: -2px; }
th .search:hover, th .search.open { opacity: 1; color: var(--st-primary-color); }
th input { display: block; margin-top: 4px; width: 100%; min-width: 120px; box-sizing: border-box;
           font: inherit; font-weight: 400; padding: 2px 6px; color: var(--st-text-color);
           background: var(--st-background-color); border: 1px solid var(--st-widget-border-color, #ccc);
           border-radius: 4px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
td.empty { color: var(--st-gray-color, #999); }
"""

JS = """
const ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2.5"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>'

export default function (component) {
  const { data, parentElement, setTriggerValue } = component

  // The search icon of a header: it opens a text box under the title; Enter filters, Escape closes it.
  function searchIcon(th, col) {
    const icon = document.createElement("span")
    icon.className = "search"
    icon.innerHTML = ICON  // a constant, never data
    icon.title = "Filter every row on " + col.title
    icon.onclick = (e) => {
      e.stopPropagation()  // not a sort
      const open = th.querySelector("input")
      if (open) { open.remove(); icon.classList.remove("open"); return }
      const input = document.createElement("input")
      input.placeholder = col.search
      input.onclick = (ev) => ev.stopPropagation()
      input.onkeydown = (ev) => {
        if (ev.key === "Enter" && input.value.trim()) {
          setTriggerValue("search", { field: col.name, text: input.value.trim() })
        } else if (ev.key === "Escape") {
          input.remove(); icon.classList.remove("open")
        }
      }
      th.appendChild(input)
      icon.classList.add("open")
      input.focus()
    }
    return icon
  }
  const head = parentElement.querySelector("thead")
  const body = parentElement.querySelector("tbody")
  if (!head || !body || !data) return

  parentElement.querySelector(".wrap").style.maxHeight = data.height
  // A click sorts by this column alone; with Shift (or Ctrl, Cmd) it adds the column to the sort.
  const sort = (e, name, desc) => {
    e.stopPropagation()
    setTriggerValue("clicked", { field: name, desc: desc, add: e.shiftKey || e.ctrlKey || e.metaKey })
  }
  const row = document.createElement("tr")
  for (const col of data.columns) {
    const th = document.createElement("th")
    th.textContent = col.title
    th.title = "Sort every row by " + col.title + " (Shift+click: add it to the sort)"
    if (col.numeric) th.className = "num"
    const at = data.order.findIndex(([name]) => name === col.name)
    const arrows = document.createElement("span")
    arrows.className = "arrows"
    for (const [desc, glyph, word] of [[false, "\\u2191", "ascending"], [true, "\\u2193", "descending"]]) {
      const arrow = document.createElement("span")
      arrow.textContent = glyph
      arrow.title = "Sort " + word + " (Shift+click: add it to the sort)"
      if (at >= 0 && data.order[at][1] === desc) arrow.className = "on"
      arrow.onclick = (e) => sort(e, col.name, desc)
      arrows.appendChild(arrow)
    }
    th.appendChild(arrows)
    if (at >= 0 && data.order.length > 1) {  // its place in the sort
      const rank = document.createElement("span")
      rank.className = "rank"
      rank.textContent = String(at + 1)
      th.appendChild(rank)
    }
    th.onclick = (e) => sort(e, col.name, null)
    if (col.search) th.appendChild(searchIcon(th, col))
    row.appendChild(th)
  }
  head.replaceChildren(row)

  const rows = data.rows.map((values) => {
    const tr = document.createElement("tr")
    values.forEach((v, i) => {
      const td = document.createElement("td")
      td.textContent = v ?? ""
      td.className = (data.columns[i].numeric ? "num" : "") + (v === null ? " empty" : "")
      tr.appendChild(td)
    })
    return tr
  })
  body.replaceChildren(...rows)
}
"""

Order = list[tuple[str, bool]]  # (field, descending) pairs, the first one first


def next_order(order: Order, field: str, desc: bool | None, add: bool = False) -> Order:
    """The sort after a click on ``field``'s header: on an arrow (``desc`` its direction), or on the title (``desc``
    None: ascending, then descending, then unsorted).

    A click sorts by ``field`` alone; clicked again, the arrow of that sort unsorts. With ``add`` (Shift+click),
    ``field`` joins ``order``, last, or changes its direction where it is, or leaves it if it had that direction.
    """
    current = dict(order)
    if desc is None:
        was = current.get(field) if add or order == [(field, current.get(field))] else None
        desc = False if was is None else True if was is False else None
    elif (current.get(field) is desc) and (add or order == [(field, desc)]):
        desc = None
    if not add:
        return [] if desc is None else [(field, desc)]
    if desc is None:
        return [(f, d) for f, d in order if f != field]
    if field in current:
        return [(f, desc if f == field else d) for f, d in order]
    return [*order, (field, desc)]




@dataclass(frozen=True)
class NumberFormat:
    """A number with ``decimals`` decimals (none on an integer column) and the separators of ``style``."""
    decimals: int = 2
    style: str = "1 234.56"

    def __call__(self, v: Any, integer: bool) -> str:
        thousands, point = NUMBER_STYLES[self.style]
        text = f"{v:,.{0 if integer else self.decimals}f}"  # Python's own: ``,`` thousands, ``.`` point
        return text.replace(",", "\0").replace(".", point).replace("\0", thousands)


def _cell(v: Any, number: NumberFormat | None, integer: bool) -> str | None:
    """A value as the table shows it: text, ``None`` for an empty one. ``number``: the format of a number column."""
    if v is None or (pd.api.types.is_scalar(v) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat(sep=" ")
    v = v.item() if hasattr(v, "item") else v
    if number and isinstance(v, int | float | Decimal) and not isinstance(v, bool):
        return number(v, integer)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(round(v, 6))
    return str(v)


def is_number(values: pd.Series) -> bool:
    """A column of numbers: a number type, or (a DECIMAL, read as Python objects) only numbers. Not booleans."""
    if pd.api.types.is_bool_dtype(values):
        return False
    if pd.api.types.is_numeric_dtype(values):
        return True
    present = values.dropna()
    return not present.empty and bool(present.map(lambda v: isinstance(v, int | float | Decimal)  # not np.bool_:
                                                  and not isinstance(v, bool)).all())         # data goes to JSON


def cells(df: pd.DataFrame, number: NumberFormat | None = None,
          integers: Collection[str] = ()) -> list[list[str | None]]:
    """The rows as text. ``number`` formats the number columns, with no decimals on an integer column: one of an
    integer type, or one of ``integers`` (an integer field with an empty value is read as floats)."""
    numeric = [is_number(df[c]) for c in df.columns]
    whole = [c in integers or pd.api.types.is_integer_dtype(df[c]) for c in df.columns]
    return [[_cell(v, number if numeric[i] else None, whole[i]) for i, v in enumerate(r)]
            for r in df.itertuples(index=False, name=None)]


DEFAULT_NUMBER = NumberFormat()  # frozen: one shared default is safe


def sort_table(df: pd.DataFrame, titles: Mapping[str, str], order: Order, *, key: str,
               on_sort: Callable[[Order], None], number: NumberFormat = DEFAULT_NUMBER,
               integers: Collection[str] = (), searches: Mapping[str, str] | None = None,
               on_search: Callable[[str, str], None] | None = None, height: str = "560px") -> None:
    """``df`` with its columns titled by ``titles`` (else their name), the arrows of ``order`` lit, its numbers
    as ``number`` writes them (see ``cells``); at most ``height`` high (a CSS length), then it scrolls.

    A header click calls ``on_sort(order)`` with the next sort (see ``next_order``), before the rerun.
    ``searches``: the columns with a search icon, each with the hint of its text box; Enter there calls
    ``on_search(field, text)``.
    """
    searches = searches or {}

    def clicked() -> None:
        click = st.session_state[key].clicked
        if click:
            on_sort(next_order(list(order), click["field"], click["desc"], bool(click.get("add"))))

    def searched() -> None:
        typed = st.session_state[key].search
        if typed and on_search:
            on_search(typed["field"], typed["text"])

    columns = [{"name": c, "title": titles.get(c, c), "numeric": is_number(df[c]), "search": searches.get(c)}
               for c in df.columns]
    # Registered on each call, not at import: the registry is the runtime's, and a module imported once outlives a
    # runtime (each AppTest has its own). The same definition again is a no-op, with no warning.
    table = st.components.v2.component("yamlboard_sort_table", html=HTML, css=CSS, js=JS)
    table(data={"columns": columns, "rows": cells(df, number, integers), "order": [list(o) for o in order],
                "height": height}, key=key,
           on_clicked_change=clicked, on_search_change=searched)
