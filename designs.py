"""Website designs. A design changes the layout, the shapes, the fonts and the movement of the whole website,
not only the colors. The colors still come from the season theme and the skin, so seasons work in every design.

Each design is one CSS file in the folder `designs/` (the file name is the design key). To add a design: add the CSS file
and one line to DESIGNS below. The bot (/settings), the website and the Owner Panel all read this list.
"""
import os

DESIGNS = {
    "blueprint": {"name": "Blueprint", "desc": "The classic drafting table: one centered panel, square corners, hard shadows.", "icon": "fa-compass-drafting"},
    "studio": {"name": "Studio", "desc": "A modern app: side menu, wide soft cards, clean text.", "icon": "fa-table-columns"},
    "terminal": {"name": "Terminal", "desc": "A hacker console: monospace text, tab bar on top, scan lines.", "icon": "fa-terminal"},
    "aurora": {"name": "Aurora", "desc": "Glass cards over a moving light show, floating menu, round shapes.", "icon": "fa-wand-magic-sparkles"},
}
DEFAULT_DESIGN = "blueprint"
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "designs")
_CACHE = {}


def valid(key) -> bool:
    return isinstance(key, str) and key in DESIGNS


def pick(*keys) -> str:
    """The first valid key (for example the personal choice, then the owner default), else the classic design."""
    for key in keys:
        if valid(key):
            return key
    return DEFAULT_DESIGN


def css(key: str) -> str:
    """The CSS of one design. The classic design has no extra CSS (it is the base look of the page)."""
    if not valid(key) or key == DEFAULT_DESIGN:
        return ""
    if key not in _CACHE:
        try:
            with open(os.path.join(_DIR, f"{key}.css"), encoding="utf-8") as f:
                _CACHE[key] = f.read()
        except OSError as e:
            print(f"[Design CSS Error] {key}: {e}")
            return ""
    return _CACHE[key]


def catalog() -> list:
    return [{"key": k, **v} for k, v in DESIGNS.items()]
