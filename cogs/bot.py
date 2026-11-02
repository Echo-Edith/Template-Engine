import os
import re
import json
import random
import asyncio
import aiohttp
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands
from discord import app_commands
from pymongo import MongoClient

# -------------------------------------------------------------
# GLOBAL CONSTANTS & LOGGING CHANNELS
# -------------------------------------------------------------
BOT_OWNER_ID = 1219266886143967245
MAIN_GUILD_ID = 1284513257574629458

PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("PREMIUM_DESIGN_CHANNEL_ID", "1563544108696477802"))
NON_PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("NON_PREMIUM_DESIGN_CHANNEL_ID", "1563524101522464900"))

GIVEAWAY_BANNER_URL = "https://res.cloudinary.com/dtz0urit6/image/upload/f_png,q_auto/cloudinary-tools-uploads/doob9hmutuwfzw6aloj5.png"

MONGO_URI = os.getenv("MONGO_URI")

db_premium = None
db_users = None
db_giveaways = None
db_guild_settings = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_premium = db["premium_users"]
        db_users = db["persistent_users"]
        db_giveaways = db["giveaways"]
        db_guild_settings = db["guild_settings"]
    except Exception as e:
        print(f"[MongoDB Warning] {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_PERSISTENT_USERS = {}
LOCAL_GUILD_SETTINGS = {}
USER_POINTS_DATA = {}
ACTIVE_GIVEAWAYS = {}

PENDING_VERIFICATION_CODES = {}
USED_DEV_PRODUCT_RECEIPTS = set()
USER_MSG_TRACKER = {}


def get_formatted_roblox_cookie() -> str:
    cookie = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip()
    if not cookie:
        return ""
    clean = cookie.strip('"\'')
    if clean.startswith(".ROBLOSECURITY="):
        return clean
    return f".ROBLOSECURITY={clean}"


def get_user_points(user_id: str) -> int:
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
    uid = str(user_id)
    current_points = get_user_points(uid)
    new_total = max(0, current_points + amount)

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
    return new_total


def get_top_leaderboard(limit: int = 10) -> list:
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


def remove_user_premium(user_id: str):
    if db_premium is not None:
        try:
            db_premium.delete_one({"discord_id": str(user_id)})
        except Exception as e:
            print(f"[Mongo Remove Premium Error] {e}")
    LOCAL_PREMIUM_USERS.pop(str(user_id), None)


def strip_emojis(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff]', '', str(text))
    return clean.strip()


def format_channel_name(name: str) -> str:
    clean = strip_emojis(name)
    if not clean:
        clean = str(name).strip()
    return clean.lower().replace(" ", "-")


async def post_roblox_group_shout(group_id: str, message: str) -> bool:
    cookie = get_formatted_roblox_cookie()
    if not cookie or not group_id or group_id == "0":
        return False

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Cookie": cookie, "User-Agent": "Mozilla/5.0"}
            async with session.post("https://auth.roblox.com/v2/login", headers=headers) as csrf_resp:
                csrf_token = csrf_resp.headers.get("x-csrf-token")

            if csrf_token:
                headers["X-CSRF-TOKEN"] = csrf_token

            shout_url = f"https://groups.roblox.com/v1/groups/{group_id}/status"
            async with session.patch(shout_url, json={"message": message}, headers=headers) as shout_resp:
                return shout_resp.status == 200
    except Exception as e:
        print(f"[Roblox Group Shout Error] {e}")
    return False


# -------------------------------------------------------------
# INTERACTIVE VIEWS
# -------------------------------------------------------------
class GiveawayEntriesView(discord.ui.View):
    def __init__(self, entries: list):
        super().__init__(timeout=60)
        self.entries = entries
        self.page = 0
        self.max_per_page = 15

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="btn_gw_entries_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_page(interaction)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="btn_gw_entries_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (self.page + 1) * self.max_per_page < len(self.entries):
            self.page += 1
            await self.update_page(interaction)
        else:
            await interaction.response.defer()

    async def update_page(self, interaction: discord.Interaction):
        start = self.page * self.max_per_page
        end = start + self.max_per_page
        current_batch = self.entries[start:end]

        lines = [f"<@{uid}>" for uid in current_batch]
        embed = discord.Embed(
            title=f"Giveaway Entrants ({len(self.entries)} Total)",
            description="\n".join(lines) if lines else "No entries recorded yet.",
            color=0x8B5CF6
        )
        embed.set_footer(text=f"Page {self.page + 1} / {((len(self.entries) - 1) // self.max_per_page) + 1}")
        await interaction.response.edit_message(embed=embed, view=self)


class GiveawayPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter / Leave", style=discord.ButtonStyle.primary, custom_id="btn_gw_enter_toggle")
    async def enter_toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        msg_id_key = str(interaction.message.id)

        gw_data = ACTIVE_GIVEAWAYS.get(msg_id_key)
        if not gw_data and db_giveaways is not None:
            gw_data = db_giveaways.find_one({"message_id": msg_id_key})

        if not gw_data:
            await interaction.followup.send("This giveaway is no longer active.", ephemeral=True)
            return

        entries = gw_data.get("entries", [])
        if user_id in entries:
            entries.remove(user_id)
            action_msg = "You left the giveaway."
        else:
            entries.append(user_id)
            action_msg = "You entered the giveaway! Good luck!"

        gw_data["entries"] = entries
        ACTIVE_GIVEAWAYS[msg_id_key] = gw_data

        if db_giveaways is not None:
            db_giveaways.update_one({"message_id": msg_id_key}, {"$set": {"entries": entries}})

        try:
            embed = interaction.message.embeds[0]
            embed.set_footer(text=f"Entries: {len(entries)} | Hosted by Template Engine")
            await interaction.message.edit(embed=embed)
        except Exception:
            pass

        await interaction.followup.send(action_msg, ephemeral=True)

    @discord.ui.button(label="Entries", style=discord.ButtonStyle.secondary, custom_id="btn_gw_view_entries")
    async def view_entries_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id_key = str(interaction.message.id)
        gw_data = ACTIVE_GIVEAWAYS.get(msg_id_key)
        if not gw_data and db_giveaways is not None:
            gw_data = db_giveaways.find_one({"message_id": msg_id_key})

        entries = gw_data.get("entries", []) if gw_data else []

        if not entries:
            await interaction.response.send_message("No entrants in this giveaway yet!", ephemeral=True)
            return

        view = GiveawayEntriesView(entries)
        lines = [f"<@{uid}>" for uid in entries[:15]]
        embed = discord.Embed(
            title=f"Giveaway Entrants ({len(entries)} Total)",
            description="\n".join(lines),
            color=0x8B5CF6
        )
        embed.set_footer(text=f"Page 1 / {((len(entries) - 1) // 15) + 1}")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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

        code_data = PENDING_VERIFICATION_CODES.get(user_id)
        if not code_data or datetime.now(timezone.utc).timestamp() > code_data.get("expires_at", 0):
            await interaction.followup.send("Verification code expired or missing. Generate a new code first.", ephemeral=True)
            return

        expected_code = code_data.get("code")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [r_username], "excludeBannedUsers": False}) as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Failed to query Roblox API.", ephemeral=True)
                        return
                    data = await resp.json()
                    user_list = data.get("data", [])
                    if not user_list:
                        await interaction.followup.send(f"Roblox user `{r_username}` not found.", ephemeral=True)
                        return
                    
                    r_id = str(user_list[0].get("id"))
                    actual_name = user_list[0].get("name")

                async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Failed to fetch user bio.", ephemeral=True)
                        return
                    p_data = await resp.json()
                    description = p_data.get("description", "")

            if expected_code not in description:
                await interaction.followup.send(f"Code `{expected_code}` was not found in your Roblox bio. Please save your bio and try again.", ephemeral=True)
                return

            if db_users is not None:
                db_users.update_one(
                    {"discord_id": user_id},
                    {"$set": {"discord_id": user_id, "roblox_data": {"id": r_id, "username": actual_name}}},
                    upsert=True
                )

            PENDING_VERIFICATION_CODES.pop(user_id, None)

            embed = discord.Embed(
                title="Account Linked!",
                description=f"Your Discord account is now linked to **{actual_name}** (`{r_id}`).",
                color=0x2ECC71
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Verification error: `{str(e)}`", ephemeral=True)


class RobloxVerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Code", style=discord.ButtonStyle.primary, custom_id="btn_verify_gen_code")
    async def gen_code_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        code = f"TE-{random.randint(10000, 99999)}"
        exp_time = datetime.now(timezone.utc).timestamp() + (15 * 60)

        PENDING_VERIFICATION_CODES[user_id] = {"code": code, "expires_at": exp_time}

        embed = discord.Embed(
            title="Verification Code Generated",
            description=f"Your code: `{code}`\n\n1. Paste code in your Roblox **About / Bio**.\n2. Save profile.\n3. Click **Verify Account**.",
            color=0x8B5CF6
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Verify Account", style=discord.ButtonStyle.success, custom_id="btn_verify_submit")
    async def verify_submit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RobloxVerifyModal())


class RobloxUnsyncView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Un-Sync Account", style=discord.ButtonStyle.danger, custom_id="btn_unsync_account")
    async def unsync_account_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        if db_users is not None:
            db_users.update_one({"discord_id": user_id}, {"$unset": {"roblox_data": ""}})

        embed = discord.Embed(title="Account Un-Synced", description="Your Roblox account link has been removed.", color=0x2ECC71)
        await interaction.followup.send(embed=embed, ephemeral=True)


class PremiumMissingBotView(discord.ui.View):
    def __init__(self, bot=None, submitting_user_id: str = "0", target_guild_id: str = "0", layout: dict = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.submitting_user_id = str(submitting_user_id)
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}

        invite_url = f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id if bot and bot.user else ''}&permissions=8&scope=bot%20applications.commands&guild_id={target_guild_id}"
        self.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link, custom_id="btn_premium_invite_link"))

    @discord.ui.button(label="Build", style=discord.ButtonStyle.success, custom_id="btn_premium_build_retry")
    async def build_retry_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        bot_inst = self.bot or interaction.client
        target_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0

        guild = bot_inst.get_guild(target_gid)
        if not guild and target_gid:
            try:
                guild = await bot_inst.fetch_guild(target_gid)
            except Exception:
                guild = None

        if not guild:
            await interaction.followup.send(f"Re-build failed: Bot is still missing from server `{self.target_guild_id}`.", ephemeral=True)
            return

        await interaction.followup.send(f"Bot detected in **{guild.name}**! Auto-building layout now...", ephemeral=True)
        cog = bot_inst.get_cog("BotCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)


class NonPremiumDeploymentView(discord.ui.View):
    def __init__(self, bot=None, submitting_user_id: str = "0", target_guild_id: str = "0", layout: dict = None, dev_product_url: str = ""):
        super().__init__(timeout=None)
        self.bot = bot
        self.submitting_user_id = str(submitting_user_id)
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}

        store_url = dev_product_url or "https://www.roblox.com/games/6094950399/Template-Engine#!/store"
        self.add_item(discord.ui.Button(label="Buy Roblox Dev Product", url=store_url, style=discord.ButtonStyle.link, custom_id="btn_buy_roblox_dev_prod"))

    @discord.ui.button(label="Verify Purchase", style=discord.ButtonStyle.success, custom_id="btn_verify_dev_product")
    async def verify_purchase_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        roblox_id = None

        if db_users is not None:
            doc = db_users.find_one({"discord_id": user_id})
            if doc and "roblox_data" in doc:
                roblox_id = doc["roblox_data"].get("id")

        if not roblox_id:
            await interaction.followup.send("Please sync your Roblox account first before verifying purchase.", ephemeral=True)
            return

        dev_product_id = os.getenv("ROBLOX_DEV_PRODUCT_ID", "").strip()
        cookie_header = get_formatted_roblox_cookie()
        is_verified = False
        receipt_token = None

        if cookie_header and roblox_id:
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"https://economy.roblox.com/v2/users/{roblox_id}/transaction-history?transactionType=Purchase"
                    headers = {"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for tx in data.get("data", []):
                                item_id = str((tx.get("details") or {}).get("id", ""))
                                tx_id = str(tx.get("id") or tx.get("transactionId"))
                                tx_created = str(tx.get("created", ""))
                                token_hash = hashlib.sha256(f"{tx_id}_{tx_created}".encode()).hexdigest()
                                if (dev_product_id == "" or item_id == dev_product_id) and token_hash not in USED_DEV_PRODUCT_RECEIPTS:
                                    is_verified = True
                                    receipt_token = token_hash
                                    break
            except Exception as e:
                print(f"[Sales Audit Error] {e}")

        if not is_verified:
            await interaction.followup.send("No unused purchase receipt found for your Roblox account.", ephemeral=True)
            return

        if receipt_token:
            USED_DEV_PRODUCT_RECEIPTS.add(receipt_token)

        bot_inst = self.bot or interaction.client
        target_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0
        guild = bot_inst.get_guild(target_gid)

        if guild:
            await interaction.followup.send(f"Purchase verified! Deploying layout to {guild.name}...", ephemeral=True)
            cog = bot_inst.get_cog("BotCog")
            if cog:
                await cog.execute_auto_build(guild, self.layout)
        else:
            await interaction.followup.send("Error: Bot is missing from target server.", ephemeral=True)

    @discord.ui.button(label="Use Points (100 Pts)", style=discord.ButtonStyle.primary, custom_id="btn_use_points_build")
    async def use_points_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        current_pts = get_user_points(user_id)

        if current_pts < 100:
            await interaction.followup.send(f"You need 100 points to deploy. Balance: {current_pts} pts", ephemeral=True)
            return

        add_user_points(user_id, -100)
        bot_inst = self.bot or interaction.client
        target_gid = int(self.target_guild_id) if self.target_guild_id != "0" else 0
        guild = bot_inst.get_guild(target_gid)

        if guild:
            await interaction.followup.send(f"Redeemed 100 points! Deploying to {guild.name}...", ephemeral=True)
            cog = bot_inst.get_cog("BotCog")
            if cog:
                await cog.execute_auto_build(guild, self.layout)
        else:
            await interaction.followup.send("Error: Bot is missing from target server.", ephemeral=True)


# -------------------------------------------------------------
# DISCORD BOT COG IMPLEMENTATION
# -------------------------------------------------------------
class BotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # =============================================================
    # 🔎 COMMAND 1: ROBLOX PROFILE SEARCH & AUDIT
    # =============================================================
    @app_commands.command(name="search", description="Search and audit a Roblox profile for group roles, giveaway indicators, and badges.")
    @app_commands.describe(query="Roblox Username or User ID")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=False)
        q = query.strip()

        try:
            async with aiohttp.ClientSession() as session:
                r_id, username = None, None
                if q.isdigit():
                    r_id = q
                    async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                        if resp.status == 200:
                            username = (await resp.json()).get("name")
                else:
                    async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [q], "excludeBannedUsers": False}) as resp:
                        if resp.status == 200:
                            user_list = (await resp.json()).get("data", [])
                            if user_list:
                                r_id = str(user_list[0].get("id"))
                                username = user_list[0].get("name")

                if not r_id or not username:
                    await interaction.followup.send(f"Could not find Roblox user matching `{q}`.")
                    return

                async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                    p_data = await resp.json() if resp.status == 200 else {}
                async with session.get(f"https://groups.roblox.com/v2/users/{r_id}/groups/roles") as resp:
                    g_data = await resp.json() if resp.status == 200 else {}
                async with session.get(f"https://thumbnails.roblox.com/v1/users/avatar?userIds={r_id}&size=720x720&format=Png") as resp:
                    thumb_data = await resp.json() if resp.status == 200 else {}

                display_name = p_data.get("displayName", username)
                groups_list = g_data.get("data", [])
                te_group_id = str(os.getenv("ROBLOX_GROUP_ID", "")).strip()

                in_te_group = False
                te_role = None
                for item in groups_list:
                    grp = item.get("group", {})
                    if te_group_id and str(grp.get("id")) == te_group_id:
                        in_te_group = True
                        te_role = item.get("role", {}).get("name")

                embed = discord.Embed(title="🔎 Roblox Lookup\n━━━━━━━━━━━━━━━━", color=0x8B5CF6)
                if thumb_data.get("data"):
                    embed.set_thumbnail(url=thumb_data["data"][0].get("imageUrl", ""))

                embed.add_field(
                    name="👤 USER",
                    value=f"**Username:** {username}\n**Display Name:** {display_name}\n**User ID:** `{r_id}`",
                    inline=False
                )

                if te_group_id:
                    status = f"Member (`{te_role}`)" if in_te_group else "Not a Member"
                    embed.add_field(name="👥 GROUPS", value=f"• **Template Engine Group:** {status}", inline=False)

                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"Search error: `{str(e)}`")

    # =============================================================
    # 🎉 COMMAND 2: GIVEAWAY SYSTEM (CREATE / EDIT / END)
    # =============================================================
    @app_commands.command(name="giveaway", description="Create, edit, or end server giveaways with group shout integration.")
    @app_commands.describe(
        action="Action: create, edit, or end",
        channel="Target channel for giveaway announcement",
        duration="Duration format e.g. 1h, 1d, 7d",
        prize="Prize details",
        description="Giveaway details/rules",
        message_link="Message ID or link for edit/end"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Create Giveaway", value="create"),
        app_commands.Choice(name="Edit Giveaway", value="edit"),
        app_commands.Choice(name="End Giveaway", value="end")
    ])
    async def giveaway_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
        duration: Optional[str] = "1d",
        prize: Optional[str] = "100 Points",
        description: Optional[str] = "Click the button below to enter!",
        message_link: Optional[str] = None
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        target_channel = channel or interaction.channel
        if action.value == "create":
            dur_td = parse_duration_to_timedelta(duration or "1d")
            end_ts = int((datetime.now(timezone.utc) + dur_td).timestamp())

            embed = discord.Embed(
                title=f"🎉 GIVEAWAY: {prize}",
                description=f"{description}\n\n**Ends:** <t:{end_ts}:R>\n**Hosted By:** {interaction.user.mention}",
                color=0x8B5CF6
            )
            embed.set_image(url="https://res.cloudinary.com/dtz0urit6/image/upload/f_png,q_auto/cloudinary-tools-uploads/doob9hmutuwfzw6aloj5.png")
            embed.set_footer(text="Entries: 0 | Hosted by Template Engine")

            view = GiveawayPanelView()
            gw_msg = await target_channel.send(embed=embed, view=view)

            gw_data = {
                "message_id": str(gw_msg.id),
                "channel_id": str(target_channel.id),
                "guild_id": str(interaction.guild_id),
                "prize": prize,
                "end_timestamp": end_ts,
                "entries": []
            }
            ACTIVE_GIVEAWAYS[str(gw_msg.id)] = gw_data

            await interaction.followup.send(f"Giveaway posted in {target_channel.mention}!", ephemeral=True)

    # =============================================================
    # ⚙️ COMMAND 3: DEPLOY SYSTEM INTERACTIVE PANELS
    # =============================================================
    @app_commands.command(name="set", description="Deploy verification or un-sync interactive embed panels into a channel.")
    @app_commands.describe(
        action="Panel type: verification or un-sync",
        channel="The target channel to send the panel into",
        description="Custom description text for the panel"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Verification Panel", value="verification"),
        app_commands.Choice(name="Un-Sync Panel", value="un-sync")
    ])
    async def set_panel_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel,
        description: str
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        if action.value == "verification":
            embed = discord.Embed(
                title="Roblox Account Verification",
                description=description,
                color=0x8B5CF6
            )
            embed.set_footer(text="Template Engine Roblox Synchronization System")
            view = RobloxVerificationView()
        else:
            embed = discord.Embed(
                title="Un-Sync Roblox Account",
                description=description,
                color=0xF43F5E
            )
            embed.set_footer(text="Template Engine Account Management")
            view = RobloxUnsyncView()

        await channel.send(embed=embed, view=view)
        await interaction.followup.send(f"Deployed {action.name} to {channel.mention}.", ephemeral=True)

    # =============================================================
    # 👋 COMMAND 4: WELCOME CHANNEL AND GREETING CONFIGURATION
    # =============================================================
    @app_commands.command(name="welcome-set", description="Set welcome greeting channel and custom greeting message.")
    @app_commands.describe(
        channel="The greeting channel",
        message="Custom greeting text. Supports: {user}, {username}, {server}"
    )
    async def welcome_set_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: Optional[str] = "Welcome {user} to **{server}**! Enjoy your stay!"
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        guild_id_str = str(interaction.guild_id)
        settings_payload = {
            "guild_id": guild_id_str,
            "welcome_channel_id": str(channel.id),
            "welcome_message": message
        }

        LOCAL_GUILD_SETTINGS[guild_id_str] = settings_payload

        embed = discord.Embed(
            title="Welcome Settings Saved",
            description=f"**Welcome Channel:** {channel.mention}\n**Preview:** {message.replace('{user}', interaction.user.mention).replace('{username}', interaction.user.name).replace('{server}', interaction.guild.name)}",
            color=0x2ECC71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # =============================================================
    # 💰 COMMAND 5: CHECK MEMBER POINTS BALANCE
    # =============================================================
    @app_commands.command(name="points", description="Check your or another member's current point balance.")
    @app_commands.describe(user="Optional member to check points for (defaults to yourself)")
    async def points_cmd(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await interaction.response.defer(ephemeral=False)
        target = user or interaction.user
        pts = get_user_points(str(target.id))
        embed = discord.Embed(
            title="Point Balance",
            description=f"{target.mention} currently has **{pts:,}** points.",
            color=0x8B5CF6
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.followup.send(embed=embed)

    # =============================================================
    # 🏆 COMMAND 6: POINTS LEADERBOARD
    # =============================================================
    @app_commands.command(name="leaderboard", description="View the top point earners in the server.")
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        top = get_top_leaderboard(10)
        lines = [f"#{i+1} <@{e['discord_id']}> - {e['points']:,} pts" for i, e in enumerate(top)]
        embed = discord.Embed(
            title="Point Leaderboard",
            description="\n".join(lines) if lines else "No active rankings recorded yet.",
            color=0xF1C40F
        )
        embed.set_footer(text="Earn +2 points per message | +20 on join")
        await interaction.followup.send(embed=embed)

    # =============================================================
    # 🛠️ COMMAND 7: MODIFY USER POINTS (ADD / DEDUCT)
    # =============================================================
    @app_commands.command(name="points-set", description="Add or deduct points for a specified user.")
    @app_commands.describe(
        user="Target member to modify points for",
        action="Action: add or deduct",
        amount="Point quantity"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add Points", value="add"),
        app_commands.Choice(name="Deduct Points", value="deduct")
    ])
    async def points_set_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        action: app_commands.Choice[str],
        amount: int
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        delta = amount if action.value == "add" else -amount
        new_total = add_user_points(str(user.id), delta)
        await interaction.followup.send(f"Updated points for {user.mention}. New Balance: **{new_total:,}** pts", ephemeral=True)

    # =============================================================
    # 👑 COMMAND 8: TE PREMIUM SUBSCRIPTION MANAGEMENT
    # =============================================================
    @app_commands.command(name="premium-set", description="Grant or revoke TE Premium subscription status for a user.")
    @app_commands.describe(
        user="Target user to modify premium status for",
        action="Action: add or remove",
        duration="Duration format e.g. 1d, 7d, 30d, 1y, inf"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Grant Premium", value="add"),
        app_commands.Choice(name="Revoke Premium", value="remove")
    ])
    async def premium_set_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        action: app_commands.Choice[str],
        duration: Optional[str] = "30d"
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        if action.value == "add":
            set_user_premium(str(user.id), parse_duration_to_timedelta(duration or "30d"))
            await interaction.followup.send(f"Granted TE Premium access to {user.mention}.", ephemeral=True)
        else:
            remove_user_premium(str(user.id))
            await interaction.followup.send(f"Revoked TE Premium access from {user.mention}.", ephemeral=True)

    @app_commands.describe(query="Roblox Username or User ID")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=False)
        q = query.strip()

        try:
            async with aiohttp.ClientSession() as session:
                r_id, username = None, None
                if q.isdigit():
                    r_id = q
                    async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                        if resp.status == 200:
                            username = (await resp.json()).get("name")
                else:
                    async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [q], "excludeBannedUsers": False}) as resp:
                        if resp.status == 200:
                            user_list = (await resp.json()).get("data", [])
                            if user_list:
                                r_id = str(user_list[0].get("id"))
                                username = user_list[0].get("name")

                if not r_id or not username:
                    await interaction.followup.send(f"Could not find Roblox user matching `{q}`.")
                    return

                async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                    p_data = await resp.json() if resp.status == 200 else {}
                async with session.get(f"https://groups.roblox.com/v2/users/{r_id}/groups/roles") as resp:
                    g_data = await resp.json() if resp.status == 200 else {}
                async with session.get(f"https://thumbnails.roblox.com/v1/users/avatar?userIds={r_id}&size=720x720&format=Png") as resp:
                    thumb_data = await resp.json() if resp.status == 200 else {}

                display_name = p_data.get("displayName", username)
                groups_list = g_data.get("data", [])
                te_group_id = str(os.getenv("ROBLOX_GROUP_ID", "")).strip()

                in_te_group = False
                te_role = None
                for item in groups_list:
                    grp = item.get("group", {})
                    if te_group_id and str(grp.get("id")) == te_group_id:
                        in_te_group = True
                        te_role = item.get("role", {}).get("name")

                embed = discord.Embed(title="🔎 Roblox Profile Lookup", color=0x8B5CF6)
                if thumb_data.get("data"):
                    embed.set_thumbnail(url=thumb_data["data"][0].get("imageUrl", ""))

                embed.add_field(name="👤 USER", value=f"**Username:** {username}\n**Display Name:** {display_name}\n**User ID:** `{r_id}`", inline=False)
                if te_group_id:
                    status = f"Member (`{te_role}`)" if in_te_group else "Not a Member"
                    embed.add_field(name="👥 GROUPS", value=f"• **Template Engine Group:** {status}", inline=False)

                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"Search error: `{str(e)}`")

    @app_commands.command(name="giveaway", description="Manage giveaways.")
    @app_commands.describe(action="create, edit, or end", channel="Target channel", duration="1h, 1d, 7d", prize="Prize details", description="Giveaway details", message_link="Message ID or link")
    @app_commands.choices(action=[
        app_commands.Choice(name="Create Giveaway", value="create"),
        app_commands.Choice(name="Edit Giveaway", value="edit"),
        app_commands.Choice(name="End Giveaway", value="end")
    ])
    async def giveaway_cmd(self, interaction: discord.Interaction, action: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None, duration: Optional[str] = "1d", prize: Optional[str] = "100 Points", description: Optional[str] = "Click below to enter!", message_link: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        target_channel = channel or interaction.channel
        if action.value == "create":
            dur_td = parse_duration_to_timedelta(duration or "1d")
            end_ts = int((datetime.now(timezone.utc) + dur_td).timestamp())

            embed = discord.Embed(title=f"🎉 GIVEAWAY: {prize}", description=f"{description}\n\n**Ends:** <t:{end_ts}:R>\n**Hosted By:** {interaction.user.mention}", color=0x8B5CF6)
            embed.set_image(url=GIVEAWAY_BANNER_URL)
            embed.set_footer(text="Entries: 0 | Hosted by Template Engine")

            view = GiveawayPanelView()
            gw_msg = await target_channel.send(embed=embed, view=view)

            gw_data = {"message_id": str(gw_msg.id), "channel_id": str(target_channel.id), "guild_id": str(interaction.guild_id), "prize": prize, "end_timestamp": end_ts, "entries": []}
            ACTIVE_GIVEAWAYS[str(gw_msg.id)] = gw_data
            if db_giveaways is not None:
                db_giveaways.insert_one(gw_data)

            await interaction.followup.send(f"Giveaway posted in {target_channel.mention}!", ephemeral=True)

    @app_commands.command(name="set", description="Deploy verification or un-sync panels.")
    @app_commands.describe(action="Panel type", channel="Target channel", description="Custom text")
    @app_commands.choices(action=[
        app_commands.Choice(name="Verification Panel", value="verification"),
        app_commands.Choice(name="Un-Sync Panel", value="un-sync")
    ])
    async def set_panel_cmd(self, interaction: discord.Interaction, action: app_commands.Choice[str], channel: discord.TextChannel, description: str):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        if action.value == "verification":
            embed = discord.Embed(title="Roblox Account Verification", description=description, color=0x8B5CF6)
            view = RobloxVerificationView()
        else:
            embed = discord.Embed(title="Un-Sync Roblox Account", description=description, color=0xF43F5E)
            view = RobloxUnsyncView()

        await channel.send(embed=embed, view=view)
        await interaction.followup.send(f"Deployed {action.name} to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="welcome-set", description="Set welcome greeting channel and message.")
    @app_commands.describe(channel="Greeting channel", message="Variables: {user}, {username}, {server}")
    async def welcome_set_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel, message: Optional[str] = "Welcome {user} to **{server}**!"):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        guild_id_str = str(interaction.guild_id)
        settings_payload = {"guild_id": guild_id_str, "welcome_channel_id": str(channel.id), "welcome_message": message}

        LOCAL_GUILD_SETTINGS[guild_id_str] = settings_payload
        if db_guild_settings is not None:
            db_guild_settings.update_one({"guild_id": guild_id_str}, {"$set": settings_payload}, upsert=True)

        await interaction.followup.send(f"Welcome channel set to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="points", description="Check member points.")
    async def points_cmd(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await interaction.response.defer(ephemeral=False)
        target = user or interaction.user
        pts = get_user_points(str(target.id))
        embed = discord.Embed(title="Point Balance", description=f"{target.mention} has **{pts:,}** points.", color=0x8B5CF6)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="leaderboard", description="View points leaderboard.")
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        top = get_top_leaderboard(10)
        lines = [f"#{i+1} <@{e['discord_id']}> - {e['points']:,} pts" for i, e in enumerate(top)]
        embed = discord.Embed(title="Point Leaderboard", description="\n".join(lines) if lines else "No entries yet.", color=0xF1C40F)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="points-set", description="Modify user points.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add Points", value="add"),
        app_commands.Choice(name="Deduct Points", value="deduct")
    ])
    async def points_set_cmd(self, interaction: discord.Interaction, user: discord.User, action: app_commands.Choice[str], amount: int):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        delta = amount if action.value == "add" else -amount
        new_total = add_user_points(str(user.id), delta)
        await interaction.followup.send(f"Updated points for {user.mention}. New total: {new_total:,} pts", ephemeral=True)

    @app_commands.command(name="premium-set", description="Grant or revoke TE Premium.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Grant Premium", value="add"),
        app_commands.Choice(name="Revoke Premium", value="remove")
    ])
    async def premium_set_cmd(self, interaction: discord.Interaction, user: discord.User, action: app_commands.Choice[str], duration: Optional[str] = "30d"):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("Restricted to bot owner.", ephemeral=True)
            return

        if action.value == "add":
            set_user_premium(str(user.id), parse_duration_to_timedelta(duration or "30d"))
            await interaction.followup.send(f"Granted TE Premium to {user.mention}.", ephemeral=True)
        else:
            remove_user_premium(str(user.id))
            await interaction.followup.send(f"Revoked TE Premium from {user.mention}.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        add_user_points(str(member.id), 20)
        settings = LOCAL_GUILD_SETTINGS.get(str(member.guild.id))
        if settings and settings.get("welcome_channel_id"):
            channel = member.guild.get_channel(int(settings["welcome_channel_id"]))
            if channel:
                raw_msg = settings.get("welcome_message", "Welcome {user} to **{server}**!")
                formatted = raw_msg.replace("{user}", member.mention).replace("{username}", member.name).replace("{server}", member.guild.name)
                embed = discord.Embed(title=f"Welcome to {member.guild.name}!", description=formatted, color=0x8B5CF6)
                embed.set_thumbnail(url=member.display_avatar.url)
                await channel.send(embed=embed)

    async def execute_auto_build(self, guild: discord.Guild, layout: dict):
        for ch in guild.channels:
            try:
                await ch.delete()
                await asyncio.sleep(0.1)
            except Exception:
                pass

        for cat_data in layout.get("categories", []):
            try:
                cat = await guild.create_category(name=strip_emojis(cat_data.get("name", "CATEGORY")))
                for chan in cat_data.get("channels", []):
                    c_name = format_channel_name(chan.get("name", "channel"))
                    if chan.get("type") == "voice":
                        await cat.create_voice_channel(name=c_name)
                    else:
                        await cat.create_text_channel(name=c_name)
                    await asyncio.sleep(0.1)
            except Exception:
                pass


async def setup(bot: commands.Bot):
    bot.add_view(RobloxVerificationView())
    bot.add_view(RobloxUnsyncView())
    bot.add_view(GiveawayPanelView())
    bot.add_view(NonPremiumDeploymentView(bot=bot))
    bot.add_view(PremiumMissingBotView(bot=bot))
    await bot.add_cog(BotCog(bot))
