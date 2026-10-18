"""Build the standalone demo page: the unchanged web UI (wifisense/static) plus an in-browser simulated backend.

The result is one self-contained HTML file that opens straight from disk, without the service or a Pi:
    python tools/build_demo.py            # writes demo/wifi-presence-demo.html
    python tools/build_demo.py --check    # exits 1 when the committed demo is out of date
"""

import argparse
import base64
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "wifisense" / "static"
SOURCES = ROOT / "tools" / "demo"
OUTPUT = ROOT / "demo" / "wifi-presence-demo.html"
# ES modules in dependency order: (name, file under static/, specifiers its importers use). The last one is the entry.
MODULES = [
    ("three-core", "vendor/three/three.core.js", ["./three.core.js"]),
    ("three", "vendor/three/three.module.js", ["three"]),
    ("orbit-controls", "vendor/three/addons/OrbitControls.js", ["three/addons/OrbitControls.js"]),
    ("css2d-renderer", "vendor/three/addons/CSS2DRenderer.js", ["three/addons/CSS2DRenderer.js"]),
    ("chart", "chart.js", ["/static/chart.js"]),
    ("scene3d", "scene3d.js", ["/static/scene3d.js"]),
    ("app", "app.js", []),
]


def replace_once(text: str, old: str, new: str, where: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"{where}: expected exactly one {old!r}; update tools/build_demo.py to match the UI")
    return text.replace(old, new)


def inline_script(code: str, where: str) -> str:
    if "</script" in code.lower() or "<!--" in code:
        raise SystemExit(f"{where}: cannot be inlined (contains </script or <!--)")
    return f"<script>\n{code}</script>\n"


def inline_assets(css: str) -> str:
    """Embed url(/static/...) assets (the self-hosted fonts) as data URIs."""
    types = {".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png"}

    def embed(match):
        path = STATIC / match.group(1)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f'url("data:{types[path.suffix]};base64,{data}")'

    return re.sub(r"""url\(["']?/static/([^"')]+)["']?\)""", embed, css)


def module_table() -> str:
    """The modules as a JSON array that is safe inside a <script> element."""
    table = [
        {"name": name, "specifiers": specifiers, "code": (STATIC / path).read_text(encoding="utf-8")}
        for name, path, specifiers in MODULES
    ]
    return json.dumps(table, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "\\u003c!--")


def build() -> str:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    icon = base64.b64encode((STATIC / "wifi.svg").read_bytes()).decode("ascii")
    styles = inline_assets((STATIC / "style.css").read_text(encoding="utf-8"))
    styles += (SOURCES / "demo.css").read_text(encoding="utf-8")
    page = replace_once(page, "<title>WiFi jelenlét</title>", "<title>WiFi jelenlét · Demó</title>", "index.html")
    page = replace_once(
        page,
        '<link rel="icon" href="/static/wifi.svg" type="image/svg+xml">',
        f'<link rel="icon" href="data:image/svg+xml;base64,{icon}">',
        "index.html",
    )
    page = replace_once(page, '<link rel="stylesheet" href="/static/style.css">', f"<style>\n{styles}</style>", "index")
    # Modules are started by the loader from Blob URLs, so neither the import map nor the module tag stays.
    page = re.sub(r'\s*<script type="importmap">.*?</script>', "", page, count=1, flags=re.S)
    page = replace_once(page, '  <script type="module" src="/static/app.js"></script>\n', "", "index.html")
    if "importmap" in page:
        raise SystemExit("index.html: import map not removed; update tools/build_demo.py to match the UI")
    loader = replace_once((SOURCES / "loader.js").read_text(encoding="utf-8"), "/*MODULES*/", "\0", "loader.js")
    loader_tag = inline_script(loader, "loader.js").replace("\0", module_table())
    scripts = inline_script((SOURCES / "mock.js").read_text(encoding="utf-8"), "mock.js") + loader_tag
    return replace_once(page, "</body>", scripts + "</body>", "index.html")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if the output differs from a fresh build")
    args = parser.parse_args()
    html = build()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != html:
            sys.exit(f"{args.output} is out of date; run: python tools/build_demo.py")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"wrote {args.output.relative_to(ROOT) if args.output.is_relative_to(ROOT) else args.output}")


if __name__ == "__main__":
    main()
