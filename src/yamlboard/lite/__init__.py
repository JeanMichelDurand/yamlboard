"""yamlboard lite: the dev app's file side as a static page, run in the browser by stlite (Streamlit on Pyodide).

Python's DuckDB has no build for the Pyodide that stlite runs, so the SQL runs on DuckDB-WASM, DuckDB's own
browser build, through ``duck``. The files a user opens are copies in the browser's memory: nothing is sent
anywhere, and nothing reaches a database server. ``build`` writes the page; ``yamlboard lite DIR`` calls it.
"""
