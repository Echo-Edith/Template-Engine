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

BOT_OWNER_ID = 1219266886143967245

CHANNEL_LOG_WEBSITE_LOGIN = 1563524100276617248
CHANNEL_LOG_NON_PREMIUM_DESIGNS = 1563524101522464900
CHANNEL_LOG_PREMIUM_DESIGNS = 1563544108696477802
CHANNEL_LOG_SUBSCRIPTIONS = 1564262413514182739

MONGO_URI = os.getenv("MONGO_URI")
ROBLOX_COOKIE = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_UNIVERSE_ID = (os.getenv("ROBLOX_UNIVERSE_ID") or "").strip()

def get_formatted_roblox_cookie():
    if not ROBLOX_COOKIE:
        return ""
    clean = ROBLOX_COOKIE.strip('"\'')
    if clean.startswith(".ROBLOSECURITY="):
        return clean
    return f".ROBLOSECURITY={clean}"

db_premium = None
db_users = None
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
        db_sub_tracker = db["roblox_subscription_tracker"]
    except Exception as e:
        print(f"[Bot Mongo Warning] {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_SUB_TRACKER = {}

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

# Core Auto-Build Logic Engine
async def build_layout_for_guild(bot: commands.Bot, guild: discord.Guild, layout_data: dict) -> dict:
    """Creates all categories and text/voice channels in target server."""
    categories_created = 0
    channels_created = 0

    try:
        categories_data = layout_data.get("layout", {}).get("categories", [])
        if not categories_data and "categories" in layout_data:
            categories_data = layout_data.get("categories", [])

        for cat_data in categories_data:
            cat_name = cat_data.get("name", "CATEGORY")
            emoji = cat_data.get("emoji", "")
            full_cat_name = f"{emoji} {cat_name}".strip() if emoji else cat_name
            
            category = await guild.create_category(name=full_cat_name)
            categories_created += 1

            for ch_data in cat_data.get("channels", []):
                ch_name = ch_data.get("name", "channel")
                ch_emoji = ch_data.get("emoji", "")
                ch_type = ch_data.get("type", "text")
                ch_topic = ch_data.get("topic", "")
                
                clean_ch_name = f"{ch_emoji}-{ch_name}".strip("-") if ch_emoji else ch_name

                if ch_type == "voice":
                    await guild.create_voice_channel(name=clean_ch_name, category=category)
                else:
                    await guild.create_text_channel(name=clean_ch_name, category=category, topic=ch_topic)
                channels_created += 1
                await asyncio.sleep(0.4)

        return {
            "success": True,
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listens for premium auto-build dispatches sent from the website server."""
        if message.channel.id != CHANNEL_LOG_PREMIUM_DESIGNS:
            return

        if "AUTOBUILD_PAYLOAD_START" in message.content:
            try:
                raw_json = message.content.split("```json")[1].split("```")[0].strip() if "```json" in message.content else message.content.split("```")[1].split("```")[0].strip()
                payload = json.loads(raw_json)

                target_server_id = payload.get("target_server_id")
                if not target_server_id or not str(target_server_id).isdigit():
                    return

                guild = self.bot.get_guild(int(target_server_id))
                if not guild:
                    print(f"[Auto-Build] Bot is not in target guild ID: {target_server_id}")
                    return

                res = await build_layout_for_guild(self.bot, guild, payload)
                if res.get("success"):
                    print(f"[Auto-Build Success] Created {res.get('categories_created')} categories & {res.get('channels_created')} channels in guild {guild.name}")
            except Exception as e:
                print(f"[Auto-Build Execution Error] {e}")

    @tasks.loop(hours=6.0)
    async def scan_roblox_subscriptions(self):
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

        except Exception as e:
            print(f"[Subscription Scanner Exception] {e}")

    @scan_roblox_subscriptions.before_loop
    async def before_scan_subscriptions(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(BotCog(bot))
