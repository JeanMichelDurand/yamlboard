"""Write yamlboard lite as a static site: an ``index.html`` that mounts the app on stlite, and the Python files it
fetches. Any static host serves it (GitHub Pages, ``python -m http.server``); it cannot open from ``file://``, where
a browser refuses the fetches.

The page loads stlite, Pyodide and DuckDB-WASM from jsDelivr on first open, pinned below.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

STLITE = "1.9.2"  # Streamlit 1.62 on Pyodide 0.29 (Python 3.13)
REQUIREMENTS = ["pydantic", "pyyaml", "sqlalchemy", "altair", "pytz"]  # pandas comes with stlite's Streamlit
PACKAGE = Path(__file__).resolve().parent.parent
# The board, the dev app and the CLI run on a server: they stay out of the page.
SERVER_ONLY = {"app.py", "board.py", "cli.py", "dev.py", "dev_state.py", "logs.py", "report_view.py"}


def streamlit_config() -> dict[str, str]:
    """The board's colours (``cli.THEME``, as ``theme.primaryColor``…), and no Deploy button."""
    from yamlboard.cli import THEME

    def option(env: str) -> str:
        head, *rest = env.removeprefix("STREAMLIT_THEME_").lower().split("_")
        return "theme." + head + "".join(w.capitalize() for w in rest)

    return {option(k): v for k, v in THEME.items()} | {"client.toolbarMode": "viewer"}


INDEX = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
  <title>yamlboard lite</title>
  <meta name="description" content="Open a CSV, JSON, Parquet, log or DuckDB file in the browser, query it with
    DuckDB and draft a yamlboard report. Nothing leaves the browser.">
  <link rel="icon" href="app/assets/yamlboard.png">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@stlite/browser@{stlite}/build/stlite.css">
</head>
<body>
  <div id="root"></div>
  <script type="module">
    import {{ mount }} from "https://cdn.jsdelivr.net/npm/@stlite/browser@{stlite}/build/stlite.js";
    const files = {files};
    const url = (p) => new URL("app/" + p, location.href).href;
    mount({{
      requirements: {requirements},
      entrypoint: "streamlit_app.py",
      streamlitConfig: {config},
      files: Object.fromEntries(files.map((p) => [p, {{ url: url(p) }}])),
    }}, document.getElementById("root"));
  </script>
</body>
</html>
"""


def sources() -> dict[str, Path]:
    """Each file of the page, by its path in the app: the entry script, its pages, the package, the icons."""
    lite = PACKAGE / "lite"
    out = {"streamlit_app.py": lite / "app.py"}
    out |= {f"lite_pages/{p.name}": p for p in sorted((lite / "lite_pages").glob("*.py"))}
    out |= {f"yamlboard/{p.name}": p for p in sorted(PACKAGE.glob("*.py")) if p.name not in SERVER_ONLY}
    out |= {f"yamlboard/lite/{p.name}": p for p in sorted(lite.glob("*.py")) if p.name not in {"app.py", "build.py"}}
    out |= {f"assets/{p.name}": p for p in sorted((PACKAGE / "assets").iterdir())}
    return out


def build(out_dir: str | Path) -> Path:
    """Write the site into ``out_dir`` (its ``app/`` is replaced); the path of its ``index.html``."""
    out = Path(out_dir)
    app = out / "app"
    if app.exists():
        shutil.rmtree(app)
    files = sources()
    for rel, src in files.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, app / rel)
    (out / ".nojekyll").touch()  # GitHub Pages: serve the files as they are
    index = out / "index.html"
    index.write_text(INDEX.format(stlite=STLITE, files=json.dumps(sorted(files)), config=json.dumps(streamlit_config()),
                                  requirements=json.dumps(REQUIREMENTS)), encoding="utf-8")
    return index
