import os
import re
import io
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
CLIENT_ID = (os.getenv("DISCORD_CLIENT_ID") or "").strip()

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

def calculate_account_age_days(discord_id: str) -> str:
    try:
        snowflake = int(discord_id)
        timestamp_ms = (snowflake >> 22) + 1420070400000
        created_at = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        days = (now - created_at).days
        return f"{days} days"
    except Exception:
        return "Unknown"

async def build_layout_for_guild(bot: commands.Bot, guild: discord.Guild, layout_data: dict) -> dict:
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

class RetryBuildView(discord.ui.View):
    def __init__(self, bot, target_server_id: str, payload: dict):
        super().__init__(timeout=86400)
        self.bot = bot
        self.target_server_id = target_server_id
        self.payload = payload

        invite_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot%20applications.commands&guild_id={target_server_id}"
        self.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="Retry Auto-Deploy", style=discord.ButtonStyle.primary, custom_id="btn_retry_autobuild")
    async def retry_build(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = self.bot.get_guild(int(self.target_server_id))
        if not guild:
            await interaction.followup.send(
                f"The bot is still not present in server ID `{self.target_server_id}`. Please invite the bot first, then click retry.",
                ephemeral=True
            )
            return

        res = await build_layout_for_guild(self.bot, guild, self.payload)
        if res.get("success"):
            await interaction.followup.send(
                f"Auto-build successful! Created {res.get('categories_created')} categories & {res.get('channels_created')} channels in **{guild.name}**.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"Auto-build error: `{res.get('error')}`", ephemeral=True)

class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scan_roblox_subscriptions.start()

    def cog_unload(self):
        self.scan_roblox_subscriptions.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id != CHANNEL_LOG_PREMIUM_DESIGNS and message.channel.id != CHANNEL_LOG_NON_PREMIUM_DESIGNS:
            return

        if "AUTOBUILD_PAYLOAD_START" in message.content:
            try:
                raw_json = message.content.split("```json")[1].split("```")[0].strip() if "```json" in message.content else message.content.split("```")[1].split("```")[0].strip()
                payload = json.loads(raw_json)

                # Attach blueprint file to the log message
                blueprint_data = payload.get("layout", payload)
                json_bytes = json.dumps(blueprint_data, indent=2).encode("utf-8")
                file_attachment = discord.File(io.BytesIO(json_bytes), filename="blueprint.json")

                target_server_id = payload.get("target_server_id")
                user_id = message.embeds[0].fields[1].value.strip("`") if message.embeds and len(message.embeds[0].fields) > 1 else None

                guild = self.bot.get_guild(int(target_server_id)) if target_server_id and str(target_server_id).isdigit() else None

                # Bot not in target server -> Notify User via DM & Log Warning in Channel
                if not guild:
                    if user_id:
                        try:
                            user_obj = await self.bot.fetch_user(int(user_id))
                            if user_obj:
                                dm_embed = discord.Embed(
                                    title="BOT NOT FOUND IN SERVER",
                                    description=(
                                        f"Your auto-build request for Target Guild ID `{target_server_id}` could not start because the bot is not in your server.\n\n"
                                        f"Please click **Add Bot to Server** below to invite it, then click **Retry Auto-Deploy**."
                                    ),
                                    color=0xf43f5e
                                )
                                view = RetryBuildView(self.bot, str(target_server_id), payload)
                                await user_obj.send(embed=dm_embed, view=view)
                        except Exception as dm_err:
                            print(f"[DM User Error] {dm_err}")

                    log_warn_embed = discord.Embed(
                        title="AUTO-BUILD PENDING (BOT NOT IN GUILD)",
                        description=f"User <@{user_id}> submitted a design for Target Guild `{target_server_id}`, but the bot is not present in that server. A DM prompt was sent to the user.",
                        color=0xf43f5e,
                        timestamp=datetime.now(timezone.utc)
                    )
                    await message.channel.send(embed=log_warn_embed, file=file_attachment)
                    return

                # Execute auto-build for premium users
                res = await build_layout_for_guild(self.bot, guild, payload)
                if res.get("success"):
                    confirm_embed = discord.Embed(
                        title="AUTO-BUILD EXECUTED SUCCESSFULLY",
                        description=f"Created `{res.get('categories_created')}` categories and `{res.get('channels_created')}` channels in **{guild.name}** (`{guild.id}`).",
                        color=0x2ecc71,
                        timestamp=datetime.now(timezone.utc)
                    )
                    await message.channel.send(embed=confirm_embed, file=file_attachment)

            except Exception as e:
                print(f"[Auto-Build Execution Exception] {e}")

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
            for sub in subscribers:
                roblox_id = str(sub.get("user", {}).get("id"))
                state = str(sub.get("state", "")).upper()
                expiration_raw = sub.get("expirationTime")

                if not roblox_id:
                    continue

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
                        description=f"Roblox User ID: `{roblox_id}`\nState: `{state}`\nExpiration: `{expiration_raw or 'N/A'}`",
                        color=0x2ecc71,
                        timestamp=now
                    )
                    await log_channel.send(embed=embed)

        except Exception as e:
            print(f"[Subscription Scanner Exception] {e}")

    @scan_roblox_subscriptions.before_loop
    async def before_scan_subscriptions(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(BotCog(bot))
