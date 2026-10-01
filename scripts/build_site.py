"""Build the public website (GitHub Pages) into _site/.

site/          -> _site/            landing page and demo data
static/index.html -> _site/demo/    the real dashboard, plus the demo API script

Run:  python scripts/build_site.py
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"
MARKER = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js'


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(ROOT / "site", OUT)
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    if html.count(MARKER) != 1:
        raise SystemExit("build_site: couldn't find the Chart.js <script> tag in static/index.html")
    # The demo API must load before the dashboard's own script.
    html = html.replace(MARKER, '<script src="../demo-api.js"></script>\n' + MARKER)
    html = html.replace("<title>NetMon</title>", "<title>NetMon demo</title>")
    (OUT / "demo").mkdir()
    (OUT / "demo" / "index.html").write_text(html, encoding="utf-8")
    (OUT / ".nojekyll").touch()
    print(f"site built in {OUT}")


if __name__ == "__main__":
    main()
