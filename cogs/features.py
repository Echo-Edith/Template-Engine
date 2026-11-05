import os
import re
import json
import random
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
db_build_auth = None
db_users = None
db_cases = None
db_counters = None
db_afk = None
db_reminders = None
db_giveaways = None
db_settings = None
db_promos = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_premium = db["premium_users"]
        db_build_auth = db["build_authorized_users"]
        db_users = db["persistent_users"]
        db_cases = db["mod_cases"]
        db_counters = db["counters"]
        db_afk = db["afk_users"]
        db_reminders = db["reminders"]
        db_giveaways = db["giveaways"]
        db_settings = db["settings"]
        db_promos = db["promo_codes"]
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


def set_user_premium(user_id: str, duration_td: timedelta):
    """Saves premium status into MongoDB or local fallback storage."""
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


def is_staff(user_id: str) -> bool:
    uid = str(user_id)
    if db_premium is not None:
        try:
            return bool(db_premium.find_one({"discord_id": uid, "staff": True}))
        except Exception as e:
            print(f"[Mongo Staff Check Error] {e}")
    return uid in LOCAL_STAFF


def remove_staff(user_id: str) -> bool:
    """Removes staff AND the premium that came with it. Returns False if the user was not staff."""
    uid = str(user_id)
    if not is_staff(uid):
        return False
    LOCAL_STAFF.discard(uid)
    remove_user_premium(uid)
    return True


def list_staff() -> List[str]:
    ids = set(LOCAL_STAFF)
    if db_premium is not None:
        try:
            ids = {str(d["discord_id"]) for d in db_premium.find({"staff": True})}
        except Exception as e:
            print(f"[Mongo Staff List Error] {e}")
    return sorted(ids)


def remove_user_premium(user_id: str):
    """Removes premium status from MongoDB or local storage."""
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


def add_case(guild_id, user_id, action: str, reason: str, moderator_id, duration: Optional[str] = None) -> dict:
    case = {
        "guild_id": str(guild_id),
        "case_id": next_case_id(guild_id),
        "user_id": str(user_id),
        "action": action,
        "reason": reason,
        "moderator_id": str(moderator_id),
        "duration": duration,
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
                               case_id: Optional[int] = None, duration: Optional[str] = None) -> bool:
    """DMs the moderated user. The moderator is never included (privacy). Returns True if delivered."""
    titles = {
        "warn": "You were warned",
        "mute": "You were muted",
        "unmute": "Your mute was removed",
        "kick": "You were kicked",
        "ban": "You were banned",
    }
    embed = discord.Embed(title=f"{titles.get(action, 'Moderation notice')} in {guild.name}", color=COLOR_RED)
    embed.add_field(name="Reason", value=(reason or "No reason provided")[:1000], inline=False)
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
GIVEAWAY_BANNER_URL = os.getenv(
    "GIVEAWAY_BANNER_URL",
    "https://cdn.discordapp.com/attachments/1564516558926123018/1567425359517978645/F3364D80-B8EA-45D9-9AC8-AF9C21B59C3F.png?ex=6aec221f&is=6aead09f&hm=26c665bf4404913fee1bd3ec6156dbc0721d0a09e34311ad788a91eac0fc22bb&"
).strip()


def build_giveaway_embed(gw: dict) -> discord.Embed:
    end_ts = int(gw["end_ts"])
    host = f"<@{gw['host_id']}>"
    if gw.get("ended"):
        winner_ids = gw.get("winner_ids", [])
        winners_text = "\n".join(f"<@{w}>" for w in winner_ids) if winner_ids else "No valid entries"
        embed = discord.Embed(
            title=gw["prize"],
            description=f"**Ended:** <t:{end_ts}:R>\n**Hosted by:** {host}\n**Winners:** {winners_text}",
            color=COLOR_GRAY
        )
        embed.set_footer(text=f"{len(gw['entries'])} entries | Giveaway ended")
    else:
        embed = discord.Embed(
            title=gw["prize"],
            description=f"Click **Enter** to join the giveaway!\n\n**Ends:** <t:{end_ts}:R>\n**Hosted by:** {host}\n**Winners:** `{int(gw['winners'])}`",
            color=COLOR_PURPLE
        )
    if GIVEAWAY_BANNER_URL:
        embed.set_image(url=GIVEAWAY_BANNER_URL)
    return embed


class GiveawayView(discord.ui.View):
    """Persistent Enter / Entries buttons. The giveaway is found by the message the button is on."""

    def __init__(self, ended: bool = False):
        super().__init__(timeout=None)
        if ended:
            self.enter_btn.disabled = True

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, custom_id="giveaway_enter")
    async def enter_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
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
            embed = embed_info("Entry Removed", f"You left the giveaway for **{gw['prize']}**.\nClick **Enter** again to rejoin.", COLOR_GOLD)
        else:
            gw["entries"].append(uid)
            save_giveaway(gw)
            embed = embed_ok("You Are In!", f"You entered the giveaway for **{gw['prize']}**.\nTotal entries: `{len(gw['entries'])}`\nClick **Enter** again to leave.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Entries", style=discord.ButtonStyle.secondary, custom_id="giveaway_entries")
    async def entries_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gw = GIVEAWAYS.get(str(interaction.message.id))
        if not gw:
            await interaction.response.send_message(embed=embed_err("Giveaway Not Found", "This giveaway is no longer tracked."), ephemeral=True)
            return
        lines = [f"<@{uid}>" for uid in gw["entries"]]
        pages = build_pages(
            "Giveaway Entries", lines, per_page=20,
            header=f"**{gw['prize']}**\nTotal entries: `{len(lines)}`\n\n"
        )
        await send_pages(interaction, pages, ephemeral=True)


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
        title = "New Winner" if rerolled else "Giveaway Winner"
        if len(winners) > 1:
            title += "s"
        embed = embed_ok(title, f"Congratulations {mentions}! You won **{gw['prize']}**.{jump}")
        content = mentions
    else:
        embed = embed_err("No Winners", f"There were no valid entries for **{gw['prize']}**.{jump}")
        content = None

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
        return False


async def finish_giveaway(bot: commands.Bot, gw: dict, ended_early: bool = False) -> List[str]:
    if gw.get("ended"):
        return gw.get("winner_ids", [])

    valid = await valid_giveaway_entries(bot, gw)
    winners = random.sample(valid, min(int(gw["winners"]), len(valid))) if valid else []

    gw["ended"] = True
    gw["winner_ids"] = winners
    if ended_early:
        gw["end_ts"] = datetime.now(timezone.utc).timestamp()
    save_giveaway(gw)

    message = await get_giveaway_message(bot, gw)
    if message:
        try:
            await message.edit(embed=build_giveaway_embed(gw), view=GiveawayView(ended=True))
        except Exception as e:
            print(f"[Giveaway Edit Error] {e}")
    await announce_giveaway_winners(bot, gw, winners, message=message)
    return winners


async def reroll_giveaway(bot: commands.Bot, gw: dict, count: Optional[int] = None) -> Optional[List[str]]:
    """Picks new winners (previous winners are skipped). Returns None if nobody else can win."""
    previous = tuple(gw.get("winner_ids", []))
    valid = await valid_giveaway_entries(bot, gw, exclude=previous)
    if not valid:
        return None
    pick = int(count or gw["winners"])
    winners = random.sample(valid, min(pick, len(valid)))
    gw["winner_ids"] = winners
    save_giveaway(gw)

    message = await get_giveaway_message(bot, gw)
    if message:
        try:
            await message.edit(embed=build_giveaway_embed(gw), view=GiveawayView(ended=True))
        except Exception as e:
            print(f"[Giveaway Edit Error] {e}")
    await announce_giveaway_winners(bot, gw, winners, rerolled=True, message=message)
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
LOCAL_PRICES: Dict[str, int] = {}


def get_prices() -> dict:
    """Current prices. dev_product_robux stays None until it is known (set by /change-price)."""
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
                 max_uses: Optional[int] = None) -> Optional[dict]:
    """Creates a code. Returns None if the code already exists."""
    code = normalize_promo_code(code)
    doc = {
        "code": code,
        "points": int(points),
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
    updates = {k: v for k, v in updates.items() if k in ("points", "expires_at", "max_uses")}
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


async def send_promo_log(bot: commands.Bot, user, promo: dict, old_points: int, new_points: int, where: str):
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

    embed = discord.Embed(
        title="Promo Code Redeemed",
        description=f"{user.mention} redeemed a promo code.",
        color=COLOR_GREEN,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User", value=f"`{user}`\n`{user.id}`", inline=True)
    embed.add_field(name="Code", value=f"`{promo['code']}`", inline=True)
    embed.add_field(name="Reward", value=f"`+{int(promo['points']):,} pts`", inline=True)
    embed.add_field(name="Previous Balance", value=f"`{old_points:,} pts`", inline=True)
    embed.add_field(name="New Balance", value=f"`{new_points:,} pts`", inline=True)
    embed.add_field(name="Code Uses", value=f"`{uses}`", inline=True)
    embed.add_field(name="Code Expires", value=f"<t:{int(expires)}:R>" if expires else "`Never`", inline=True)
    embed.add_field(name="Redeemed In", value=f"`{where}`", inline=True)
    embed.set_thumbnail(url=user.display_avatar.url)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[Promo Log Error] {e}")


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
    def __init__(self, bot=None, submitting_user_id: str = "0", target_guild_id: str = "0", layout: dict = None, dev_product_url: str = "", points_cost: int = 100):
        super().__init__(timeout=None)
        self.bot = bot
        # The price shown in the DM is the price charged, even if /change-price runs later
        self.points_cost = int(points_cost)
        self.use_points_btn.label = f"Use Points ({self.points_cost:,} Pts)"
        self.submitting_user_id = str(submitting_user_id)
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}

        final_url = dev_product_url or "https://www.roblox.com/games/6094950399/Template-Engine#!/store"
        self.add_item(discord.ui.Button(label="Buy Roblox Dev Product", url=final_url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="Verify Purchase", style=discord.ButtonStyle.success, custom_id="btn_verify_dev_product")
    async def verify_purchase_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)

        # Safety check first, so a blocked user never uses up a purchase
        early_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0
        early_guild = await fetch_target_guild(self.bot or interaction.client, early_gid)
        if not early_guild:
            await interaction.followup.send("Error: The bot is no longer in the target server.", ephemeral=True)
            return
        allowed, reason = await can_build_in_guild(early_guild, interaction.user.id)
        if not allowed:
            await interaction.followup.send(embed=build_denied_embed(early_guild.name, reason), ephemeral=True)
            return

        roblox_id = None
        if db_users is not None:
            doc = db_users.find_one({"discord_id": user_id})
            if doc and "roblox_data" in doc:
                roblox_id = doc["roblox_data"].get("id")

        if not roblox_id:
            embed = discord.Embed(
                title="Roblox Account Unlinked",
                description="Please sync your Roblox account via the account sync channel before verifying purchases.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        dev_product_id = os.getenv("ROBLOX_DEV_PRODUCT_ID", "").strip()
        open_cloud_key = os.getenv("ROBLOX_OPEN_CLOUD_KEY", "").strip()
        cookie_headers = get_roblox_cookie_header()

        is_verified = False
        receipt_token = None

        # 1. PRIMARY CHECK: ROBLOX COOKIE ECONOMY API SALES AUDIT
        if cookie_headers and roblox_id:
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"https://economy.roblox.com/v2/users/{roblox_id}/transaction-history?transactionType=Purchase&limit=50"
                    async with session.get(url, headers=cookie_headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            transactions = data.get("data", [])
                            if isinstance(transactions, list):
                                for item in transactions:
                                    tx_id = str(item.get("id") or item.get("transactionId") or "").strip()
                                    details = item.get("details") or {}
                                    item_id = str(details.get("id") or item.get("targetId") or "").strip()
                                    created_time = item.get("created", "")

                                    unique_tx_key = f"cookie_tx_{tx_id}_{created_time}"

                                    if (not dev_product_id or item_id == dev_product_id) and unique_tx_key not in USED_DEV_PRODUCT_RECEIPTS:
                                        is_verified = True
                                        receipt_token = unique_tx_key
                                        break
            except Exception as cookie_err:
                print(f"[Roblox Cookie Economy API Exception] {cookie_err}")

        # 2. FALLBACK CHECK: OPEN CLOUD API
        if not is_verified and open_cloud_key and dev_product_id:
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"https://apis.roblox.com/developer-products/v1/developer-products/{dev_product_id}/purchases?userId={roblox_id}"
                    headers = {"x-api-key": open_cloud_key}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            purchases = data.get("purchases", [])
                            if isinstance(purchases, list):
                                for p in purchases:
                                    r_id = str(p.get("purchaseToken") or p.get("receipt") or "").strip()
                                    p_prod_id = str(p.get("developerProductId") or p.get("productId") or "").strip()
                                    
                                    unique_opencloud_key = f"oc_{r_id}"

                                    if r_id and (not dev_product_id or p_prod_id == dev_product_id) and unique_opencloud_key not in USED_DEV_PRODUCT_RECEIPTS:
                                        is_verified = True
                                        receipt_token = unique_opencloud_key
                                        break
            except Exception as oc_err:
                print(f"[Dev Product Verify OpenCloud Exception] {oc_err}")

        if not is_verified:
            embed = discord.Embed(
                title="Purchase Verification Failed",
                description="No unused Roblox purchase transaction was detected for your synced Roblox account.\n\n"
                            "• Make sure you purchased the Developer Product on Roblox using your synced account.\n"
                            "• Each purchase transaction can only be redeemed **ONCE** for a server deployment.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if receipt_token:
            USED_DEV_PRODUCT_RECEIPTS.add(receipt_token)

        bot_inst = self.bot or interaction.client
        target_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0

        guild = bot_inst.get_guild(target_gid) if target_gid else None
        if not guild and target_gid:
            try:
                guild = await bot_inst.fetch_guild(target_gid)
            except Exception:
                guild = None

        if not guild:
            await interaction.followup.send("Error: The bot is no longer in the target server.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Purchase Verified! Deploying Layout...",
            description=f"Single-use purchase transaction verified (`{receipt_token[:18]}...`). Now building {guild.name}!",
            color=0x2ECC71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        cog = bot_inst.get_cog("FeaturesCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)

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
            await cog.execute_auto_build(guild, self.layout)



# -------------------------------------------------------------
# FEATURES COG: points listener, welcome bonus, auto DM, auto build
# -------------------------------------------------------------
class FeaturesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------
    # BACKGROUND SCHEDULER: reminders and giveaway endings
    # -------------------------------------------------------------
    async def cog_load(self):
        load_runtime_state()
        self.scheduler_loop.start()

    async def cog_unload(self):
        self.scheduler_loop.cancel()

    @tasks.loop(seconds=10)
    async def scheduler_loop(self):
        try:
            await self.process_reminders()
        except Exception as e:
            print(f"[Reminder Loop Error] {e}")
        try:
            await self.process_giveaways()
        except Exception as e:
            print(f"[Giveaway Loop Error] {e}")

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
        for gw in [g for g in GIVEAWAYS.values() if not g.get("ended") and g["end_ts"] <= now]:
            try:
                await finish_giveaway(self.bot, gw)
            except Exception as e:
                print(f"[Giveaway Finish Error] {gw.get('message_id')}: {e}")

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
    # EVENT LISTENER: WELCOME BONUS (20 POINTS)
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        add_user_points(str(member.id), 20)
        print(f"[Welcome Bonus] Awarded 20 points to new member: {member.name} ({member.id})")

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
            payload = json.loads(attachment_bytes.decode("utf-8"))
        except Exception as e:
            print(f"[Auto-Build Parsing Error] Could not read JSON attachment: {e}")
            return

        submitting_user_id = str(payload.get("submitting_user_id", "0"))
        target_guild_id = str(payload.get("target_server_id", "0")).strip()
        layout = payload.get("layout", {})
        is_premium_submission = payload.get("is_premium", False)

        if not target_guild_id or target_guild_id == "0":
            print("[Auto-Build Error] Target server ID is missing or 0.")
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
            await self.notify_missing_bot(submitting_user_id, target_guild_id, layout)
            return

        allowed, reason = await can_build_in_guild(guild, submitting_user_id)
        if not allowed:
            await self.notify_build_denied(submitting_user_id, guild.name, reason)
            return

        if not is_premium_submission and str(message.channel.id) != str(PREMIUM_DESIGN_CHANNEL_ID):
            print(f"[Frontend Direct DM] Initiating direct DM with 5 retries to submitter {submitting_user_id} before logging...")
            await self.send_non_premium_verification_dm(submitting_user_id, target_guild_id, layout, guild.name, message.channel)
        else:
            print(f"[Auto-Build] Premium deployment for {guild.name} ({guild.id})")
            await self.execute_auto_build(guild, layout)

    async def send_non_premium_verification_dm(self, user_id: str, target_guild_id: str, layout: dict, guild_name: str, fallback_channel: Optional[discord.TextChannel] = None):
        dev_product_url = os.getenv("ROBLOX_DEV_PRODUCT_URL", "https://www.roblox.com/games/6094950399/Template-Engine#!/store").strip()
        user_pts = get_user_points(user_id)
        prices = get_prices()
        points_cost = prices["points_cost"]
        dev_price = prices["dev_product_robux"]

        embed = discord.Embed(
            title="Server Deployment Options",
            description=f"<@{user_id}> Your blueprint for **{guild_name}** has been submitted!\n\nTo build this layout, select one of the deployment options below:",
            color=0x8B5CF6
        )
        embed.add_field(
            name="Option 1: Buy Dev Product",
            value=(
                f"Purchase the single-use Roblox Developer Product (**{dev_price:,} Robux**), then click **Verify Purchase**."
                if dev_price else "Purchase the single-use Roblox Developer Product, then click **Verify Purchase**."
            ),
            inline=False
        )
        embed.add_field(
            name="Option 2: Redeem Points",
            value=f"Use **{points_cost:,} server points** to deploy immediately.\nYour Points: {user_pts:,} pts",
            inline=False
        )

        view = NonPremiumDeploymentView(
            bot=self.bot,
            submitting_user_id=user_id,
            target_guild_id=target_guild_id,
            layout=layout,
            dev_product_url=dev_product_url,
            points_cost=points_cost
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

    async def execute_auto_build(self, guild: discord.Guild, layout: dict):
        try:
            await self.nuke_guild_structure(guild)

            created_roles = {}
            roles_data = layout.get("roles", [])

            for r_info in roles_data:
                role_name = strip_emojis(r_info.get("name", "Role"))
                if not role_name:
                    role_name = str(r_info.get("name", "Role"))
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

                try:
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
                        chan_type = chan.get("type", "text")
                        chan_topic = chan.get("topic", "")

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
        except Exception as top_err:
            print(f"[Auto-Build Execution Error] {top_err}")


async def setup(bot: commands.Bot):
    # Register persistent views for static panels
    bot.add_view(RobloxVerificationView())
    bot.add_view(RobloxUnsyncView())
    bot.add_view(PremiumMissingBotView())
    bot.add_view(GiveawayView())
    await bot.add_cog(FeaturesCog(bot))
