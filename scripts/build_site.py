"""Build the public website (GitHub Pages) into _site/.

site/                         -> _site/        landing page and demo data
static/index.html, report.html -> _site/demo/   the real dashboard pages, plus the demo API script

Run:  python scripts/build_site.py
"""

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"
MARKER = "\n<script"  # the first script tag on each page
PAGES = {"index.html": "NetMon demo", "report.html": "NetMon demo report"}


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(ROOT / "site", OUT)
    (OUT / "demo").mkdir()
    for name, title in PAGES.items():
        html = (ROOT / "static" / name).read_text(encoding="utf-8")
        if MARKER not in html:
            raise SystemExit(f"build_site: no <script> tag in static/{name}")
        # The demo API must load before the page's own scripts.
        html = html.replace(MARKER, '\n<script src="../demo-api.js"></script>' + MARKER, 1)
        html = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", html, count=1)
        # GitHub Pages serves plain files, so the dashboard's "report" link needs the .html.
        html = html.replace('href="report"', 'href="report.html"')
        (OUT / "demo" / name).write_text(html, encoding="utf-8")
    (OUT / ".nojekyll").touch()
    print(f"site built in {OUT}")


if __name__ == "__main__":
    main()
