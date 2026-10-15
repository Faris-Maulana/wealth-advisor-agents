"""Build the static web demo into site/index.html.

Bundles the `wealth_advisor` package source, the mock data and web/bridge.py
into a single self-contained HTML file. Pyodide executes the bundled Python in
the browser, so the demo needs nothing but static hosting (GitHub Pages).

    python scripts/build_site.py [--repo-url URL]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# jsDelivr's /npm/ path serves the npm tarball byte-for-byte, so the SRI hash in
# the template can be verified against `npm pack pyodide@0.26.4`.
PYODIDE_BASE = "https://cdn.jsdelivr.net/npm/pyodide@0.26.4/"
EXCLUDE = {"api.py", "cli.py", "__main__.py"}  # server/CLI entry points are not needed in the browser


def collect() -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted((ROOT / "wealth_advisor").rglob("*.py")):
        if p.name in EXCLUDE or "__pycache__" in p.parts:
            continue
        files[str(p.relative_to(ROOT))] = p.read_text(encoding="utf-8")
    for p in sorted((ROOT / "data").rglob("*.json")):
        files[str(p.relative_to(ROOT))] = p.read_text(encoding="utf-8")
    files["bridge.py"] = (ROOT / "web" / "bridge.py").read_text(encoding="utf-8")
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-url", default="https://github.com/Faris-Maulana/wealth-advisor-agents")
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--pyodide-base", default=PYODIDE_BASE,
                    help="where pyodide.js/.wasm are served from (override for offline testing)")
    args = ap.parse_args()

    bundle = json.dumps(collect()).replace("</", "<\\/")  # cannot terminate the <script> element early
    html = (ROOT / "web" / "index.template.html").read_text(encoding="utf-8")
    html = html.replace("__BUNDLE__", bundle).replace("__REPO_URL__", args.repo_url)
    html = html.replace("__PYODIDE_BASE__", args.pyodide_base)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("")
    print(f"wrote {out / 'index.html'} ({len(html) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
