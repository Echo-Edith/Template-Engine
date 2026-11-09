"""Cosmetic items of the points shop (website themes and name colors).
This file is used by the bot (to check prices) and by the website (to show the items), so both always agree."""
import colorsys

# id: (name, price, background hue, accent hue, accent saturation percent)
SKINS = {
    "midnight": ("Midnight", 100, 225, 212, 90),
    "sakura": ("Sakura", 150, 340, 340, 80),
    "ocean": ("Ocean", 150, 200, 185, 85),
    "forest": ("Forest", 150, 140, 135, 60),
    "sunset": ("Sunset", 200, 10, 28, 90),
    "neon": ("Neon", 250, 265, 300, 100),
    "royal": ("Royal", 300, 262, 45, 85),
}

# id: (name, price, css). A css text that starts with "linear-gradient" is a gradient name.
NAME_COLORS = {
    "red": ("Red", 50, "#ff6b6b"),
    "orange": ("Orange", 50, "#ffa24c"),
    "green": ("Green", 50, "#4ade80"),
    "cyan": ("Cyan", 50, "#22d3ee"),
    "blue": ("Blue", 50, "#60a5fa"),
    "pink": ("Pink", 50, "#f472b6"),
    "purple": ("Purple", 50, "#c084fc"),
    "gold": ("Gold", 100, "#ffd24a"),
    "ice": ("Ice", 300, "linear-gradient(90deg,#38bdf8,#e0f2fe,#7dd3fc)"),
    "fire": ("Fire", 300, "linear-gradient(90deg,#ff512f,#f09819,#ffd200)"),
    "rainbow": ("Rainbow", 400, "linear-gradient(90deg,#ff5f6d,#ffc371,#7bed9f,#5fb3ff,#c084fc,#ff5f6d)"),
}


# Prices the owner changed in the Owner Panel (item id -> price). The website and the bot load them from the database.
OVERRIDES = {}


def set_overrides(prices: dict):
    OVERRIDES.clear()
    for key, value in (prices or {}).items():
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1000000:
            OVERRIDES[str(key)] = value


def base_price(item_id: str):
    """The built-in price (without the owner's change)."""
    kind, _, key = str(item_id).partition(":")
    if kind == "skin" and key in SKINS:
        return SKINS[key][1]
    if kind == "color" and key in NAME_COLORS:
        return NAME_COLORS[key][1]
    return None


def _hsl(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _rgba(h, s, l, a):
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return f"rgba({round(r * 255)}, {round(g * 255)}, {round(b * 255)}, {a})"


def price_of(item_id: str):
    """Price of an item id like 'skin:sakura' or 'color:gold'. None when the item does not exist."""
    base = base_price(item_id)
    if base is None:
        return None
    return OVERRIDES.get(str(item_id), base)


def name_of(item_id: str) -> str:
    kind, _, key = str(item_id).partition(":")
    return (SKINS.get(key) if kind == "skin" else NAME_COLORS.get(key) or ("Item",))[0]


def catalog() -> list:
    items = []
    for key, (name, price, bg, ah, sat) in SKINS.items():
        items.append({"id": f"skin:{key}", "kind": "skin", "key": key, "name": name, "price": price_of(f"skin:{key}"),
                      "swatch": [_hsl(bg, 0.45, 0.07), _hsl(bg, 0.40, 0.14), _hsl(ah, sat / 100.0, 0.62)]})
    for key, (name, price, css) in NAME_COLORS.items():
        items.append({"id": f"color:{key}", "kind": "color", "key": key, "name": name, "price": price_of(f"color:{key}")})
    return items


def skin_css() -> str:
    """CSS for all themes. The dark rules do not apply in light mode, where only the accent color changes."""
    out = []
    for key, (name, price, bg, ah, sat) in SKINS.items():
        s = sat / 100.0
        dark = (
            f"html[data-skin=\"{key}\"]:not([data-theme=\"light\"]){{"
            f"--bg-base:{_hsl(bg, .45, .06)};--grid-minor:{_rgba(bg, .6, .75, .05)};--grid-major:{_rgba(bg, .6, .75, .11)};"
            f"--panel-bg:{_hsl(bg, .42, .10)};--panel-border:{_hsl(bg, .32, .34)};--glass-inner-bg:{_hsl(bg, .40, .13)};"
            f"--glass-inner-border:{_hsl(bg, .32, .24)};--input-bg:{_hsl(bg, .48, .05)};--input-border:{_hsl(bg, .32, .28)};"
            f"--discord-sidebar:{_hsl(bg, .45, .07)};--discord-channel:{_hsl(bg, .30, .72)};--discord-hover:{_rgba(ah, s, .62, .16)};"
            f"--text-sub:{_hsl(bg, .30, .76)};--text-muted:{_hsl(bg, .18, .56)};"
            f"--accent-purple:{_hsl(ah, s, .62)};--accent-purple-glow:{_rgba(ah, s, .62, .45)};--accent-soft:{_rgba(ah, s, .62, .22)};"
            f"--accent-soft-2:{_rgba(ah, s, .62, .12)};--accent-soft-3:{_rgba(ah, s, .62, .10)};--accent-edge:{_hsl(ah, s, .24)};"
            f"--on-accent:{_hsl(ah, .6, .08)};--accent-violet:{_hsl(ah, s, .72)};--card-bg:{_hsl(bg, .42, .11)};--card-border:{_hsl(bg, .32, .24)};}}"
        )
        light = (
            f"html[data-theme=\"light\"][data-skin=\"{key}\"]{{"
            f"--accent-purple:{_hsl(ah, s, .32)};--accent-purple-glow:{_rgba(ah, s, .32, .35)};--accent-soft:{_rgba(ah, s, .32, .16)};"
            f"--accent-soft-2:{_rgba(ah, s, .32, .10)};--accent-soft-3:{_rgba(ah, s, .32, .08)};--accent-edge:{_hsl(ah, s, .22)};"
            f"--accent-violet:{_hsl(ah, s, .28)};--discord-hover:{_rgba(ah, s, .32, .12)};}}"
        )
        out += [dark, light]
    return "\n".join(out)


def name_color_css() -> str:
    out = ["@keyframes ncflow{0%{background-position:0% 50%}100%{background-position:200% 50%}}",
           "html[data-theme=\"light\"] [class*=\"nc-\"]{text-shadow:0 0 1px rgba(0,0,0,.55)}"]
    for key, (name, price, css) in NAME_COLORS.items():
        if css.startswith("linear-gradient"):
            out.append(f".nc-{key}{{background:{css};background-size:200% 100%;-webkit-background-clip:text;background-clip:text;"
                       f"-webkit-text-fill-color:transparent;color:transparent;animation:ncflow 5s linear infinite}}")
        else:
            out.append(f".nc-{key}{{color:{css}}}")
    return "\n".join(out)
