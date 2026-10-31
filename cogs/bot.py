import os
import re
import json
import random
import asyncio
import aiohttp
import discord
from datetime import datetime, timezone, timedelta
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient

# --- CONFIGURATION & CONSTANTS ---
BOT_OWNER_ID = 1219266886143967245
GIVEAWAY_BANNER_URL = "https://res.cloudinary.com/dtz0urit6/image/upload/f_png,q_auto/cloudinary-tools-uploads/qxpfnwtebf6jqybaayjo.png"

# Dedicated Logging Channels
CHANNEL_LOG_WEBSITE_LOGIN = 1563524100276617248      # Website entry-Login ONLY
CHANNEL_LOG_NON_PREMIUM_DESIGNS = 1563524101522464900 # Designs (non-premium only)
CHANNEL_LOG_PREMIUM_DESIGNS = 1563544108696477802     # Designs (Premium only)
CHANNEL_LOG_SUBSCRIPTIONS = 1564262413514182739       # Subscriptions (New, Renewed, Expired)

MONGO_URI = os.getenv("MONGO_URI")
ROBLOX_OPEN_CLOUD_KEY = (os.getenv("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_COOKIE = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()
ROBLOX_UNIVERSE_ID = (os.getenv("ROBLOX_UNIVERSE_ID") or "").strip()

def get_formatted_roblox_cookie():
    if not ROBLOX_COOKIE:
        return ""
    clean = ROBLOX_COOKIE.strip('"\'')
    if clean.startswith(".ROBLOSECURITY="):
        return clean
    return f".ROBLOSECURITY={clean}"

# --- DATABASE SETUP ---
db_premium = None
db_users = None
db_giveaways = None
db_settings = None
db_sub_tracker = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client["static_studio_db"]
        db_premium = db["premium_users"]
        db_users = db["persistent_users"]
        db_giveaways = db["giveaways"]
        db_settings = db["guild_settings"]
        db_sub_tracker = db["roblox_subscription_tracker"]
    except Exception as e:
        print(f"[Bot Mongo Warning] Could not connect to MongoDB: {e}")

LOCAL_PERSISTENT_USERS = {}
LOCAL_PREMIUM_USERS = {}
LOCAL_GIVEAWAYS = {}
LOCAL_GUILD_SETTINGS = {}
LOCAL_SUB_TRACKER = {}

# --- HELPER FUNCTIONS ---
def parse_duration_to_timedelta(duration_str: str) -> timedelta:
    if not duration_str or duration_str.lower() in ["inf", "infinite"]:
        return timedelta(days=36500)
    match = re.match(r"^(\d+)\s*(s|h|d|mo|y)$", duration_str.strip().lower())
    if not match:
        return timedelta(days=30)
    val, unit = match.groups()
    num = int(val)
    if unit == "s": return timedelta(seconds=num)
    elif unit == "h": return timedelta(hours=num)
    elif unit == "d": return timedelta(days=num)
    elif unit == "mo": return timedelta(days=num * 30)
    elif unit == "y": return timedelta(days=num * 365)
    return timedelta(days=30)

def is_user_premium(user_id: str) -> bool:
    uid = str(user_id)
    if int(uid) == BOT_OWNER_ID:
        return True
    now = datetime.now(timezone.utc)
    if db_premium is not None:
        doc = db_premium.find_one({"user_id": uid})
        if doc and "expires_at" in doc:
            expires_at = doc["expires_at"]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return expires_at > now
        return False
    else:
        doc = LOCAL_PREMIUM_USERS.get(uid)
        if doc:
            exp = doc.get("expires_at")
            if isinstance(exp, str): exp = datetime.fromisoformat(exp)
            if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
            return exp > now
        return False

# --- SEPARATED LOGGING FUNCTIONS ---

async def log_website_login(bot: commands.Bot, user: discord.User, ip_address: str = "Hidden/Proxy"):
    """Logs ONLY when a user enters/logs into the website."""
    is_prem = is_user_premium(str(user.id))
    now = datetime.now(timezone.utc)

    login_embed = discord.Embed(
        title="Website Entry Login",
        description=(
            f"**User:** {user.mention} (`{user.id}`)\n"
            f"**Username:** `{user.name}`\n"
            f"**Status:** {'**TE Premium**' if is_prem else '**Standard User**'}\n"
            f"**Access Time:** <t:{int(now.timestamp())}:F>"
        ),
        color=0xF1C40F if is_prem else 0x3b82f6,
        timestamp=now
    )
    login_embed.set_footer(text="Template Engine Website Analytics")

    ch_login = bot.get_channel(CHANNEL_LOG_WEBSITE_LOGIN)
    if ch_login:
        try:
            await ch_login.send(embed=login_embed)
        except Exception as e:
            print(f"[Log Error - Website Login] {e}")

async def log_submitted_design(bot: commands.Bot, user: discord.User, guild: discord.Guild, design_title: str, categories_count: int, channels_count: int):
    """Logs submitted designs strictly to Non-Premium OR Premium design channels."""
    is_prem = is_user_premium(str(user.id))
    now = datetime.now(timezone.utc)

    design_embed = discord.Embed(
        title="New Design Submitted",
        description=(
            f"**Design Title:** `{design_title}`\n"
            f"**Creator:** {user.mention} (`{user.id}`)\n"
            f"**Target Guild:** {guild.name if guild else 'Web Dashboard'} (`{guild.id if guild else 'N/A'}`)\n"
            f"**Tier:** {'**TE Premium**' if is_prem else '**Standard Member**'}\n"
            f"**Structure:** `{categories_count}` Categories | `{channels_count}` Channels"
        ),
        color=0xF1C40F if is_prem else 0x3b82f6,
        timestamp=now
    )
    design_embed.set_footer(text="Template Engine Design Repository")

    if is_prem:
        ch_premium = bot.get_channel(CHANNEL_LOG_PREMIUM_DESIGNS)
        if ch_premium:
            try:
                await ch_premium.send(embed=design_embed)
            except Exception as e:
                print(f"[Log Error - Premium Designs] {e}")
    else:
        ch_non_premium = bot.get_channel(CHANNEL_LOG_NON_PREMIUM_DESIGNS)
        if ch_non_premium:
            try:
                await ch_non_premium.send(embed=design_embed)
            except Exception as e:
                print(f"[Log Error - Non-Premium Designs] {e}")

# --- CORE AUTO-BUILD LOGIC ENGINE ---
async def build_layout_for_guild(bot: commands.Bot, guild: discord.Guild, user: discord.User, layout_data: dict) -> dict:
    """Core auto-build logic engine for premium users."""
    user_id_str = str(user.id)
    if not is_user_premium(user_id_str):
        return {"success": False, "error": "TE Premium status is required to use the auto-builder."}

    if not layout_data or "categories" not in layout_data:
        return {"success": False, "error": "Invalid layout template configuration structure."}

    categories_created = 0
    channels_created = 0

    try:
        for cat_data in layout_data.get("categories", []):
            cat_name = cat_data.get("name", "CATEGORY")
            category = await guild.create_category(name=cat_name)
            categories_created += 1

            for ch_data in cat_data.get("channels", []):
                ch_name = ch_data.get("name", "channel")
                ch_type = ch_data.get("type", "text")
                ch_topic = ch_data.get("topic", "")

                if ch_type == "voice":
                    await guild.create_voice_channel(name=ch_name, category=category)
                else:
                    await guild.create_text_channel(name=ch_name, category=category, topic=ch_topic)
                channels_created += 1
                await asyncio.sleep(0.4)

        await log_submitted_design(
            bot=bot,
            user=user,
            guild=guild,
            design_title=layout_data.get("name", "Custom Design"),
            categories_count=categories_created,
            channels_count=channels_created
        )

        return {
            "success": True,
            "layout_name": layout_data.get("name", "Custom Layout"),
            "categories_created": categories_created,
            "channels_created": channels_created
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scan_roblox_subscriptions.start()

    def cog_unload(self):
        self.scan_roblox_subscriptions.cancel()

    def check_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == BOT_OWNER_ID

    # --- AUTOMATED ROBLOX SUBSCRIPTION TRACKER TASK ---
    @tasks.loop(hours=6.0)
    async def scan_roblox_subscriptions(self):
        """Scans Roblox subscriptions via cookie and logs New, Renewed, or Expired states."""
        cookie = get_formatted_roblox_cookie()
        if not cookie or not ROBLOX_UNIVERSE_ID or not ROBLOX_SUBSCRIPTION_ID:
            return

        log_channel = self.bot.get_channel(CHANNEL_LOG_SUBSCRIPTIONS)
        if not log_channel:
            return

        now = datetime.now(timezone.utc)
        url = f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/subscriptions/{ROBLOX_SUBSCRIPTION_ID}/subscribers"
        headers = {"Cookie": cookie, "User-Agent": "Mozilla/5.0"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            subscribers = data.get("subscribers", [])
            active_roblox_ids = set()

            for sub in subscribers:
                roblox_id = str(sub.get("user", {}).get("id"))
                state = str(sub.get("state", "")).upper()
                expiration_raw = sub.get("expirationTime")

                if not roblox_id:
                    continue

                active_roblox_ids.add(roblox_id)

                record = db_sub_tracker.find_one({"roblox_id": roblox_id}) if db_sub_tracker is not None else LOCAL_SUB_TRACKER.get(roblox_id)

                if not record:
                    new_doc = {
                        "roblox_id": roblox_id,
                        "state": state,
                        "last_seen": now.isoformat(),
                        "expiration_time": expiration_raw,
                        "renew_count": 0,
                        "logged_expired": False
                    }
                    if db_sub_tracker is not None:
                        db_sub_tracker.insert_one(new_doc)
                    else:
                        LOCAL_SUB_TRACKER[roblox_id] = new_doc

                    embed = discord.Embed(
                        title="NEW SUBSCRIPTION DETECTED",
                        description=f"**Roblox User ID:** `{roblox_id}`\n**State:** `{state}`\n**Cycle Expires:** `{expiration_raw or 'N/A'}`",
                        color=0x2ecc71,
                        timestamp=now
                    )
                    embed.set_footer(text="Roblox Subscription System - New Purchase")
                    await log_channel.send(embed=embed)

                else:
                    old_expiration = record.get("expiration_time")

                    if expiration_raw and old_expiration and expiration_raw != old_expiration and state in ["ACTIVE", "RENEWING"]:
                        renew_count = record.get("renew_count", 0) + 1
                        update_fields = {
                            "state": state,
                            "last_seen": now.isoformat(),
                            "expiration_time": expiration_raw,
                            "renew_count": renew_count,
                            "logged_expired": False
                        }
                        if db_sub_tracker is not None:
                            db_sub_tracker.update_one({"roblox_id": roblox_id}, {"$set": update_fields})
                        else:
                            LOCAL_SUB_TRACKER[roblox_id].update(update_fields)

                        embed = discord.Embed(
                            title="SUBSCRIPTION RENEWED",
                            description=f"**Roblox User ID:** `{roblox_id}`\n**Renewal Count:** `#{renew_count}`\n**New Cycle Expiration:** `{expiration_raw}`",
                            color=0x8b5cf6,
                            timestamp=now
                        )
                        embed.set_footer(text="Roblox Subscription System - Automatic Renewal")
                        await log_channel.send(embed=embed)
                    else:
                        if db_sub_tracker is not None:
                            db_sub_tracker.update_one({"roblox_id": roblox_id}, {"$set": {"last_seen": now.isoformat(), "state": state}})

            all_records = list(db_sub_tracker.find()) if db_sub_tracker is not None else list(LOCAL_SUB_TRACKER.values())
            for record in all_records:
                r_id = record["roblox_id"]
                if r_id not in active_roblox_ids and not record.get("logged_expired"):
                    exp_raw = record.get("expiration_time")
                    if exp_raw:
                        exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
                        if exp_dt.tzinfo is None: exp_dt = exp_dt.replace(tzinfo=timezone.utc)

                        if now >= (exp_dt + timedelta(days=1)):
                            if db_sub_tracker is not None:
                                db_sub_tracker.update_one({"roblox_id": r_id}, {"$set": {"logged_expired": True, "state": "EXPIRED"}})
                            else:
                                LOCAL_SUB_TRACKER[r_id]["logged_expired"] = True

                            embed = discord.Embed(
                                title="SUBSCRIPTION EXPIRED",
                                description=f"**Roblox User ID:** `{r_id}`\n**Expired On:** `<t:{int(exp_dt.timestamp())}:F>`\n**Grace Period:** 24h Elapsed (Confirmed Non-Renewed Expiration)",
                                color=0xf43f5e,
                                timestamp=now
                            )
                            embed.set_footer(text="Roblox Subscription System - Non-Renewed Expiration")
                            await log_channel.send(embed=embed)

        except Exception as e:
            print(f"[Subscription Scanner Exception] {e}")

    @scan_roblox_subscriptions.before_loop
    async def before_scan_subscriptions(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(BotCog(bot))
