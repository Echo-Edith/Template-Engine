import os
import re
import time
import json
import random
import io
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

import discord
from discord.ext import commands, tasks
from pymongo import MongoClient, ReturnDocument

# -------------------------------------------------------------
# GLOBAL CONSTANTS & LOGGING CHANNELS
# -------------------------------------------------------------
BOT_OWNER_ID = 1219266886143967245
MAIN_GUILD_ID = 1284513257574629458

PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("PREMIUM_DESIGN_CHANNEL_ID", "1563544108696477802"))
NON_PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("NON_PREMIUM_DESIGN_CHANNEL_ID", "1563524101522464900"))
WEBSITE_LOGIN_CHANNEL_ID = int(os.getenv("WEBSITE_LOGIN_CHANNEL_ID", "1563524100276617248"))
SUBSCRIPTION_LOG_CHANNEL_ID = int(os.getenv("SUBSCRIPTION_LOG_CHANNEL_ID", "1564262413514182739"))

MONGO_URI = os.getenv("MONGO_URI")

db_premium = None
db_web_users = None
db_web_inbox = None
db_build_auth = None
db_users = None
db_cases = None
db_counters = None
db_afk = None
db_reminders = None
db_giveaways = None
db_settings = None
db_promos = None
db_broadcasts = None
db_backups = None
db_shop = None
db_tickets = None
db_owner_jobs = None
db_import_requests = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_premium = db["premium_users"]
        db_web_users = db["web_users"]
        db_web_inbox = db["web_inbox"]
        db_build_auth = db["build_authorized_users"]
        db_users = db["persistent_users"]
        db_cases = db["mod_cases"]
        db_counters = db["counters"]
        db_afk = db["afk_users"]
        db_reminders = db["reminders"]
        db_giveaways = db["giveaways"]
        db_settings = db["settings"]
        db_promos = db["promo_codes"]
        db_broadcasts = db["broadcasts"]
        db_backups = db["backups"]
        db_shop = db["shop_items"]
        db_tickets = db["tickets"]
        db_owner_jobs = db["owner_jobs"]
        db_import_requests = db["import_requests"]
    except Exception as e:
        print(f"[MongoDB Warning] {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_AUTHORIZED_BUILDERS = set()
LOCAL_PERSISTENT_USERS = {}
USER_POINTS_DATA = {}

# In-memory tracking for generated verification codes { user_id: { "code": "TE-12345", "expires_at": float } }
PENDING_VERIFICATION_CODES = {}

# In-memory tracking for used Dev Product receipt/transaction IDs to ensure ONE-TIME use
USED_DEV_PRODUCT_RECEIPTS = set()

# In-memory tracking for points anti-spam system
# Structure: { user_id: { "last_time": float, "msg_count": int, "last_content": str, "penalty_until": float } }
USER_MSG_TRACKER = {}


def get_roblox_cookie_header() -> dict:
    """Helper function to format ROBLOSECURITY headers for API calls."""
    raw_cookie = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip().strip('"\'')
    if not raw_cookie:
        return {}
    if not raw_cookie.startswith(".ROBLOSECURITY="):
        raw_cookie = f".ROBLOSECURITY={raw_cookie}"
    return {"Cookie": raw_cookie}


def get_user_points(user_id: str) -> int:
    """Retrieves point balance for a given user ID."""
    uid = str(user_id)
    if uid in USER_POINTS_DATA:
        return USER_POINTS_DATA[uid]
    if db_users is not None:
        try:
            doc = db_users.find_one({"discord_id": uid})
            if doc and "points" in doc:
                pts = int(doc["points"])
                USER_POINTS_DATA[uid] = pts
                return pts
        except Exception as e:
            print(f"[Mongo Get Points Error] {e}")
    return 0


def add_user_points(user_id: str, amount: int) -> int:
    """Adds or deducts points for a user and returns updated total."""
    uid = str(user_id)
    current_points = get_user_points(uid)
    new_total = current_points + amount
    if new_total < 0:
        new_total = 0

    if db_users is not None:
        try:
            db_users.update_one(
                {"discord_id": uid},
                {"$set": {
                    "discord_id": uid,
                    "points": new_total,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }},
                upsert=True
            )
        except Exception as e:
            print(f"[Mongo Add Points Error] {e}")

    USER_POINTS_DATA[uid] = new_total
    if uid not in LOCAL_PERSISTENT_USERS:
        LOCAL_PERSISTENT_USERS[uid] = {}
    LOCAL_PERSISTENT_USERS[uid]["points"] = new_total
    return new_total


def get_top_leaderboard(limit: int = 10) -> list:
    """Retrieves top point earners from MongoDB or local storage."""
    top_list = []
    if db_users is not None:
        try:
            cursor = db_users.find({"points": {"$gt": 0}}).sort("points", -1).limit(limit)
            for doc in cursor:
                top_list.append({"discord_id": doc.get("discord_id"), "points": doc.get("points", 0)})
            return top_list
        except Exception as e:
            print(f"[Leaderboard DB Fetch Error] {e}")

    for uid, pts in USER_POINTS_DATA.items():
        if pts > 0:
            top_list.append({"discord_id": uid, "points": pts})
    top_list.sort(key=lambda x: x["points"], reverse=True)
    return top_list[:limit]


def parse_duration_to_timedelta(duration_str: str) -> timedelta:
    """Parses duration strings like 1d, 7d, 30d, 1y, inf into timedelta objects."""
    if not duration_str:
        return timedelta(days=30)
    dur = duration_str.lower().strip()
    if dur in ["inf", "infinite", "perma", "permanent", "lifetime"]:
        return timedelta(days=36500)
    
    match = re.match(r'^(\d+)\s*([smhdwy])$', dur)
    if not match:
        return timedelta(days=30)
    
    val, unit = int(match.group(1)), match.group(2)
    if unit == 's': return timedelta(seconds=val)
    if unit == 'm': return timedelta(minutes=val)
    if unit == 'h': return timedelta(hours=val)
    if unit == 'd': return timedelta(days=val)
    if unit == 'w': return timedelta(weeks=val)
    if unit == 'y': return timedelta(days=val * 365)
    return timedelta(days=30)


PREMIUM_ROLE_DIRTY: set = set()  # users whose TE Premium role must be checked soon


def set_user_premium(user_id: str, duration_td: timedelta):
    """Saves premium status into MongoDB or local fallback storage."""
    PREMIUM_ROLE_DIRTY.add(str(user_id))
    exp_date = datetime.now(timezone.utc) + duration_td
    iso_exp = exp_date.isoformat()
    if db_premium is not None:
        try:
            db_premium.update_one(
                {"discord_id": str(user_id)},
                {"$set": {
                    "discord_id": str(user_id),
                    "expires_at": iso_exp,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }},
                upsert=True
            )
        except Exception as e:
            print(f"[Mongo Set Premium Error] {e}")
    LOCAL_PREMIUM_USERS[str(user_id)] = iso_exp


LOCAL_STAFF: set = set()
# Always staff with TE Premium. Cannot be removed with /staff. (Bot owner + one more person)
HARDCODED_STAFF = {str(BOT_OWNER_ID)}
# These IDs can run every owner-only command. Only HARDCODED_STAFF are permanent staff with premium.
COMMAND_ADMINS = {str(BOT_OWNER_ID), "1356624733373857902"}
STAFF_PREMIUM_DAYS = 36500  # about 100 years: staff premium does not run out


def add_staff(user_id: str) -> bool:
    """Makes the user staff and gives them TE Premium. Returns False if the database save failed."""
    uid = str(user_id)
    LOCAL_STAFF.add(uid)
    exp = (datetime.now(timezone.utc) + timedelta(days=STAFF_PREMIUM_DAYS)).isoformat()
    LOCAL_PREMIUM_USERS[uid] = exp
    if db_premium is not None:
        try:
            db_premium.update_one(
                {"discord_id": uid},
                {"$set": {"discord_id": uid, "expires_at": exp, "staff": True,
                          "updated_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True
            )
        except Exception as e:
            print(f"[Mongo Add Staff Error] {e}")
            return False
    return True


LOCAL_STAFF_ROLE: dict = {}


def set_staff_role(guild_id: int, role_id: int) -> bool:
    """Saves the Discord role that /staff add gives. Returns False if the database save failed."""
    LOCAL_STAFF_ROLE.clear()
    LOCAL_STAFF_ROLE.update({"guild_id": int(guild_id), "role_id": int(role_id)})
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": "staff_role"}, {"$set": dict(LOCAL_STAFF_ROLE)}, upsert=True)
        except Exception as e:
            print(f"[Mongo Staff Role Error] {e}")
            return False
    return True


def get_staff_role() -> Optional[dict]:
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "staff_role"})
            if doc and doc.get("role_id"):
                return {"guild_id": int(doc["guild_id"]), "role_id": int(doc["role_id"])}
            return None
        except Exception as e:
            print(f"[Mongo Staff Role Read Error] {e}")
    return dict(LOCAL_STAFF_ROLE) or None


def is_staff(user_id: str) -> bool:
    uid = str(user_id)
    if uid in HARDCODED_STAFF:
        return True
    if db_premium is not None:
        try:
            return bool(db_premium.find_one({"discord_id": uid, "staff": True}))
        except Exception as e:
            print(f"[Mongo Staff Check Error] {e}")
    return uid in LOCAL_STAFF


def remove_staff(user_id: str) -> bool:
    """Removes staff AND the premium that came with it. Returns False if the user was not staff."""
    uid = str(user_id)
    if uid in HARDCODED_STAFF or not is_staff(uid):
        return False
    LOCAL_STAFF.discard(uid)
    remove_user_premium(uid)
    return True


def list_staff() -> List[str]:
    ids = set(LOCAL_STAFF) | HARDCODED_STAFF
    if db_premium is not None:
        try:
            ids = {str(d["discord_id"]) for d in db_premium.find({"staff": True})} | HARDCODED_STAFF
        except Exception as e:
            print(f"[Mongo Staff List Error] {e}")
    return sorted(ids)


def extend_user_premium(user_id: str, days: int) -> datetime:
    """Adds days of premium. If the user already has premium, the days are added after it ends.
    Returns the new end date."""
    uid = str(user_id)
    PREMIUM_ROLE_DIRTY.add(uid)
    now = datetime.now(timezone.utc)
    current = None
    raw = None
    if db_premium is not None:
        try:
            doc = db_premium.find_one({"discord_id": uid}) or {}
            raw = doc.get("expires_at")
        except Exception as e:
            print(f"[Mongo Premium Read Error] {e}")
    if raw is None:
        raw = LOCAL_PREMIUM_USERS.get(uid)
    if raw:
        try:
            current = datetime.fromisoformat(str(raw))
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
        except ValueError:
            current = None
    base = current if current and current > now else now
    new_exp = base + timedelta(days=int(days))
    if db_premium is not None:
        try:
            db_premium.update_one(
                {"discord_id": uid},
                {"$set": {"discord_id": uid, "expires_at": new_exp.isoformat(), "updated_at": now.isoformat()}},
                upsert=True
            )
        except Exception as e:
            print(f"[Mongo Extend Premium Error] {e}")
    LOCAL_PREMIUM_USERS[uid] = new_exp.isoformat()
    return new_exp


LIFETIME_PREMIUM_DAYS = 36500  # about 100 years = lifetime


def get_premium_expiry(user_id: str) -> Optional[datetime]:
    """When the user's premium ends (None if they have none)."""
    uid = str(user_id)
    raw = None
    if db_premium is not None:
        try:
            raw = (db_premium.find_one({"discord_id": uid}) or {}).get("expires_at")
        except Exception as e:
            print(f"[Mongo Premium Read Error] {e}")
    if raw is None:
        raw = LOCAL_PREMIUM_USERS.get(uid)
    if not raw:
        return None
    try:
        exp = datetime.fromisoformat(str(raw))
        return exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def user_has_premium(user_id: str) -> bool:
    """True if the user has TE Premium right now (lifetime/staff included)."""
    if str(user_id) in HARDCODED_STAFF:
        return True
    exp = get_premium_expiry(str(user_id))
    return bool(exp and exp > datetime.now(timezone.utc))


def has_lifetime_premium(user_id: str) -> bool:
    if str(user_id) in HARDCODED_STAFF:
        return True
    exp = get_premium_expiry(user_id)
    return bool(exp and exp > datetime.now(timezone.utc) + timedelta(days=LIFETIME_PREMIUM_DAYS - 365))


def list_premium_users() -> List[str]:
    """IDs of everyone with active TE Premium (permanent staff included)."""
    now = datetime.now(timezone.utc)

    def active(raw) -> bool:
        if not raw:
            return True
        try:
            exp = datetime.fromisoformat(str(raw))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp > now
        except ValueError:
            return True

    ids = {uid for uid, raw in LOCAL_PREMIUM_USERS.items() if active(raw)}
    if db_premium is not None:
        try:
            ids = {str(d["discord_id"]) for d in db_premium.find({}) if d.get("discord_id") and active(d.get("expires_at"))}
        except Exception as e:
            print(f"[Mongo Premium List Error] {e}")
    ids |= HARDCODED_STAFF
    return sorted(ids, key=int)


def reduce_user_premium(user_id: str, days: int):
    """Takes days of premium away. If nothing is left, premium is removed."""
    uid = str(user_id)
    PREMIUM_ROLE_DIRTY.add(uid)
    raw = None
    if db_premium is not None:
        try:
            raw = (db_premium.find_one({"discord_id": uid}) or {}).get("expires_at")
        except Exception as e:
            print(f"[Mongo Premium Read Error] {e}")
    if raw is None:
        raw = LOCAL_PREMIUM_USERS.get(uid)
    if not raw:
        return
    try:
        current = datetime.fromisoformat(str(raw))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    except ValueError:
        return
    new_exp = current - timedelta(days=int(days))
    if new_exp <= datetime.now(timezone.utc):
        remove_user_premium(uid)
        return
    if db_premium is not None:
        try:
            db_premium.update_one({"discord_id": uid}, {"$set": {"expires_at": new_exp.isoformat()}})
        except Exception as e:
            print(f"[Mongo Reduce Premium Error] {e}")
    LOCAL_PREMIUM_USERS[uid] = new_exp.isoformat()


def remove_user_premium(user_id: str):
    """Removes premium status from MongoDB or local storage."""
    PREMIUM_ROLE_DIRTY.add(str(user_id))
    if db_premium is not None:
        try:
            db_premium.delete_one({"discord_id": str(user_id)})
        except Exception as e:
            print(f"[Mongo Remove Premium Error] {e}")
    LOCAL_PREMIUM_USERS.pop(str(user_id), None)


def strip_emojis(text: str) -> str:
    """Strips unicode emojis from channel or category titles."""
    if not text:
        return ""
    clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff]', '', str(text))
    return clean.strip()


def format_channel_name(name: str) -> str:
    """Formats channel names into Discord lowercase hyphens format without emojis."""
    clean = strip_emojis(name)
    if not clean:
        clean = str(name).strip()
    return clean.lower().replace(" ", "-")


# -------------------------------------------------------------
# AUTO-BUILD PERMISSION HELPERS
# The website saves permissions in two shapes:
#   per role : {"Member": {"view": True, "send": False, ...}, "@everyone": {...}}
#   flat     : {"view": True, "send": False, ...}   (from the AI, describes @everyone)
# -------------------------------------------------------------
ROLE_PERMISSION_ALIASES = {"manage_server": "manage_guild"}

# website permission id -> Discord channel overwrite field
CHANNEL_PERMISSION_MAP = {
    "view": "view_channel",
    "send": "send_messages",
    "embed": "embed_links",
    "attach": "attach_files",
    "history": "read_message_history",
    "connect": "connect",
    "speak": "speak",
    "manage": "manage_channels",
}

# For these, an unchecked box means "deny". For the others, unchecked means "leave the default".
DENY_WHEN_UNCHECKED = {"view", "send", "connect", "speak"}


def build_role_permissions(perm_dict) -> Optional[discord.Permissions]:
    """Turns the website's role permission toggles into a Permissions object (None = keep Discord's default)."""
    if not isinstance(perm_dict, dict) or not perm_dict:
        return None
    flags = {}
    for key, value in perm_dict.items():
        name = ROLE_PERMISSION_ALIASES.get(str(key), str(key))
        if name == "permissions":
            continue
        try:
            discord.Permissions(**{name: True})  # raises TypeError for unknown permission names
        except TypeError:
            print(f"[Auto-Build Permission Warning] Unknown role permission ignored: {key}")
            continue
        flags[name] = bool(value)
    return discord.Permissions(**flags) if flags else None


def resolve_layout_role(name: str, created_roles: dict, guild: discord.Guild) -> Optional[discord.Role]:
    """Finds the Discord role for a role name used in the layout (@everyone or one we just created)."""
    raw = str(name or "").strip()
    if raw.lower() in ("@everyone", "everyone"):
        return guild.default_role
    if raw in created_roles:
        return created_roles[raw]
    cleaned = strip_emojis(raw)
    if cleaned in created_roles:
        return created_roles[cleaned]
    lowered = (cleaned or raw).lower()
    for key, role in created_roles.items():
        if key.lower() == lowered:
            return role
    return None


def _copy_overwrites(base: Optional[dict]) -> dict:
    copied = {}
    for target, overwrite in (base or {}).items():
        copied[target] = discord.PermissionOverwrite(**{k: v for k, v in overwrite if v is not None})
    return copied


def collect_overwrites(guild: discord.Guild, created_roles: dict, permissions, read_only: bool = False,
                       base: Optional[dict] = None) -> dict:
    """Builds {role: PermissionOverwrite} for a category or channel. `base` (the category) is merged in first."""
    overwrites = _copy_overwrites(base)

    if isinstance(permissions, dict):
        for key, value in permissions.items():
            if isinstance(value, dict):
                role = resolve_layout_role(key, created_roles, guild)
                if role is None:
                    print(f"[Auto-Build Permission Warning] Role not found for permissions: {key}")
                    continue
                overwrite = overwrites.get(role) or discord.PermissionOverwrite()
                for perm_id, field in CHANNEL_PERMISSION_MAP.items():
                    if perm_id not in value:
                        continue
                    if value[perm_id]:
                        setattr(overwrite, field, True)
                    elif perm_id in DENY_WHEN_UNCHECKED:
                        setattr(overwrite, field, False)
                overwrites[role] = overwrite
            elif isinstance(value, bool) and key in CHANNEL_PERMISSION_MAP and value is False:
                # Flat format (from the AI) describes @everyone. Only "False" changes anything.
                overwrite = overwrites.get(guild.default_role) or discord.PermissionOverwrite()
                setattr(overwrite, CHANNEL_PERMISSION_MAP[key], False)
                overwrites[guild.default_role] = overwrite

    if read_only:
        overwrite = overwrites.get(guild.default_role) or discord.PermissionOverwrite()
        overwrite.send_messages = False
        overwrites[guild.default_role] = overwrite

    return overwrites


async def create_with_overwrite_fallback(create_fn, label: str, **kwargs):
    """Creates a channel or category. If Discord rejects the permission overwrites, retries without them."""
    try:
        return await create_fn(**kwargs)
    except discord.HTTPException as e:
        if not kwargs.get("overwrites"):
            raise
        print(f"[Auto-Build Permission Warning] {label}: {e}. Retrying without permission overwrites.")
        kwargs.pop("overwrites")
        return await create_fn(**kwargs)


# -------------------------------------------------------------
# SHARED EMBED HELPERS
# -------------------------------------------------------------
COLOR_PURPLE = 0x8B5CF6
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xF43F5E
COLOR_GOLD = 0xF1C40F
COLOR_GRAY = 0x95A5A6

LOG_DIVIDER = "~~" + "\u2800" * 26 + "~~"  # solid line, no gaps


def build_log_embed(title: str, color: int, items: list, thumbnail: Optional[str] = None, footer: Optional[str] = None) -> discord.Embed:
    """One clean log layout for every log: bold label, value, thin line between items. No emojis."""
    body = f"\n{LOG_DIVIDER}\n".join(f"**{label}**\n{value}" for label, value in items)
    embed = discord.Embed(title=title, description=body[:4000], color=color, timestamp=datetime.now(timezone.utc))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if footer:
        embed.set_footer(text=footer)
    return embed



def embed_ok(title: str, description: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_GREEN)


def embed_err(title: str, description: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_RED)


def embed_info(title: str, description: Optional[str] = None, color: int = COLOR_PURPLE) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


# -------------------------------------------------------------
# DURATION HELPERS (strict: invalid text returns None)
# -------------------------------------------------------------
DURATION_FULL_RE = re.compile(r'^(?:\d+[smhdwy])+$')
DURATION_PART_RE = re.compile(r'(\d+)([smhdwy])')
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def parse_duration_strict(text: Optional[str]) -> Optional[timedelta]:
    """Parses '10m', '2h30m', '1d', '1w' ... Returns None if the text is not a valid duration."""
    if not text:
        return None
    cleaned = re.sub(r'\s+', '', text.lower())
    if not DURATION_FULL_RE.match(cleaned):
        return None
    try:
        total = sum(int(n) * DURATION_UNITS[u] for n, u in DURATION_PART_RE.findall(cleaned))
    except Exception:
        return None
    if total <= 0 or total > 100 * DURATION_UNITS["y"]:
        return None
    return timedelta(seconds=total)


def format_timedelta(td: timedelta) -> str:
    secs = int(td.total_seconds())
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if secs >= size:
            value, secs = divmod(secs, size)
            parts.append(f"{value}{label}")
    return " ".join(parts) or "0s"


# -------------------------------------------------------------
# PAGINATION (used by modlog, rolelist and giveaway entries)
# -------------------------------------------------------------
class PaginatorView(discord.ui.View):
    def __init__(self, pages: List[discord.Embed], timeout: float = 300):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = 0
        self._refresh()

    def _refresh(self):
        self.prev_btn.disabled = self.index <= 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1
        self.counter_btn.label = f"{self.index + 1}/{len(self.pages)}"

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


def build_pages(title: str, lines: List[str], per_page: int = 20, color: int = COLOR_PURPLE, header: str = "") -> List[discord.Embed]:
    """Splits lines into embed pages (one line per row)."""
    if not lines:
        return [discord.Embed(title=title, description=(header + "Nothing to show.").strip(), color=color)]
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    pages = []
    for idx, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(title=title, description=header + "\n".join(chunk), color=color)
        embed.set_footer(text=f"Page {idx}/{len(chunks)}")
        pages.append(embed)
    return pages


async def send_pages(interaction: discord.Interaction, pages: List[discord.Embed], ephemeral: bool = True):
    kwargs = {"view": PaginatorView(pages)} if len(pages) > 1 else {}
    if interaction.response.is_done():
        await interaction.followup.send(embed=pages[0], ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(embed=pages[0], ephemeral=ephemeral, **kwargs)


# -------------------------------------------------------------
# MODERATION CASE LOG
# -------------------------------------------------------------
MOD_ACTION_LABELS = {
    "warn": "Warn",
    "mute": "Mute (Timeout)",
    "unmute": "Unmute",
    "kick": "Kick",
    "ban": "Ban",
    "unban": "Unban",
}

LOCAL_CASES: List[dict] = []
LOCAL_CASE_COUNTERS: Dict[str, int] = {}


def next_case_id(guild_id) -> int:
    gid = str(guild_id)
    if db_counters is not None:
        try:
            doc = db_counters.find_one_and_update(
                {"_id": f"cases:{gid}"},
                {"$inc": {"value": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER
            )
            return int(doc["value"])
        except Exception as e:
            print(f"[Case Counter Error] {e}")
    LOCAL_CASE_COUNTERS[gid] = LOCAL_CASE_COUNTERS.get(gid, 0) + 1
    return LOCAL_CASE_COUNTERS[gid]


def add_case(guild_id, user_id, action: str, reason: str, moderator_id, duration: Optional[str] = None, moderator_message: Optional[str] = None) -> dict:
    case = {
        "guild_id": str(guild_id),
        "case_id": next_case_id(guild_id),
        "user_id": str(user_id),
        "action": action,
        "reason": reason,
        "moderator_id": str(moderator_id),
        "duration": duration,
        "moderator_message": moderator_message,
        "created_at": datetime.now(timezone.utc).timestamp(),
    }
    if db_cases is not None:
        try:
            db_cases.insert_one(dict(case))
            return case
        except Exception as e:
            print(f"[Case Insert Error] {e}")
    LOCAL_CASES.append(case)
    return case


def get_cases(guild_id, user_id) -> List[dict]:
    gid, uid = str(guild_id), str(user_id)
    if db_cases is not None:
        try:
            return list(db_cases.find({"guild_id": gid, "user_id": uid}, {"_id": 0}).sort("case_id", -1))
        except Exception as e:
            print(f"[Case Fetch Error] {e}")
    found = [c for c in LOCAL_CASES if c["guild_id"] == gid and c["user_id"] == uid]
    found.sort(key=lambda c: c["case_id"], reverse=True)
    return found


def delete_cases(guild_id, user_id, action: str, case_id: Optional[int] = None) -> int:
    """Removes records from the mod log. Returns how many were removed."""
    gid, uid = str(guild_id), str(user_id)
    query = {"guild_id": gid, "user_id": uid, "action": action}
    if case_id is not None:
        query["case_id"] = int(case_id)
    removed = 0
    if db_cases is not None:
        try:
            removed += db_cases.delete_many(query).deleted_count
        except Exception as e:
            print(f"[Case Delete Error] {e}")
    before = len(LOCAL_CASES)
    LOCAL_CASES[:] = [
        c for c in LOCAL_CASES
        if not all(c.get(k) == v for k, v in query.items())
    ]
    removed += before - len(LOCAL_CASES)
    return removed


def get_case(guild_id, case_id) -> Optional[dict]:
    """Finds one case by its ID. Case IDs are unique per server."""
    gid, cid = str(guild_id), int(case_id)
    if db_cases is not None:
        try:
            doc = db_cases.find_one({"guild_id": gid, "case_id": cid}, {"_id": 0})
            if doc:
                return doc
        except Exception as e:
            print(f"[Case Lookup Error] {e}")
    return next((c for c in LOCAL_CASES if c["guild_id"] == gid and c["case_id"] == cid), None)


def delete_case(guild_id, case_id) -> bool:
    """Removes one case by its ID. Returns True if a record was removed."""
    gid, cid = str(guild_id), int(case_id)
    removed = False
    if db_cases is not None:
        try:
            removed = db_cases.delete_one({"guild_id": gid, "case_id": cid}).deleted_count > 0
        except Exception as e:
            print(f"[Case Delete Error] {e}")
    before = len(LOCAL_CASES)
    LOCAL_CASES[:] = [c for c in LOCAL_CASES if not (c["guild_id"] == gid and c["case_id"] == cid)]
    return removed or len(LOCAL_CASES) < before


async def dm_moderation_notice(user, guild: discord.Guild, action: str, reason: str,
                               case_id: Optional[int] = None, duration: Optional[str] = None,
                               moderator_message: Optional[str] = None) -> bool:
    """DMs the moderated user. The moderator's name is never included (privacy), but the moderator's message is. Returns True if delivered."""
    titles = {
        "warn": "You were warned",
        "mute": "You were muted",
        "unmute": "Your mute was removed",
        "kick": "You were kicked",
        "ban": "You were banned",
    }
    embed = discord.Embed(title=f"{titles.get(action, 'Moderation notice')} in {guild.name}", color=COLOR_RED)
    embed.add_field(name="Reason", value=(reason or "No reason provided")[:1000], inline=False)
    if moderator_message:
        embed.add_field(name="Message From Moderator", value=moderator_message[:1000], inline=False)
    if duration:
        embed.add_field(name="Duration", value=f"`{duration}`", inline=True)
    if case_id:
        embed.add_field(name="Case ID", value=f"`#{case_id}`", inline=True)
    try:
        await user.send(embed=embed)
        return True
    except Exception:
        return False


# -------------------------------------------------------------
# AFK, REMINDERS & GIVEAWAY STORAGE (memory cache + MongoDB write-through)
# -------------------------------------------------------------
AFK_USERS: Dict[tuple, dict] = {}
REMINDERS: List[dict] = []
GIVEAWAYS: Dict[str, dict] = {}
GIVEAWAY_DEBUG: Dict[str, Any] = {"last_error": None, "last_tick": 0.0}  # shown by /giveaway status
MAX_REMINDERS_PER_USER = 25


def load_runtime_state():
    """Loads AFK users, reminders and giveaways from MongoDB (if configured)."""
    if db_afk is not None:
        try:
            AFK_USERS.clear()
            for d in db_afk.find({}, {"_id": 0}):
                AFK_USERS[(d["guild_id"], d["user_id"])] = d
        except Exception as e:
            print(f"[Load AFK Error] {e}")
    if db_reminders is not None:
        try:
            REMINDERS.clear()
            REMINDERS.extend(db_reminders.find({}, {"_id": 0}))
        except Exception as e:
            print(f"[Load Reminders Error] {e}")
    if db_giveaways is not None:
        try:
            GIVEAWAYS.clear()
            for d in db_giveaways.find({}, {"_id": 0}):
                GIVEAWAYS[d["message_id"]] = d
            print(f"[Giveaways] Loaded {len(GIVEAWAYS)} from the database.")
        except Exception as e:
            print(f"[Load Giveaways Error] {e}")


def set_afk(guild_id, user_id, reason: str):
    doc = {
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "reason": reason,
        "since": datetime.now(timezone.utc).timestamp(),
    }
    AFK_USERS[(doc["guild_id"], doc["user_id"])] = doc
    if db_afk is not None:
        try:
            db_afk.update_one({"guild_id": doc["guild_id"], "user_id": doc["user_id"]}, {"$set": doc}, upsert=True)
        except Exception as e:
            print(f"[Set AFK Error] {e}")


def get_afk(guild_id, user_id) -> Optional[dict]:
    return AFK_USERS.get((str(guild_id), str(user_id)))


def clear_afk(guild_id, user_id):
    AFK_USERS.pop((str(guild_id), str(user_id)), None)
    if db_afk is not None:
        try:
            db_afk.delete_one({"guild_id": str(guild_id), "user_id": str(user_id)})
        except Exception as e:
            print(f"[Clear AFK Error] {e}")


def count_user_reminders(user_id) -> int:
    return sum(1 for r in REMINDERS if r["user_id"] == str(user_id))


def add_reminder(user_id, channel_id, message: str, due_ts: float) -> dict:
    doc = {
        "id": os.urandom(4).hex(),
        "user_id": str(user_id),
        "channel_id": str(channel_id),
        "message": message,
        "created_ts": datetime.now(timezone.utc).timestamp(),
        "due_ts": due_ts,
    }
    REMINDERS.append(doc)
    if db_reminders is not None:
        try:
            db_reminders.insert_one(dict(doc))
        except Exception as e:
            print(f"[Add Reminder Error] {e}")
    return doc


def remove_reminder(reminder_id: str):
    REMINDERS[:] = [r for r in REMINDERS if r["id"] != reminder_id]
    if db_reminders is not None:
        try:
            db_reminders.delete_one({"id": reminder_id})
        except Exception as e:
            print(f"[Remove Reminder Error] {e}")


def save_giveaway(gw: dict):
    GIVEAWAYS[gw["message_id"]] = gw
    if db_giveaways is not None:
        try:
            db_giveaways.replace_one({"message_id": gw["message_id"]}, dict(gw), upsert=True)
        except Exception as e:
            print(f"[Save Giveaway Error] {e}")


def delete_giveaway_record(message_id: str):
    GIVEAWAYS.pop(str(message_id), None)
    if db_giveaways is not None:
        try:
            db_giveaways.delete_one({"message_id": str(message_id)})
        except Exception as e:
            print(f"[Delete Giveaway Error] {e}")


# -------------------------------------------------------------
# GIVEAWAY ENGINE
# -------------------------------------------------------------
GIVEAWAY_DIVIDER = "\u2500" * 28


def build_giveaway_embed(gw: dict) -> discord.Embed:
    end_ts = int(gw["end_ts"])
    host = f"<@{gw['host_id']}>"
    if gw.get("ended"):
        winner_ids = gw.get("winner_ids", [])
        winners_text = "\n".join(f"<@{w}>" for w in winner_ids) if winner_ids else "No valid entries"
        label = "Winner" if len(winner_ids) == 1 else "Winners"
        embed = discord.Embed(
            title=gw["prize"],
            description=f"**Hosted by:** {host}\n**{label}:** {winners_text}",
            color=COLOR_GRAY
        )
        if gw.get("host_message"):
            embed.add_field(name="Message from Host", value=gw["host_message"], inline=False)
        embed.set_footer(text=f"{len(gw['entries'])} entries | Giveaway ended")
    else:
        embed = discord.Embed(
            title=gw["prize"],
            description=(
                f"Ends <t:{end_ts}:R> \u2022 <t:{end_ts}:F>\n"
                f"{GIVEAWAY_DIVIDER}\n"
                f"**Hosted by:** {host}\n"
                f"**Entries:** {len(gw['entries'])}\n"
                f"**Winners:** {int(gw['winners'])}"
                + (f"\n{giveaway_luck_text(gw)}" if giveaway_luck_text(gw) else "")
            ),
            color=COLOR_PURPLE
        )
        if gw.get("host_message"):
            embed.add_field(name="Message from Host", value=gw["host_message"], inline=False)
    return embed


async def giveaway_enter(interaction: discord.Interaction):
    """Enter / leave a giveaway (used by both the classic and the card layout buttons)."""
    gw = GIVEAWAYS.get(str(interaction.message.id))
    if not gw:
        await interaction.response.send_message(embed=embed_err("Giveaway Not Found", "This giveaway is no longer tracked."), ephemeral=True)
        return
    if gw.get("ended") or gw["end_ts"] <= datetime.now(timezone.utc).timestamp():
        await interaction.response.send_message(embed=embed_err("Giveaway Ended", "This giveaway has already ended."), ephemeral=True)
        return

    uid = str(interaction.user.id)
    if uid in gw["entries"]:
        gw["entries"].remove(uid)
        save_giveaway(gw)
        embed = embed_info("Entry Removed", f"You left the giveaway for **{gw['prize']}**.\nClick the gift button again to rejoin.", COLOR_GOLD)
    else:
        gw["entries"].append(uid)
        save_giveaway(gw)
        embed = embed_ok("You Are In!", f"You entered the giveaway for **{gw['prize']}**.\nTotal entries: `{len(gw['entries'])}`\nClick the gift button again to leave.")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    try:
        await interaction.message.edit(**giveaway_payload(gw, interaction.message))
    except Exception as e:
        print(f"[Giveaway Count Update Error] {e!r}")


async def giveaway_entries(interaction: discord.Interaction):
    gw = GIVEAWAYS.get(str(interaction.message.id))
    if not gw:
        await interaction.response.send_message(embed=embed_err("Giveaway Not Found", "This giveaway is no longer tracked."), ephemeral=True)
        return
    if gw.get("ended"):
        await interaction.response.send_message(embed=embed_err("Giveaway Ended", "This giveaway has already ended."), ephemeral=True)
        return
    lines = [f"<@{uid}>" for uid in gw["entries"]]
    pages = build_pages(
        "Giveaway Entries", lines, per_page=20,
        header=f"**{gw['prize']}**\nTotal entries: `{len(lines)}`\n\n"
    )
    await send_pages(interaction, pages, ephemeral=True)


class GiveawayView(discord.ui.View):
    """Classic layout (embed + buttons under it). Used for old giveaway messages and when the card layout is not available."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await blocked_check(interaction)

    def __init__(self, ended: bool = False, entries: int = 0):
        super().__init__(timeout=None)
        self.entries_btn.label = f"Entries ({entries})"
        if ended:
            self.enter_btn.disabled = True
            self.entries_btn.disabled = True

    @classmethod
    def for_giveaway(cls, gw: dict) -> "GiveawayView":
        return cls(ended=bool(gw.get("ended")), entries=len(gw["entries"]))

    @discord.ui.button(emoji="\U0001F381", style=discord.ButtonStyle.primary, custom_id="giveaway_enter")
    async def enter_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await giveaway_enter(interaction)

    @discord.ui.button(label="Entries", style=discord.ButtonStyle.secondary, custom_id="giveaway_entries")
    async def entries_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await giveaway_entries(interaction)


# Card layout: the buttons sit INSIDE the card (needs discord.py 2.6 or newer)
HAS_CARD_LAYOUT = all(hasattr(discord.ui, n) for n in ("LayoutView", "Container", "ActionRow", "TextDisplay", "Separator"))

if HAS_CARD_LAYOUT:
    class _GiveawayEnterButton(discord.ui.Button):
        def __init__(self, disabled: bool = False):
            super().__init__(emoji="\U0001F381", style=discord.ButtonStyle.primary, custom_id="giveaway_enter", disabled=disabled)

        async def callback(self, interaction: discord.Interaction):
            await giveaway_enter(interaction)

    class _GiveawayEntriesButton(discord.ui.Button):
        def __init__(self, entries: int = 0, disabled: bool = False):
            super().__init__(label=f"Entries ({entries})", style=discord.ButtonStyle.secondary, custom_id="giveaway_entries", disabled=disabled)

        async def callback(self, interaction: discord.Interaction):
            await giveaway_entries(interaction)

    class GiveawayLayout(discord.ui.LayoutView):
        """The whole giveaway as one card: prize, end time, host, entries, winners and the buttons."""

        def __init__(self, gw: Optional[dict] = None):
            super().__init__(timeout=None)
            if gw is None:  # used only to register the buttons when the bot starts
                gw = {"prize": "", "end_ts": 0, "host_id": 0, "entries": [], "winners": 1, "ended": False}
            ended = bool(gw.get("ended"))
            host = f"<@{gw['host_id']}>"
            end_ts = int(gw["end_ts"])

            container = discord.ui.Container(accent_colour=discord.Colour(COLOR_GRAY if ended else COLOR_PURPLE))
            container.add_item(discord.ui.TextDisplay(f"### {gw['prize']}"))
            if ended:
                winner_ids = gw.get("winner_ids", [])
                label = "Winner" if len(winner_ids) == 1 else "Winners"
                winners_text = ", ".join(f"<@{w}>" for w in winner_ids) if winner_ids else "No valid entries"
                container.add_item(discord.ui.Separator())
                container.add_item(discord.ui.TextDisplay(f"**Hosted by:** {host}\n**{label}:** {winners_text}"))
                if gw.get("host_message"):
                    container.add_item(discord.ui.Separator())
                    container.add_item(discord.ui.TextDisplay(f"**Message from Host**\n{gw['host_message']}"))
            else:
                container.add_item(discord.ui.TextDisplay(f"Ends <t:{end_ts}:R> \u2022 <t:{end_ts}:F>"))
                container.add_item(discord.ui.Separator())
                container.add_item(discord.ui.TextDisplay(
                    f"**Hosted by:** {host}\n**Entries:** {len(gw['entries'])}\n**Winners:** {int(gw['winners'])}"
                    + (f"\n{giveaway_luck_text(gw)}" if giveaway_luck_text(gw) else "")
                ))
                if gw.get("host_message"):
                    container.add_item(discord.ui.Separator())
                    container.add_item(discord.ui.TextDisplay(f"**Message from Host**\n{gw['host_message']}"))
            row = discord.ui.ActionRow()
            row.add_item(_GiveawayEnterButton(disabled=ended))
            row.add_item(_GiveawayEntriesButton(len(gw["entries"]), disabled=ended))
            container.add_item(row)
            self.add_item(container)


HAS_FILE_COMPONENT = HAS_CARD_LAYOUT and hasattr(discord.ui, "File")


def log_payload(title: str, color: int, items: list, file: Optional[discord.File] = None) -> dict:
    """Arguments for channel.send() that show a clean log.
    Card layout: title, then a thin separator line between every item (like the Discord ad card).
    Old discord.py: falls back to a normal embed with text lines."""
    can_card = HAS_CARD_LAYOUT and (file is None or HAS_FILE_COMPONENT)
    if not can_card:
        payload = {"embed": build_log_embed(title, color, items)}
        if file is not None:
            payload["file"] = file
        return payload
    container = discord.ui.Container(accent_colour=discord.Colour(color))
    container.add_item(discord.ui.TextDisplay(f"### {title}"))
    for label, value in items:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"**{label}**\n{value}"[:1200]))
    if file is not None:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.File(f"attachment://{file.filename}"))
    container.add_item(discord.ui.TextDisplay(f"-# <t:{int(datetime.now(timezone.utc).timestamp())}:F>"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    payload = {"view": view}
    if file is not None:
        payload["file"] = file
    return payload


WEBSITE_URL = os.getenv("WEBSITE_URL", "https://templateengine-xi.vercel.app").strip()
WEBSITE_COOLDOWN_SECONDS = 30
WEBSITE_WORD = re.compile(r"\bwebsite\b", re.IGNORECASE)
WEBSITE_LAST_REPLY: Dict[int, float] = {}  # channel id -> time of the last reply


def website_payload() -> dict:
    """The reply for the word 'website': a card (or embed on old discord.py) with a link button to the site."""
    title = "Template Engine Website"
    text = "Build your Discord server layout with AI, then deploy it with the bot."
    if HAS_CARD_LAYOUT:
        container = discord.ui.Container(accent_colour=discord.Colour(COLOR_PURPLE))
        container.add_item(discord.ui.TextDisplay(f"### {title}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(text))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(label="Open Website", style=discord.ButtonStyle.link, url=WEBSITE_URL))
        container.add_item(row)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        return {"view": view}
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Open Website", style=discord.ButtonStyle.link, url=WEBSITE_URL))
    return {"embed": discord.Embed(title=title, description=text, color=COLOR_PURPLE), "view": view}


def message_uses_card_layout(message: Optional[discord.Message]) -> bool:
    flags = getattr(message, "flags", None)
    return bool(getattr(flags, "components_v2", False))


def giveaway_payload(gw: dict, message: Optional[discord.Message] = None) -> dict:
    """Arguments for channel.send() / message.edit() that show the giveaway.
    New giveaways use the card layout. Old giveaway messages (and bots without discord.py 2.6) use the embed layout."""
    if HAS_CARD_LAYOUT and (message is None or message_uses_card_layout(message)):
        return {"view": GiveawayLayout(gw)}
    payload = {"embed": build_giveaway_embed(gw), "view": GiveawayView.for_giveaway(gw)}
    if message is not None and message_uses_card_layout(message):
        payload["view"] = GiveawayView.for_giveaway(gw)
    return payload


async def _resolve_member(guild: Optional[discord.Guild], uid: str):
    if guild is None:
        return None
    member = guild.get_member(int(uid))
    if member:
        return member
    try:
        return await guild.fetch_member(int(uid))
    except Exception:
        return None


async def valid_giveaway_entries(bot: commands.Bot, gw: dict, exclude: tuple = ()) -> List[str]:
    """Entries whose user is still in the server and is not a bot."""
    guild = bot.get_guild(int(gw["guild_id"]))
    valid = []
    for uid in gw["entries"]:
        if uid in exclude:
            continue
        if guild is None:
            valid.append(uid)
            continue
        member = await _resolve_member(guild, uid)
        if member and not member.bot:
            valid.append(uid)
    return valid


def giveaway_luck_text(gw: dict) -> str:
    """Short text for the giveaway message, for example: 2x luck for @VIP, @Booster. Empty if no luck is set."""
    luck = int(gw.get("luck") or 1)
    roles = gw.get("luck_roles") or []
    if luck <= 1 or not roles:
        return ""
    return f"**Luck:** {luck}x for " + ", ".join(f"<@&{r}>" for r in roles)


async def pick_giveaway_winners(bot: commands.Bot, gw: dict, valid: List[str], count: int) -> List[str]:
    """Picks winners at random. Members with a luck role are `luck` times more likely to win (everyone else counts once)."""
    count = min(int(count), len(valid))
    if count <= 0:
        return []
    luck = int(gw.get("luck") or 1)
    luck_roles = {int(r) for r in (gw.get("luck_roles") or [])}
    if luck <= 1 or not luck_roles:
        return random.sample(valid, count)
    guild = bot.get_guild(int(gw["guild_id"]))
    keyed = []
    for uid in valid:
        weight = 1
        member = await _resolve_member(guild, uid) if guild else None
        if member and any(role.id in luck_roles for role in member.roles):
            weight = luck
        # weighted random without repeats: a higher weight gives a better chance of a high key
        keyed.append((random.random() ** (1.0 / weight), uid))
    keyed.sort(reverse=True)
    return [uid for _, uid in keyed[:count]]


async def get_giveaway_message(bot: commands.Bot, gw: dict) -> Optional[discord.Message]:
    channel = bot.get_channel(int(gw["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(gw["channel_id"]))
        except Exception:
            return None
    try:
        return await channel.fetch_message(int(gw["message_id"]))
    except Exception:
        return None


async def announce_giveaway_winners(bot: commands.Bot, gw: dict, winners: List[str], rerolled: bool = False,
                                    message: Optional[discord.Message] = None) -> bool:
    """Posts the winner announcement in the giveaway channel. Works even if the giveaway message is gone."""
    channel = message.channel if message else bot.get_channel(int(gw["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(gw["channel_id"]))
        except Exception as e:
            print(f"[Giveaway Announce Error] Channel {gw['channel_id']} not found: {e}")
            return False

    jump = f"\n\n[Jump to giveaway]({message.jump_url})" if message else ""
    if winners:
        mentions = ", ".join(f"<@{w}>" for w in winners)
        if rerolled:
            content = f"\U0001F381 {mentions} {'is the new winner' if len(winners) == 1 else 'are the new winners'}!"
        else:
            content = f"\U0001F381 {mentions} {'is the winner' if len(winners) == 1 else 'are the winners'}!"

        # Heist style: prize as the title, then Mention / Username / User ID for each winner
        users = {}
        for w in winners:
            user = bot.get_user(int(w))
            if user is None:
                try:
                    user = await asyncio.wait_for(bot.fetch_user(int(w)), timeout=10)
                except Exception:
                    user = None
            users[w] = user

        blocks = []
        for n, w in enumerate(winners, start=1):
            head = "Winner" if len(winners) == 1 else f"Winner {n}"
            name = users[w].name if users[w] else "Unknown"
            blocks.append(f"**{head}**\n**Mention:** <@{w}>\n**Username:** {name}\n**User ID:** `{w}`")
        embed = discord.Embed(title=gw["prize"], description="\n".join(blocks) + jump, color=COLOR_GRAY)
        reward_text = giveaway_reward_text(gw)
        if reward_text:
            embed.add_field(name="Reward Given Automatically", value=f"`{reward_text}` each", inline=False)
        if len(winners) == 1 and users[winners[0]]:
            embed.set_thumbnail(url=users[winners[0]].display_avatar.url)
    else:
        content = None
        embed = embed_err("No Winners", f"There were no valid entries for **{gw['prize']}**.{jump}")

    allowed = discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False)
    if message:
        try:
            await message.reply(content=content, embed=embed, allowed_mentions=allowed)
            return True
        except Exception as e:
            print(f"[Giveaway Announce Reply Error] {e}. Trying a normal message.")
    try:
        await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        return True
    except Exception as e:
        print(f"[Giveaway Announce Error] Could not send in channel {gw['channel_id']}: {e}")
        GIVEAWAY_DEBUG["last_error"] = f"Could not post in channel {gw['channel_id']}: {e!r}"
        return False


def giveaway_reward_text(gw: dict) -> str:
    parts = []
    if int(gw.get("reward_points", 0) or 0) > 0:
        parts.append(f"{int(gw['reward_points']):,} points")
    if int(gw.get("reward_premium_days", 0) or 0) > 0:
        parts.append(f"{int(gw['reward_premium_days'])} days TE Premium")
    return " + ".join(parts)


def grant_giveaway_rewards(gw: dict, winners: List[str]) -> int:
    """Gives the automatic reward (points and/or TE Premium) to winners who have not got it yet."""
    if not giveaway_reward_text(gw):
        return 0
    rewarded = gw.setdefault("rewarded", [])
    given = 0
    for uid in winners:
        if uid in rewarded:
            continue
        try:
            if int(gw.get("reward_points", 0) or 0) > 0:
                add_user_points(str(uid), int(gw["reward_points"]))
            if int(gw.get("reward_premium_days", 0) or 0) > 0:
                extend_user_premium(str(uid), int(gw["reward_premium_days"]))
            rewarded.append(uid)
            given += 1
        except Exception as e:
            print(f"[Giveaway Reward Error] {uid}: {e!r}")
    save_giveaway(gw)
    return given


def revoke_giveaway_rewards(gw: dict, old_winners: List[str]):
    """Takes the automatic reward back from earlier winners (used when a giveaway is rerolled)."""
    rewarded = gw.setdefault("rewarded", [])
    for uid in old_winners:
        if uid not in rewarded:
            continue
        try:
            if int(gw.get("reward_points", 0) or 0) > 0:
                add_user_points(str(uid), -int(gw["reward_points"]))
            if int(gw.get("reward_premium_days", 0) or 0) > 0:
                reduce_user_premium(str(uid), int(gw["reward_premium_days"]))
            rewarded.remove(uid)
        except Exception as e:
            print(f"[Giveaway Reward Revoke Error] {uid}: {e!r}")


async def finish_giveaway(bot: commands.Bot, gw: dict, ended_early: bool = False) -> List[str]:
    if gw.get("ended"):
        return gw.get("winner_ids", [])

    mid = gw.get("message_id")
    GIVEAWAY_DEBUG["stage"] = f"{mid}: checking entries"
    try:
        valid = await asyncio.wait_for(valid_giveaway_entries(bot, gw), timeout=30)
    except asyncio.TimeoutError:
        print(f"[Giveaway Warning] {mid}: member check timed out. Using all entries.")
        valid = list(gw["entries"])
    winners = await pick_giveaway_winners(bot, gw, valid, int(gw["winners"])) if valid else []

    gw["ended"] = True
    gw["winner_ids"] = winners
    if ended_early:
        gw["end_ts"] = datetime.now(timezone.utc).timestamp()
    save_giveaway(gw)
    grant_giveaway_rewards(gw, winners)

    print(f"[Giveaway] Ended {mid} ({gw['prize']}): {len(gw['entries'])} entries, winners: {winners}")
    GIVEAWAY_DEBUG["stage"] = f"{mid}: finding message"
    try:
        message = await asyncio.wait_for(get_giveaway_message(bot, gw), timeout=20)
    except asyncio.TimeoutError:
        message = None
    if message is None:
        print(f"[Giveaway Warning] Message {mid} not found in channel {gw['channel_id']}. Announcing in the channel only.")
    else:
        GIVEAWAY_DEBUG["stage"] = f"{mid}: editing message"
        try:
            await asyncio.wait_for(message.edit(**giveaway_payload(gw, message)), timeout=20)
        except Exception as e:
            print(f"[Giveaway Edit Error] {mid}: {e!r}")
            GIVEAWAY_DEBUG["last_error"] = f"Edit failed: {e!r}"
    GIVEAWAY_DEBUG["stage"] = f"{mid}: posting winners"
    try:
        await asyncio.wait_for(announce_and_record(bot, gw, winners, message=message), timeout=40)
    except asyncio.TimeoutError:
        GIVEAWAY_DEBUG["last_error"] = f"Posting winners timed out for {mid}"
        gw["announced"] = False
    return winners


async def announce_and_record(bot: commands.Bot, gw: dict, winners: List[str], rerolled: bool = False,
                              message: Optional[discord.Message] = None) -> bool:
    """Posts the winner message and remembers if it worked. If it fails, the host gets a DM with the reason."""
    try:
        ok = await announce_giveaway_winners(bot, gw, winners, rerolled=rerolled, message=message)
    except Exception as e:
        print(f"[Giveaway Announce Crash] {e!r}")
        GIVEAWAY_DEBUG["last_error"] = f"Announce crashed: {e!r}"
        ok = False
    gw["announced"] = bool(ok)
    gw["last_try"] = datetime.now(timezone.utc).timestamp()
    gw["announce_tries"] = int(gw.get("announce_tries", 0)) + 1
    save_giveaway(gw)
    if not ok and gw["announce_tries"] == 1:
        try:
            host = await bot.fetch_user(int(gw["host_id"]))
            names = ", ".join(f"<@{w}>" for w in winners) or "nobody"
            await host.send(embed=embed_err(
                "Could Not Post Giveaway Winners",
                f"I could not post the winners for **{gw['prize']}** in <#{gw['channel_id']}>.\n"
                f"Winners: {names}\n"
                "Check that I can `View Channel`, `Send Messages` and `Embed Links` there. I will try again in a minute."
            ))
        except Exception as e:
            print(f"[Giveaway Host DM Error] {e!r}")
    return ok


async def reroll_giveaway(bot: commands.Bot, gw: dict, count: Optional[int] = None) -> Optional[List[str]]:
    """Picks new winners (previous winners are skipped). Returns None if nobody else can win."""
    # Everyone who already won (any earlier draw) can never be picked again
    past = list(dict.fromkeys(list(gw.get("past_winners", [])) + list(gw.get("winner_ids", []))))
    valid = await valid_giveaway_entries(bot, gw, exclude=tuple(past))
    if not valid:
        return None
    pick = int(count or gw["winners"])
    winners = await pick_giveaway_winners(bot, gw, valid, pick)
    gw["past_winners"] = past
    revoke_giveaway_rewards(gw, list(gw.get("winner_ids", [])))  # first winners lose the reward
    gw["winner_ids"] = winners
    save_giveaway(gw)
    grant_giveaway_rewards(gw, winners)

    message = await get_giveaway_message(bot, gw)
    if message:
        try:
            await message.edit(**giveaway_payload(gw, message))
        except Exception as e:
            print(f"[Giveaway Edit Error] {e}")
    await announce_and_record(bot, gw, winners, rerolled=True, message=message)
    return winners


# -------------------------------------------------------------
# BUILD SAFETY CHECK
# A build deletes every channel and role in the target server, so only the
# server owner (or an Administrator of that server) may start one.
# The bot owner is always allowed.
# -------------------------------------------------------------
BUILD_ALLOW_ADMINS = True  # set to False to allow the server owner only


async def fetch_target_guild(bot, guild_id: int) -> Optional[discord.Guild]:
    if not guild_id:
        return None
    guild = bot.get_guild(guild_id)
    if guild:
        return guild
    try:
        return await bot.fetch_guild(guild_id)
    except Exception:
        return None


async def can_build_in_guild(guild: discord.Guild, user_id) -> Tuple[bool, str]:
    """Returns (allowed, reason). The reason is shown to the user when not allowed."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, "The submitting user could not be identified."

    if uid == BOT_OWNER_ID or guild.owner_id == uid:
        return True, ""

    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except Exception:
            member = None
    if member is None:
        print(f"[Build Blocked] User {uid} is not a member of {guild.name} ({guild.id}).")
        return False, "You are not a member of the target server."

    if BUILD_ALLOW_ADMINS and member.guild_permissions.administrator:
        return True, ""

    print(f"[Build Blocked] User {uid} is not the owner or an administrator of {guild.name} ({guild.id}).")
    who = "the server owner or an Administrator" if BUILD_ALLOW_ADMINS else "the server owner"
    return False, f"Only {who} of that server can build a layout there. Ask them to submit the design."


def build_denied_embed(guild_name: str, reason: str) -> discord.Embed:
    embed = embed_err("Build Not Allowed", f"{reason}\n\nTarget server: **{guild_name}**")
    embed.set_footer(text="Nothing was changed in the server.")
    return embed


# -------------------------------------------------------------
# PRICES (shared with the website through MongoDB: settings / "prices")
# -------------------------------------------------------------
DEFAULT_PRICES = {"subscription_robux": 299, "points_cost": 100, "dev_product_robux": None}
# =====================================================================
# WEBSITE THEMES (/theme). The website reads the same database value.
# =====================================================================
SITE_THEMES = {
    "blueprint": "Blueprint (default)",
    "winter": "Winter",
    "halloween": "Halloween",
    "christmas": "Christmas",
    "spring": "Spring",
    "summer": "Summer",
    "stpatrick": "St. Patrick's Day",
}
LOCAL_SITE_THEME = {"theme": "blueprint"}


def get_site_theme() -> str:
    theme = LOCAL_SITE_THEME["theme"]
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "website_theme"}) or {}
            if doc.get("theme") in SITE_THEMES:
                theme = doc["theme"]
        except Exception as e:
            print(f"[Theme Read Error] {e}")
    return theme


def set_site_theme(theme: str) -> bool:
    """Saves the website theme. Returns False if it could not be saved to the database."""
    if theme not in SITE_THEMES:
        return False
    LOCAL_SITE_THEME["theme"] = theme
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": "website_theme"}, {"$set": {"theme": theme}}, upsert=True)
            return True
        except Exception as e:
            print(f"[Theme Save Error] {e}")
            return False
    return False


# =====================================================================
# SERVER BOOST REWARD: TE Premium while someone boosts the server.
# 30 days at a time, renewed while the boost lasts. If the boost ends (or is refunded),
# the unused premium days are taken back, so boosting and cancelling gives nothing.
# =====================================================================
BOOST_GUILD_ID = int(os.getenv("BOOST_GUILD_ID", "1284513257574629458"))
BOOST_BLOCK_DAYS = 30
LOCAL_BOOST_REWARDS: Dict[str, float] = {}  # user id -> time (ts) until which we already gave premium
BOOST_SYNC = {"last": 0.0}


def load_boost_rewards() -> Dict[str, float]:
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "boost_rewards"}) or {}
            return {str(k): float(v) for k, v in (doc.get("users") or {}).items()}
        except Exception as e:
            print(f"[Boost Read Error] {e}")
    return dict(LOCAL_BOOST_REWARDS)


def save_boost_rewards(rewards: Dict[str, float]):
    LOCAL_BOOST_REWARDS.clear()
    LOCAL_BOOST_REWARDS.update(rewards)
    if db_settings is not None:
        try:
            db_settings.replace_one({"_id": "boost_rewards"}, {"_id": "boost_rewards", "users": rewards}, upsert=True)
        except Exception as e:
            print(f"[Boost Save Error] {e}")


async def sync_boost_rewards(bot: commands.Bot) -> int:
    """Gives, renews or takes back boost premium for the whole server. Returns how many users changed."""
    guild = bot.get_guild(BOOST_GUILD_ID)
    if guild is None:
        return 0
    # If the member list is not fully loaded yet, do nothing (otherwise boosters could lose premium by mistake).
    if not guild.chunked or len(guild.members) < 0.9 * (guild.member_count or 0):
        return 0
    now = datetime.now(timezone.utc).timestamp()
    boosters = {str(m.id) for m in guild.members if m.premium_since and not m.bot}
    rewards = load_boost_rewards()
    changed = 0
    notices = []  # (user id, text)

    for uid in boosters:
        until = rewards.get(uid)
        if until is None:
            extend_user_premium(uid, BOOST_BLOCK_DAYS)
            rewards[uid] = now + BOOST_BLOCK_DAYS * 86400
            notices.append((uid, f"Thank you for boosting **{guild.name}**! You got **{BOOST_BLOCK_DAYS} days of TE Premium**. It renews while your boost is active."))
            changed += 1
        elif until - now < 2 * 86400:
            extend_user_premium(uid, BOOST_BLOCK_DAYS)
            rewards[uid] = until + BOOST_BLOCK_DAYS * 86400
            changed += 1

    for uid in [u for u in rewards if u not in boosters]:
        unused_days = int(max(0.0, rewards[uid] - now) // 86400)
        if unused_days > 0:
            reduce_user_premium(uid, unused_days)
        rewards.pop(uid, None)
        notices.append((uid, f"Your boost for **{guild.name}** ended, so the TE Premium from boosting ended too."))
        changed += 1

    if changed:
        save_boost_rewards(rewards)
    for uid, text in notices:
        try:
            if not user_allows_dms(uid):
                continue
            user = await bot.fetch_user(int(uid))
            await user.send(embed=embed_info("Server Boost Reward", text))
        except Exception:
            pass
    return changed


# =====================================================================
# PERSONAL SETTINGS (/settings): saved in the same "web_users" collection the website uses
# =====================================================================
DEFAULT_USER_SETTINGS = {"bot_dms": True, "inbox_notify": True, "profile_public": False, "theme_mode": "default", "blocked": []}
LOCAL_USER_SETTINGS: Dict[str, dict] = {}
THEME_MODES = ("default", "dark", "light")


def get_user_settings(user_id) -> dict:
    uid = str(user_id)
    doc = dict(LOCAL_USER_SETTINGS.get(uid, {}))
    if db_web_users is not None:
        try:
            doc = db_web_users.find_one({"_id": uid}) or {}
        except Exception as e:
            print(f"[Settings Read Error] {e}")
    result = dict(DEFAULT_USER_SETTINGS)
    for key in ("bot_dms", "inbox_notify", "profile_public"):
        if isinstance(doc.get(key), bool):
            result[key] = doc[key]
    if doc.get("theme_mode") in THEME_MODES:
        result["theme_mode"] = doc["theme_mode"]
    result["blocked"] = [str(b) for b in (doc.get("blocked") or [])]
    return result


def set_user_settings(user_id, **fields) -> bool:
    uid = str(user_id)
    LOCAL_USER_SETTINGS.setdefault(uid, {}).update(fields)
    if db_web_users is not None:
        try:
            db_web_users.update_one({"_id": uid}, {"$set": fields}, upsert=True)
        except Exception as e:
            print(f"[Settings Save Error] {e}")
            return False
    return True


def user_allows_dms(user_id) -> bool:
    """False if the person turned off optional bot messages in /settings. Payment, moderation and reminder DMs ignore this."""
    return get_user_settings(user_id)["bot_dms"]


def remove_inbox_from(user_id, sender_ids) -> None:
    """Deletes designs that blocked people already sent to this user (website inbox)."""
    if db_web_inbox is not None and sender_ids:
        try:
            db_web_inbox.delete_many({"to": str(user_id), "from": {"$in": [str(x) for x in sender_ids]}})
        except Exception as e:
            print(f"[Inbox Cleanup Error] {e}")


# =====================================================================
# TE PREMIUM ROLE: everyone with TE Premium (any way) gets the role in the main server.
# This checks the premium list, so it covers the shop, promo codes, giveaways, boosts,
# /premium set, the Roblox subscription and anything else that saves premium.
# =====================================================================
PREMIUM_ROLE_GUILD_ID = int(os.getenv("PREMIUM_ROLE_GUILD_ID", "1284513257574629458"))
PREMIUM_ROLE_NAME = os.getenv("PREMIUM_ROLE_NAME", "TE Premium")
PREMIUM_ROLE_DEFAULT_ID = "1563842016985489420"  # the TE Premium role (PREMIUM_ROLE_ID overrides it)
PREMIUM_ROLE_SYNC = {"last": 0.0, "warned": False}


async def get_premium_role(guild: discord.Guild) -> Optional[discord.Role]:
    """Finds the TE Premium role (PREMIUM_ROLE_ID, or by name). Creates it if it does not exist."""
    role_id = os.getenv("PREMIUM_ROLE_ID", "1563842016985489420").strip()
    role = guild.get_role(int(role_id)) if role_id.isdigit() else None
    if role is None:
        role = discord.utils.find(lambda r: r.name.strip().lower() == PREMIUM_ROLE_NAME.lower(), guild.roles)
    if role is None:
        try:
            role = await guild.create_role(name=PREMIUM_ROLE_NAME, colour=discord.Colour(0xC9962B), hoist=True,
                                           reason="TE Premium role for premium members")
        except Exception as e:
            print(f"[Premium Role] Could not create the role: {e!r}")
            return None
    if role >= guild.me.top_role and not PREMIUM_ROLE_SYNC["warned"]:
        PREMIUM_ROLE_SYNC["warned"] = True
        print(f"[Premium Role] Move my bot role above '{role.name}' in Server Settings > Roles, or I can not give it.")
    return role


async def apply_premium_role(member: discord.Member, role: discord.Role, should_have: bool) -> bool:
    """Adds or removes the role if needed. Returns True if something changed."""
    has = role in member.roles
    try:
        if should_have and not has:
            await member.add_roles(role, reason="TE Premium active")
            return True
        if not should_have and has:
            await member.remove_roles(role, reason="TE Premium ended")
            return True
    except Exception as e:
        print(f"[Premium Role] {member} ({member.id}): {e!r}")
    return False


async def sync_premium_roles(bot: commands.Bot, only_users: Optional[set] = None) -> int:
    """Makes the role match the premium list. only_users = check just these people (fast path)."""
    guild = bot.get_guild(PREMIUM_ROLE_GUILD_ID)
    if guild is None:
        return 0
    role = await get_premium_role(guild)
    if role is None:
        return 0
    premium = set(list_premium_users())
    changed = 0
    if only_users is not None:
        for uid in only_users:
            member = guild.get_member(int(uid))
            if member and not member.bot and await apply_premium_role(member, role, uid in premium):
                changed += 1
                await asyncio.sleep(0.3)
        return changed
    # Full check. If the member list is not loaded yet, do nothing (nobody loses the role by mistake).
    if not guild.chunked or len(guild.members) < 0.9 * (guild.member_count or 0):
        return 0
    for member in list(guild.members):
        if member.bot:
            continue
        if await apply_premium_role(member, role, str(member.id) in premium):
            changed += 1
            await asyncio.sleep(0.3)
    return changed


# =====================================================================
# FREE MODE (/free): server builds need no payment while it is on.
# =====================================================================
LOCAL_FREE_MODE = {"on": False}


def is_free_mode() -> bool:
    on = LOCAL_FREE_MODE["on"]
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "free_mode"}) or {}
            on = bool(doc.get("on", False))
        except Exception as e:
            print(f"[Free Mode Read Error] {e}")
    return on


def set_free_mode(on: bool, by: str = "") -> bool:
    """Turns free mode on or off. Returns False if it could not be saved to the database."""
    LOCAL_FREE_MODE["on"] = bool(on)
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": "free_mode"}, {"$set": {"on": bool(on), "changed_by": str(by),
                                   "changed_at": datetime.now(timezone.utc).timestamp()}}, upsert=True)
            return True
        except Exception as e:
            print(f"[Free Mode Save Error] {e}")
            return False
    return False


# -------------------------------------------------------------
# FREE LIMITS: standard members get a small amount, TE Premium is unlimited.
# 0 means "no limit". Days and weeks follow Tokyo time (JST).
# -------------------------------------------------------------
DEFAULT_LIMITS = {"ai_per_day": 3, "builds_per_week": 1}
LOCAL_LIMITS: Dict[str, int] = {}
LOCAL_USAGE: Dict[str, dict] = {}
JST = timezone(timedelta(hours=9))


def get_limits() -> dict:
    limits = dict(DEFAULT_LIMITS)
    limits.update({k: v for k, v in LOCAL_LIMITS.items() if k in limits})
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "limits"}) or {}
            for key in limits:
                value = doc.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    limits[key] = value
        except Exception as e:
            print(f"[Limits Read Error] {e}")
    return limits


def set_limit(key: str, value: int) -> bool:
    if key not in DEFAULT_LIMITS:
        return False
    LOCAL_LIMITS[key] = int(value)
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": "limits"}, {"$set": {key: int(value)}}, upsert=True)
        except Exception as e:
            print(f"[Limits Save Error] {e}")
            return False
    return True


def build_week_info() -> tuple:
    """(week key, unix time when the next Monday 00:00 JST starts)"""
    now = datetime.now(JST)
    iso = now.isocalendar()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return f"{iso[0]}-W{iso[1]:02d}", int((monday + timedelta(days=7)).timestamp())


def get_builds_used(user_id: str) -> int:
    week, _ = build_week_info()
    doc = LOCAL_USAGE.get(str(user_id), {})
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": f"usage:{user_id}"}) or {}
        except Exception as e:
            print(f"[Usage Read Error] {e}")
    return int(doc.get("build_count", 0)) if doc.get("build_week") == week else 0


def check_build_limit(user_id: str) -> tuple:
    """(allowed, limit, used, reset_unix). TE Premium and a limit of 0 are never blocked."""
    limit = get_limits()["builds_per_week"]
    _, reset = build_week_info()
    if user_has_premium(user_id) or limit <= 0:
        return True, limit, 0, reset
    used = get_builds_used(user_id)
    return used < limit, limit, used, reset


def record_build(user_id: str):
    """Counts one build for a standard member (premium is not counted)."""
    if user_has_premium(user_id):
        return
    week, _ = build_week_info()
    used = get_builds_used(user_id) + 1
    LOCAL_USAGE[str(user_id)] = {"build_week": week, "build_count": used}
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": f"usage:{user_id}"}, {"$set": {"build_week": week, "build_count": used}}, upsert=True)
        except Exception as e:
            print(f"[Usage Save Error] {e}")


# -------------------------------------------------------------
# BUILD PROGRESS: the website reads this to show a live progress screen
# -------------------------------------------------------------
LOCAL_BUILD_STATUS: Dict[str, dict] = {}


def set_build_status(build_id, user_id, stage: str, detail: str = "", done: int = 0, total: int = 0):
    """stage: received, waiting_payment, building, done, failed. Never raises."""
    if not build_id:
        return
    doc = {"user_id": str(user_id), "stage": stage, "detail": detail[:300], "done": int(done), "total": int(total),
           "updated_at": datetime.now(timezone.utc).timestamp()}
    LOCAL_BUILD_STATUS[str(build_id)] = doc
    if len(LOCAL_BUILD_STATUS) > 300:
        LOCAL_BUILD_STATUS.pop(next(iter(LOCAL_BUILD_STATUS)))
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": f"build:{build_id}"}, {"$set": doc}, upsert=True)
        except Exception as e:
            print(f"[Build Status Error] {e}")


def build_limit_embed(limit: int, reset_unix: int) -> discord.Embed:
    embed = discord.Embed(
        title="Weekly Build Limit Reached",
        description=(
            f"Standard members can build `{limit}` server{'s' if limit != 1 else ''} per week.\n"
            f"Your limit comes back <t:{reset_unix}:R> (<t:{reset_unix}:f>).\n\n"
            "TE Premium members have no weekly limit."
        ),
        color=0xF43F5E
    )
    return embed


DESIGN_DONE: set = set()  # design messages already handled (so the DM is sent once)
LOCAL_PRICES: Dict[str, int] = {}


def get_prices() -> dict:
    """Current prices. dev_product_robux stays None until it is known (set by /change price)."""
    prices = dict(DEFAULT_PRICES)
    prices.update({k: v for k, v in LOCAL_PRICES.items() if k in prices})
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "prices"}) or {}
            for key in prices:
                value = doc.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    prices[key] = value
        except Exception as e:
            print(f"[Prices Read Error] {e}")
    return prices


def set_price(key: str, value: int) -> bool:
    """Saves one price. Returns False if it could not be saved to the database."""
    if key not in DEFAULT_PRICES:
        return False
    LOCAL_PRICES[key] = int(value)
    if db_settings is not None:
        try:
            db_settings.update_one({"_id": "prices"}, {"$set": {key: int(value)}}, upsert=True)
        except Exception as e:
            print(f"[Prices Save Error] {e}")
            return False
    return True


# -------------------------------------------------------------
# PROMO CODES
# A code looks like:
#   {"code", "points", "created_by", "created_at", "expires_at" (None = never),
#    "max_uses" (None = unlimited), "uses", "redeemed_by": [user ids]}
# -------------------------------------------------------------
PROMO_LOG_CHANNEL_ID = int(os.getenv("PROMO_LOG_CHANNEL_ID", "1567516050218811495"))
PROMO_CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")
LOCAL_PROMOS: Dict[str, dict] = {}


def normalize_promo_code(code) -> str:
    return re.sub(r"\s+", "", str(code or "")).upper()


def promo_is_expired(promo: dict, now: Optional[float] = None) -> bool:
    expires = promo.get("expires_at")
    if expires is None:
        return False
    return expires <= (now if now is not None else datetime.now(timezone.utc).timestamp())


def promo_is_used_up(promo: dict) -> bool:
    limit = promo.get("max_uses")
    return limit is not None and int(promo.get("uses", 0)) >= int(limit)


def promo_is_active(promo: dict) -> bool:
    return not promo_is_expired(promo) and not promo_is_used_up(promo)


def create_promo(code: str, points: int, created_by, expires_at: Optional[float] = None,
                 max_uses: Optional[int] = None, premium_days: int = 0) -> Optional[dict]:
    """Creates a code. Returns None if the code already exists."""
    code = normalize_promo_code(code)
    doc = {
        "code": code,
        "points": int(points),
        "premium_days": int(premium_days or 0),
        "created_by": str(created_by),
        "created_at": datetime.now(timezone.utc).timestamp(),
        "expires_at": expires_at,
        "max_uses": max_uses,
        "uses": 0,
        "redeemed_by": [],
    }
    if db_promos is not None:
        try:
            result = db_promos.update_one({"code": code}, {"$setOnInsert": doc}, upsert=True)
            return doc if result.upserted_id is not None else None
        except Exception as e:
            print(f"[Promo Create Error] {e}")
    if code in LOCAL_PROMOS:
        return None
    LOCAL_PROMOS[code] = doc
    return dict(doc)


def get_promo(code: str) -> Optional[dict]:
    code = normalize_promo_code(code)
    if db_promos is not None:
        try:
            return db_promos.find_one({"code": code}, {"_id": 0})
        except Exception as e:
            print(f"[Promo Fetch Error] {e}")
    promo = LOCAL_PROMOS.get(code)
    return dict(promo) if promo else None


def list_promos(active_only: bool = True) -> List[dict]:
    promos: List[dict] = []
    loaded = False
    if db_promos is not None:
        try:
            promos = list(db_promos.find({}, {"_id": 0}))
            loaded = True
        except Exception as e:
            print(f"[Promo List Error] {e}")
    if not loaded:
        promos = [dict(p) for p in LOCAL_PROMOS.values()]
    if active_only:
        promos = [p for p in promos if promo_is_active(p)]
    promos.sort(key=lambda p: p.get("created_at", 0), reverse=True)
    return promos


def _promo_failure_reason(promo: Optional[dict], uid: str) -> str:
    if not promo:
        return "not_found"
    if uid in promo.get("redeemed_by", []):
        return "already_redeemed"
    if promo_is_expired(promo):
        return "expired"
    if promo_is_used_up(promo):
        return "used_up"
    return "error"


def redeem_promo(code: str, user_id) -> Tuple[str, Optional[dict]]:
    """Claims a code for a user. Returns (status, promo).
    status: ok | not_found | already_redeemed | expired | used_up | error.
    The claim is one atomic step, so two people cannot take the last use at the same time."""
    code = normalize_promo_code(code)
    uid = str(user_id)
    now = datetime.now(timezone.utc).timestamp()

    if db_promos is not None:
        try:
            promo = db_promos.find_one_and_update(
                {
                    "code": code,
                    "redeemed_by": {"$ne": uid},
                    "$and": [
                        {"$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}]},
                        {"$or": [{"max_uses": None}, {"$expr": {"$lt": ["$uses", "$max_uses"]}}]},
                    ],
                },
                {"$inc": {"uses": 1}, "$push": {"redeemed_by": uid}},
                return_document=ReturnDocument.AFTER,
                projection={"_id": 0},
            )
            if promo:
                return "ok", promo
            current = db_promos.find_one({"code": code}, {"_id": 0})
            return _promo_failure_reason(current, uid), current
        except Exception as e:
            print(f"[Promo Redeem Error] {e}")

    promo = LOCAL_PROMOS.get(code)
    if promo is None or uid in promo["redeemed_by"] or promo_is_expired(promo, now) or promo_is_used_up(promo):
        return _promo_failure_reason(promo, uid), (dict(promo) if promo else None)
    promo["uses"] += 1
    promo["redeemed_by"].append(uid)
    return "ok", dict(promo)


def update_promo(code: str, updates: dict) -> Optional[dict]:
    """Edits points / expires_at / max_uses. Returns the updated code, or None if it does not exist."""
    code = normalize_promo_code(code)
    updates = {k: v for k, v in updates.items() if k in ("points", "premium_days", "expires_at", "max_uses")}
    if not updates:
        return get_promo(code)
    if db_promos is not None:
        try:
            return db_promos.find_one_and_update(
                {"code": code}, {"$set": updates}, return_document=ReturnDocument.AFTER, projection={"_id": 0}
            )
        except Exception as e:
            print(f"[Promo Update Error] {e}")
    promo = LOCAL_PROMOS.get(code)
    if not promo:
        return None
    promo.update(updates)
    return dict(promo)


def delete_promo(code: str) -> bool:
    code = normalize_promo_code(code)
    if db_promos is not None:
        try:
            return db_promos.delete_one({"code": code}).deleted_count > 0
        except Exception as e:
            print(f"[Promo Delete Error] {e}")
    return LOCAL_PROMOS.pop(code, None) is not None


async def send_promo_log(bot: commands.Bot, user, promo: dict, old_points: int, new_points: int, where: str,
                         premium_until: Optional[datetime] = None):
    """Posts every redeem to the promo log channel."""
    channel = bot.get_channel(PROMO_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(PROMO_LOG_CHANNEL_ID)
        except Exception as e:
            print(f"[Promo Log Error] Could not open log channel {PROMO_LOG_CHANNEL_ID}: {e}")
            return

    limit = promo.get("max_uses")
    uses = f"{promo.get('uses', 0)}/{limit}" if limit is not None else f"{promo.get('uses', 0)}/unlimited"
    expires = promo.get("expires_at")

    reward_parts = []
    if int(promo.get("points", 0)) > 0:
        reward_parts.append(f"+{int(promo['points']):,} pts")
    if int(promo.get("premium_days", 0)) > 0:
        reward_parts.append(f"{int(promo['premium_days'])} days TE Premium")
    items = [
        ("User", f"{user.mention}\n`{user}`\n`{user.id}`"),
        ("Code", f"`{promo['code']}`"),
        ("Reward", f"`{' + '.join(reward_parts) or 'Nothing'}`"),
    ]
    if premium_until is not None:
        items.append(("Premium Until", f"<t:{int(premium_until.timestamp())}:F>"))
    items += [
        ("Previous Balance", f"`{old_points:,} pts`"),
        ("New Balance", f"`{new_points:,} pts`"),
        ("Code Uses", f"`{uses}`"),
        ("Code Expires", f"<t:{int(expires)}:R>" if expires else "`Never`"),
        ("Redeemed In", f"`{where}`"),
    ]
    payload = log_payload("Promo Code Redeemed", COLOR_GREEN, items)
    try:
        await channel.send(**payload, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[Promo Log Error] {e}")


# -------------------------------------------------------------
# CHANNEL MIRROR: messages the owner posts in one channel are copied to every member's DM.
# Editing the message edits the DMs. Deleting the message deletes the DMs.
# -------------------------------------------------------------
BROADCAST_CHANNEL_ID = int(os.getenv("BROADCAST_CHANNEL_ID", "1567477641861144618"))
BROADCAST_AUTHOR_ID = BOT_OWNER_ID
BROADCAST_GUILD_ID = int(os.getenv("BROADCAST_GUILD_ID", "1284513257574629458"))  # only this server + saved users
BROADCAST_DELAY = 1.5  # seconds between DMs. Going faster risks Discord flagging the bot as spam.
BROADCASTS: Dict[str, dict] = {}  # source message id -> {"dms": {user_id: dm_message_id}, ...}


def broadcast_text(message: discord.Message) -> str:
    text = message.content or ""
    for att in message.attachments:
        text += f"\n{att.url}"
    return text.strip()[:2000]


def save_broadcast(source_id: str, rec: dict):
    if db_broadcasts is not None:
        try:
            db_broadcasts.replace_one(
                {"source_id": source_id},
                {"source_id": source_id, "dms": rec["dms"]},
                upsert=True
            )
        except Exception as e:
            print(f"[Broadcast Save Error] {e}")


def load_broadcast(source_id: str) -> Optional[dict]:
    rec = BROADCASTS.get(source_id)
    if rec is None and db_broadcasts is not None:
        try:
            doc = db_broadcasts.find_one({"source_id": source_id})
        except Exception as e:
            print(f"[Broadcast Load Error] {e}")
            doc = None
        if doc:
            rec = {"dms": dict(doc.get("dms", {})), "content": None, "content_of": {}, "deleted": False,
                   "lock": asyncio.Lock()}
            BROADCASTS[source_id] = rec
    return rec


def forget_broadcast(source_id: str):
    BROADCASTS.pop(source_id, None)
    if db_broadcasts is not None:
        try:
            db_broadcasts.delete_one({"source_id": source_id})
        except Exception as e:
            print(f"[Broadcast Forget Error] {e}")


# -------------------------------------------------------------
# PER-SERVER SETTINGS: join / leave messages and the message log channel
# -------------------------------------------------------------
GUILD_CONFIG: Dict[int, dict] = {}
DEFAULT_GREETINGS = {
    "join": "Welcome {user} to **{server}**! You are member **#{count}**. Read the rules and enjoy your stay.",
    "leave": "**{username}** has left **{server}**. We are now **{count}** members.",
}


def get_guild_config(guild_id: int) -> dict:
    gid = int(guild_id)
    if gid in GUILD_CONFIG:
        return GUILD_CONFIG[gid]
    cfg: dict = {}
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": f"guild:{gid}"})
            if doc:
                cfg = {k: v for k, v in doc.items() if k != "_id"}
        except Exception as e:
            print(f"[Mongo Guild Config Read Error] {e}")
    GUILD_CONFIG[gid] = cfg
    return cfg


def save_guild_config(guild_id: int) -> bool:
    gid = int(guild_id)
    cfg = GUILD_CONFIG.get(gid, {})
    if db_settings is not None:
        try:
            db_settings.replace_one({"_id": f"guild:{gid}"}, {"_id": f"guild:{gid}", **cfg}, upsert=True)
        except Exception as e:
            print(f"[Mongo Guild Config Save Error] {e}")
            return False
    return True


def render_greeting(template: str, member, guild) -> str:
    """Fills in {user} {username} {server} {count} {id} in a join/leave message."""
    return (template
            .replace("{user}", member.mention)
            .replace("{username}", member.name)
            .replace("{server}", guild.name)
            .replace("{count}", f"{guild.member_count:,}")
            .replace("{id}", str(member.id)))


def build_greeting_embed(kind: str, template: str, member, guild) -> discord.Embed:
    """Clean embed for a join or leave message."""
    text = render_greeting(template, member, guild)[:3500]
    if kind == "join":
        embed = discord.Embed(title="Welcome!", description=text, color=COLOR_GREEN)
        embed.add_field(name="Member", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="Members", value=f"`{guild.member_count:,}`", inline=True)
        embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    else:
        embed = discord.Embed(title="Goodbye", description=text, color=COLOR_RED)
        embed.add_field(name="Member", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="Members", value=f"`{guild.member_count:,}`", inline=True)
        joined = getattr(member, "joined_at", None)
        if joined:
            embed.add_field(name="Joined", value=f"<t:{int(joined.timestamp())}:R>", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


# -------------------------------------------------------------
# SERVER BACKUPS (/backup)
# -------------------------------------------------------------
BACKUP_LIMIT_PER_GUILD = 10
LOCAL_BACKUPS: Dict[str, dict] = {}
LOCAL_BACKUP_COUNTER = {"n": 0}
BACKUP_SAVED = [
    "Roles and their permissions",
    "Categories and channels (names, topics, settings, order)",
    "Channel and category permission overwrites (for roles)",
    "Server settings (name, verification, notifications, AFK, system channel)",
    "Bot settings (join/leave messages, message log)",
]
BACKUP_NOT_SAVED = [
    "Messages and files",
    "Members and the roles they have",
    "Emojis and stickers",
    "Server icon, banner, webhooks and integrations",
]
CHANNEL_KIND = {
    discord.ChannelType.text: "text",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.news: "news",
}


def normalize_backup_id(text: str) -> str:
    return (text or "").strip().upper()


def next_backup_id() -> str:
    number = None
    if db_counters is not None:
        try:
            doc = db_counters.find_one_and_update(
                {"_id": "backups"}, {"$inc": {"value": 1}}, upsert=True, return_document=ReturnDocument.AFTER
            )
            number = int(doc["value"])
        except Exception as e:
            print(f"[Backup Counter Error] {e}")
    if number is None:
        LOCAL_BACKUP_COUNTER["n"] += 1
        number = LOCAL_BACKUP_COUNTER["n"]
    return f"TE-BK-{number:05d}"


def backup_counts(backup: dict) -> Tuple[int, int, int]:
    """(channels, roles, categories)"""
    return len(backup.get("channels", [])), len(backup.get("roles", [])), len(backup.get("categories", []))


def _overwrites_data(channel) -> list:
    data = []
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role):
            allow, deny = ow.pair()
            data.append({"target_id": target.id, "allow": allow.value, "deny": deny.value})
    return data


def snapshot_guild(guild: discord.Guild, created_by) -> dict:
    roles = []
    for r in sorted(guild.roles, key=lambda r: r.position):
        if r.is_default() or r.managed:
            continue
        roles.append({
            "id": r.id, "name": r.name, "color": r.color.value, "hoist": r.hoist,
            "mentionable": r.mentionable, "permissions": r.permissions.value, "position": r.position,
        })
    categories = [
        {"id": c.id, "name": c.name, "position": c.position, "overwrites": _overwrites_data(c)}
        for c in sorted(guild.categories, key=lambda c: c.position)
    ]
    channels = []
    for ch in sorted((c for c in guild.channels if not isinstance(c, discord.CategoryChannel)), key=lambda c: c.position):
        kind = CHANNEL_KIND.get(ch.type)
        if kind is None:
            continue  # threads and unknown types are not saved
        item = {"id": ch.id, "name": ch.name, "kind": kind, "category_id": ch.category_id,
                "position": ch.position, "overwrites": _overwrites_data(ch)}
        if kind in ("text", "news", "forum"):
            item["topic"] = getattr(ch, "topic", None)
            item["nsfw"] = bool(getattr(ch, "nsfw", False))
            item["slowmode_delay"] = int(getattr(ch, "slowmode_delay", 0) or 0)
        if kind in ("voice", "stage"):
            item["bitrate"] = int(getattr(ch, "bitrate", 64000) or 64000)
            item["user_limit"] = int(getattr(ch, "user_limit", 0) or 0)
        channels.append(item)
    settings = {
        "name": guild.name,
        "verification_level": guild.verification_level.value,
        "default_notifications": guild.default_notifications.value,
        "explicit_content_filter": guild.explicit_content_filter.value,
        "afk_timeout": guild.afk_timeout,
        "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
        "system_channel_id": guild.system_channel.id if guild.system_channel else None,
    }
    return {
        "backup_id": next_backup_id(),
        "guild_id": guild.id,
        "guild_name": guild.name,
        "created_by": str(created_by),
        "created_at": datetime.now(timezone.utc).timestamp(),
        "everyone_permissions": guild.default_role.permissions.value,
        "roles": roles,
        "categories": categories,
        "channels": channels,
        "settings": settings,
        "bot_config": dict(get_guild_config(guild.id)),
    }


def save_backup(backup: dict) -> bool:
    LOCAL_BACKUPS[backup["backup_id"]] = backup
    if db_backups is not None:
        try:
            db_backups.replace_one({"backup_id": backup["backup_id"]}, backup, upsert=True)
        except Exception as e:
            print(f"[Backup Save Error] {e}")
            return False
    return True


def get_backup(backup_id: str) -> Optional[dict]:
    bid = normalize_backup_id(backup_id)
    if db_backups is not None:
        try:
            doc = db_backups.find_one({"backup_id": bid}, {"_id": 0})
            if doc:
                return doc
            return None
        except Exception as e:
            print(f"[Backup Read Error] {e}")
    return LOCAL_BACKUPS.get(bid)


def list_backups(guild_id: Optional[int] = None) -> List[dict]:
    items: List[dict] = []
    if db_backups is not None:
        try:
            query = {"guild_id": int(guild_id)} if guild_id is not None else {}
            items = list(db_backups.find(query, {"_id": 0}))
        except Exception as e:
            print(f"[Backup List Error] {e}")
            items = []
    else:
        items = [b for b in LOCAL_BACKUPS.values() if guild_id is None or b["guild_id"] == int(guild_id)]
    return sorted(items, key=lambda b: b.get("created_at", 0), reverse=True)


AUTO_BACKUPS_KEPT = 3


def make_auto_backup(guild: discord.Guild, user_id) -> dict:
    """Safety backup taken before a TE Premium build. Only the newest few are kept."""
    backup = snapshot_guild(guild, user_id)
    backup["auto_build"] = True
    save_backup(backup)
    autos = [b for b in list_backups(guild.id) if b.get("auto_build")]  # newest first
    for old in autos[AUTO_BACKUPS_KEPT:]:
        delete_backup(old["backup_id"])
    return backup


def latest_auto_backup(guild_id: int) -> Optional[dict]:
    for b in list_backups(guild_id):
        if b.get("auto_build"):
            return b
    return None


def delete_backup(backup_id: str) -> bool:
    bid = normalize_backup_id(backup_id)
    existed = LOCAL_BACKUPS.pop(bid, None) is not None
    if db_backups is not None:
        try:
            return db_backups.delete_one({"backup_id": bid}).deleted_count > 0
        except Exception as e:
            print(f"[Backup Delete Error] {e}")
    return existed


async def restore_guild(guild: discord.Guild, backup: dict) -> dict:
    """Rebuilds roles, categories, channels, settings and bot settings from a backup.
    The caller must clear the server first. Returns counts and a list of problems."""
    problems: List[str] = []
    made = {"roles": 0, "categories": 0, "channels": 0}
    role_map: Dict[int, discord.Role] = {int(backup["guild_id"]): guild.default_role}
    channel_map: Dict[int, Any] = {}
    me_perms = guild.me.guild_permissions.value

    async def pause():
        await asyncio.sleep(0.3)

    # @everyone permissions
    try:
        await guild.default_role.edit(permissions=discord.Permissions(backup.get("everyone_permissions", 0) & me_perms))
    except Exception as e:
        problems.append(f"@everyone permissions: {e}")

    # roles (highest first, so the order ends up the same as in the backup)
    for r in sorted(backup["roles"], key=lambda r: r["position"], reverse=True):
        kwargs = dict(name=r["name"], colour=discord.Colour(r["color"]), hoist=r["hoist"], mentionable=r["mentionable"],
                      reason="Template Engine backup restore")
        try:
            try:
                new_role = await guild.create_role(permissions=discord.Permissions(r["permissions"]), **kwargs)
            except discord.Forbidden:
                new_role = await guild.create_role(permissions=discord.Permissions(r["permissions"] & me_perms), **kwargs)
            role_map[int(r["id"])] = new_role
            made["roles"] += 1
        except Exception as e:
            problems.append(f"Role {r['name']}: {e}")
        await pause()

    def build_overwrites(items: list) -> dict:
        result = {}
        for o in items:
            role = role_map.get(int(o["target_id"]))
            if role is not None:
                result[role] = discord.PermissionOverwrite.from_pair(discord.Permissions(o["allow"]), discord.Permissions(o["deny"]))
        return result

    async def create_with_fallback(factory, name: str, items: list):
        try:
            return await factory(overwrites=build_overwrites(items))
        except discord.Forbidden:
            problems.append(f"{name}: permissions could not be copied")
            return await factory()

    # categories
    for c in sorted(backup["categories"], key=lambda c: c["position"]):
        try:
            cat = await create_with_fallback(
                lambda **kw: guild.create_category(c["name"], reason="Template Engine backup restore", **kw), f"Category {c['name']}", c["overwrites"])
            channel_map[int(c["id"])] = cat
            made["categories"] += 1
        except Exception as e:
            problems.append(f"Category {c['name']}: {e}")
        await pause()

    # channels
    for ch in sorted(backup["channels"], key=lambda c: c["position"]):
        category = channel_map.get(int(ch["category_id"])) if ch.get("category_id") else None
        kind = ch["kind"]
        common = {"category": category, "reason": "Template Engine backup restore"}
        try:
            if kind in ("text", "news"):
                def factory(**kw):
                    return guild.create_text_channel(ch["name"], topic=ch.get("topic"), nsfw=ch.get("nsfw", False),
                                                     slowmode_delay=ch.get("slowmode_delay", 0), **common, **kw)
            elif kind == "forum":
                def factory(**kw):
                    return guild.create_forum(ch["name"], topic=ch.get("topic") or "", nsfw=ch.get("nsfw", False),
                                              slowmode_delay=ch.get("slowmode_delay", 0), **common, **kw)
            elif kind == "voice":
                def factory(**kw):
                    return guild.create_voice_channel(ch["name"], bitrate=min(ch.get("bitrate", 64000), guild.bitrate_limit),
                                                      user_limit=ch.get("user_limit", 0), **common, **kw)
            else:
                def factory(**kw):
                    return guild.create_stage_channel(ch["name"], **common, **kw)
            new_ch = await create_with_fallback(factory, f"Channel {ch['name']}", ch["overwrites"])
            channel_map[int(ch["id"])] = new_ch
            made["channels"] += 1
            if kind == "news":
                problems.append(f"{ch['name']}: restored as a normal text channel (announcement channels need Community)")
        except Exception as e:
            problems.append(f"Channel {ch['name']}: {e}")
        await pause()

    # server settings
    st = backup.get("settings", {})
    try:
        await guild.edit(
            name=st.get("name", guild.name),
            verification_level=discord.VerificationLevel(st.get("verification_level", 0)),
            default_notifications=discord.NotificationLevel(st.get("default_notifications", 0)),
            explicit_content_filter=discord.ContentFilter(st.get("explicit_content_filter", 0)),
            afk_timeout=st.get("afk_timeout", 300),
            afk_channel=channel_map.get(int(st["afk_channel_id"])) if st.get("afk_channel_id") else None,
            system_channel=channel_map.get(int(st["system_channel_id"])) if st.get("system_channel_id") else None,
            reason="Template Engine backup restore",
        )
    except Exception as e:
        problems.append(f"Server settings: {e}")

    # bot settings (channel IDs are mapped to the new channels)
    try:
        cfg = get_guild_config(guild.id)
        cfg.clear()
        for key, value in (backup.get("bot_config") or {}).items():
            if key == "message_log":
                new = channel_map.get(int(value))
                if new:
                    cfg["message_log"] = new.id
            elif key in ("join", "leave") and isinstance(value, dict):
                entry = dict(value)
                new = channel_map.get(int(entry.get("channel_id", 0)))
                if new:
                    entry["channel_id"] = new.id
                    cfg[key] = entry
        save_guild_config(guild.id)
    except Exception as e:
        problems.append(f"Bot settings: {e}")

    return {"made": made, "problems": problems}


# -------------------------------------------------------------
# POINTS SHOP (/shop to buy, /sell to add items)
# -------------------------------------------------------------
SHOP_LOG_CHANNEL_ID = int(os.getenv("SHOP_LOG_CHANNEL_ID", "1568091563123417149"))
LOCAL_SHOP: Dict[str, dict] = {}
SHOP_LOCKS: Dict[int, asyncio.Lock] = {}


def shop_key(name: str) -> str:
    return " ".join((name or "").lower().split())


def list_shop_items() -> List[dict]:
    items: List[dict] = list(LOCAL_SHOP.values())
    if db_shop is not None:
        try:
            items = list(db_shop.find({}, {"_id": 0}))
        except Exception as e:
            print(f"[Shop List Error] {e}")
    return sorted(items, key=lambda i: (i.get("price", 0), i.get("name", "").lower()))


def get_shop_item(name: str) -> Optional[dict]:
    key = shop_key(name)
    if db_shop is not None:
        try:
            return db_shop.find_one({"key": key}, {"_id": 0})
        except Exception as e:
            print(f"[Shop Read Error] {e}")
    return LOCAL_SHOP.get(key)


def add_shop_item(item: dict) -> bool:
    """False if an item with this name already exists (or the save failed)."""
    key = item["key"]
    if get_shop_item(key) is not None:
        return False
    LOCAL_SHOP[key] = item
    if db_shop is not None:
        try:
            db_shop.insert_one(dict(item))
        except Exception as e:
            print(f"[Shop Save Error] {e}")
            LOCAL_SHOP.pop(key, None)
            return False
    return True


def remove_shop_item(name: str) -> bool:
    key = shop_key(name)
    existed = LOCAL_SHOP.pop(key, None) is not None
    if db_shop is not None:
        try:
            return db_shop.delete_one({"key": key}).deleted_count > 0
        except Exception as e:
            print(f"[Shop Delete Error] {e}")
    return existed


def take_stock(key: str) -> bool:
    """Takes one item from the stock. Unlimited items always work. False if sold out."""
    item = get_shop_item(key)
    if item is None:
        return False
    if item.get("stock") is None:
        return True
    if db_shop is not None:
        try:
            doc = db_shop.find_one_and_update({"key": key, "stock": {"$gt": 0}}, {"$inc": {"stock": -1, "sold": 1}})
            return doc is not None
        except Exception as e:
            print(f"[Shop Stock Error] {e}")
            return False
    local = LOCAL_SHOP.get(key)
    if local and local["stock"] > 0:
        local["stock"] -= 1
        local["sold"] = local.get("sold", 0) + 1
        return True
    return False


def return_stock(key: str):
    """Gives one item back (used when a purchase fails after the stock was taken)."""
    item = get_shop_item(key)
    if item is None or item.get("stock") is None:
        return
    if db_shop is not None:
        try:
            db_shop.update_one({"key": key}, {"$inc": {"stock": 1, "sold": -1}})
        except Exception as e:
            print(f"[Shop Stock Return Error] {e}")
    elif key in LOCAL_SHOP:
        LOCAL_SHOP[key]["stock"] += 1
        LOCAL_SHOP[key]["sold"] = max(0, LOCAL_SHOP[key].get("sold", 1) - 1)


def count_shop_sale(key: str):
    """Counts a sale for unlimited items (limited items are counted in take_stock)."""
    item = get_shop_item(key)
    if item is None or item.get("stock") is not None:
        return
    if db_shop is not None:
        try:
            db_shop.update_one({"key": key}, {"$inc": {"sold": 1}})
        except Exception as e:
            print(f"[Shop Sold Count Error] {e}")
    elif key in LOCAL_SHOP:
        LOCAL_SHOP[key]["sold"] = LOCAL_SHOP[key].get("sold", 0) + 1


# -------------------------------------------------------------
# SERVER ANALYSIS (/analyze): summary + automatic checks
# -------------------------------------------------------------
def build_server_summary(guild: discord.Guild) -> Tuple[dict, List[str]]:
    """Returns (a compact summary of the server for the AI, a list of problems found by simple checks)."""
    everyone = guild.default_role
    findings: List[str] = []

    role_info = []
    for r in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if r.is_default():
            continue
        p = r.permissions
        role_info.append({
            "name": r.name, "members": len(r.members), "managed_by_bot": r.managed, "hoisted": r.hoist,
            "administrator": p.administrator, "mention_everyone": p.mention_everyone,
            "manage_server": p.manage_guild, "manage_roles": p.manage_roles, "manage_channels": p.manage_channels,
            "ban_members": p.ban_members, "kick_members": p.kick_members,
        })

    categories = []
    for cat in sorted(guild.categories, key=lambda c: c.position):
        categories.append({
            "name": cat.name,
            "private": cat.overwrites_for(everyone).view_channel is False,
            "channels": [{"name": c.name, "type": str(c.type).replace("ChannelType.", ""),
                          "has_topic": bool(getattr(c, "topic", None)),
                          "slowmode": getattr(c, "slowmode_delay", 0),
                          "read_only": c.overwrites_for(everyone).send_messages is False}
                         for c in sorted(cat.channels, key=lambda c: c.position)],
        })
    uncategorized = [{"name": c.name, "type": str(c.type).replace("ChannelType.", "")}
                     for c in guild.channels if not isinstance(c, discord.CategoryChannel) and c.category is None]

    bots = sum(1 for m in guild.members if m.bot)
    summary = {
        "name": guild.name,
        "members": guild.member_count,
        "bots": bots,
        "boost_level": guild.premium_tier,
        "verification_level": str(guild.verification_level),
        "content_filter": str(guild.explicit_content_filter),
        "default_notifications": str(guild.default_notifications),
        "features": list(guild.features)[:15],
        "role_count": len(role_info),
        "roles": role_info[:60],
        "categories": categories[:30],
        "uncategorized_channels": uncategorized[:40],
        "has_icon": guild.icon is not None,
        "has_banner": guild.banner is not None,
        "emoji_count": len(guild.emojis),
    }

    # ---- simple automatic checks
    ep = everyone.permissions
    if ep.administrator:
        findings.append("HIGH: @everyone has the Administrator permission. Anyone can control the server.")
    if ep.mention_everyone:
        findings.append("HIGH: @everyone can mention @everyone and @here (spam risk).")
    if ep.manage_channels or ep.manage_roles or ep.manage_guild or ep.ban_members or ep.kick_members:
        findings.append("HIGH: @everyone has moderation or management permissions.")
    admins = [r["name"] for r in role_info if r["administrator"] and not r["managed_by_bot"]]
    if len(admins) > 3:
        findings.append(f"MEDIUM: {len(admins)} normal roles have Administrator ({', '.join(admins[:6])}). Fewer is safer.")
    if str(guild.verification_level) in ("VerificationLevel.none", "none"):
        findings.append("MEDIUM: Verification level is None. Raids and spam accounts can join easily.")
    if uncategorized:
        findings.append(f"LOW: {len(uncategorized)} channels are not in any category.")
    empty = [c["name"] for c in categories if not c["channels"]]
    if empty:
        findings.append(f"LOW: Empty categories: {', '.join(empty[:6])}.")
    unused = [r["name"] for r in role_info if r["members"] == 0 and not r["managed_by_bot"]]
    if len(unused) > 3:
        findings.append(f"LOW: {len(unused)} roles have no members ({', '.join(unused[:6])}).")
    all_names = " ".join(c.name.lower() for c in guild.channels)
    if "rule" not in all_names:
        findings.append("MEDIUM: There is no rules channel.")
    if "welcome" not in all_names and "start" not in all_names and "intro" not in all_names:
        findings.append("LOW: There is no welcome or start-here channel.")
    if "log" not in all_names:
        findings.append("MEDIUM: There is no log channel for moderation.")
    if "announce" not in all_names and "news" not in all_names:
        findings.append("LOW: There is no announcements channel.")
    return summary, findings


# -------------------------------------------------------------
# DAILY REWARD WITH STREAK (/daily)
# -------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    DAILY_TZ = ZoneInfo(os.getenv("DAILY_TIMEZONE", "Asia/Tokyo"))
except Exception:
    DAILY_TZ = timezone(timedelta(hours=9))  # Tokyo time if the timezone data is missing

DAILY_BASE = 10          # reward on streak day 1
DAILY_STEP = 1           # reward goes up by this much each day
DAILY_MAX = 50           # a normal day never pays more than this
DAILY_MILESTONE_EVERY = 50
DAILY_MILESTONE_REWARD = 100  # every 50th streak day pays this instead
LOCAL_DAILY: Dict[str, dict] = {}
DAILY_LOCKS: Dict[int, asyncio.Lock] = {}


def daily_reward_for(streak: int) -> int:
    if streak > 0 and streak % DAILY_MILESTONE_EVERY == 0:
        return DAILY_MILESTONE_REWARD
    return min(DAILY_MAX, DAILY_BASE + DAILY_STEP * (streak - 1))


def daily_today():
    return datetime.now(DAILY_TZ).date()


def next_daily_reset_ts() -> int:
    tomorrow = datetime.combine(daily_today() + timedelta(days=1), datetime.min.time(), tzinfo=DAILY_TZ)
    return int(tomorrow.timestamp())


def get_daily_state(user_id: str) -> dict:
    uid = str(user_id)
    if db_users is not None:
        try:
            doc = db_users.find_one({"discord_id": uid}) or {}
            return {"streak": int(doc.get("daily_streak", 0)), "best": int(doc.get("daily_best", 0)),
                    "last": doc.get("daily_last")}
        except Exception as e:
            print(f"[Mongo Daily Read Error] {e}")
    return dict(LOCAL_DAILY.get(uid, {"streak": 0, "best": 0, "last": None}))


def save_daily_state(user_id: str, state: dict) -> bool:
    uid = str(user_id)
    LOCAL_DAILY[uid] = dict(state)
    if db_users is not None:
        try:
            db_users.update_one(
                {"discord_id": uid},
                {"$set": {"discord_id": uid, "daily_streak": state["streak"], "daily_best": state["best"], "daily_last": state["last"]}},
                upsert=True
            )
        except Exception as e:
            print(f"[Mongo Daily Save Error] {e}")
            return False
    return True


def claim_daily(user_id: str) -> dict:
    """Claims today's reward. The streak grows if yesterday was claimed, and resets to 1 after a missed day.
    Returns {"status": "ok"|"already", "streak", "best", "reward", "new_total"}."""
    state = get_daily_state(user_id)
    today = daily_today()
    last = None
    if state.get("last"):
        try:
            last = datetime.strptime(state["last"], "%Y-%m-%d").date()
        except ValueError:
            last = None

    if last == today:
        return {"status": "already", "streak": state["streak"], "best": state["best"],
                "reward": 0, "new_total": get_user_points(user_id)}

    streak = state["streak"] + 1 if last == today - timedelta(days=1) else 1  # missed a day = back to 1
    reward = daily_reward_for(streak)
    best = max(state.get("best", 0), streak)
    saved = save_daily_state(user_id, {"streak": streak, "best": best, "last": today.isoformat()})
    new_total = add_user_points(user_id, reward)
    return {"status": "ok", "streak": streak, "best": best, "reward": reward, "new_total": new_total, "saved": saved,
            "was_reset": bool(state["streak"] > 1 and streak == 1)}


# -------------------------------------------------------------
# TICKET SYSTEM (support, premium purchases and shop orders)
# -------------------------------------------------------------
TICKET_KINDS = {
    "support": "Support",
    "purchase": "Premium Purchase",
    "order": "Shop Order",
}
LOCAL_TICKETS: Dict[int, dict] = {}   # channel id -> ticket record
LOCAL_TICKET_COUNTERS: Dict[int, int] = {}


def ticket_cfg(guild_id: int) -> dict:
    return get_guild_config(guild_id).get("tickets") or {}


def next_ticket_number(guild_id: int) -> int:
    if db_counters is not None:
        try:
            doc = db_counters.find_one_and_update(
                {"_id": f"tickets:{guild_id}"}, {"$inc": {"value": 1}}, upsert=True, return_document=ReturnDocument.AFTER
            )
            return int(doc["value"])
        except Exception as e:
            print(f"[Ticket Counter Error] {e}")
    LOCAL_TICKET_COUNTERS[guild_id] = LOCAL_TICKET_COUNTERS.get(guild_id, 0) + 1
    return LOCAL_TICKET_COUNTERS[guild_id]


def save_ticket(ticket: dict):
    LOCAL_TICKETS[int(ticket["channel_id"])] = ticket
    if db_tickets is not None:
        try:
            db_tickets.replace_one({"channel_id": int(ticket["channel_id"])}, ticket, upsert=True)
        except Exception as e:
            print(f"[Ticket Save Error] {e}")


def get_ticket_by_channel(channel_id: int) -> Optional[dict]:
    if db_tickets is not None:
        try:
            doc = db_tickets.find_one({"channel_id": int(channel_id)}, {"_id": 0})
            return doc
        except Exception as e:
            print(f"[Ticket Read Error] {e}")
    return LOCAL_TICKETS.get(int(channel_id))


def find_open_ticket(guild_id: int, user_id: int, kind: str) -> Optional[dict]:
    query = {"guild_id": int(guild_id), "user_id": int(user_id), "kind": kind, "status": "open"}
    if db_tickets is not None:
        try:
            return db_tickets.find_one(query, {"_id": 0})
        except Exception as e:
            print(f"[Ticket Find Error] {e}")
    for t in LOCAL_TICKETS.values():
        if all(t.get(k) == v for k, v in query.items()):
            return t
    return None


def is_ticket_staff(member: discord.Member, cfg: dict) -> bool:
    if str(member.id) in COMMAND_ADMINS:
        return True
    perms = member.guild_permissions
    if perms.administrator or perms.manage_channels:
        return True
    role_id = cfg.get("staff_role_id")
    return bool(role_id and any(r.id == int(role_id) for r in member.roles))


async def create_ticket(guild: discord.Guild, user: discord.abc.User, kind: str, subject: str, details: str = "",
                        extra_fields: Optional[List[Tuple[str, str]]] = None) -> Tuple[Optional[discord.TextChannel], str]:
    """Opens a private ticket channel. Returns (channel, "") or (None / existing channel, reason)."""
    cfg = ticket_cfg(guild.id)
    category = guild.get_channel(int(cfg["category_id"])) if cfg.get("category_id") else None
    staff_role = guild.get_role(int(cfg["staff_role_id"])) if cfg.get("staff_role_id") else None
    if category is None or staff_role is None:
        return None, "Tickets are not set up in this server. An admin must run `/ticket setup`."

    if kind == "support":  # one open support ticket per person
        existing = find_open_ticket(guild.id, user.id, kind)
        if existing:
            channel = guild.get_channel(int(existing["channel_id"]))
            if channel is not None:
                return channel, "exists"

    number = next_ticket_number(guild.id)
    safe_name = re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-")[:20] or "user"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                attach_files=True, embed_links=True, manage_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              manage_channels=True, embed_links=True, attach_files=True),
    }
    try:
        channel = await guild.create_text_channel(
            f"{kind}-{number:04d}-{safe_name}", category=category, overwrites=overwrites,
            topic=f"Ticket #{number:04d} | {user} | {user.id}", reason=f"Ticket opened by {user}"
        )
    except Exception as e:
        print(f"[Ticket Create Error] {e!r}")
        return None, f"I could not create the ticket channel: `{str(e)[:200]}`"

    ticket = {"ticket_id": number, "guild_id": guild.id, "channel_id": channel.id, "user_id": user.id, "kind": kind,
              "subject": subject[:200], "created_at": datetime.now(timezone.utc).timestamp(), "status": "open"}
    save_ticket(ticket)

    embed = discord.Embed(title=f"Ticket #{number:04d} | {TICKET_KINDS.get(kind, kind)}", color=COLOR_PURPLE)
    embed.add_field(name="Opened By", value=f"{user.mention}\n`{user.id}`", inline=True)
    embed.add_field(name="Type", value=f"`{TICKET_KINDS.get(kind, kind)}`", inline=True)
    embed.add_field(name="Subject", value=f"`{subject[:200]}`", inline=False)
    if details:
        embed.add_field(name="Details", value=details[:1000], inline=False)
    for name, value in (extra_fields or []):
        embed.add_field(name=name, value=value, inline=True)
    embed.set_footer(text="Staff: press Close Ticket when this is done. A transcript is saved.")
    try:
        await channel.send(
            content=f"{user.mention} {staff_role.mention}", embed=embed, view=TicketCloseView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=[staff_role])
        )
    except Exception as e:
        print(f"[Ticket Welcome Error] {e!r}")
    return channel, ""


async def build_transcript(channel: discord.TextChannel) -> Tuple[str, int]:
    lines = []
    count = 0
    async for m in channel.history(limit=None, oldest_first=True):
        count += 1
        stamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        text = m.content or ""
        for e in m.embeds:
            bits = [x for x in (e.title, e.description) if x]
            text += (" " if text else "") + "[embed: " + " | ".join(bits)[:400] + "]"
            for f in e.fields:
                text += f"\n    {f.name}: {f.value}"
        for a in m.attachments:
            text += f"\n    [file] {a.filename}: {a.url}"
        lines.append(f"[{stamp}] {m.author} ({m.author.id}): {text}")
    return "\n".join(lines) or "(no messages)", count


async def close_ticket(bot: commands.Bot, channel: discord.TextChannel, closed_by: discord.abc.User) -> bool:
    """Saves a transcript to the log channel (and DMs it to the opener), then deletes the channel."""
    ticket = get_ticket_by_channel(channel.id)
    if ticket is None:
        return False
    guild = channel.guild
    cfg = ticket_cfg(guild.id)
    text, count = await build_transcript(channel)
    filename = f"transcript-{ticket['ticket_id']:04d}.txt"
    header = (f"Ticket #{ticket['ticket_id']:04d} | {TICKET_KINDS.get(ticket['kind'], ticket['kind'])}\n"
              f"Subject: {ticket['subject']}\nOpened by: {ticket['user_id']}\nClosed by: {closed_by} ({closed_by.id})\n"
              f"Messages: {count}\n{'-' * 40}\n")
    data = (header + text).encode("utf-8")
    duration = datetime.now(timezone.utc).timestamp() - ticket["created_at"]
    hours, rest = divmod(int(duration), 3600)

    log_items = [
        ("Opened By", f"<@{ticket['user_id']}>\n`{ticket['user_id']}`"),
        ("Closed By", f"{closed_by.mention}\n`{closed_by.id}`"),
        ("Type", f"`{TICKET_KINDS.get(ticket['kind'], ticket['kind'])}`"),
        ("Server", f"`{guild.name}`\n`{guild.id}`"),
        ("Subject", f"`{ticket['subject'][:200]}`"),
        ("Messages", f"`{count}`"),
        ("Open For", f"`{hours}h {rest // 60}m`"),
    ]
    embed = build_log_embed(f"Ticket #{ticket['ticket_id']:04d} Closed", COLOR_GRAY, log_items)  # used for the DM copy

    delivered = False
    log_channel = bot.get_channel(int(cfg["log_channel_id"])) if cfg.get("log_channel_id") else None  # can be in another server
    if log_channel is not None:
        try:
            await log_channel.send(**log_payload(f"Ticket #{ticket['ticket_id']:04d} Closed", COLOR_GRAY, log_items,
                                                 file=discord.File(io.BytesIO(data), filename=filename)),
                                   allowed_mentions=discord.AllowedMentions.none())
            delivered = True
        except Exception as e:
            print(f"[Ticket Transcript Error] {e!r}")
    try:  # the person who opened it also gets a copy
        opener = bot.get_user(int(ticket["user_id"])) or await bot.fetch_user(int(ticket["user_id"]))
        await opener.send(embed=embed, file=discord.File(io.BytesIO(data), filename=filename))
    except Exception:
        pass

    if not delivered:
        return False  # keep the channel so the transcript is not lost
    ticket["status"] = "closed"
    ticket["closed_by"] = closed_by.id
    ticket["closed_at"] = datetime.now(timezone.utc).timestamp()
    save_ticket(ticket)
    LOCAL_TICKETS.pop(channel.id, None)
    try:
        await channel.delete(reason=f"Ticket closed by {closed_by}")
    except Exception as e:
        print(f"[Ticket Delete Error] {e!r}")
    return True


class TicketOpenModal(discord.ui.Modal):
    subject = discord.ui.TextInput(label="Subject", max_length=100, placeholder="A short title")
    details = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, required=False, max_length=900,
                                   placeholder="Tell us what you need")

    def __init__(self, kind: str):
        super().__init__(title="Premium Purchase" if kind == "purchase" else "Open A Ticket")
        self.kind = kind
        if kind == "purchase":
            self.subject.label = "What do you want to buy?"
            self.details.label = "How do you want to pay? Any notes?"

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel, note = await create_ticket(interaction.guild, interaction.user, self.kind, self.subject.value, self.details.value)
        if channel is None:
            await interaction.followup.send(embed=embed_err("Could Not Open Ticket", note), ephemeral=True)
        elif note == "exists":
            await interaction.followup.send(embed=embed_info("You Already Have A Ticket", f"Please use {channel.mention}.", COLOR_GOLD), ephemeral=True)
        else:
            await interaction.followup.send(embed=embed_ok("Ticket Opened", f"Go to {channel.mention}. Staff will help you there."), ephemeral=True)


class TicketPanelView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await blocked_check(interaction)

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Support", style=discord.ButtonStyle.primary, custom_id="ticket_open_support")
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketOpenModal("support"))

    @discord.ui.button(label="Buy Premium", style=discord.ButtonStyle.success, custom_id="ticket_open_purchase")
    async def purchase_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketOpenModal("purchase"))


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = ticket_cfg(interaction.guild.id)
        if not is_ticket_staff(interaction.user, cfg):
            await interaction.response.send_message(embed=embed_err("Staff Only", "Only staff can close tickets."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_info("Closing Ticket", "Saving the transcript. This channel will be deleted in a moment.", COLOR_GOLD))
        ok = await close_ticket(interaction.client, interaction.channel, interaction.user)
        if not ok:
            try:
                await interaction.followup.send(embed=embed_err("Could Not Close", "I could not save the transcript, so the ticket stays open. Check the log channel and my permissions."))
            except Exception:
                pass


# -------------------------------------------------------------
# ROBLOX VERIFICATION MODAL & PERSISTENT VIEWS
# -------------------------------------------------------------
class RobloxVerifyModal(discord.ui.Modal, title="Roblox Account Verification"):
    username_input = discord.ui.TextInput(
        label="Exact Roblox Username",
        placeholder="e.g. Builderman",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        r_username = self.username_input.value.strip()

        # Retrieve stored verification code
        code_data = PENDING_VERIFICATION_CODES.get(user_id)
        if not code_data or datetime.now(timezone.utc).timestamp() > code_data.get("expires_at", 0):
            embed = discord.Embed(
                title="Code Expired or Missing",
                description="No active verification code found. Please click **Generate Code** first before verifying.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        expected_code = code_data.get("code")

        try:
            async with aiohttp.ClientSession() as session:
                # Resolve Roblox Username to User ID
                async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [r_username], "excludeBannedUsers": False}) as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Failed to query Roblox API. Please try again later.", ephemeral=True)
                        return
                    data = await resp.json()
                    user_list = data.get("data", [])
                    if not user_list:
                        await interaction.followup.send(f"Roblox user `{r_username}` was not found.", ephemeral=True)
                        return
                    
                    r_id = str(user_list[0].get("id"))
                    actual_name = user_list[0].get("name")

                # Fetch Roblox Bio / Description
                async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Failed to fetch user profile details from Roblox.", ephemeral=True)
                        return
                    profile_data = await resp.json()
                    description = profile_data.get("description", "")

            if expected_code not in description:
                embed = discord.Embed(
                    title="Verification Code Missing in Bio",
                    description=f"Could not find `{expected_code}` in the profile bio for **{actual_name}**.\n\nPlease update your Roblox bio, save it, and click Verify again.",
                    color=0xF43F5E
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            roblox_payload = {"id": r_id, "username": actual_name}
            if db_users is not None:
                try:
                    db_users.update_one(
                        {"discord_id": user_id},
                        {"$set": {
                            "discord_id": user_id,
                            "roblox_data": roblox_payload,
                            "updated_at": datetime.now(timezone.utc).isoformat()
                        }},
                        upsert=True
                    )
                except Exception as e:
                    print(f"[Mongo Save Roblox Link Error] {e}")

            PENDING_VERIFICATION_CODES.pop(user_id, None)

            embed = discord.Embed(
                title="Account Successfully Linked!",
                description=f"Your Discord account has been verified and linked to Roblox account **{actual_name}** (`{r_id}`).",
                color=0x2ECC71
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[Roblox Verification Error] {e}")
            await interaction.followup.send(f"Verification error: `{str(e)}`", ephemeral=True)


class RobloxVerificationView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await blocked_check(interaction)

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Code", style=discord.ButtonStyle.primary, custom_id="btn_verify_gen_code")
    async def gen_code_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        rand_num = random.randint(10000, 99999)
        code = f"TE-{rand_num}"
        exp_time = datetime.now(timezone.utc).timestamp() + (15 * 60)

        PENDING_VERIFICATION_CODES[user_id] = {
            "code": code,
            "expires_at": exp_time
        }

        embed = discord.Embed(
            title="Verification Code Generated",
            description=f"Your verification code is:\n\n`{code}`\n\n**Instructions:**\n1. Copy the code above.\n2. Paste it anywhere into your Roblox profile **About / Bio**.\n3. Save your Roblox profile.\n4. Click the **Verify Account** button below.",
            color=0x8B5CF6
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Verify Account", style=discord.ButtonStyle.success, custom_id="btn_verify_submit")
    async def verify_submit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RobloxVerifyModal())

    @discord.ui.button(label="Help", style=discord.ButtonStyle.secondary, custom_id="btn_verify_help")
    async def verify_help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Roblox Verification Guide",
            description="**How to link your account:**\n\n1. Click **Generate Code** to receive a code starting with `TE-`.\n2. Open your Roblox profile settings.\n3. Paste the code into your **About** (Bio) section and click Save.\n4. Return here and click **Verify Account**, then enter your exact Roblox username.\n\nOnce verified, your Roblox account will be connected to Template Engine.",
            color=0x8B5CF6
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RobloxUnsyncView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await blocked_check(interaction)

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Un-Sync Account", style=discord.ButtonStyle.danger, custom_id="btn_unsync_account")
    async def unsync_account_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)

        if db_users is not None:
            try:
                db_users.update_one(
                    {"discord_id": user_id},
                    {"$unset": {"roblox_data": ""}}
                )
            except Exception as e:
                print(f"[Mongo Unsync Error] {e}")

        embed = discord.Embed(
            title="Account Un-Synced",
            description="Your Roblox account link has been successfully removed.",
            color=0x2ECC71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# -------------------------------------------------------------
# INTERACTIVE UI FOR PREMIUM MISSING BOT RETRY
# -------------------------------------------------------------
class PremiumMissingBotView(discord.ui.View):
    def __init__(self, bot=None, submitting_user_id: str = "0", target_guild_id: str = "0", layout: dict = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.submitting_user_id = str(submitting_user_id)
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}

        invite_url = f"https://discord.com/api/oauth2/authorize?client_id={os.getenv('DISCORD_CLIENT_ID', '')}&permissions=8&scope=bot%20applications.commands"
        if self.target_guild_id and self.target_guild_id != "0":
            invite_url += f"&guild_id={self.target_guild_id}"
        self.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="Build", style=discord.ButtonStyle.success, custom_id="btn_premium_build_retry")
    async def retry_build_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        bot_inst = self.bot or interaction.client
        target_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0

        guild = bot_inst.get_guild(target_gid) if target_gid else None
        if not guild and target_gid:
            try:
                guild = await bot_inst.fetch_guild(target_gid)
            except Exception:
                guild = None

        if not guild:
            embed = discord.Embed(
                title="Re-Build Failed",
                description=f"Re-build failed because the bot is still missing from target server ID `{self.target_guild_id}`.\n\nPlease invite the bot using the link button above and try clicking **Build** again.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        allowed, reason = await can_build_in_guild(guild, interaction.user.id)
        if not allowed:
            await interaction.followup.send(embed=build_denied_embed(guild.name, reason), ephemeral=True)
            return

        embed = discord.Embed(
            title="Bot Detected! Deploying Layout...",
            description=f"Successfully joined **{guild.name}**!\nNow wiping existing channels/roles and deploying your blueprint...",
            color=0x2ECC71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        cog = bot_inst.get_cog("FeaturesCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)


# -------------------------------------------------------------
# INTERACTIVE UI FOR NON-PREMIUM VERIFICATION & POINTS PURCHASE
# -------------------------------------------------------------
class NonPremiumDeploymentView(discord.ui.View):
    def __init__(self, bot=None, submitting_user_id: str = "0", target_guild_id: str = "0", layout: dict = None, points_cost: int = 100, build_id: str = ""):
        super().__init__(timeout=None)
        self.bot = bot
        self.build_id = str(build_id or "")
        # The price shown in the DM is the price charged, even if /change price runs later
        self.points_cost = int(points_cost)
        self.use_points_btn.label = f"Use Points ({self.points_cost:,} Pts)"
        self.submitting_user_id = str(submitting_user_id)
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}

        premium_url = os.getenv("ROBLOX_STORE_URL", "https://www.roblox.com/games/6094950399/Template-Engine#!/store").strip()
        if premium_url:
            self.add_item(discord.ui.Button(label="Get TE Premium", url=premium_url, style=discord.ButtonStyle.link))
        invite_url = os.getenv("DISCORD_SERVER_INVITE", "").strip()
        if invite_url.startswith("http"):
            self.add_item(discord.ui.Button(label="Join Our Server", url=invite_url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="Use Points (100 Pts)", style=discord.ButtonStyle.primary, custom_id="btn_use_points_build")
    async def use_points_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)

        # Safety check first, so a blocked user never loses points
        early_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0
        early_guild = await fetch_target_guild(self.bot or interaction.client, early_gid)
        if not early_guild:
            await interaction.followup.send("Error: Bot is missing from target server.", ephemeral=True)
            return
        allowed, reason = await can_build_in_guild(early_guild, interaction.user.id)
        if not allowed:
            await interaction.followup.send(embed=build_denied_embed(early_guild.name, reason), ephemeral=True)
            return

        ok, limit, _used, reset_unix = check_build_limit(user_id)
        if not ok:
            await interaction.followup.send(embed=build_limit_embed(limit, reset_unix), ephemeral=True)
            return

        current_pts = get_user_points(user_id)

        cost = self.points_cost
        if current_pts < cost:
            embed = discord.Embed(
                title="Insufficient Points",
                description=f"You need at least {cost:,} points to deploy this layout.\n\nYour Balance: {current_pts:,} pts\nMissing: {cost - current_pts:,} pts",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        new_total = add_user_points(user_id, -cost)
        record_build(user_id)

        bot_inst = self.bot or interaction.client
        target_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0

        guild = bot_inst.get_guild(target_gid) if target_gid else None
        if not guild and target_gid:
            try:
                guild = await bot_inst.fetch_guild(target_gid)
            except Exception:
                guild = None

        if not guild:
            await interaction.followup.send("Error: Bot is missing from target server.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Points Redeemed! Deploying Layout...",
            description=f"Deducted {cost:,} points!\n\nUpdated Balance: {new_total:,} pts\nNow nuking and building {guild.name}...",
            color=0x8B5CF6
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        cog = bot_inst.get_cog("FeaturesCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout, "rebuild", self.build_id, self.submitting_user_id)




# =====================================================================
# BLACKLIST: blocked users and servers cannot use the bot or the website.
# The owner changes it in the website Owner Panel. The main staff are never blocked.
# =====================================================================
LOCAL_BLACKLIST: Dict[str, dict] = {"users": {}, "guilds": {}}
BLACKLIST_CACHE = {"at": 0.0, "data": {"users": {}, "guilds": {}}}


def get_blacklist() -> dict:
    now = time.time()
    if now - BLACKLIST_CACHE["at"] < 20:
        return BLACKLIST_CACHE["data"]
    data = {"users": dict(LOCAL_BLACKLIST["users"]), "guilds": dict(LOCAL_BLACKLIST["guilds"])}
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "blacklist"}) or {}
            data = {"users": dict(doc.get("users") or {}), "guilds": dict(doc.get("guilds") or {})}
        except Exception as e:
            print(f"[Blacklist Read Error] {e}")
    BLACKLIST_CACHE["at"], BLACKLIST_CACHE["data"] = now, data
    return data


def is_user_blacklisted(user_id) -> bool:
    uid = str(user_id)
    return uid not in COMMAND_ADMINS and uid in get_blacklist()["users"]


def is_guild_blacklisted(guild_id) -> bool:
    return str(guild_id) in get_blacklist()["guilds"]


def set_blacklist(kind: str, target_id, add: bool, reason: str = "", by: str = "") -> bool:
    key = "users" if kind == "user" else "guilds"
    tid = str(target_id)
    if add:
        entry = {"reason": reason[:200], "by": str(by), "at": datetime.now(timezone.utc).timestamp()}
        LOCAL_BLACKLIST[key][tid] = entry
    else:
        LOCAL_BLACKLIST[key].pop(tid, None)
    BLACKLIST_CACHE["at"] = 0.0
    if db_settings is not None:
        try:
            if add:
                db_settings.update_one({"_id": "blacklist"}, {"$set": {f"{key}.{tid}": entry}}, upsert=True)
            else:
                db_settings.update_one({"_id": "blacklist"}, {"$unset": {f"{key}.{tid}": ""}}, upsert=True)
        except Exception as e:
            print(f"[Blacklist Save Error] {e}")
            return False
    return True


# =====================================================================
# MAINTENANCE MODE: everyone except the staff sees "Back soon". The owner turns it on in the website Owner Panel.
# =====================================================================
MAINTENANCE_CACHE = {"at": 0.0, "data": {"on": False, "message": ""}}
LOCAL_MAINTENANCE = {"on": False, "message": ""}
DEFAULT_MAINTENANCE_TEXT = "Template Engine is being updated. Please try again in a little while."


def get_maintenance() -> dict:
    now = time.time()
    if now - MAINTENANCE_CACHE["at"] < 10:
        return MAINTENANCE_CACHE["data"]
    data = dict(LOCAL_MAINTENANCE)
    if db_settings is not None:
        try:
            doc = db_settings.find_one({"_id": "maintenance"}) or {}
            data = {"on": bool(doc.get("on")), "message": str(doc.get("message") or "")}
        except Exception as e:
            print(f"[Maintenance Read Error] {e}")
    MAINTENANCE_CACHE["at"], MAINTENANCE_CACHE["data"] = now, data
    return data


def set_maintenance(on: bool, message: str = "", by: str = "") -> bool:
    data = {"on": bool(on), "message": message[:300]}
    LOCAL_MAINTENANCE.update(data)
    MAINTENANCE_CACHE["at"] = 0.0
    if db_settings is None:
        return True
    try:
        db_settings.update_one({"_id": "maintenance"}, {"$set": {**data, "by": str(by),
                               "at": datetime.now(timezone.utc).timestamp()}}, upsert=True)
        return True
    except Exception as e:
        print(f"[Maintenance Save Error] {e}")
        return False


async def blocked_check(interaction: discord.Interaction) -> bool:
    """True = allowed. Used for every slash command and for the main buttons."""
    uid = str(interaction.user.id)
    if uid in COMMAND_ADMINS:
        return True
    why, title = None, "Access Blocked"
    maintenance = get_maintenance()
    if maintenance["on"]:
        why, title = maintenance["message"] or DEFAULT_MAINTENANCE_TEXT, "Back Soon"
    elif is_user_blacklisted(uid):
        why = "You are blocked from using Template Engine. If you think this is a mistake, contact the staff."
    elif interaction.guild is not None and is_guild_blacklisted(interaction.guild.id):
        why = "This server is blocked from using Template Engine."
    if why is None:
        return True
    try:
        embed = discord.Embed(title=title, description=why, color=COLOR_RED if title == "Access Blocked" else 0xF59E0B)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        print(f"[Blocked Reply Error] {e}")
    return False


# =====================================================================
# IMPORT A REAL SERVER: the website asks, the bot reads the server and gives back a layout.
# Only people with Administrator or Manage Server in that server can import it.
# =====================================================================
IMPORT_LIMITS = {"categories": 50, "channels": 300, "roles": 100}
IMPORT_CHANNEL_TYPES = {"text": "text", "voice": "voice", "news": "announcement", "forum": "forum", "stage_voice": "stage"}
IMPORT_ROLE_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_messages", "kick_members", "ban_members", "manage_channels")


def read_guild_layout(guild: discord.Guild) -> dict:
    """Turns a real server into the website's layout format (roles, categories, channels)."""
    everyone = guild.default_role
    roles = []
    truncated = False
    for r in sorted(guild.roles, key=lambda x: x.position, reverse=True):
        if r.is_default() or r.managed:
            continue
        if len(roles) >= IMPORT_LIMITS["roles"]:
            truncated = True
            break
        perms = {("manage_server" if k == "manage_guild" else k): True for k in IMPORT_ROLE_PERMS if getattr(r.permissions, k, False)}
        roles.append({"name": r.name[:100], "color": f"#{r.color.value:06x}" if r.color.value else "#99aab5",
                      "hoist": bool(r.hoist), "mentionable": bool(r.mentionable), "permissions": perms})

    def read_channel(ch):
        kind = IMPORT_CHANNEL_TYPES.get(ch.type.name)
        if kind is None:
            return None
        ow = ch.overwrites_for(everyone)
        perms = {}
        if ow.view_channel is False:
            perms["view"] = False
        if ow.send_messages is False and kind in ("text", "announcement", "forum"):
            perms["send"] = False
        return {"name": ch.name[:100], "type": kind, "topic": (getattr(ch, "topic", None) or "")[:1000], "emoji": "",
                "read_only": ow.send_messages is False and kind in ("text", "announcement"), "permissions": perms,
                "formatted_name": ch.name[:100]}

    categories, total = [], 0
    groups = [(c.name, c.channels) for c in guild.categories]
    loose = [c for c in guild.channels if getattr(c, "category", None) is None and not isinstance(c, discord.CategoryChannel)]
    if loose:
        groups.append(("NO CATEGORY", sorted(loose, key=lambda c: c.position)))
    for name, chans in groups:
        if len(categories) >= IMPORT_LIMITS["categories"]:
            truncated = True
            break
        items = []
        for ch in chans:
            if total >= IMPORT_LIMITS["channels"]:
                truncated = True
                break
            item = read_channel(ch)
            if item:
                items.append(item)
                total += 1
        categories.append({"name": name[:100], "emoji": "", "channels": items})
    return {"server_name": guild.name[:100], "description": (guild.description or "")[:300], "roles": roles,
            "categories": categories, "truncated": truncated}


async def process_import_requests(bot):
    if db_import_requests is None:
        return
    try:
        for _ in range(5):
            req = db_import_requests.find_one_and_update({"status": "pending"}, {"$set": {"status": "running"}},
                                                         sort=[("created_at", 1)], return_document=ReturnDocument.AFTER)
            if not req:
                break
            result = {"status": "error", "error": "Something went wrong."}
            try:
                guild = bot.get_guild(int(req["guild_id"]))
                if guild is None:
                    raise ValueError("The bot is not in that server. Add the bot first, then try again.")
                uid = int(req["user_id"])
                member = guild.get_member(uid)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid)
                    except Exception:
                        member = None
                if member is None:
                    raise ValueError("You are not a member of that server.")
                gp = member.guild_permissions
                if not (guild.owner_id == uid or gp.administrator or gp.manage_guild):
                    raise ValueError("You need the Administrator or Manage Server permission in that server.")
                result = {"status": "done", "layout": read_guild_layout(guild)}
            except ValueError as e:
                result = {"status": "error", "error": str(e)}
            except Exception as e:
                print(f"[Import Error] {e!r}")
            db_import_requests.update_one({"_id": req["_id"]}, {"$set": result})
        db_import_requests.delete_many({"created_at": {"$lt": datetime.now(timezone.utc).timestamp() - 3600}})
    except Exception as e:
        print(f"[Import Requests Error] {e!r}")


# =====================================================================
# OWNER PANEL (website): the website saves a "job" in the database and the bot runs it here.
# This way the website uses the exact same code as the slash commands.
# Only jobs made by an owner ID are run.
# =====================================================================
OWNER_IDS = {str(BOT_OWNER_ID)} | {x.strip() for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip().isdigit()}
OWNER_PUBLISH = {"last": 0.0}
VERIFY_PANEL_TEXT = "Link your Roblox account to Template Engine. Press **Generate Code**, paste the code in your Roblox bio, then press **Verify Account**."
UNSYNC_PANEL_TEXT = "Press the button below to remove the Roblox account that is linked to your Discord account."


def build_ticket_panel_embed() -> discord.Embed:
    panel = discord.Embed(
        title="Need Help?",
        description="Press a button below to open a private ticket. Our staff will answer you there.",
        color=COLOR_PURPLE
    )
    panel.add_field(name="Support", value="Questions or problems.", inline=True)
    panel.add_field(name="Buy Premium", value="Buy TE Premium with help from staff.", inline=True)
    return panel


def _job_id(params: dict, key: str) -> int:
    raw = str(params.get(key, "")).strip()
    if not (raw.isdigit() and 15 <= len(raw) <= 21):
        raise ValueError(f"`{key}` is not a valid Discord ID.")
    return int(raw)


def _job_int(params: dict, key: str, lo: int, hi: int) -> int:
    try:
        value = int(params.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"`{key}` must be a number.")
    if not lo <= value <= hi:
        raise ValueError(f"`{key}` must be from {lo:,} to {hi:,}.")
    return value


async def _job_text_channel(bot, params: dict, key: str = "channel_id"):
    cid = _job_id(params, key)
    ch = bot.get_channel(cid)
    if ch is None:
        try:
            ch = await bot.fetch_channel(cid)
        except Exception:
            ch = None
    if ch is None or not hasattr(ch, "send") or not getattr(ch, "guild", None):
        raise ValueError("I cannot see that channel. Check that the bot is in that server.")
    perms = ch.permissions_for(ch.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise ValueError(f"I need `View Channel`, `Send Messages` and `Embed Links` in #{ch.name}.")
    return ch


async def run_owner_job(bot, job: dict) -> str:
    """Runs one owner job. Raises ValueError with a clear message when the input is wrong."""
    t = str(job.get("type", ""))
    p = job.get("params") or {}
    by = str(job.get("by", ""))
    if by not in OWNER_IDS:
        raise ValueError("This job was not made by an owner.")

    if t == "panel":
        kind = p.get("kind")
        ch = await _job_text_channel(bot, p)
        text = str(p.get("description") or "").strip()[:2000]
        if kind == "verification":
            embed = discord.Embed(title="Roblox Account Verification", description=text or VERIFY_PANEL_TEXT, color=0x8B5CF6)
            embed.set_footer(text="Template Engine Roblox Synchronization System")
            view, label = RobloxVerificationView(), "Verification"
        elif kind == "unsync":
            embed = discord.Embed(title="Un-Sync Roblox Account", description=text or UNSYNC_PANEL_TEXT, color=0xF43F5E)
            embed.set_footer(text="Template Engine Account Management")
            view, label = RobloxUnsyncView(), "Un-Sync"
        elif kind == "ticket":
            embed, view, label = build_ticket_panel_embed(), TicketPanelView(), "Ticket"
        else:
            raise ValueError("Unknown panel type.")
        await ch.send(embed=embed, view=view)
        return f"Posted the {label} panel in #{ch.name} ({ch.guild.name})."

    if t == "ticket_setup":
        cat_id, role_id, log_id = _job_id(p, "category_id"), _job_id(p, "staff_role_id"), _job_id(p, "log_channel_id")
        category = bot.get_channel(cat_id)
        log_ch = bot.get_channel(log_id)
        if category is None or getattr(category, "guild", None) is None or not isinstance(category, discord.CategoryChannel):
            raise ValueError("Category not found.")
        guild = category.guild
        role = guild.get_role(role_id)
        if role is None or role.is_default() or role.managed:
            raise ValueError("Pick a normal staff role.")
        if log_ch is None or not hasattr(log_ch, "send"):
            raise ValueError("I cannot see the log channel. Check that the bot is in that server.")
        if not guild.me.guild_permissions.manage_channels:
            raise ValueError("I need `Manage Channels` to create ticket channels.")
        lp = log_ch.permissions_for(log_ch.guild.me)
        if not (lp.view_channel and lp.send_messages and lp.attach_files and lp.embed_links):
            raise ValueError(f"I need `View Channel`, `Send Messages`, `Embed Links` and `Attach Files` in #{log_ch.name}.")
        cfg = get_guild_config(guild.id)
        cfg["tickets"] = {"category_id": cat_id, "staff_role_id": role_id, "log_channel_id": log_id}
        save_guild_config(guild.id)
        msg = f"Tickets are ready in {guild.name}."
        if p.get("channel_id"):
            ch = await _job_text_channel(bot, p)
            await ch.send(embed=build_ticket_panel_embed(), view=TicketPanelView())
            msg += f" Ticket panel posted in #{ch.name}."
        return msg

    if t in ("greeting", "messagelog"):
        guild = bot.get_guild(_job_id(p, "guild_id"))
        if guild is None:
            raise ValueError("The bot is not in that server.")
        cfg = get_guild_config(guild.id)
        enabled = bool(p.get("enabled", True))
        if t == "messagelog":
            if not enabled:
                cfg.pop("message_log", None)
                save_guild_config(guild.id)
                return f"Message log is off in {guild.name}."
            ch = await _job_text_channel(bot, p)
            cfg["message_log"] = ch.id
            save_guild_config(guild.id)
            return f"Message log is on in {guild.name}: #{ch.name}."
        kind = p.get("kind")
        if kind not in ("join", "leave"):
            raise ValueError("Choose join or leave.")
        entry = dict(cfg.get(kind) or {})
        if p.get("channel_id"):
            ch = await _job_text_channel(bot, p)
            entry["channel_id"] = ch.id
        if not entry.get("channel_id"):
            raise ValueError("Choose a channel the first time you set this message.")
        msg_text = str(p.get("message") or "").strip()[:1500]
        if msg_text:
            entry["message"] = msg_text
        entry.setdefault("message", DEFAULT_GREETINGS[kind])
        entry["enabled"] = enabled
        cfg[kind] = entry
        save_guild_config(guild.id)
        return f"{kind.title()} message saved in {guild.name} ({'on' if enabled else 'off'})."

    if t == "promo_create":
        code = normalize_promo_code(p.get("code"))
        if not PROMO_CODE_RE.match(code):
            raise ValueError("Use 3 to 32 letters, numbers, `-` or `_`.")
        points = _job_int({"v": p.get("points") or 0}, "v", 0, 1000000)
        days = _job_int({"v": p.get("premium_days") or 0}, "v", 0, 3650)
        if points == 0 and days == 0:
            raise ValueError("Give points, premium days, or both.")
        expires_at = None
        if str(p.get("duration") or "").strip():
            delta = parse_duration_strict(str(p["duration"]))
            if delta is None:
                raise ValueError("Use a time like `12h`, `7d` or `2w`.")
            expires_at = datetime.now(timezone.utc).timestamp() + delta.total_seconds()
        max_uses = _job_int({"v": p["max_uses"]}, "v", 1, 1000000) if p.get("max_uses") else None
        if create_promo(code, points, by, expires_at, max_uses, days) is None:
            raise ValueError(f"`{code}` already exists.")
        return f"Code {code} created."

    if t == "promo_update":
        code = normalize_promo_code(p.get("code"))
        updates = {}
        if p.get("points") not in (None, ""):
            updates["points"] = _job_int(p, "points", 0, 1000000)
        if p.get("premium_days") not in (None, ""):
            updates["premium_days"] = _job_int(p, "premium_days", 0, 3650)
        if p.get("max_uses") not in (None, ""):
            updates["max_uses"] = _job_int(p, "max_uses", 1, 1000000)
        if str(p.get("duration") or "").strip():
            delta = parse_duration_strict(str(p["duration"]))
            if delta is None:
                raise ValueError("Use a time like `12h`, `7d` or `2w`.")
            updates["expires_at"] = datetime.now(timezone.utc).timestamp() + delta.total_seconds()
        if not updates:
            raise ValueError("Nothing to change.")
        if update_promo(code, updates) is None:
            raise ValueError("That code does not exist.")
        return f"Code {code} updated."

    if t == "promo_delete":
        code = normalize_promo_code(p.get("code"))
        if not delete_promo(code):
            raise ValueError("That code does not exist.")
        return f"Code {code} deleted."

    if t == "points":
        uid = str(_job_id(p, "user_id"))
        amount = _job_int(p, "amount", -1000000, 1000000)
        if amount == 0:
            raise ValueError("The amount cannot be 0.")
        new_total = add_user_points(uid, amount)
        return f"{'+' if amount > 0 else ''}{amount:,} points. New balance: {new_total:,}."

    if t == "premium":
        uid = str(_job_id(p, "user_id"))
        action = p.get("action")
        if action == "remove":
            if uid in HARDCODED_STAFF:
                raise ValueError("This person is permanent staff.")
            remove_user_premium(uid)
            return "TE Premium removed."
        if action == "grant":
            if p.get("permanent"):
                set_user_premium(uid, timedelta(days=36500))
                return "Permanent TE Premium granted."
            days = _job_int(p, "days", 1, 3650)
            set_user_premium(uid, timedelta(days=days))
            return f"TE Premium granted for {days} days."
        if action == "extend":
            days = _job_int(p, "days", 1, 3650)
            exp = extend_user_premium(uid, days)
            return f"Added {days} days. Ends {exp.strftime('%Y-%m-%d')}."
        if action == "reduce":
            days = _job_int(p, "days", 1, 3650)
            reduce_user_premium(uid, days)
            return f"Removed {days} days."
        raise ValueError("Unknown premium action.")

    if t == "staff":
        uid = _job_id(p, "user_id")
        info = get_staff_role()
        guild = bot.get_guild(info["guild_id"]) if info else None
        role = guild.get_role(info["role_id"]) if guild else None
        member = None
        if guild:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except Exception:
                    member = None
        if p.get("action") == "add":
            if is_staff(uid):
                raise ValueError("Already staff.")
            add_staff(uid)
            note = ""
            if role and member:
                try:
                    await member.add_roles(role, reason="Staff added from the website panel")
                    note = " Staff role given."
                except Exception as e:
                    note = f" Could not give the role: {e}"
            return "Staff added. They have TE Premium." + note
        if p.get("action") == "remove":
            if str(uid) in HARDCODED_STAFF:
                raise ValueError("This person is permanent staff.")
            if not remove_staff(uid):
                raise ValueError("Not on the staff list.")
            note = ""
            if role and member:
                try:
                    await member.remove_roles(role, reason="Staff removed from the website panel")
                except Exception:
                    pass
            return "Staff removed. Their TE Premium was removed too." + note
        raise ValueError("Unknown staff action.")

    if t == "free":
        on = bool(p.get("on"))
        if not set_free_mode(on, by):
            raise ValueError("Could not save to the database.")
        return f"Free mode is {'ON' if on else 'OFF'}."

    if t == "limit":
        key = str(p.get("key", ""))
        if key not in DEFAULT_LIMITS:
            raise ValueError("Unknown limit.")
        value = _job_int(p, "value", 0, 100000)
        set_limit(key, value)
        return f"{key} is now {value}" + (" (no limit)." if value == 0 else ".")

    if t == "theme":
        theme = str(p.get("theme", ""))
        if not set_site_theme(theme):
            raise ValueError("Unknown theme or the database could not be reached.")
        return f"Website theme is now {SITE_THEMES[theme]}."

    if t in ("blacklist_add", "blacklist_remove"):
        kind = p.get("kind")
        if kind not in ("user", "guild"):
            raise ValueError("Choose user or server.")
        target = str(_job_id(p, "id"))
        if t == "blacklist_remove":
            set_blacklist(kind, target, False)
            return f"Unblocked {kind} {target}."
        if kind == "user" and target in COMMAND_ADMINS:
            raise ValueError("The main staff cannot be blocked.")
        if not set_blacklist(kind, target, True, str(p.get("reason") or ""), by):
            raise ValueError("Could not save to the database.")
        note = ""
        if kind == "guild":
            guild = bot.get_guild(int(target))
            if guild is not None:
                try:
                    await guild.leave()
                    note = f" The bot left {guild.name}."
                except Exception as e:
                    note = f" Could not leave the server: {e}"
        return f"Blocked {kind} {target}." + note

    if t == "maintenance":
        on = bool(p.get("on"))
        if not set_maintenance(on, str(p.get("message") or "").strip(), by):
            raise ValueError("Could not save to the database.")
        return "Maintenance mode is ON. Only staff can use the bot and website." if on else "Maintenance mode is OFF. Everyone can use it again."

    if t == "invite":
        guild = bot.get_guild(_job_id(p, "guild_id"))
        if guild is None:
            raise ValueError("The bot is not in that server.")
        me = guild.me
        choices = [c for c in [guild.system_channel, guild.rules_channel] + list(guild.text_channels) if c is not None]
        channel = next((c for c in choices if c.permissions_for(me).create_instant_invite), None)
        if channel is None:
            raise ValueError(f"I need the `Create Invite` permission in at least one channel of {guild.name}.")
        invite = await channel.create_invite(max_age=3600, max_uses=0, unique=True, reason="Owner panel (website)")
        return f"Invite for {guild.name} (works for 1 hour): {invite.url}"

    if t == "sync_roles":
        changed = await sync_premium_roles(bot)
        return f"TE Premium roles checked ({changed} changes)."

    raise ValueError("Unknown job type.")


async def process_owner_jobs(bot):
    """Runs waiting owner jobs. Called every scheduler tick."""
    if db_owner_jobs is None:
        return
    now = datetime.now(timezone.utc).timestamp()
    try:
        if now - OWNER_PUBLISH.get("maint", 0.0) >= 60:
            OWNER_PUBLISH["maint"] = now
            # a job that was running when the bot stopped would wait forever
            db_owner_jobs.update_many({"status": "running", "started_at": {"$lt": now - 300}},
                                      {"$set": {"status": "error", "result": "The bot stopped while running this.", "finished_at": now}})
            db_owner_jobs.delete_many({"finished_at": {"$lt": now - 14 * 86400}})
        for _ in range(10):
            job = db_owner_jobs.find_one_and_update(
                {"status": "pending"}, {"$set": {"status": "running", "started_at": now}},
                sort=[("created_at", 1)], return_document=ReturnDocument.AFTER)
            if not job:
                break
            try:
                result, status = await asyncio.wait_for(run_owner_job(bot, job), timeout=60), "done"
            except ValueError as e:
                result, status = str(e), "error"
            except Exception as e:
                print(f"[Owner Job Error] {e!r}")
                result, status = f"Unexpected error: {e!r}"[:300], "error"
            db_owner_jobs.update_one({"_id": job["_id"]}, {"$set": {"status": status, "result": result,
                                     "finished_at": datetime.now(timezone.utc).timestamp()}})
    except Exception as e:
        print(f"[Owner Jobs Error] {e!r}")


def publish_owner_data(bot):
    """Saves the list of servers, channels, categories and roles so the website panel can show them."""
    if db_settings is None:
        return
    guilds = []
    for g in bot.guilds[:100]:
        guilds.append({
            "id": str(g.id), "name": g.name, "members": g.member_count or 0,
            "channels": [{"id": str(c.id), "name": c.name} for c in g.text_channels[:400]],
            "categories": [{"id": str(c.id), "name": c.name} for c in g.categories[:100]],
            "roles": [{"id": str(r.id), "name": r.name} for r in g.roles[:250] if not r.is_default() and not r.managed],
        })
    try:
        db_settings.update_one({"_id": "owner_guilds"}, {"$set": {"guilds": guilds,
                               "updated_at": datetime.now(timezone.utc).timestamp()}}, upsert=True)
    except Exception as e:
        print(f"[Owner Data Error] {e!r}")


# -------------------------------------------------------------
# FEATURES COG: points listener, welcome bonus, auto DM, auto build
# -------------------------------------------------------------
class FeaturesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.tree.interaction_check = blocked_check  # every slash command goes through the blacklist

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if is_guild_blacklisted(guild.id):
            try:
                await guild.leave()
            except Exception as e:
                print(f"[Blacklist Leave Error] {e}")

    # -------------------------------------------------------------
    # BACKGROUND SCHEDULER: reminders and giveaway endings
    # -------------------------------------------------------------
    async def cog_load(self):
        load_runtime_state()
        self.scheduler_loop.start()
        self.owner_jobs_loop.start()

    async def cog_unload(self):
        self.scheduler_loop.cancel()
        self.owner_jobs_loop.cancel()

    # Own fast loop: website owner panel actions start within about 1 second,
    # even while the slower scheduler is busy with giveaways.
    @tasks.loop(seconds=1)
    async def owner_jobs_loop(self):
        await process_owner_jobs(self.bot)
        await process_import_requests(self.bot)

    @owner_jobs_loop.before_loop
    async def before_owner_jobs_loop(self):
        await self.bot.wait_until_ready()
        if db_owner_jobs is not None:
            try:
                db_owner_jobs.create_index([("status", 1), ("created_at", 1)])
                db_import_requests.create_index([("status", 1), ("created_at", 1)])
            except Exception as e:
                print(f"[Owner Jobs Index Error] {e}")

    @tasks.loop(seconds=10)
    async def scheduler_loop(self):
        GIVEAWAY_DEBUG["last_tick"] = datetime.now(timezone.utc).timestamp()
        # Every step has a time limit, so one stuck Discord call can never freeze the timer again.
        GIVEAWAY_DEBUG["stage"] = "reminders"
        try:
            await asyncio.wait_for(self.process_reminders(), timeout=60)
        except asyncio.TimeoutError:
            print("[Reminder Loop Error] Timed out after 60s")
            GIVEAWAY_DEBUG["last_error"] = "Reminders timed out (60s)"
        except Exception as e:
            print(f"[Reminder Loop Error] {e!r}")
            GIVEAWAY_DEBUG["last_error"] = f"Reminder loop: {e!r}"
        GIVEAWAY_DEBUG["stage"] = "giveaways"
        try:
            await asyncio.wait_for(self.process_giveaways(), timeout=120)
        except asyncio.TimeoutError:
            print("[Giveaway Loop Error] Timed out after 120s")
            GIVEAWAY_DEBUG["last_error"] = f"Giveaways timed out (120s) at: {GIVEAWAY_DEBUG.get('stage')}"
        except Exception as e:
            print(f"[Giveaway Loop Error] {e!r}")
            GIVEAWAY_DEBUG["last_error"] = f"Giveaway loop: {e!r}"
        if datetime.now(timezone.utc).timestamp() - BOOST_SYNC["last"] >= 600:
            BOOST_SYNC["last"] = datetime.now(timezone.utc).timestamp()
            GIVEAWAY_DEBUG["stage"] = "boost rewards"
            try:
                await asyncio.wait_for(sync_boost_rewards(self.bot), timeout=60)
            except Exception as e:
                print(f"[Boost Sync Error] {e!r}")
        # TE Premium role: check people whose premium just changed, and everyone every 5 minutes
        try:
            now_ts = datetime.now(timezone.utc).timestamp()
            if now_ts - PREMIUM_ROLE_SYNC["last"] >= 300:
                PREMIUM_ROLE_SYNC["last"] = now_ts
                PREMIUM_ROLE_DIRTY.clear()
                GIVEAWAY_DEBUG["stage"] = "premium role"
                await asyncio.wait_for(sync_premium_roles(self.bot), timeout=120)
            elif PREMIUM_ROLE_DIRTY:
                dirty = set(PREMIUM_ROLE_DIRTY)
                PREMIUM_ROLE_DIRTY.difference_update(dirty)
                await asyncio.wait_for(sync_premium_roles(self.bot, dirty), timeout=60)
        except Exception as e:
            print(f"[Premium Role Sync Error] {e!r}")
        # Website owner panel: run waiting jobs, and refresh the server list every minute
        try:
            if datetime.now(timezone.utc).timestamp() - OWNER_PUBLISH["last"] >= 60:
                OWNER_PUBLISH["last"] = datetime.now(timezone.utc).timestamp()
                publish_owner_data(self.bot)
                for g in list(self.bot.guilds):
                    if is_guild_blacklisted(g.id):
                        try:
                            await g.leave()
                        except Exception as e:
                            print(f"[Blacklist Leave Error] {e}")
        except Exception as e:
            print(f"[Owner Panel Error] {e!r}")
        GIVEAWAY_DEBUG["stage"] = "idle"
        GIVEAWAY_DEBUG["last_tick"] = datetime.now(timezone.utc).timestamp()

    @scheduler_loop.error
    async def scheduler_loop_error(self, error):
        # Without this a crash would stop the loop for good. Log it and start it again.
        print(f"[Scheduler Crashed] {error!r}")
        GIVEAWAY_DEBUG["last_error"] = f"Scheduler crashed: {error!r}"
        try:
            self.scheduler_loop.restart()
        except Exception as e:
            print(f"[Scheduler Restart Error] {e!r}")

    @scheduler_loop.before_loop
    async def before_scheduler_loop(self):
        await self.bot.wait_until_ready()

    async def process_reminders(self):
        now = datetime.now(timezone.utc).timestamp()
        for reminder in [r for r in REMINDERS if r["due_ts"] <= now]:
            remove_reminder(reminder["id"])
            embed = embed_info("Reminder", reminder["message"])
            embed.add_field(name="Set", value=f"<t:{int(reminder['created_ts'])}:R>", inline=False)

            delivered = False
            try:
                user = await self.bot.fetch_user(int(reminder["user_id"]))
                await user.send(embed=embed)
                delivered = True
            except Exception:
                pass

            if not delivered:
                channel = self.bot.get_channel(int(reminder["channel_id"]))
                if channel:
                    try:
                        await channel.send(
                            content=f"<@{reminder['user_id']}>",
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(reminder["user_id"]))])
                        )
                    except Exception as e:
                        print(f"[Reminder Delivery Error] {e}")

    async def process_giveaways(self):
        now = datetime.now(timezone.utc).timestamp()

        # Each giveaway is handled on its own, so one broken record can never block the others.
        for gw in list(GIVEAWAYS.values()):
            try:
                end_ts = float(gw.get("end_ts") or 0)
                if end_ts <= 0:
                    continue

                if not gw.get("ended"):
                    if end_ts <= now:
                        await finish_giveaway(self.bot, gw)
                    continue

                # Retry winner messages that failed to post (up to 5 tries, within a day of ending)
                if (gw.get("announced") is False and int(gw.get("announce_tries", 0)) < 5
                        and now - end_ts < 86400 and now - float(gw.get("last_try", 0)) >= 60):
                    message = await get_giveaway_message(self.bot, gw)
                    await announce_and_record(self.bot, gw, gw.get("winner_ids", []), message=message)
            except Exception as e:
                import traceback
                print(f"[Giveaway Finish Error] {gw.get('message_id')}: {e!r}")
                traceback.print_exc()
                GIVEAWAY_DEBUG["last_error"] = f"{gw.get('message_id')}: {e!r}"

    # -------------------------------------------------------------
    # AFK SYSTEM: remove AFK when the user talks, tell people who ping an AFK user
    # -------------------------------------------------------------
    async def handle_afk(self, message: discord.Message):
        gid = message.guild.id
        no_pings = discord.AllowedMentions.none()

        if get_afk(gid, message.author.id):
            entry = get_afk(gid, message.author.id)
            clear_afk(gid, message.author.id)
            try:
                await message.reply(
                    embed=embed_ok("Welcome Back", f"Your AFK was removed. You were away since <t:{int(entry['since'])}:R>."),
                    allowed_mentions=no_pings,
                    delete_after=10
                )
            except Exception:
                pass

        lines = []
        seen = set()
        for member in message.mentions:
            if member.bot or member.id == message.author.id or member.id in seen:
                continue
            seen.add(member.id)
            entry = get_afk(gid, member.id)
            if entry:
                lines.append(f"**{member.display_name}** is AFK: {entry['reason']} (since <t:{int(entry['since'])}:R>)")
        if lines:
            try:
                await message.reply(embed=embed_info("User Is AFK", "\n".join(lines)), allowed_mentions=no_pings)
            except Exception:
                pass

    # -------------------------------------------------------------
    # CHANNEL MIRROR LISTENERS (send / edit / delete)
    # -------------------------------------------------------------
    async def _dm_partial(self, uid: str, dm_message_id: str):
        user = self.bot.get_user(int(uid))
        if user is None:
            user = await self.bot.fetch_user(int(uid))
        dm = user.dm_channel or await user.create_dm()
        return dm.get_partial_message(int(dm_message_id))

    @commands.Cog.listener("on_member_update")
    async def boost_on_member_update(self, before: discord.Member, after: discord.Member):
        """A member starts or stops boosting: update their premium right away (not only every 10 minutes)."""
        if after.guild.id != BOOST_GUILD_ID or before.premium_since == after.premium_since:
            return
        try:
            await sync_boost_rewards(self.bot)
        except Exception as e:
            print(f"[Boost Update Error] {e!r}")

    @commands.Cog.listener("on_message")
    async def website_on_message(self, message: discord.Message):
        """Someone writes the word 'website' -> reply with the website button. One reply per channel every 30 seconds."""
        if message.author.bot or not message.guild or not message.content:
            return
        if not WEBSITE_URL or not WEBSITE_WORD.search(message.content):
            return
        now = datetime.now(timezone.utc).timestamp()
        if now - WEBSITE_LAST_REPLY.get(message.channel.id, 0.0) < WEBSITE_COOLDOWN_SECONDS:
            return
        WEBSITE_LAST_REPLY[message.channel.id] = now  # set first so quick messages cannot slip through
        try:
            await message.reply(**website_payload(), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            print(f"[Website Reply Error] {e!r}")

    @commands.Cog.listener("on_message")
    async def broadcast_on_message(self, message: discord.Message):
        if message.channel.id != BROADCAST_CHANNEL_ID or message.author.id != BROADCAST_AUTHOR_ID or message.author.bot:
            return
        if not message.guild:
            return
        text = broadcast_text(message)
        if not text:
            return
        sid = str(message.id)
        rec = {"dms": {}, "content": text, "content_of": {}, "deleted": False, "lock": asyncio.Lock()}
        BROADCASTS[sid] = rec
        asyncio.create_task(self._broadcast_send(message, rec))

    async def _broadcast_send(self, message: discord.Message, rec: dict):
        sid = str(message.id)
        sent = failed = 0
        async with rec["lock"]:
            targets = await self._broadcast_targets(message)
            print(f"[Broadcast] Sending message {sid} to {len(targets)} people.")
            for member in targets:
                if rec["deleted"]:
                    break
                text = rec["content"]
                try:
                    dm = member.dm_channel or await member.create_dm()
                    msg = await dm.send(text, allowed_mentions=discord.AllowedMentions.none())
                    rec["dms"][str(member.id)] = str(msg.id)
                    rec["content_of"][str(member.id)] = text
                    sent += 1
                except (discord.Forbidden, discord.NotFound):
                    failed += 1  # DMs closed
                except discord.HTTPException as e:
                    failed += 1
                    print(f"[Broadcast] DM to {member.id} failed: {e!r}")
                    if getattr(e, "status", 0) == 429:
                        await asyncio.sleep(10)
                if (sent + failed) % 25 == 0:
                    save_broadcast(sid, rec)
                await asyncio.sleep(BROADCAST_DELAY)
            save_broadcast(sid, rec)

        if rec["deleted"]:
            await self._broadcast_delete_dms(sid, rec)
            return
        try:
            await message.add_reaction("\u2705")
            await message.reply(
                embed=embed_ok("Sent To Everyone", f"People found: `{sent + failed}`\nDelivered: `{sent}`\nCould not deliver (DMs closed): `{failed}`\nEdit or delete this message to edit or delete the DMs too."),
                allowed_mentions=discord.AllowedMentions.none(), mention_author=False
            )
        except Exception as e:
            print(f"[Broadcast] Summary error: {e!r}")

    async def _broadcast_targets(self, message: discord.Message) -> list:
        """Members of the one broadcast server, plus every user saved in the database."""
        ids = set()
        guild = self.bot.get_guild(BROADCAST_GUILD_ID)
        if guild is None:
            print(f"[Broadcast] The bot is not in server {BROADCAST_GUILD_ID}. Only saved users will get the message.")
        else:
            try:
                async for m in guild.fetch_members(limit=None):
                    if not m.bot:
                        ids.add(m.id)
            except Exception as e:
                print(f"[Broadcast] Could not list members of {guild.name}: {e!r}")
                ids.update(m.id for m in guild.members if not m.bot)
        if db_users is not None:
            try:
                for doc in db_users.find({}, {"discord_id": 1}):
                    if str(doc.get("discord_id", "")).isdigit():
                        ids.add(int(doc["discord_id"]))
            except Exception as e:
                print(f"[Broadcast] Could not read saved users: {e!r}")
        ids.discard(message.author.id)
        ids.discard(self.bot.user.id)

        targets = []
        for uid in ids:
            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except Exception:
                    continue
            if not user.bot:
                targets.append(user)
        return targets

    async def _broadcast_delete_dms(self, sid: str, rec: dict):
        async with rec["lock"]:
            if rec.get("purged"):
                return
            rec["purged"] = True
            for uid, mid in list(rec["dms"].items()):
                try:
                    partial = await self._dm_partial(uid, mid)
                    await partial.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"[Broadcast] Delete DM {uid} failed: {e!r}")
                await asyncio.sleep(BROADCAST_DELAY)
            forget_broadcast(sid)
        print(f"[Broadcast] Deleted DMs for message {sid}.")

    async def _broadcast_edit_dms(self, sid: str, rec: dict):
        async with rec["lock"]:
            for uid, mid in list(rec["dms"].items()):
                text = rec["content"]
                if rec["content_of"].get(uid) == text:
                    continue
                try:
                    partial = await self._dm_partial(uid, mid)
                    await partial.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
                    rec["content_of"][uid] = text
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"[Broadcast] Edit DM {uid} failed: {e!r}")
                await asyncio.sleep(BROADCAST_DELAY)
        print(f"[Broadcast] Edited DMs for message {sid}.")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.channel_id != BROADCAST_CHANNEL_ID:
            return
        sid = str(payload.message_id)
        rec = load_broadcast(sid)
        if rec is None:
            return
        try:
            channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except Exception as e:
            print(f"[Broadcast] Could not read edited message: {e!r}")
            return
        if message.author.id != BROADCAST_AUTHOR_ID:
            return
        text = broadcast_text(message)
        if not text or text == rec.get("content"):
            return
        rec["content"] = text
        asyncio.create_task(self._broadcast_edit_dms(sid, rec))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.channel_id != BROADCAST_CHANNEL_ID:
            return
        sid = str(payload.message_id)
        rec = load_broadcast(sid)
        if rec is None:
            return
        rec["deleted"] = True  # stops a send that is still running
        asyncio.create_task(self._broadcast_delete_dms(sid, rec))

    # -------------------------------------------------------------
    # JOIN / LEAVE MESSAGES  (set with /greeting)
    # -------------------------------------------------------------
    async def _send_greeting(self, kind: str, member: discord.Member):
        cfg = get_guild_config(member.guild.id).get(kind)
        if not cfg or not cfg.get("enabled", True) or not cfg.get("channel_id"):
            return
        channel = member.guild.get_channel(int(cfg["channel_id"]))
        if channel is None:
            return
        try:
            embed = build_greeting_embed(kind, cfg.get("message") or DEFAULT_GREETINGS[kind], member, member.guild)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            print(f"[Greeting Error] {kind} in {member.guild.id}: {e!r}")

    @commands.Cog.listener("on_member_join")
    async def greeting_on_join(self, member: discord.Member):
        await self._send_greeting("join", member)

    @commands.Cog.listener("on_member_remove")
    async def greeting_on_leave(self, member: discord.Member):
        await self._send_greeting("leave", member)

    # -------------------------------------------------------------
    # MESSAGE LOG: edited and deleted messages  (set with /messagelog)
    # -------------------------------------------------------------
    def _message_log_channel(self, guild_id: Optional[int]):
        if not guild_id:
            return None
        channel_id = get_guild_config(guild_id).get("message_log")
        return self.bot.get_channel(int(channel_id)) if channel_id else None

    @commands.Cog.listener("on_raw_message_edit")
    async def msglog_on_edit(self, payload: discord.RawMessageUpdateEvent):
        log_channel = self._message_log_channel(payload.guild_id)
        if log_channel is None or payload.channel_id == log_channel.id:
            return
        data = payload.data or {}
        if "content" not in data:
            return  # only a link preview or embed changed
        cached = payload.cached_message
        author = cached.author if cached else None
        if (author and author.bot) or (data.get("author") or {}).get("bot"):
            return
        before = cached.content if cached else None
        after = data.get("content", "")
        if before is not None and before == after:
            return
        author_id = author.id if author else int((data.get("author") or {}).get("id", 0) or 0)
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        log_out = log_payload("Message Edited", COLOR_GOLD, [
            ("Author", f"<@{author_id}>\n`{author_id}`" if author_id else "`Unknown`"),
            ("Channel", f"<#{payload.channel_id}>"),
            ("Before", f"```{before[:900]}```" if before else "`Unknown (sent before the bot started)`"),
            ("After", f"```{after[:900]}```" if after else "`(empty)`"),
            ("Message", f"[Jump to message]({jump})"),
        ])
        try:
            await log_channel.send(**log_out, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            print(f"[Message Log Error] {e!r}")

    @commands.Cog.listener("on_raw_message_delete")
    async def msglog_on_delete(self, payload: discord.RawMessageDeleteEvent):
        log_channel = self._message_log_channel(payload.guild_id)
        if log_channel is None or payload.channel_id == log_channel.id:
            return
        cached = payload.cached_message
        if cached and cached.author.bot:
            return
        items = [
            ("Author", f"{cached.author.mention}\n`{cached.author.id}`" if cached else "`Unknown`"),
            ("Channel", f"<#{payload.channel_id}>"),
        ]
        if cached:
            content = cached.content
            items.append(("Content", f"```{content[:900]}```" if content else "`(no text)`"))
            if cached.attachments:
                items.append(("Attachments", "\n".join(f"`{a.filename}`" for a in cached.attachments)[:1000]))
        else:
            items.append(("Content", "`Unknown (sent before the bot started)`"))
        items.append(("Message ID", f"`{payload.message_id}`"))
        log_out = log_payload("Message Deleted", COLOR_RED, items)
        try:
            await log_channel.send(**log_out, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            print(f"[Message Log Error] {e!r}")

    # -------------------------------------------------------------
    # EVENT LISTENER: WELCOME BONUS (20 POINTS)
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        add_user_points(str(member.id), 20)
        print(f"[Welcome Bonus] Awarded 20 points to new member: {member.name} ({member.id})")
        if member.guild.id == PREMIUM_ROLE_GUILD_ID and user_has_premium(str(member.id)):
            PREMIUM_ROLE_DIRTY.add(str(member.id))

    # -------------------------------------------------------------
    # LISTEN FOR MESSAGES & PREMIUM AUTO-BUILD LOG MESSAGES
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        is_design_channel = str(message.channel.id) in [str(PREMIUM_DESIGN_CHANNEL_ID), str(NON_PREMIUM_DESIGN_CHANNEL_ID)]
        
        if message.author.bot and not (is_design_channel and message.attachments):
            return

        # CHAT POINTING & ANTI-SPAM SYSTEM
        if not message.author.bot:
            user_id_str = str(message.author.id)
            now_ts = datetime.now(timezone.utc).timestamp()
            content_clean = message.content.strip().lower()

            if user_id_str not in USER_MSG_TRACKER:
                USER_MSG_TRACKER[user_id_str] = {
                    "last_time": 0.0,
                    "msg_count": 0,
                    "last_content": "",
                    "penalty_until": 0.0
                }

            user_track = USER_MSG_TRACKER[user_id_str]

            if now_ts >= user_track["penalty_until"]:
                time_passed = now_ts - user_track["last_time"]

                if time_passed < 30.0 and content_clean and content_clean == user_track["last_content"]:
                    user_track["penalty_until"] = now_ts + (30 * 60)
                    user_track["last_time"] = now_ts
                    user_track["msg_count"] = 0
                    print(f"[Points Penalty] User {message.author.name} ({user_id_str}) penalized for 30 mins due to duplicate message.")
                else:
                    if time_passed >= 30.0:
                        user_track["last_time"] = now_ts
                        user_track["msg_count"] = 1
                        user_track["last_content"] = content_clean
                        add_user_points(user_id_str, 2)
                    else:
                        user_track["msg_count"] += 1
                        if user_track["msg_count"] > 5:
                            user_track["last_time"] = now_ts
                            user_track["msg_count"] = 0
                        user_track["last_content"] = content_clean

        # AFK SYSTEM
        if not message.author.bot and message.guild:
            try:
                await self.handle_afk(message)
            except Exception as afk_err:
                print(f"[AFK Error] {afk_err}")

        # AUTO-BUILD / NON-PREMIUM DM LISTENER
        if not is_design_channel:
            return

        print(f"[Design Channel] Message {message.id} from {message.author} ({message.author.id}) in {message.channel.id}: {len(message.attachments)} attachment(s).")
        json_attachment = None
        for attachment in message.attachments:
            if attachment.filename.endswith(".json"):
                json_attachment = attachment
                break

        if not json_attachment:
            return

        # The website posts submissions with the bot's own token. Anyone else could fake the submitting user.
        if message.author.id not in (self.bot.user.id, BOT_OWNER_ID):
            print(f"[Auto-Build Blocked] Ignored a design file posted by {message.author} ({message.author.id}).")
            return

        try:
            attachment_bytes = await json_attachment.read()
        except Exception as e:
            print(f"[Auto-Build Parsing Error] Could not read JSON attachment: {e}")
            return
        await self.process_design_submission(message.id, message.channel, attachment_bytes)

    # -------------------------------------------------------------
    # BACKUP READER: website cards (new message layout) read straight from the raw gateway data,
    # so the DM still works even if the library does not show the file on the message.
    # -------------------------------------------------------------
    @commands.Cog.listener("on_socket_raw_receive")
    async def design_raw_receive(self, raw):
        if not isinstance(raw, str) or "MESSAGE_CREATE" not in raw:
            return
        if not any(str(c) in raw for c in (PREMIUM_DESIGN_CHANNEL_ID, NON_PREMIUM_DESIGN_CHANNEL_ID)):
            return
        try:
            event = json.loads(raw)
            if event.get("t") != "MESSAGE_CREATE":
                return
            data = event.get("d") or {}
            channel_id = str(data.get("channel_id"))
            if channel_id not in (str(PREMIUM_DESIGN_CHANNEL_ID), str(NON_PREMIUM_DESIGN_CHANNEL_ID)):
                return
            author_id = int((data.get("author") or {}).get("id", 0))
            if author_id not in (self.bot.user.id, BOT_OWNER_ID):
                return
            message_id = int(data["id"])
        except Exception:
            return

        await asyncio.sleep(3)  # give the normal reader a chance first
        if message_id in DESIGN_DONE:
            return

        url = None
        for att in data.get("attachments") or []:
            if str(att.get("filename", "")).endswith(".json"):
                url = att.get("url")
                break

        def find_file(components):
            for comp in components or []:
                if comp.get("type") == 13:
                    f = comp.get("file") or {}
                    name = str(comp.get("name", ""))
                    for key in ("proxy_url", "url"):
                        if str(f.get(key, "")).startswith("http") and (name.endswith(".json") or ".json" in str(f.get(key, ""))):
                            return f[key]
                found = find_file(comp.get("components"))
                if found:
                    return found
            return None

        if not url:
            url = find_file(data.get("components"))
        print(f"[Design Raw] Message {message_id}: file url {'found' if url else 'NOT found'}.")
        if not url:
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    file_bytes = await resp.read()
        except Exception as e:
            print(f"[Design Raw] Could not download the design file: {e}")
            return

        channel = self.bot.get_channel(int(channel_id))
        await self.process_design_submission(message_id, channel, file_bytes)

    async def process_design_submission(self, message_id: int, channel, attachment_bytes: bytes):
        """Reads the design file and starts the build or the deployment DM. Runs once per message."""
        if message_id in DESIGN_DONE:
            return
        DESIGN_DONE.add(message_id)
        if len(DESIGN_DONE) > 500:
            DESIGN_DONE.clear()
            DESIGN_DONE.add(message_id)

        try:
            payload = json.loads(attachment_bytes.decode("utf-8"))
        except Exception as e:
            print(f"[Auto-Build Parsing Error] Could not read JSON attachment: {e}")
            return

        submitting_user_id = str(payload.get("submitting_user_id", "0"))
        target_guild_id = str(payload.get("target_server_id", "0")).strip()
        build_id = str(message_id)
        set_build_status(build_id, submitting_user_id, "received", "The bot got your design.")
        layout = payload.get("layout", {})
        is_premium_submission = payload.get("is_premium", False)
        build_mode = "add" if (is_premium_submission and payload.get("build_mode") == "add") else "rebuild"

        if not target_guild_id or target_guild_id == "0":
            print("[Auto-Build Error] Target server ID is missing or 0.")
            set_build_status(build_id, submitting_user_id, "failed", "The Server ID is missing.")
            return

        # Fetch target guild
        guild = self.bot.get_guild(int(target_guild_id))
        if not guild:
            try:
                guild = await self.bot.fetch_guild(int(target_guild_id))
            except Exception as fetch_err:
                print(f"[Auto-Build Fetch Error] Could not fetch target guild {target_guild_id}: {fetch_err}")
                guild = None

        if not guild:
            set_build_status(build_id, submitting_user_id, "failed", "The bot is not in your server yet. Add the bot, then submit again.")
            await self.notify_missing_bot(submitting_user_id, target_guild_id, layout)
            return

        allowed, reason = await can_build_in_guild(guild, submitting_user_id)
        if not allowed:
            set_build_status(build_id, submitting_user_id, "failed", "The bot could not build in this server. Check your Discord DMs.")
            await self.notify_build_denied(submitting_user_id, guild.name, reason)
            return

        is_standard = not is_premium_submission and str(getattr(channel, 'id', 0)) != str(PREMIUM_DESIGN_CHANNEL_ID)

        if is_standard:
            ok, limit, _used, reset_unix = check_build_limit(submitting_user_id)
            if not ok:
                set_build_status(build_id, submitting_user_id, "failed", "You used your free builds for this week. Check your Discord DMs.")
                await self.send_limit_dm(submitting_user_id, limit, reset_unix)
                return

        if is_standard and not is_free_mode():
            set_build_status(build_id, submitting_user_id, "waiting_payment", "Open your Discord DMs and choose how to pay (points or TE Premium).")
            print(f"[Frontend Direct DM] Initiating direct DM with 5 retries to submitter {submitting_user_id} before logging...")
            await self.send_non_premium_verification_dm(submitting_user_id, target_guild_id, layout, guild.name, channel, build_id)
        else:
            print(f"[Auto-Build] {'Free mode' if is_standard else 'Premium'} deployment for {guild.name} ({guild.id})")
            safety_backup = None
            if is_standard:
                record_build(submitting_user_id)
            else:
                # TE Premium: save the server first, so the build can be undone
                try:
                    safety_backup = make_auto_backup(guild, submitting_user_id)
                except Exception as e:
                    print(f"[Auto Backup Error] {e!r}")
            await self.execute_auto_build(guild, layout, build_mode, build_id, submitting_user_id)
            if safety_backup:
                await self.send_backup_dm(submitting_user_id, guild.name, safety_backup["backup_id"])

    async def send_backup_dm(self, user_id: str, guild_name: str, backup_id: str):
        if not user_allows_dms(user_id):
            return
        embed = discord.Embed(
            title="Build Finished",
            description=(
                f"Your layout for **{guild_name}** is built.\n\n"
                f"A safety backup of the old server was saved first: `{backup_id}`\n"
                "Not happy with the result? Run `/backup` in that server and choose **Undo Last Build**. "
                "The server owner or you can do this (you need Administrator in that server)."
            ),
            color=0xC9962B
        )
        embed.set_footer(text="TE Premium: the last 3 builds are kept.")
        try:
            user = await self.bot.fetch_user(int(user_id))
            await user.send(embed=embed)
        except Exception as e:
            print(f"[Backup DM Error] {e}")

    async def send_limit_dm(self, user_id: str, limit: int, reset_unix: int):
        if not user_allows_dms(user_id):
            return
        try:
            user = await self.bot.fetch_user(int(user_id))
            await user.send(embed=build_limit_embed(limit, reset_unix))
        except Exception as e:
            print(f"[Limit DM Error] {e}")

    async def send_non_premium_verification_dm(self, user_id: str, target_guild_id: str, layout: dict, guild_name: str, fallback_channel: Optional[discord.TextChannel] = None, build_id: str = ""):
        user_pts = get_user_points(user_id)
        prices = get_prices()
        points_cost = prices["points_cost"]

        embed = discord.Embed(
            title="Server Deployment Options",
            description=f"<@{user_id}> Your blueprint for **{guild_name}** has been submitted!\n\nTo build this layout, select one of the deployment options below:",
            color=0x8B5CF6
        )
        limits = get_limits()
        used = get_builds_used(user_id)
        build_cap = limits["builds_per_week"]
        embed.add_field(
            name="Option 1: Server Points",
            value=(
                f"Click **Use Points ({points_cost:,} Pts)** to build right now.\n"
                f"Your points: `{user_pts:,} pts`\n"
                "Earn points by chatting, with `/daily`, in giveaways, and in `/shop` deals."
            ),
            inline=False
        )
        embed.add_field(
            name="Option 2: TE Premium",
            value=(
                f"Subscribe on Roblox (**{prices['subscription_robux']:,} Robux/mo**) "
                "for instant automatic builds with no limits, or get it free for 30 days while you boost our server.\n"
                "The subscription needs a synced Roblox account (see below)."
            ),
            inline=False
        )
        embed.add_field(
            name="Your Free Limits",
            value=(
                (f"Builds this week: `{used}/{build_cap}`\n" if build_cap > 0 else "Builds: `no limit`\n")
                + (f"AI generations: `{limits['ai_per_day']} per day`\n" if limits["ai_per_day"] > 0 else "AI generations: `no limit`\n")
                + "TE Premium members have no limits."
            ),
            inline=False
        )
        embed.add_field(
            name="How to Sync Your Roblox Account",
            value=(
                "Only TE Premium (Roblox subscription) needs this.\n"
                "1. Join the Template Engine Discord server.\n"
                "2. Open the `#account-sync` channel.\n"
                "3. Follow the steps there to link your Roblox account."
            ),
            inline=False
        )

        view = NonPremiumDeploymentView(
            bot=self.bot,
            submitting_user_id=user_id,
            target_guild_id=target_guild_id,
            layout=layout,
            points_cost=points_cost,
            build_id=build_id
        )

        dm_sent = False
        user = None

        try:
            user = await self.bot.fetch_user(int(user_id))
        except Exception as e:
            print(f"[DM Error] Could not fetch user {user_id}: {e}")

        if user:
            for attempt in range(1, 6):
                try:
                    dm_channel = user.dm_channel or await user.create_dm()
                    await dm_channel.send(embed=embed, view=view)
                    print(f"[DM Success] Direct message successfully delivered on attempt {attempt}/5 to {user.name} ({user_id})")
                    dm_sent = True
                    break
                except discord.Forbidden:
                    print(f"[DM Blocked] Direct Messages are disabled for user {user_id}.")
                    break
                except Exception as attempt_err:
                    print(f"[DM Retry {attempt}/5] DM delivery failed: {attempt_err}")
                    if attempt < 5:
                        await asyncio.sleep(1.5)

        if dm_sent:
            return

        # FALLBACK 1: TARGET GUILD TEXT CHANNEL
        target_gid = int(target_guild_id) if target_guild_id and target_guild_id != "0" else 0
        target_guild = self.bot.get_guild(target_gid) if target_gid else None

        if not target_guild and target_gid:
            try:
                target_guild = await self.bot.fetch_guild(target_gid)
            except Exception:
                target_guild = None

        if target_guild:
            target_channel = None
            for ch in target_guild.text_channels:
                if ch.permissions_for(target_guild.me).send_messages:
                    target_channel = ch
                    break

            if target_channel:
                try:
                    await target_channel.send(content=f"<@{user_id}>", embed=embed, view=view)
                    print(f"[Fallback Success] Posted deployment embed in target server channel #{target_channel.name}")
                    return
                except Exception as t_err:
                    print(f"[Target Server Send Error] Could not post in target server {target_guild.name}: {t_err}")

        # FALLBACK 2: MAIN SUBMISSION CHANNEL
        if fallback_channel:
            try:
                await fallback_channel.send(content=f"<@{user_id}>", embed=embed, view=view)
                print(f"[Fallback Success] Posted deployment embed in submission channel #{fallback_channel.name}")
            except Exception as chan_err:
                print(f"[Fallback Channel Send Error] Could not post to submission channel: {chan_err}")

    async def notify_build_denied(self, user_id: str, guild_name: str, reason: str):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await user.send(embed=build_denied_embed(guild_name, reason))
        except Exception as e:
            print(f"[DM Warning] Failed sending build denied notice to {user_id}: {e}")

    async def notify_missing_bot(self, user_id: str, target_guild_id: str, layout: dict = None):
        try:
            user = await self.bot.fetch_user(int(user_id))
            if user:
                embed = discord.Embed(
                    title="Bot Missing in Target Server",
                    description=f"Auto-deployment paused. The bot is not present in target server ID `{target_guild_id}`.\n\nPlease add the bot using the link button below, then click **Build** to trigger deployment.",
                    color=0xF43F5E
                )
                view = PremiumMissingBotView(
                    bot=self.bot,
                    submitting_user_id=user_id,
                    target_guild_id=target_guild_id,
                    layout=layout
                )
                await user.send(embed=embed, view=view)
        except Exception as e:
            print(f"[DM Warning] Failed sending missing bot notification to {user_id}: {e}")

    async def nuke_guild_structure(self, guild: discord.Guild):
        """Deletes ALL channels, categories and every role the bot is allowed to delete, before building."""
        reason = "Template Engine Nuke Before Auto-Build"

        # Community servers cannot delete their rules/updates channels, so turn Community off first.
        if "COMMUNITY" in guild.features:
            try:
                await guild.edit(community=False, reason=reason)
            except Exception as e:
                print(f"[Nuke Community Warning] Could not turn off Community: {e}")

        failed = 0
        for attempt in range(2):  # second pass catches anything that failed the first time
            # channels first, categories last
            targets = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)]
            targets += [c for c in guild.channels if isinstance(c, discord.CategoryChannel)]
            failed = 0
            for channel in targets:
                try:
                    await channel.delete(reason=reason)
                    await asyncio.sleep(0.2)
                except discord.NotFound:
                    pass
                except Exception as e:
                    failed += 1
                    print(f"[Nuke Channel Warning] Failed to delete {channel.name}: {e}")
            if not failed:
                break
            await asyncio.sleep(1)

        role_failed = 0
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            if role.is_default() or role.managed:
                continue
            if role.position >= guild.me.top_role.position:
                role_failed += 1
                continue
            try:
                await role.delete(reason=reason)
                await asyncio.sleep(0.2)
            except discord.NotFound:
                pass
            except Exception as e:
                role_failed += 1
                print(f"[Nuke Role Warning] Failed to delete role {role.name}: {e}")

        print(f"[Nuke] {guild.name}: {failed} channels and {role_failed} roles could not be deleted.")

    async def execute_auto_build(self, guild: discord.Guild, layout: dict, mode: str = "rebuild", build_id: str = "", user_id: str = "0"):
        """mode "rebuild": delete everything, then build. mode "add" (TE Premium): keep the server, only add what is missing."""
        add_mode = mode == "add"
        total_steps = len(layout.get("roles", [])) + len(layout.get("categories", [])) + sum(
            len(c.get("channels", []) or []) for c in layout.get("categories", []))
        progress = {"done": 0, "last": 0.0}

        def tick(text: str):
            """Counts one finished step and tells the website (at most every 1.5 seconds)."""
            progress["done"] += 1
            now = time.monotonic()
            if now - progress["last"] >= 1.5:
                progress["last"] = now
                set_build_status(build_id, user_id, "building", text, progress["done"], total_steps)

        try:
            set_build_status(build_id, user_id, "building", "Clearing the old layout..." if not add_mode else "Getting ready...", 0, total_steps)
            if not add_mode:
                await self.nuke_guild_structure(guild)

            def same_name(a: str, b: str) -> bool:
                return (strip_emojis(a) or a).strip().lower() == (strip_emojis(b) or b).strip().lower()

            created_roles = {}
            if add_mode:
                for existing_role in guild.roles:
                    if not existing_role.is_default():
                        created_roles[existing_role.name] = existing_role
            roles_data = layout.get("roles", [])

            for r_info in roles_data:
                role_name = strip_emojis(r_info.get("name", "Role"))
                if not role_name:
                    role_name = str(r_info.get("name", "Role"))
                tick(f"Creating role: {role_name}")
                if add_mode and any(same_name(role_name, n) for n in created_roles):
                    continue  # this role is already in the server
                color_hex = str(r_info.get("color", "#8b5cf6")).lstrip('#')
                try:
                    color_int = int(color_hex, 16)
                except ValueError:
                    color_int = 0x8b5cf6

                create_kwargs = {
                    "name": role_name,
                    "color": discord.Color(color_int),
                    "reason": "Template Engine Auto-Build",
                }
                role_perms = build_role_permissions(r_info.get("permissions"))
                if role_perms is not None:
                    create_kwargs["permissions"] = role_perms

                try:
                    try:
                        role = await guild.create_role(**create_kwargs)
                    except discord.HTTPException as perm_err:
                        if "permissions" not in create_kwargs:
                            raise
                        print(f"[Auto-Build Role Permission Warning] {role_name}: {perm_err}. Retrying without permissions.")
                        create_kwargs.pop("permissions")
                        role = await guild.create_role(**create_kwargs)
                    created_roles[role_name] = role
                    await asyncio.sleep(0.2)
                except Exception as e:
                    print(f"[Auto-Build Role Error] {role_name}: {e}")

            categories = layout.get("categories", [])
            for cat_data in categories:
                cat_raw_name = cat_data.get("name", "CATEGORY")
                cat_clean_name = strip_emojis(cat_raw_name) or cat_raw_name
                tick(f"Creating category: {cat_clean_name}")

                try:
                    category = None
                    if add_mode:
                        category = next((c for c in guild.categories if same_name(c.name, cat_clean_name)), None)
                    if category is not None:
                        category_overwrites = dict(category.overwrites)
                    else:
                        category_overwrites = collect_overwrites(guild, created_roles, cat_data.get("permissions"))
                        cat_kwargs = {"name": cat_clean_name, "reason": "Template Engine Auto-Build"}
                        if category_overwrites:
                            cat_kwargs["overwrites"] = category_overwrites
                        category = await create_with_overwrite_fallback(guild.create_category, cat_clean_name, **cat_kwargs)
                        await asyncio.sleep(0.2)
                except Exception as e:
                    print(f"[Auto-Build Cat Error] {cat_clean_name}: {e}")
                    continue

                for chan in cat_data.get("channels", []):
                    chan_label = str(chan.get("name") or chan.get("clean_name") or "channel")
                    try:
                        chan_clean_name = format_channel_name(chan_label)
                        tick(f"Creating channel: {chan_clean_name}")
                        chan_type = chan.get("type", "text")
                        chan_topic = chan.get("topic", "")
                        if add_mode and any(same_name(c.name, chan_clean_name) for c in category.channels):
                            continue  # this channel is already in the category

                        # Category permissions first, then this channel's own permissions on top
                        overwrites = collect_overwrites(
                            guild, created_roles, chan.get("permissions"),
                            read_only=bool(chan.get("read_only")), base=category_overwrites
                        )
                        kwargs = {"name": chan_clean_name, "reason": "Template Engine Auto-Build"}
                        if overwrites:
                            kwargs["overwrites"] = overwrites

                        if chan_type == "voice":
                            await create_with_overwrite_fallback(category.create_voice_channel, chan_clean_name, **kwargs)
                        elif chan_type in ["forum", "stage"]:
                            kwargs["topic"] = f"[{chan_type.upper()}] {chan_topic}"
                            await create_with_overwrite_fallback(category.create_text_channel, chan_clean_name, **kwargs)
                        else:
                            kwargs["topic"] = chan_topic
                            await create_with_overwrite_fallback(category.create_text_channel, chan_clean_name, **kwargs)
                        await asyncio.sleep(0.2)
                    except Exception as e:
                        print(f"[Auto-Build Channel Error] {chan_label}: {e}")
            set_build_status(build_id, user_id, "done", "Your server is ready.", total_steps, total_steps)
        except Exception as top_err:
            print(f"[Auto-Build Execution Error] {top_err}")
            set_build_status(build_id, user_id, "failed", "The build stopped because of an error. Check the bot's permissions.", progress["done"], total_steps)


async def setup(bot: commands.Bot):
    # Register persistent views for static panels
    bot.add_view(RobloxVerificationView())
    bot.add_view(RobloxUnsyncView())
    bot.add_view(PremiumMissingBotView())
    bot.add_view(GiveawayView())
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())
    if HAS_CARD_LAYOUT:
        try:
            bot.add_view(GiveawayLayout())
        except Exception as e:
            print(f"[Giveaway Layout Warning] Could not register the card buttons: {e!r}")
    await bot.add_cog(FeaturesCog(bot))
