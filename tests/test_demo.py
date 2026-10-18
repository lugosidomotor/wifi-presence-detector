import json
import re

from tools import build_demo


def test_demo_inlines_the_real_ui_with_the_mock_backend_first():
    html = build_demo.build()
    app = (build_demo.STATIC / "app.js").read_text(encoding="utf-8")
    assert 'src="/static/' not in html and 'href="/static/' not in html  # self-contained: opens from disk
    assert "importmap" not in html.split("<script>")[0]
    assert "/static/fonts/" not in html and "data:font/woff2;base64," in html
    mock, loader = html.index("window.fetch = async"), html.index("URL.createObjectURL")
    assert mock < loader  # the fake API is in place before the UI's first request
    table = json.loads(re.search(r"const MODULES = (\[.*?\]);\n", html, re.S).group(1))
    assert [m["name"] for m in table][-1] == "app"
    assert table[-1]["code"] == app  # the unchanged UI code, not a copy that can drift
    names = {m["name"]: m for m in table}
    assert names["three"]["specifiers"] == ["three"] and "/static/scene3d.js" in names["scene3d"]["specifiers"]
    assert "</script" not in html.split("const MODULES = ")[1].split("</script>")[0]


def test_every_ui_import_is_resolved_by_the_loader():
    table = {spec for _, _, specs in build_demo.MODULES for spec in specs}
    pattern = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)["']([^"']+)["']""")
    for _, path, _ in build_demo.MODULES:
        code = (build_demo.STATIC / path).read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)  # JSDoc usage examples are not imports
        for specifier in pattern.findall(code):
            assert specifier in table, f"{path} imports {specifier!r}, which the demo loader cannot resolve"


def test_demo_build_fails_loudly_when_the_ui_changes_shape(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    for name in ("app.js", "style.css", "wifi.svg"):
        (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(build_demo, "STATIC", tmp_path)
    try:
        build_demo.build()
    except SystemExit as exc:
        assert "update tools/build_demo.py" in str(exc)
    else:
        raise AssertionError("a stale build script must not produce a broken demo silently")


def test_committed_demo_is_up_to_date():
    assert build_demo.OUTPUT.read_text(encoding="utf-8") == build_demo.build(), "run: python tools/build_demo.py"
