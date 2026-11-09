"""One place for the brand (name, short name, tagline).

The owner changes it in the website (Owner Panel > Rebrand). It is saved in the database (collection
settings, id "brand"). Both the website and the bot read it, so nothing else has to be edited.

How the text changes: the code keeps its normal wording, and every text that leaves the program
(website pages, Discord embeds, buttons, messages, slash command descriptions) goes through apply().
apply() swaps the old brand words for the current brand.
"""
import re
import time

DEFAULT = {"name": "Template Engine", "short": "TE", "tagline": "Discord Architecture"}
LIMITS = {"name": 32, "short": 10, "tagline": 48}
# letters, numbers, spaces and a few safe signs (no quotes, brackets or markup, so it is safe in HTML, JS and Discord)
_OK = re.compile(r"^[\w .&!+:#@/\-]+$")
_S = {"cfg": dict(DEFAULT), "at": 0.0, "col": None, "auto": True, "ver": 0, "pat": None}

# The words in the code that are replaced.
_OLD_NAME, _OLD_SHORT, _OLD_TAG = DEFAULT["name"], DEFAULT["short"], DEFAULT["tagline"]
_SHORT_WORDS = "Premium|Points|points|Vault|Roles"
_SHORT_UP = "PREMIUM|POINTS|ROLES"


def configure(col, auto: bool = True):
    """col: the settings collection. auto=True: get() reads the database by itself (website).
    auto=False: the bot calls refresh() in its own loop."""
    _S["col"], _S["auto"] = col, auto


def validate(data: dict):
    """Returns (clean brand, error text)."""
    out = {}
    for key in ("name", "short", "tagline"):
        val = re.sub(r"\s+", " ", str((data or {}).get(key) or "")).strip()
        if not val:
            return None, f"{key.capitalize()} cannot be empty."
        if len(val) > LIMITS[key]:
            return None, f"{key.capitalize()} can have at most {LIMITS[key]} characters."
        if not _OK.match(val):
            return None, f"{key.capitalize()} can only use letters, numbers, spaces and . & ! + : # @ / -"
        out[key] = val
    return out, None


def refresh(force: bool = False):
    """Reads the saved brand from the database (at most every 5 seconds). Returns True if it changed."""
    now = time.time()
    if not force and now - _S["at"] < 5:
        return False
    _S["at"] = now
    col = _S["col"]
    if col is None:
        return False
    try:
        doc = col.find_one({"_id": "brand"}) or {}
    except Exception as e:
        print(f"[Brand Read Error] {e}")
        return False
    cfg, err = validate({k: doc.get(k) or DEFAULT[k] for k in DEFAULT})
    if err or cfg is None:
        cfg = dict(DEFAULT)
    if cfg != _S["cfg"]:
        _S["cfg"], _S["pat"] = cfg, None
        _S["ver"] += 1
        return True
    return False


def get() -> dict:
    if _S["auto"]:
        refresh()
    return _S["cfg"]


def version() -> int:
    return _S["ver"]


def save(cfg: dict):
    """Saves a brand that already passed validate()."""
    col = _S["col"]
    if col is not None:
        col.update_one({"_id": "brand"}, {"$set": {**cfg, "updated_at": time.time()}}, upsert=True)
    _S["cfg"], _S["pat"], _S["at"] = dict(cfg), None, time.time()
    _S["ver"] += 1


def reset():
    col = _S["col"]
    if col is not None:
        col.delete_one({"_id": "brand"})
    _S["cfg"], _S["pat"], _S["at"] = dict(DEFAULT), None, time.time()
    _S["ver"] += 1


def _patterns(cfg: dict):
    if _S["pat"] is None or _S["pat"][0] != cfg:
        _S["pat"] = (dict(cfg), [
            (re.compile(re.escape(_OLD_NAME.upper())), cfg["name"].upper()),
            (re.compile(re.escape(_OLD_NAME)), cfg["name"]),
            (re.compile(r"\b" + _OLD_SHORT + r"(?= (?:" + _SHORT_UP + r")\b)"), cfg["short"].upper()),
            (re.compile(r"\b" + _OLD_SHORT + r"(?= (?:" + _SHORT_WORDS + r")\b)"), cfg["short"]),
            (re.compile(re.escape(_OLD_TAG)), cfg["tagline"]),
        ])
    return _S["pat"][1]


def apply(text, cfg: dict = None):
    """Swaps the old brand words in a text for the current brand."""
    if not isinstance(text, str) or not text:
        return text
    cfg = cfg or get()
    if cfg == DEFAULT:
        return text
    for pat, new in _patterns(cfg):
        text = pat.sub(lambda m, n=new: n, text)
    return text


# ---------------- Discord side ----------------
def _embed_dict(d: dict):
    for key in ("title", "description"):
        if isinstance(d.get(key), str):
            d[key] = apply(d[key])
    for part, key in (("footer", "text"), ("author", "name")):
        if isinstance(d.get(part), dict) and isinstance(d[part].get(key), str):
            d[part][key] = apply(d[part][key])
    for f in d.get("fields") or []:
        for key in ("name", "value"):
            if isinstance(f.get(key), str):
                f[key] = apply(f[key])


def install_discord_hooks():
    """Makes every embed, button and message text pass through apply() when it is sent."""
    import functools
    import discord
    if getattr(discord, "_brand_hooked", False):
        return
    discord._brand_hooked = True

    orig_embed = discord.Embed.to_dict

    def embed_to_dict(self):
        d = orig_embed(self)
        try:
            _embed_dict(d)
        except Exception as e:
            print(f"[Brand Embed Error] {e}")
        return d
    discord.Embed.to_dict = embed_to_dict

    orig_button = discord.ui.Button.to_component_dict

    def button_dict(self):
        d = orig_button(self)
        if isinstance(d.get("label"), str):
            d["label"] = apply(d["label"])
        return d
    discord.ui.Button.to_component_dict = button_dict

    def wrap(owner, name):
        fn = getattr(owner, name, None)
        if fn is None:
            return

        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            if args and isinstance(args[0], str) and name not in ("edit_message", "edit", "edit_original_response"):
                args = (apply(args[0]),) + args[1:]
            if isinstance(kwargs.get("content"), str):
                kwargs["content"] = apply(kwargs["content"])
            return await fn(self, *args, **kwargs)
        setattr(owner, name, wrapper)

    for owner, name in ((discord.abc.Messageable, "send"), (discord.InteractionResponse, "send_message"),
                        (discord.InteractionResponse, "edit_message"), (discord.Webhook, "send"),
                        (discord.Interaction, "edit_original_response"), (discord.Message, "edit"),
                        (discord.WebhookMessage, "edit")):
        try:
            wrap(owner, name)
        except Exception as e:
            print(f"[Brand Hook Error] {owner}.{name}: {e}")


_ORIG = {}   # slash command texts as they are written in the code


def rebrand_tree(tree):
    """Changes the descriptions of the slash commands (they are saved at Discord when the tree is synced)."""
    cfg = get()
    for cmd in tree.walk_commands():
        try:
            key = id(cmd)
            _ORIG.setdefault(("cmd", key), cmd.description)
            cmd.description = apply(_ORIG[("cmd", key)], cfg)[:100]
            for pname, param in (getattr(cmd, "_params", None) or {}).items():
                _ORIG.setdefault(("p", key, pname), param.description)
                param.description = apply(_ORIG[("p", key, pname)], cfg)[:100]
                for ch in getattr(param, "choices", None) or []:
                    _ORIG.setdefault(("c", key, pname, str(ch.value)), ch.name)
                    ch.name = apply(_ORIG[("c", key, pname, str(ch.value))], cfg)[:100]
        except Exception as e:
            print(f"[Brand Tree Error] {getattr(cmd, 'name', '?')}: {e}")
