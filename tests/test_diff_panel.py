"""Behavior tests for the diff popup and the Keys panel (cards b4e872, 6b29b4).

The panel logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/diff_panel_check.js`) and checks that a diff renders line by line with
the right classifier, that every line is a TEXT NODE (the content is
model-written — never innerHTML), that the verdict sits next to the diff, that
the panel floats next to the clicked card instead of covering the board, that
D + click opens it while a plain click still opens the editor, and that the
Keys panel lists N, Esc, D + click and C + click from the one KEYBINDINGS list.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "diff_panel_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js diff model")


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def _served_app_js(env) -> Path:
    """Fetch app.js through the API (what the browser actually gets)."""
    r = env.client.get("/app.js")
    assert r.status_code == 200, r.text
    out = env.tmp_path / "app.js"
    out.write_text(r.text)
    return out


def _run_diff_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"diff_panel_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_diff_panel_renders_lines_as_text_nodes(env):
    result = _run_diff_check(env)
    assert result["ok"] is True, result["failures"]


def test_diff_panel_check_covers_the_spec(env):
    """Guard against the reproducer silently checking nothing."""
    source = HARNESS_JS.read_text()
    for needle in ("classifyDiffLine", "TEXT NODE", "KEYBINDINGS",
                   "scrolledIntoView", "not git-backed", "D + click",
                   "diff-popup", "pointermove", "hoverCardId"):
        assert needle in source, needle


def test_diff_popup_markup_is_a_floating_panel(env):
    """The diff is its own small panel — not an .overlay modal."""
    html = env.client.get("/").text
    assert 'id="diff-popup"' in html
    assert 'id="diff-popup-head"' in html
    assert 'id="diff-ranges"' in html
    assert 'id="diff-verdict"' in html
    assert 'id="diff-files"' in html
    assert 'id="diff-body"' in html
    assert 'id="diff-close"' in html
    # It is not wrapped in the full-screen overlay the modals use.
    popup_at = html.index('id="diff-popup"')
    assert 'class="overlay"' not in html[popup_at - 200:popup_at]
    # ...and the old full-screen diff modal is gone.
    assert 'id="diff-overlay"' not in html
    assert "diff-modal" not in html


def test_hover_then_press_d_gesture_is_gone(env):
    """D is a held modifier now: no hover tracking, no bare-D keydown branch."""
    js = env.client.get("/app.js").text
    for gone in ("hoverCardId", "hoveredCardId"):
        assert gone not in js, gone
    # The board no longer tracks the card under the pointer.
    assert "boardEl.addEventListener('mouseover'" not in js
    assert "const boardEl = $('board')" not in js
    # D is tracked as a held key, like C.
    assert "dKeyDown" in js
    assert "cKeyDown" in js


def _css_rule(css: str, selector: str) -> str:
    """The declarations of one CSS rule (the first exact `selector {` match)."""
    at = css.index(selector + " {")
    return css[at + len(selector) + 2: css.index("}", at)]


def test_diff_popup_styles_are_floating(env):
    css = env.client.get("/style.css").text
    # The floating behaviour lives in the shared base...
    base = _css_rule(css, ".floating-panel")
    assert "position: fixed" in base
    # ...and the diff popup only adds its own size on top of it.
    block = _css_rule(css, ".diff-popup")
    assert "720px" in block
    assert "640px" in block
    # The old full-screen modal sizing is gone.
    assert ".diff-modal" not in css


def test_floating_panels_share_one_base(env):
    """The diff popup is the activity popup's sibling, not a second copy of it.

    Both carry the `.floating-panel` base; each rule adds only what differs
    (its size), so a change to the panel chrome lands in one place.
    """
    html = env.client.get("/").text
    assert 'class="floating-panel activity-popup"' in html
    assert 'class="floating-panel diff-popup"' in html
    assert 'class="floating-panel-head activity-head"' in html
    assert 'class="floating-panel-head diff-popup-head"' in html
    assert 'class="floating-panel-title"' in html

    css = env.client.get("/style.css").text
    base = _css_rule(css, ".floating-panel")
    for prop in ("position: fixed", "display: flex", "flex-direction: column",
                 "background: var(--surface)", "border-radius: 10px",
                 "box-shadow:", "overflow: hidden"):
        assert prop in base, prop
    # Each popup adds its size and nothing the base already says.
    for selector, size in ((".activity-popup", "420px"), (".diff-popup", "720px")):
        block = _css_rule(css, selector)
        assert size in block, selector
        for prop in ("position: fixed", "background:", "border-radius",
                     "box-shadow", "overflow:"):
            assert prop not in block, f"{selector} repeats {prop} from .floating-panel"
    # The header chrome is shared too; each head only says how it lays out.
    head = _css_rule(css, ".floating-panel-head")
    for prop in ("display: flex", "gap: 8px", "padding: 8px 10px",
                 "border-bottom: 1px solid var(--border-soft)",
                 "background: var(--surface-2)"):
        assert prop in head, prop
    for selector in (".activity-head", ".diff-popup-head"):
        block = _css_rule(css, selector)
        for prop in ("padding:", "border-bottom", "background:"):
            assert prop not in block, f"{selector} repeats {prop} from .floating-panel-head"
    # The title truncation is shared too — no per-popup copy of it.
    title = _css_rule(css, ".floating-panel-title")
    for prop in ("white-space: nowrap", "text-overflow: ellipsis",
                 "font-weight: 600"):
        assert prop in title, prop
    for clone in (".activity-title {", ".diff-popup-title {"):
        assert clone not in css, clone


def test_diff_popup_has_layout_and_palette_controls(env):
    """The popup carries the layout toggle and the palette toggle."""
    html = env.client.get("/").text
    assert 'id="diff-views"' in html
    assert 'id="diff-palette"' in html
    # The body is a block container now, not a <pre> of coloured lines.
    assert '<div class="diff-body" id="diff-body"></div>' in html


def test_diff_renderer_never_uses_innerhtml(env):
    """The diff is model-written: no property that parses markup anywhere."""
    js = env.client.get("/app.js").text
    assert ".innerHTML" not in js
    assert "outerHTML" not in js


def test_diff_styles_are_restrained_and_palette_switches(env):
    """Diff hue on the gutter rule and a faint row fill; code in normal ink."""
    css = env.client.get("/style.css").text
    body = _css_rule(css, ".diff-body")
    for var in ("--diff-add:", "--diff-del:", "--diff-add-fill:", "--diff-del-fill:",
                "--diff-add-word:", "--diff-del-word:", "--diff-ink:"):
        assert var in body, var
    # The code is normal ink, not a full-saturation red/green.
    code = _css_rule(css, ".diff-code")
    assert "color: var(--diff-ink)" in code
    # The colourblind palette overrides the hues (blue additions, orange deletions).
    cb = _css_rule(css, '.diff-body[data-palette="colorblind"]')
    assert "--diff-add: #4c8dff" in cb
    assert "--diff-del: #e08a2e" in cb
    # The hue is on the gutter rule and a faint row fill...
    add_row = _css_rule(css, ".diff-row--add")
    assert "background: var(--diff-add-fill)" in add_row
    add_gutter = _css_rule(css, ".diff-row--add .diff-gutter")
    assert "border-left-color: var(--diff-add)" in add_gutter
    # ...and the changed word gets a stronger tint.
    word = _css_rule(css, ".diff-word--add")
    assert "background: var(--diff-add-word)" in word
    # Side-by-side has a 2px rule between the cells.
    cell = _css_rule(css, ".diff-cell + .diff-cell")
    assert "border-left: 2px solid" in cell
    # The file header sticks while its file scrolls.
    head = _css_rule(css, ".diff-file-head")
    assert "position: sticky" in head
    assert "top: 0" in head
    # The collapsed run is a button.
    collapsed = _css_rule(css, ".diff-collapsed")
    assert "cursor: pointer" in collapsed


def test_how_it_works_documents_the_diff_renderer(env):
    doc = (Path(__file__).resolve().parent.parent / "docs" / "how-it-works.md").read_text()
    for needle in ("line numbers", "side-by-side", "word-level", "unchanged lines",
                   "colourblind", "localstorage"):
        assert needle in doc.lower(), needle


#: The token classes the hand-written tokenizer emits (see app.js).
TOKEN_CLASSES = ("tok-keyword", "tok-string", "tok-comment", "tok-number",
                 "tok-fn", "tok-op", "tok-punct", "tok-tag", "tok-attr",
                 "tok-heading")


def _css_vars(block: str) -> dict[str, str]:
    """The custom properties declared in one CSS rule block."""
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[\w-]+):\s*([^;]+);", block)}


def _rgb(value: str) -> tuple[float, float, float, float]:
    """`#rrggbb` or `rgba(r, g, b, a)` -> (r, g, b, alpha)."""
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.match(r"rgba?\(([^)]+)\)", value)
    assert m, value
    parts = [p.strip() for p in m.group(1).split(",")]
    r, g, b = (float(p) for p in parts[:3])
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return (r, g, b, a)


def _over(fg: tuple[float, float, float, float],
          bg: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Composite a possibly translucent colour over an opaque one."""
    r, g, b, a = fg
    return (r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a),
            b * a + bg[2] * (1 - a), 1.0)


def _luminance(rgb: tuple[float, float, float, float]) -> float:
    def channel(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_every_token_class_has_a_colour(env):
    """Each token class the tokenizer emits is coloured from one variable."""
    css = env.client.get("/style.css").text
    for cls in TOKEN_CLASSES:
        rule = _css_rule(css, "." + cls)
        assert "color: var(--tok-" in rule, cls


def test_python_token_colours_are_distinct(env):
    """Keywords, strings, comments (and the rest of the code tokens) differ.

    These are the classes a Python line can mix, so sharing a colour would
    make the highlighting pointless.
    """
    css = env.client.get("/style.css").text
    body = _css_vars(_css_rule(css, ".diff-body"))
    core = ("--tok-keyword", "--tok-string", "--tok-comment", "--tok-number",
            "--tok-fn", "--tok-op", "--tok-punct")
    colours = [body[name] for name in core]
    assert len(set(colours)) == len(core), colours


def test_token_colours_stay_legible_on_every_row_tint(env):
    """Token colours sit on the add/del row tint and the word-diff tint.

    Both palettes are checked: the colourblind one swaps the row hues, so a
    token colour that only works on green/red would fail there. WCAG AA for
    normal text is 4.5:1.
    """
    css = env.client.get("/style.css").text
    body_rule = _css_rule(css, ".diff-body")
    body = _css_vars(body_rule)
    base = _rgb(re.search(r"background:\s*(#[0-9a-fA-F]{6})", body_rule).group(1))
    palettes = {
        "default": body,
        "colorblind": {**body, **_css_vars(_css_rule(css, '.diff-body[data-palette="colorblind"]'))},
    }
    for name, palette in palettes.items():
        add = _over(_rgb(palette["--diff-add-fill"]), base)
        dele = _over(_rgb(palette["--diff-del-fill"]), base)
        backgrounds = {
            "base": base,
            "add": add,
            "del": dele,
            "add word": _over(_rgb(palette["--diff-add-word"]), add),
            "del word": _over(_rgb(palette["--diff-del-word"]), dele),
        }
        for cls in TOKEN_CLASSES:
            colour = _rgb(palette["--" + cls])
            for where, bg in backgrounds.items():
                ratio = _contrast(colour, bg)
                assert ratio >= 4.5, (
                    f"{name} palette: {cls} on {where} is {ratio:.2f}:1, needs 4.5:1")
