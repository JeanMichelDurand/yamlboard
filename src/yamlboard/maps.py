"""The Map view: a report's rows as points on a map, gathered by zoom.

The database groups the rows into the cells of a grid (``query.map_cells``), as fine as ``query.MAX_CELLS`` cells
allow: every distinct place when they are fewer. The browser then gathers the cells into clusters by zoom
(MapLibre's own clustering): zoomed out, a circle per area; zoomed in, the cells, then the points. A cluster adds
its cells' parts up (rows, sums, counts, mins, maxes), so its measure is the one of its rows, an average included.

A count or a sum sizes the circles; an average, a min or a max colours them, sized by their rows. A click on a
cluster zooms into it. A custom component (Streamlit's v2), inline; MapLibre GL JS from a CDN, the background from
the report's raster tiles: the browser needs the internet, the server does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st

from yamlboard.schema import Aggregation, MapDef

MAPLIBRE = "https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl"  # .js and .css
HEIGHT = 560  # pixels

HTML = """
<div class="map"></div>
"""

CSS = """
.map { width: 100%; height: 560px; border-radius: var(--st-base-radius, 0.5rem); overflow: hidden;
       font-family: var(--st-font); }
.maplibregl-popup-content { font-family: var(--st-font); font-size: 13px; color: #222; padding: 6px 10px; }
.maplibregl-popup-content b { font-weight: 600; }
"""

JS = """
// MapLibre once per page: a script tag in the document, its stylesheet in each component's shadow root.
function loadMaplibre(base) {
  if (!window.__ybMaplibre) {
    window.__ybMaplibre = new Promise((resolve, reject) => {
      if (window.maplibregl) { resolve(window.maplibregl); return }
      const s = document.createElement("script")
      s.src = base + ".js"
      s.onload = () => resolve(window.maplibregl)
      s.onerror = () => { window.__ybMaplibre = null; reject(new Error("cannot load " + s.src)) }
      document.head.appendChild(s)
    })
  }
  return window.__ybMaplibre
}

const maps = new WeakMap()  // parentElement -> { map, signature, data }

// A feature's measure, for a cell as for a cluster (whose parts its cells added up).
function valueExpr(kind) {
  if (kind === "count_field") return ["get", "c"]
  if (kind === "sum") return ["get", "s"]
  if (kind === "avg") return ["case", [">", ["get", "c"], 0], ["/", ["get", "s"], ["get", "c"]], 0]
  if (kind === "min") return ["get", "mn"]
  if (kind === "max") return ["get", "mx"]
  return ["get", "n"]
}

function geojson(d) {
  const features = d.lat.map((lat, i) => {
    const p = { n: d.n[i] }
    if (d.s) p.s = d.s[i] ?? 0
    if (d.c) p.c = d.c[i] ?? 0
    if (d.mn) p.mn = d.mn[i]
    if (d.mx) p.mx = d.mx[i]
    if (d.label && d.n[i] === 1 && d.label[i] !== null) p.label = String(d.label[i])
    return { type: "Feature", geometry: { type: "Point", coordinates: [d.lon[i], lat] }, properties: p }
  })
  return { type: "FeatureCollection", features }
}

function clusterProperties(kind) {
  const props = {}
  if (kind === "sum" || kind === "avg") props.s = ["+", ["get", "s"]]
  if (kind === "count_field" || kind === "avg") props.c = ["+", ["get", "c"]]
  if (kind === "min") props.mn = ["min", ["get", "mn"]]
  if (kind === "max") props.mx = ["max", ["get", "mx"]]
  props.n = ["+", ["get", "n"]]
  return props
}

function fmt(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–"
  const a = Math.abs(v)
  if (a >= 1e9) return (v / 1e9).toFixed(1) + " bn"
  if (a >= 1e6) return (v / 1e6).toFixed(1) + " M"
  if (a >= 1e4) return (v / 1e3).toFixed(1) + " k"
  return Number.isInteger(v) ? v.toLocaleString("en").replace(/,/g, " ") : v.toFixed(2)
}

function featureValue(kind, p) {
  if (kind === "count_field") return p.c
  if (kind === "sum") return p.s
  if (kind === "avg") return p.c > 0 ? p.s / p.c : null
  if (kind === "min") return p.mn
  if (kind === "max") return p.mx
  return p.n
}

function addLayers(map, d) {
  const size = d.additive ? valueExpr(d.kind) : ["get", "n"]
  map.addSource("cells", {
    type: "geojson", data: geojson(d.cells), cluster: true, clusterRadius: 48, clusterMaxZoom: 16,
    clusterProperties: clusterProperties(d.kind),
  })
  const color = d.additive ? d.color
    : ["interpolate", ["linear"], valueExpr(d.kind), d.range[0], d.ramp[0], d.range[1], d.ramp[1]]
  map.addLayer({ id: "points", type: "circle", source: "cells", paint: {
    "circle-color": color, "circle-opacity": 0.75, "circle-stroke-color": "#ffffff", "circle-stroke-width": 1,
    "circle-radius": ["interpolate", ["linear"], ["sqrt", ["max", size, 0]], 0, 4, 1, 6],
  } })
  rescale(map, d, size)
  map.on("moveend", () => rescale(map, d, size))
  map.on("sourcedata", (e) => { if (e.sourceId === "cells" && e.isSourceLoaded) rescale(map, d, size) })
}

// Circles from 5 to 30 px, the largest one in view the largest: the scale follows the zoom.
function rescale(map, d, size) {
  if (!map.getLayer("points")) return
  let top = 0
  for (const f of map.queryRenderedFeatures({ layers: ["points"] })) {
    const p = f.properties
    const v = d.additive ? featureValue(d.kind, p) : p.n
    if (v > top) top = v
  }
  if (top <= 0) return
  map.setPaintProperty("points", "circle-radius",
    ["interpolate", ["linear"], ["sqrt", ["max", size, 0]], 0, 5, Math.sqrt(top), 30])
}

function popupHtml(d, p) {
  const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c])
  const lines = []
  if (p.label !== undefined) lines.push("<b>" + esc(p.label) + "</b>")
  lines.push(esc(d.rows_label) + ": " + fmt(p.n))
  if (d.kind !== "count") lines.push(esc(d.measure_label) + ": " + fmt(featureValue(d.kind, p)))
  if (p.cluster) lines.push("<i>Click to zoom in</i>")
  return lines.join("<br>")
}

export default function (component) {
  const { data, parentElement } = component
  const box = parentElement.querySelector(".map")
  if (!box || !data) return
  if (box.style.height !== data.height) {  // full screen and back
    box.style.height = data.height
    maps.get(parentElement)?.map.resize()
  }
  if (!parentElement.querySelector("link[data-maplibre]")) {
    const link = document.createElement("link")
    link.rel = "stylesheet"; link.href = data.maplibre + ".css"; link.dataset.maplibre = "1"
    parentElement.appendChild(link)
  }
  const signature = JSON.stringify([data.signature, data.kind, data.color, data.tiles])
  const known = maps.get(parentElement)
  if (known && known.box === box && known.signature === signature) return  // same cells: keep the user's view
  loadMaplibre(data.maplibre).then((maplibregl) => {
    let state = maps.get(parentElement)
    if (state && state.box !== box) { state.map.remove(); state = null }  // drawn again from scratch
    if (state && state.signature === signature) return
    if (state && state.map.getSource("cells") && state.tiles === data.tiles && state.kind === data.kind
        && state.color === data.color) {
      state.map.getSource("cells").setData(geojson(data.cells))  // new filters: same map, new cells
      state.signature = signature
      state.data = data
      return
    }
    if (state) state.map.remove()
    const map = new maplibregl.Map({
      container: box,
      style: { version: 8, sources: { tiles: { type: "raster", tiles: [data.tiles], tileSize: 256,
                                                attribution: data.attribution } },
               layers: [{ id: "tiles", type: "raster", source: "tiles" }] },
      bounds: data.bounds, fitBoundsOptions: { padding: 40, maxZoom: 13 }, attributionControl: { compact: true },
    })
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right")
    map.addControl(new maplibregl.ScaleControl(), "bottom-left")
    state = { map, box, signature, data, tiles: data.tiles, kind: data.kind, color: data.color }
    maps.set(parentElement, state)
    map.on("load", () => addLayers(map, state.data))
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 })
    map.on("mousemove", "points", (e) => {
      map.getCanvas().style.cursor = "pointer"
      const f = e.features[0]
      popup.setLngLat(f.geometry.coordinates).setHTML(popupHtml(state.data, f.properties)).addTo(map)
    })
    map.on("mouseleave", "points", () => { map.getCanvas().style.cursor = ""; popup.remove() })
    map.on("click", "points", async (e) => {
      const f = e.features[0]
      if (!f.properties.cluster) return
      const zoom = await map.getSource("cells").getClusterExpansionZoom(f.properties.cluster_id)
      map.easeTo({ center: f.geometry.coordinates, zoom })
    })
  }).catch((err) => { box.textContent = "The map library could not be loaded: " + err.message })
}
"""

ADDITIVE = (Aggregation.COUNT, Aggregation.SUM)
RAMP = ("#c6dbef", "#08306b")  # a colour scale's low and high ends


@dataclass(frozen=True)
class MapMeasure:
    """What the circles show: ``kind`` (count: the rows; count_field, sum, avg, min, max: a field's) and its label."""
    kind: str
    label: str

    @property
    def additive(self) -> bool:
        return self.kind in ("count", "count_field", "sum")


def map_measure(field: str, aggregation: Aggregation, label: str) -> MapMeasure:
    if field == "*":
        return MapMeasure("count", label)
    return MapMeasure("count_field" if aggregation is Aggregation.COUNT else aggregation.value, label)


def _column(cells: pd.DataFrame, name: str) -> list[Any] | None:
    """A column as JSON-ready values (None for an empty one), or None if the result lacks it."""
    if name not in cells.columns:
        return None
    values = cells[name]
    if pd.api.types.is_numeric_dtype(values) or values.map(lambda v: v is None or isinstance(v, int | float)).all():
        values = pd.to_numeric(values, errors="coerce").astype(float)
    return [None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in values]


def payload(cells: pd.DataFrame, spec: MapDef, measure: MapMeasure, rows_label: str, color: str,
            signature: str) -> dict[str, Any]:
    """The component's data: the cells as columns, the measure, the colours, the bounds to open on."""
    columns = {"lat": "yb_lat", "lon": "yb_lon", "n": "yb_n", "s": "yb_s", "c": "yb_c", "mn": "yb_min",
               "mx": "yb_max", "label": "yb_label"}
    data = {k: _column(cells, v) for k, v in columns.items()}
    data = {k: v for k, v in data.items() if v is not None}
    data["n"] = [int(n) for n in data.get("n", [])]
    if cells.empty:
        bounds = [[-10.0, 35.0], [30.0, 60.0]]
    else:
        bounds = [[float(cells["yb_lon"].min()), float(cells["yb_lat"].min())],
                  [float(cells["yb_lon"].max()), float(cells["yb_lat"].max())]]
    values = value_range(cells, measure)
    return {"cells": data, "kind": measure.kind, "additive": measure.additive, "measure_label": measure.label,
            "rows_label": rows_label, "color": color, "ramp": list(RAMP), "range": values, "bounds": bounds,
            "tiles": spec.tiles, "attribution": spec.attribution, "maplibre": MAPLIBRE, "signature": signature}


def value_range(cells: pd.DataFrame, measure: MapMeasure) -> list[float]:
    """The colour scale's ends: the lowest and highest cell values (a cluster's average, min or max lies between)."""
    if measure.additive or cells.empty:
        return [0.0, 1.0]
    if measure.kind == "avg":
        c = pd.to_numeric(cells["yb_c"]).replace(0, float("nan"))
        values = pd.to_numeric(cells["yb_s"]) / c
    else:
        values = pd.to_numeric(cells["yb_" + measure.kind])
    values = values.dropna()
    if values.empty:
        return [0.0, 1.0]
    lo, hi = float(values.min()), float(values.max())
    return [lo, hi if hi > lo else lo + 1.0]


def map_chart(cells: pd.DataFrame, spec: MapDef, measure: MapMeasure, *, key: str, rows_label: str = "Rows",
              color: str = "#2A78D6", signature: str = "", height: int | str = HEIGHT) -> None:
    """``cells`` (``query.map_cells``' result) on a map, clustered by zoom. ``signature`` names the query: the
    map keeps its zoom and place over reruns with the same one. ``height``: pixels, or a CSS length
    (``calc(100vh - 200px)``)."""
    # Registered on each call, as sort_table: the registry is the runtime's (each AppTest has its own).
    component = st.components.v2.component("yamlboard_map", html=HTML, css=CSS, js=JS)
    data = payload(cells, spec, measure, rows_label, color, signature) | {
        "height": f"{height}px" if isinstance(height, int) else height}
    component(data=data, key=key, height=height if isinstance(height, int) else "content")
