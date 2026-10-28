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
MAIN_OWNER_GUILD_ID = 1284513257574629458

MONTHLY_ANALYTICS_CHANNEL_ID = 1564263447057670154
GIVEAWAY_BANNER_URL = "https://res.cloudinary.com/dtz0urit6/image/upload/f_png,q_auto/cloudinary-tools-uploads/qxpfnwtebf6jqybaayjo.png"

SYSTEM_LOG_WEBHOOK_URL = (os.getenv("SYSTEM_LOG_WEBHOOK_URL") or "").strip()
MONGO_URI = os.getenv("MONGO_URI")
ROBLOX_OPEN_CLOUD_KEY = (os.getenv("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_COOKIE = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()
ROBLOX_UNIVERSE_ID = (os.getenv("ROBLOX_UNIVERSE_ID") or "").strip()
ROBLOX_GROUP_ID = (os.getenv("ROBLOX_GROUP_ID") or "").strip()

def get_formatted_roblox_cookie():
    if not ROBLOX_COOKIE:
        return ""
    clean = ROBLOX_COOKIE.strip('"\'')
    if clean.startswith(".ROBLOSECURITY="):
        return clean
    return f".ROBLOSECURITY={clean}"

db_premium = None
db_build_access = None
db_users = None
db_giveaways = None
db_settings = None
db_premium_config = None
db_layout_ticket_config = None
db_pending_blueprints = None
db_pending_invites = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client["static_studio_db"]
        db_premium = db["premium_users"]
        db_build_access = db["build_authorized_users"]
        db_users = db["persistent_users"]
        db_giveaways = db["giveaways"]
        db_settings = db["guild_settings"]
        db_premium_config = db["premium_config"]
        db_layout_ticket_config = db["layout_ticket_config"]
        db_pending_blueprints = db["pending_blueprints"]
        db_pending_invites = db["pending_invites"]
    except Exception as e:
        print(f"[Bot Mongo Warning] Could not connect to MongoDB: {e}")

LOCAL_PERSISTENT_USERS = {}
LOCAL_PENDING_INVITES = {}
LOCAL_GIVEAWAYS = {}
LOCAL_GUILD_SETTINGS = {}
PENDING_VERIFICATION_CODES = {}
SERVER_INVITE_CACHE = {}

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

def set_guild_welcome_setting(guild_id: str, channel_id: str = None, message_template: str = None):
    g_id = str(guild_id)
    update_data = {}
    if channel_id is not None: update_data["welcome_channel_id"] = str(channel_id)
    if message_template is not None: update_data["welcome_message"] = message_template

    if db_settings is not None:
        db_settings.update_one({"guild_id": g_id}, {"$set": update_data}, upsert=True)
    else:
        current = LOCAL_GUILD_SETTINGS.get(g_id, {})
        current.update(update_data)
        LOCAL_GUILD_SETTINGS[g_id] = current

def get_guild_settings(guild_id: str) -> dict:
    g_id = str(guild_id)
    if db_settings is not None:
        return db_settings.find_one({"guild_id": g_id}) or {}
    return LOCAL_GUILD_SETTINGS.get(g_id, {})

def get_user_points(discord_id: str) -> int:
    uid = str(discord_id)
    if db_users is not None:
        doc = db_users.find_one({"discord_id": uid})
        return doc.get("points", 0) if doc else 0
    doc = LOCAL_PERSISTENT_USERS.get(uid, {})
    return doc.get("points", 0)

def add_user_points(discord_id: str, amount: int) -> int:
    uid = str(discord_id)
    now = datetime.now(timezone.utc).isoformat()
    if db_users is not None:
        db_users.update_one(
            {"discord_id": uid},
            {
                "$inc": {"points": amount},
                "$setOnInsert": {"discord_id": uid},
                "$set": {"updated_at": now}
            },
            upsert=True
        )
        doc = db_users.find_one({"discord_id": uid})
        return doc.get("points", 0)
    else:
        user_data = LOCAL_PERSISTENT_USERS.get(uid, {"discord_id": uid, "points": 0})
        user_data["points"] = user_data.get("points", 0) + amount
        user_data["updated_at"] = now
        LOCAL_PERSISTENT_USERS[uid] = user_data
        return user_data["points"]

def is_user_premium(user_id: str) -> bool:
    user_id_str = str(user_id)
    now = datetime.now(timezone.utc)
    if db_premium is not None:
        doc = db_premium.find_one({"user_id": user_id_str})
        if doc and "expires_at" in doc:
            expires_at = doc["expires_at"]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return expires_at > now
        return False
    return False

def get_linked_roblox_user(discord_id: str):
    discord_id_str = str(discord_id)
    if db_users is not None:
        doc = db_users.find_one({"discord_id": discord_id_str})
        if doc and "roblox_data" in doc:
            return doc["roblox_data"]
    else:
        doc = LOCAL_PERSISTENT_USERS.get(discord_id_str)
        if doc and "roblox_data" in doc:
            return doc["roblox_data"]
    return None

def save_user_roblox_link(discord_id: str, discord_username: str, roblox_id: str, roblox_username: str):
    discord_id_str = str(discord_id)
    now = datetime.now(timezone.utc).isoformat()
    roblox_data = {"id": str(roblox_id), "username": roblox_username}

    if db_users is not None:
        db_users.update_one(
            {"discord_id": discord_id_str},
            {
                "$set": {
                    "discord_id": discord_id_str,
                    "discord_data": {"id": discord_id_str, "username": discord_username},
                    "roblox_data": roblox_data,
                    "updated_at": now
                }
            },
            upsert=True
        )
    else:
        existing = LOCAL_PERSISTENT_USERS.get(discord_id_str, {})
        existing.update({
            "discord_id": discord_id_str,
            "discord_data": {"id": discord_id_str, "username": discord_username},
            "roblox_data": roblox_data,
            "updated_at": now
        })
        LOCAL_PERSISTENT_USERS[discord_id_str] = existing

def remove_user_roblox_link(discord_id: str):
    discord_id_str = str(discord_id)
    if db_users is not None:
        db_users.update_one({"discord_id": discord_id_str}, {"$unset": {"roblox_data": ""}})
    else:
        if discord_id_str in LOCAL_PERSISTENT_USERS:
            LOCAL_PERSISTENT_USERS[discord_id_str].pop("roblox_data", None)

async def check_roblox_subscription_status(roblox_id: str) -> bool:
    clean_cookie = get_formatted_roblox_cookie()
    if clean_cookie and ROBLOX_UNIVERSE_ID and ROBLOX_SUBSCRIPTION_ID:
        cookie_headers = {"Cookie": clean_cookie, "User-Agent": "Mozilla/5.0"}
        url = f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/subscriptions/{ROBLOX_SUBSCRIPTION_ID}/user-status/{roblox_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=cookie_headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("isSubscribed") or data.get("state", "").upper() in ["ACTIVE", "RENEWING"]:
                            return True
        except Exception as e:
            print(f"[Alt Cookie Sub Check Error] {e}")

    if ROBLOX_OPEN_CLOUD_KEY and ROBLOX_SUBSCRIPTION_ID:
        url = f"https://apis.roblox.com/cloud/v2/users/{roblox_id}/subscriptions/{ROBLOX_SUBSCRIPTION_ID}"
        oc_headers = {"x-api-key": ROBLOX_OPEN_CLOUD_KEY}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=oc_headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        state = data.get("state", "").upper()
                        return state in ["ACTIVE", "RENEWING"]
        except Exception as e:
            print(f"[Roblox Open Cloud Sub Check Error] {e}")

    return False

async def fetch_roblox_revenue_data(timeframe: str = "1d") -> dict:
    timeframe_map = {"1d": "Day", "7d": "Week", "30d": "Month", "1y": "Year", "365d": "Year"}
    tf_param = timeframe_map.get(timeframe.lower(), "Day")

    primary_headers = {}
    fallback_headers = {}

    clean_cookie = get_formatted_roblox_cookie()
    if clean_cookie:
        primary_headers["Cookie"] = clean_cookie
        primary_headers["User-Agent"] = "Mozilla/5.0"

    if ROBLOX_OPEN_CLOUD_KEY:
        fallback_headers["x-api-key"] = ROBLOX_OPEN_CLOUD_KEY

    urls = []
    if ROBLOX_GROUP_ID:
        urls.append(f"https://economy.roblox.com/v1/groups/{ROBLOX_GROUP_ID}/revenue/summary/{tf_param}")
    if ROBLOX_UNIVERSE_ID:
        urls.append(f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/economy/revenue/summary/{tf_param}")

    if primary_headers and urls:
        try:
            async with aiohttp.ClientSession() as session:
                for url in urls:
                    async with session.get(url, headers=primary_headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return {
                                "sale_of_goods": data.get("itemSaleRevenue", data.get("itemSale", 0)),
                                "advance_rebates": data.get("publishingAdvanceRebates", 0),
                                "commissions": data.get("commissionRevenue", data.get("comission", 0)),
                                "payouts": data.get("groupPayouts", data.get("groupPayout", 0)),
                                "creator_rewards": data.get("creatorRewards", 0),
                                "total": data.get("totalRevenue", data.get("total", 0)),
                                "pending_robux": data.get("pendingRobux", 0),
                                "auth_source": "Alt Cookie (Primary)"
                            }
        except Exception as e:
            print(f"[Primary Cookie Revenue Error] {e}")

    if fallback_headers and urls:
        try:
            async with aiohttp.ClientSession() as session:
                for url in urls:
                    async with session.get(url, headers=fallback_headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return {
                                "sale_of_goods": data.get("itemSaleRevenue", data.get("itemSale", 0)),
                                "advance_rebates": data.get("publishingAdvanceRebates", 0),
                                "commissions": data.get("commissionRevenue", data.get("comission", 0)),
                                "payouts": data.get("groupPayouts", data.get("groupPayout", 0)),
                                "creator_rewards": data.get("creatorRewards", 0),
                                "total": data.get("totalRevenue", data.get("total", 0)),
                                "pending_robux": data.get("pendingRobux", 0),
                                "auth_source": "Open Cloud Key (Fallback)"
                            }
        except Exception as e:
            print(f"[Fallback Open Cloud Revenue Error] {e}")

    return {"sale_of_goods": 0, "advance_rebates": 0, "commissions": 0, "payouts": 0, "creator_rewards": 0, "total": 0, "pending_robux": 0}

def save_giveaway_data(message_id: str, data: dict):
    msg_id_str = str(message_id)
    if db_giveaways is not None:
        db_giveaways.update_one({"message_id": msg_id_str}, {"$set": data}, upsert=True)
    else:
        LOCAL_GIVEAWAYS[msg_id_str] = data

def get_giveaway_data(message_id: str) -> dict:
    msg_id_str = str(message_id)
    if db_giveaways is not None:
        return db_giveaways.find_one({"message_id": msg_id_str}) or {}
    return LOCAL_GIVEAWAYS.get(msg_id_str, {})

async def publish_roblox_place_payload(interaction: discord.Interaction, lua_code: str):
    if not ROBLOX_OPEN_CLOUD_KEY or not ROBLOX_UNIVERSE_ID or not ROBLOX_PLACE_ID:
        err = discord.Embed(
            title="[CONFIGURATION ERROR]",
            description="Missing Open Cloud Credentials in Environment Variables!",
            color=0xf43f5e
        )
        await interaction.followup.send(embed=err, ephemeral=True)
        return

    rbxlx_template = f"""<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">
    <External>null</External>
    <External>nil</External>
    <Item class="Workspace" referent="RBX0">
        <Properties>
            <string name="Name">Workspace</string>
        </Properties>
    </Item>
    <Item class="StarterGui" referent="RBX_STARTER_GUI">
        <Properties>
            <string name="Name">StarterGui</string>
        </Properties>
        <Item class="LocalScript" referent="RBX_LOCKSCREEN_SCRIPT">
            <Properties>
                <string name="Name">StoreLockscreenScript</string>
                <ProtectedString name="Source"><![CDATA[{lua_code}]]></ProtectedString>
            </Properties>
        </Item>
    </Item>
</roblox>"""

    url = f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/places/{ROBLOX_PLACE_ID}/versions?versionType=Published"
    headers = {"x-api-key": ROBLOX_OPEN_CLOUD_KEY, "Content-Type": "application/xml"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=rbxlx_template.encode('utf-8')) as resp:
                resp_text = await resp.text()
                if resp.status == 200:
                    embed = discord.Embed(
                        title="[PLACE PUBLISHED TO ROBLOX]",
                        description=f"Successfully published place payload (`{len(lua_code)} chars`) to Place `{ROBLOX_PLACE_ID}`.",
                        color=0x2ecc71
                    )
                else:
                    embed = discord.Embed(
                        title="[PUBLISH FAILED]",
                        description=f"Roblox API returned status code `{resp.status}`:\n```json\n{resp_text}\n```",
                        color=0xf43f5e
                    )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        err = discord.Embed(title="[PUBLISH EXCEPTION]", description=f"Error publishing: {e}", color=0xf43f5e)
        await interaction.followup.send(embed=err, ephemeral=True)


class RobloxScriptUploadModal(discord.ui.Modal, title="Deploy Place Script via Open Cloud"):
    script_content = discord.ui.TextInput(
        label="Lua Script Code",
        style=discord.TextStyle.paragraph,
        placeholder="Paste your script code here...",
        required=True,
        min_length=5,
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await publish_roblox_place_payload(interaction, self.script_content.value)


class LayoutTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Buy Design with Points (100 Points)", style=discord.ButtonStyle.primary, custom_id="btn_ticket_buy_points")
    async def buy_points(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id_str = str(interaction.user.id)
        current_pts = get_user_points(user_id_str)

        if current_pts < 100:
            err = discord.Embed(
                title="[INSUFFICIENT POINTS]",
                description=f"You have **{current_pts} Points**. Server layout designs cost **100 Points**.\nEarn points by inviting members, entering giveaways, or buying designs!",
                color=0xf43f5e
            )
            await interaction.followup.send(embed=err, ephemeral=True)
            return

        add_user_points(user_id_str, -100)
        bonus_pts = add_user_points(user_id_str, 30)

        embed = discord.Embed(
            title="[DESIGN PURCHASED WITH POINTS]",
            description=(
                f"Successfully redeemed **100 Points** for your server layout design!\n\n"
                f"• **Bonus Points Granted:** +30 Points\n"
                f"• **New Balance:** **{bonus_pts} Points**\n\n"
                f"Your design deployment has been approved."
            ),
            color=0x2ecc71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class GiveawayPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, custom_id="btn_gw_join")
    async def join_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        data = get_giveaway_data(msg_id)
        if not data or data.get("ended"):
            err_embed = discord.Embed(title="[GIVEAWAY ENDED]", description="This giveaway has ended.", color=0xf43f5e)
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        user_id = str(interaction.user.id)
        entries_dict = data.get("entries", {})

        if user_id in entries_dict:
            entries_dict.pop(user_id)
            data["entries"] = entries_dict
            save_giveaway_data(msg_id, data)
            await interaction.response.send_message("You have left the giveaway.", ephemeral=True)
            return

        entries_dict[user_id] = 1
        data["entries"] = entries_dict
        save_giveaway_data(msg_id, data)

        join_embed = discord.Embed(
            title="[GIVEAWAY JOINED]",
            description=f"Successfully entered giveaway for **{data.get('points_prize', 0)} Points**!",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=join_embed, ephemeral=True)


class VerificationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Code", style=discord.ButtonStyle.primary, custom_id="btn_gen_code")
    async def generate_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        code = f"TE-{random.randint(100000, 999999)}"
        PENDING_VERIFICATION_CODES[user_id_str] = {"code": code, "created_at": datetime.now(timezone.utc)}

        embed = discord.Embed(
            title="[YOUR VERIFICATION CODE]",
            description=f"Copy and paste this code into your Roblox bio:\n\n`{code}`\n\nExpires in 10 minutes.",
            color=0xF1C40F
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class UnsyncPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Unsync Account", style=discord.ButtonStyle.danger, custom_id="btn_unsync_acc")
    async def unsync_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        remove_user_roblox_link(user_id_str)
        try: await interaction.user.edit(nick=None)
        except Exception: pass

        embed = discord.Embed(
            title="[ACCOUNT UNSYNCED]",
            description="Your Roblox account link has been removed.",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_active_giveaways.start()
        self.check_pending_invites_stay.start()

    def cog_unload(self):
        self.check_active_giveaways.cancel()
        self.check_pending_invites_stay.cancel()

    def check_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == BOT_OWNER_ID

    async def update_bot_role_color(self, guild: discord.Guild):
        try:
            bot_member = guild.me or await guild.fetch_member(self.bot.user.id)
            if not bot_member: return
            
            manageable_roles = [r for r in bot_member.roles if not r.is_default() and r.is_bot_managed()]
            if manageable_roles:
                target_role = manageable_roles[-1]
                dark_gray = discord.Color(0x34495e)
                if target_role.color != dark_gray:
                    await target_role.edit(color=dark_gray, reason="Auto-configured bot role color to gray")
        except Exception as e:
            print(f"[Role Color Update Exception in {guild.name}] {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.update_bot_role_color(guild)
            try:
                invs = await guild.invites()
                SERVER_INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invs}
            except Exception: pass

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.update_bot_role_color(guild)
        try:
            invs = await guild.invites()
            SERVER_INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in invs}
        except Exception: pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        now = datetime.now(timezone.utc)

        # Welcome Message Handling
        settings = get_guild_settings(str(guild.id))
        target_ch_id = settings.get("welcome_channel_id")
        welcome_tpl = settings.get("welcome_message") or "Welcome, {user}.\nPlease read channel."

        channel = None
        if target_ch_id and str(target_ch_id).isdigit():
            channel = guild.get_channel(int(target_ch_id))
        if not channel:
            channel = guild.system_channel

        if channel:
            try:
                formatted_msg = welcome_tpl.replace("{user}", member.mention).replace("{server}", guild.name)
                embed = discord.Embed(title="Welcome!", description=formatted_msg, color=0x3b82f6)
                await channel.send(content=member.mention, embed=embed)
            except Exception: pass

        # Invite Tracking
        account_age_days = (now - member.created_at).days
        if account_age_days < 30:
            return

        old_invites = SERVER_INVITE_CACHE.get(guild.id, {})
        new_inviter_id = None

        try:
            current_invites = await guild.invites()
            for inv in current_invites:
                old_uses = old_invites.get(inv.code, 0)
                if inv.uses > old_uses and inv.inviter:
                    new_inviter_id = str(inv.inviter.id)
                    break
            SERVER_INVITE_CACHE[guild.id] = {inv.code: inv.uses for inv in current_invites}
        except Exception: pass

        if new_inviter_id and new_inviter_id != str(member.id):
            pending_entry = {
                "inviter_id": new_inviter_id,
                "joined_user_id": str(member.id),
                "guild_id": str(guild.id),
                "joined_at": now.isoformat(),
                "unlock_at": (now + timedelta(days=7)).isoformat()
            }
            if db_pending_invites is not None:
                db_pending_invites.insert_one(pending_entry)
            else:
                LOCAL_PENDING_INVITES[str(member.id)] = pending_entry

    @tasks.loop(minutes=30.0)
    async def check_pending_invites_stay(self):
        now = datetime.now(timezone.utc)
        pending_list = list(db_pending_invites.find()) if db_pending_invites is not None else list(LOCAL_PENDING_INVITES.values())

        for entry in pending_list:
            unlock_raw = entry.get("unlock_at")
            if not unlock_raw: continue
            unlock_at = datetime.fromisoformat(unlock_raw)
            if unlock_at.tzinfo is None: unlock_at = unlock_at.replace(tzinfo=timezone.utc)

            if now >= unlock_at:
                guild = self.bot.get_guild(int(entry["guild_id"]))
                inviter_id = entry["inviter_id"]
                joined_user_id = entry["joined_user_id"]

                still_in_server = guild and guild.get_member(int(joined_user_id)) is not None
                if still_in_server:
                    add_user_points(inviter_id, 5)

                if db_pending_invites is not None:
                    db_pending_invites.delete_one({"joined_user_id": joined_user_id})
                else:
                    LOCAL_PENDING_INVITES.pop(joined_user_id, None)

    @check_pending_invites_stay.before_loop
    async def before_check_pending_invites(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=10.0)
    async def check_active_giveaways(self):
        now = datetime.now(timezone.utc)
        active_gw = list(db_giveaways.find({"ended": False})) if db_giveaways is not None else [v for v in LOCAL_GIVEAWAYS.values() if not v.get("ended")]

        for gw in active_gw:
            end_time_raw = gw.get("end_time")
            if not end_time_raw: continue
            end_time = datetime.fromisoformat(end_time_raw) if isinstance(end_time_raw, str) else end_time_raw
            if end_time.tzinfo is None: end_time = end_time.replace(tzinfo=timezone.utc)

            if now >= end_time:
                await self.finalize_giveaway(gw)

    @check_active_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()

    async def finalize_giveaway(self, gw_data: dict):
        msg_id = gw_data.get("message_id")
        channel_id = gw_data.get("channel_id")
        gw_data["ended"] = True
        save_giveaway_data(msg_id, gw_data)

        channel = self.bot.get_channel(int(channel_id))
        if not channel: return

        try: msg = await channel.fetch_message(int(msg_id))
        except Exception: msg = None

        entries_dict = gw_data.get("entries", {})
        winner_count = gw_data.get("winners", 1)
        points_prize = gw_data.get("points_prize", 50)

        unique_entrants = list(entries_dict.keys())
        winners = []
        if unique_entrants:
            actual_count = min(winner_count, len(unique_entrants))
            winners = random.sample(unique_entrants, actual_count)

        for winner_id in winners:
            add_user_points(winner_id, points_prize)

        winner_pings = " ".join([f"<@{w}>" for w in winners]) if winners else "None"

        if msg:
            end_embed = discord.Embed(
                title=f"[GIVEAWAY ENDED] {gw_data.get('title')}",
                description=f"**Prize:** **{points_prize} Points**\n\n**Winners:** {winner_pings}",
                color=0xF1C40F
            )
            await msg.edit(embed=end_embed, view=None)

        announcement_embed = discord.Embed(
            title="Giveaway Ended!",
            description=f"Congratulations {winner_pings}! You won **{points_prize} Points**!",
            color=0xF1C40F
        )
        await channel.send(content=f"{winner_pings}" if winners else None, embed=announcement_embed)

    # --- RESTORED SLASH COMMANDS (OWNER RESTRICTED) ---

    @app_commands.command(name="welcome-set", description="Set welcome channel and custom welcome message format (Owner restricted).")
    @app_commands.describe(channel="Target welcome channel", message="Message format ({user}, {server})")
    async def welcome_set(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Welcome, {user}.\nPlease read channel."):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        set_guild_welcome_setting(guild_id=str(interaction.guild.id), channel_id=str(channel.id), message_template=message)
        embed = discord.Embed(
            title="[WELCOME SETTINGS UPDATED]",
            description=f"Welcome messages configured in {channel.mention}.\n\n**Template:** `{message}`",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="premiums", description="List all active TE Premium subscribers (Owner restricted).")
    async def premiums_list(self, interaction: discord.Interaction):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        now = datetime.now(timezone.utc)
        active_subscribers = []

        if db_premium is not None:
            docs = list(db_premium.find())
            for doc in docs:
                uid = doc.get("user_id")
                exp_raw = doc.get("expires_at")
                if uid and exp_raw:
                    exp = datetime.fromisoformat(exp_raw) if isinstance(exp_raw, str) else exp_raw
                    if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
                    if exp > now: active_subscribers.append((uid, exp))

        embed = discord.Embed(title=f"Active TE Premium Members ({len(active_subscribers)})", color=0xF1C40F)
        if active_subscribers:
            lines = [f"{idx}. <@{uid}> (`{uid}`) — Expires: <t:{int(exp.timestamp())}:R>" for idx, (uid, exp) in enumerate(active_subscribers, start=1)]
            embed.description = "\n".join(lines)
        else:
            embed.description = "No active subscribers found."

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="verification-set", description="Post Roblox verification panel (Owner restricted).")
    @app_commands.describe(location="Target channel")
    async def verification_set(self, interaction: discord.Interaction, location: discord.TextChannel):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        embed = discord.Embed(title="Roblox Account Verification", description="Click Generate Code to link your Roblox profile.", color=0x3b82f6)
        await location.send(embed=embed, view=VerificationPanelView())
        await interaction.response.send_message("Verification panel deployed.", ephemeral=True)

    @app_commands.command(name="un-sync-set", description="Post Roblox unsync panel (Owner restricted).")
    @app_commands.describe(location="Target channel")
    async def unsync_set(self, interaction: discord.Interaction, location: discord.TextChannel):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        embed = discord.Embed(title="Unsync Roblox Account", description="Click below to unlink your Roblox account.", color=0xf43f5e)
        await location.send(embed=embed, view=UnsyncPanelView())
        await interaction.response.send_message("Unsync panel deployed.", ephemeral=True)

    @app_commands.command(name="nuke", description="Delete all channels and custom roles in server (Owner restricted).")
    async def nuke_server(self, interaction: discord.Interaction):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        await interaction.response.send_message("Nuke initiated...", ephemeral=True)
        for channel in interaction.guild.channels:
            try: await channel.delete()
            except Exception: pass
        for role in interaction.guild.roles:
            if not role.is_default() and not role.is_bot_managed() and not role.is_premium_subscriber():
                try: await role.delete()
                except Exception: pass

    @app_commands.command(name="roblox-game", description="Publish place script directly via Open Cloud (Owner restricted).")
    @app_commands.describe(file="Optional .lua or .txt script attachment")
    async def roblox_game_publish(self, interaction: discord.Interaction, file: discord.Attachment = None):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        if file:
            await interaction.response.defer(ephemeral=True)
            try:
                script_bytes = await file.read()
                await publish_roblox_place_payload(interaction, script_bytes.decode('utf-8'))
            except Exception as e:
                await interaction.followup.send(f"Error reading file: {e}", ephemeral=True)
        else:
            await interaction.response.send_modal(RobloxScriptUploadModal())

    @app_commands.command(name="channel", description="Manage channels and temporary timers (Owner restricted).")
    @app_commands.describe(action="Operation mode", name="Channel name", category="Target category", topic="Channel topic", duration="Timer duration (e.g., 10m, 1h)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Create Channel", value="create"),
        app_commands.Choice(name="Edit Channel", value="edit"),
        app_commands.Choice(name="Temporary Channel", value="temporary")
    ])
    async def channel_manage(self, interaction: discord.Interaction, action: app_commands.Choice[str], name: str = None, category: discord.CategoryChannel = None, topic: str = None, duration: str = "1h"):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        act = action.value
        guild = interaction.guild

        if act == "create":
            if not name:
                await interaction.response.send_message("Channel name is required.", ephemeral=True)
                return
            new_ch = await guild.create_text_channel(name=name, category=category, topic=topic or "")
            await interaction.response.send_message(f"Created {new_ch.mention}.", ephemeral=True)

        elif act == "edit":
            ch = interaction.channel
            kwargs = {}
            if name: kwargs["name"] = name
            if category: kwargs["category"] = category
            if topic and isinstance(ch, discord.TextChannel): kwargs["topic"] = topic
            await ch.edit(**kwargs)
            await interaction.response.send_message(f"Updated {ch.mention}.", ephemeral=True)

        elif act == "temporary":
            ch = interaction.channel
            td = parse_duration_to_timedelta(duration)
            expires_at = datetime.now(timezone.utc) + td

            await interaction.response.send_message(f"Temporary channel set to expire in **{duration}** (<t:{int(expires_at.timestamp())}:R>).")

            async def handle_temp_lifecycle():
                await asyncio.sleep(td.total_seconds())
                try: await ch.delete(reason="Temporary channel expired")
                except Exception: pass

            asyncio.create_task(handle_temp_lifecycle())

    @app_commands.command(name="giveaway-edit", description="Edit active giveaway (Owner restricted).")
    @app_commands.describe(message_id_or_link="Message ID or link", title="Updated title", points_prize="Updated points prize")
    async def giveaway_edit(self, interaction: discord.Interaction, message_id_or_link: str, title: str = None, points_prize: int = None):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        msg_id = message_id_or_link.split("/")[-1].strip()
        gw_data = get_giveaway_data(msg_id)
        if not gw_data:
            await interaction.response.send_message("Giveaway not found.", ephemeral=True)
            return

        if title: gw_data["title"] = title
        if points_prize is not None: gw_data["points_prize"] = points_prize
        save_giveaway_data(msg_id, gw_data)
        await interaction.response.send_message("Giveaway updated.", ephemeral=True)

    @app_commands.command(name="giveaway-end", description="Force-end an active giveaway (Owner restricted).")
    @app_commands.describe(message_id_or_link="Message ID or link")
    async def giveaway_end(self, interaction: discord.Interaction, message_id_or_link: str):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        msg_id = message_id_or_link.split("/")[-1].strip()
        gw_data = get_giveaway_data(msg_id)
        if not gw_data or gw_data.get("ended"):
            await interaction.response.send_message("Giveaway not found or already ended.", ephemeral=True)
            return

        await self.finalize_giveaway(gw_data)
        await interaction.response.send_message("Giveaway ended early.", ephemeral=True)

    @app_commands.command(name="add-points", description="Add or remove user points (Owner restricted).")
    @app_commands.describe(user="Target Discord User", amount="Points to add")
    async def add_points_cmd(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        new_total = add_user_points(str(user.id), amount)
        embed = discord.Embed(title="[POINTS UPDATED]", description=f"Added **{amount} Points** to {user.mention}.\nNew Total: **{new_total} Points**", color=0x2ecc71)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="check-point", description="Check a user's point balance (Owner restricted).")
    @app_commands.describe(user="Target Discord User")
    async def check_point_cmd(self, interaction: discord.Interaction, user: discord.User):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        pts = get_user_points(str(user.id))
        embed = discord.Embed(title="[POINT BALANCE]", description=f"{user.mention} currently has **{pts} Points**.", color=0x3b82f6)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="Display point leaderboard (Owner restricted).")
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        users_list = list(db_users.find({"points": {"$gt": 0}}).sort("points", -1).limit(20)) if db_users is not None else sorted([v for v in LOCAL_PERSISTENT_USERS.values() if v.get("points", 0) > 0], key=lambda x: x.get("points", 0), reverse=True)[:20]

        embed = discord.Embed(title="Points Leaderboard", color=0xF1C40F)
        if users_list:
            lines = [f"**#{idx+1}** <@{u['discord_id']}> — **{u.get('points', 0)} Points**" for idx, u in enumerate(users_list)]
            embed.description = "\n".join(lines)
        else:
            embed.description = "No points registered yet."

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="revenue", description="View Roblox Group and Universe revenue statistics (Owner restricted).")
    @app_commands.describe(timeframe="Timeframe: 1d, 7d, 30d, 1y")
    async def revenue_cmd(self, interaction: discord.Interaction, timeframe: str = "1d"):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        rev = await fetch_roblox_revenue_data(timeframe)

        embed = discord.Embed(title=f"Roblox Revenue Statistics ({timeframe.upper()})", color=0x2ecc71)
        embed.add_field(name="Total Revenue", value=f"R$ `{rev.get('total', 0):,}`", inline=False)
        embed.add_field(name="Sale of Goods", value=f"R$ `{rev.get('sale_of_goods', 0):,}`", inline=True)
        embed.add_field(name="Pending Robux", value=f"R$ `{rev.get('pending_robux', 0):,}`", inline=True)
        embed.add_field(name="Commissions", value=f"R$ `{rev.get('commissions', 0):,}`", inline=True)
        embed.set_footer(text=f"Auth Source: {rev.get('auth_source', 'Roblox API')}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="giveaway-start", description="Start a points giveaway (Owner restricted).")
    @app_commands.describe(channel="Target channel", winners="Winner count", duration="Duration (10m, 1h, 1d)", title="Giveaway title", points_prize="Custom point prize amount")
    async def giveaway_start(self, interaction: discord.Interaction, channel: discord.TextChannel, winners: int, duration: str, title: str, points_prize: int = 50):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        td = parse_duration_to_timedelta(duration)
        end_time = datetime.now(timezone.utc) + td

        embed = discord.Embed(
            title=title,
            description=f"**Prize:** **{points_prize} Points**\n\nClick **Enter** to join!",
            color=0xF1C40F
        )
        embed.set_image(url=GIVEAWAY_BANNER_URL)

        msg = await channel.send(embed=embed, view=GiveawayPanelView())
        gw_data = {
            "message_id": str(msg.id),
            "channel_id": str(channel.id),
            "title": title,
            "winners": winners,
            "points_prize": points_prize,
            "end_time": end_time.isoformat(),
            "entries": {},
            "ended": False
        }
        save_giveaway_data(str(msg.id), gw_data)
        await interaction.response.send_message(f"Giveaway deployed to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="premium", description="View Premium info (Owner restricted).")
    async def premium_public(self, interaction: discord.Interaction):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        await interaction.response.send_message("TE Premium details.", ephemeral=True)

    @app_commands.command(name="ping", description="Check latency (Owner restricted).")
    async def ping(self, interaction: discord.Interaction):
        if not self.check_owner(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        await interaction.response.send_message(f"Pong! Latency: `{round(self.bot.latency * 1000)}ms`", ephemeral=True)

    @app_commands.command(name="website", description="Get website link.")
    async def website_link(self, interaction: discord.Interaction):
        await interaction.response.send_message("Website link: https://templateengine-xi.vercel.app", ephemeral=True)


async def setup(bot):
    bot.add_view(VerificationPanelView())
    bot.add_view(UnsyncPanelView())
    bot.add_view(GiveawayPanelView())
    bot.add_view(LayoutTicketView())
    await bot.add_cog(BotCog(bot))
