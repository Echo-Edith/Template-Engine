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
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient

# -------------------------------------------------------------
# GLOBAL CONSTANTS & LOGGING CHANNELS
# -------------------------------------------------------------
BOT_OWNER_ID = 1219266886143967245
MAIN_GUILD_ID = 1284513257574629458

PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("PREMIUM_DESIGN_CHANNEL_ID", "1563544108696477802"))
NON_PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("NON_PREMIUM_DESIGN_CHANNEL_ID", "1563524101522464900"))
WEBSITE_LOGIN_CHANNEL_ID = int(os.getenv("WEBSITE_LOGIN_CHANNEL_ID", "1563524100276617248"))
SUBSCRIPTION_LOG_CHANNEL_ID = int(os.getenv("SUBSCRIPTION_LOG_CHANNEL_ID", "1564262413514182739"))

GIVEAWAY_BANNER_URL = "https://res.cloudinary.com/dtz0urit6/image/upload/f_png,q_auto/cloudinary-tools-uploads/doob9hmutuwfzw6aloj5.png"

MONGO_URI = os.getenv("MONGO_URI")

db_premium = None
db_build_auth = None
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
        db_build_auth = db["build_authorized_users"]
        db_users = db["persistent_users"]
        db_giveaways = db["giveaways"]
        db_guild_settings = db["guild_settings"]
    except Exception as e:
        print(f"[MongoDB Warning] {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_AUTHORIZED_BUILDERS = set()
LOCAL_PERSISTENT_USERS = {}
LOCAL_GUILD_SETTINGS = {}
USER_POINTS_DATA = {}
ACTIVE_GIVEAWAYS = {}

# In-memory tracking for generated verification codes { user_id: { "code": "TE-12345", "expires_at": float } }
PENDING_VERIFICATION_CODES = {}

# In-memory tracking for used Dev Product receipt IDs to ensure ONE-TIME use
USED_DEV_PRODUCT_RECEIPTS = set()

# In-memory tracking for points anti-spam system
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


async def post_roblox_group_shout(group_id: str, message: str) -> bool:
    """Posts a group shout announcement on Roblox using ROBLOX_COOKIE."""
    cookie = get_formatted_roblox_cookie()
    if not cookie or not group_id or group_id == "0":
        return False

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Cookie": cookie, "User-Agent": "Mozilla/5.0"}
            
            # Get X-CSRF-TOKEN
            async with session.post("https://auth.roblox.com/v2/login", headers=headers) as csrf_resp:
                csrf_token = csrf_resp.headers.get("x-csrf-token")

            if csrf_token:
                headers["X-CSRF-TOKEN"] = csrf_token

            shout_url = f"https://groups.roblox.com/v1/groups/{group_id}/status"
            async with session.patch(shout_url, json={"message": message}, headers=headers) as shout_resp:
                if shout_resp.status == 200:
                    print(f"[Roblox Group Shout] Posted announcement successfully: {message[:30]}...")
                    return True
                else:
                    print(f"[Roblox Group Shout Error] Status {shout_resp.status}")
    except Exception as e:
        print(f"[Roblox Group Shout Exception] {e}")
    return False


# -------------------------------------------------------------
# GIVEAWAY INTERACTIVE VIEWS
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
    def __init__(self, msg_id: str = "0"):
        super().__init__(timeout=None)
        self.msg_id = str(msg_id)

    @discord.ui.button(label="Enter / Leave", style=discord.ButtonStyle.primary, custom_id="btn_gw_enter_toggle")
    async def enter_toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        msg_id_key = str(interaction.message.id)

        gw_data = ACTIVE_GIVEAWAYS.get(msg_id_key)
        if not gw_data and db_giveaways is not None:
            gw_data = db_giveaways.find_one({"message_id": msg_id_key})

        if not gw_data:
            await interaction.followup.send("This giveaway is no longer active or could not be found.", ephemeral=True)
            return

        entries = gw_data.get("entries", [])
        if user_id in entries:
            entries.remove(user_id)
            action_msg = "You have left the giveaway."
        else:
            entries.append(user_id)
            action_msg = "You have entered the giveaway! Good luck!"

        gw_data["entries"] = entries
        ACTIVE_GIVEAWAYS[msg_id_key] = gw_data

        if db_giveaways is not None:
            try:
                db_giveaways.update_one({"message_id": msg_id_key}, {"$set": {"entries": entries}})
            except Exception as e:
                print(f"[Mongo Giveaway Update Error] {e}")

        # Update message footer count
        try:
            embed = interaction.message.embeds[0]
            embed.set_footer(text=f"Entries: {len(entries)} | Hosted by Template Engine")
            await interaction.message.edit(embed=embed)
        except Exception as edit_err:
            print(f"[Giveaway Embed Edit Error] {edit_err}")

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


# -------------------------------------------------------------
# INTERACTIVE UI FOR MISSING BOT & NON-PREMIUM VERIFICATION
# -------------------------------------------------------------
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

        guild = bot_inst.get_guild(target_gid) if target_gid else None
        if not guild and target_gid:
            try:
                guild = await bot_inst.fetch_guild(target_gid)
            except Exception:
                guild = None

        if not guild:
            embed = discord.Embed(
                title="Re-Build Failed",
                description=f"Re-build failed because the bot is still missing from target server ID `{self.target_guild_id}`.\n\nPlease click **Add Bot to Server** above to invite the bot, then click **Build** again.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Bot Detected! Executing Auto-Build...",
            description=f"Successfully joined **{guild.name}**! Nuking old structure and auto-building layout now...",
            color=0x2ECC71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
            embed = discord.Embed(
                title="Roblox Account Unlinked",
                description="Please sync your Roblox account via the account sync channel before verifying purchases.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
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
                            tx_list = data.get("data", [])
                            for tx in tx_list:
                                item_info = tx.get("details", {})
                                item_id = str(item_info.get("id", ""))
                                tx_id = str(tx.get("id") or tx.get("transactionId"))
                                tx_created = str(tx.get("created", ""))
                                
                                token_hash = hashlib.sha256(f"{tx_id}_{tx_created}".encode()).hexdigest()
                                if (dev_product_id == "" or item_id == dev_product_id) and token_hash not in USED_DEV_PRODUCT_RECEIPTS:
                                    is_verified = True
                                    receipt_token = token_hash
                                    break
            except Exception as e:
                print(f"[Cookie Sales Verify Error] {e}")

        if not is_verified:
            embed = discord.Embed(
                title="Purchase Verification Failed",
                description="No unused Roblox Dev Product purchase receipt was detected for your synced Roblox account.\n\nNote: Each purchase receipt can only be used ONCE per server deployment.",
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
            description=f"Single-use purchase receipt verified ({receipt_token[:12]}...). Now building {guild.name}!",
            color=0x2ECC71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        cog = bot_inst.get_cog("BotCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)

    @discord.ui.button(label="Use Points (100 Pts)", style=discord.ButtonStyle.primary, custom_id="btn_use_points_build")
    async def use_points_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        current_pts = get_user_points(user_id)

        if current_pts < 100:
            embed = discord.Embed(
                title="Insufficient Points",
                description=f"You need at least 100 points to deploy this layout.\n\nYour Balance: {current_pts} pts\nMissing: {100 - current_pts} pts",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        new_total = add_user_points(user_id, -100)

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
            description=f"Deducted 100 points!\n\nUpdated Balance: {new_total:,} pts\nNow nuking and building {guild.name}...",
            color=0x8B5CF6
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        cog = bot_inst.get_cog("BotCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)


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
# DISCORD BOT COG IMPLEMENTATION
# -------------------------------------------------------------
class BotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------
    # SLASH COMMAND: ROBLOX PROFILE LOOKUP & INTELLIGENCE
    # -------------------------------------------------------------
    @app_commands.command(name="search", description="Search and audit a Roblox profile for giveaway indicators, badges, and games.")
    @app_commands.describe(query="Roblox Username or Roblox User ID to search.")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=False)
        q = query.strip()

        try:
            async with aiohttp.ClientSession() as session:
                r_id = None
                username = None

                # Resolve query to Roblox User ID
                if q.isdigit():
                    r_id = q
                    async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                        if resp.status == 200:
                            u_data = await resp.json()
                            username = u_data.get("name")
                else:
                    async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [q], "excludeBannedUsers": False}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            user_list = data.get("data", [])
                            if user_list:
                                r_id = str(user_list[0].get("id"))
                                username = user_list[0].get("name")

                if not r_id or not username:
                    await interaction.followup.send(f"Could not find Roblox user matching `{q}`.")
                    return

                # Concurrent API queries
                async with session.get(f"https://users.roblox.com/v1/users/{r_id}") as resp:
                    p_data = await resp.json() if resp.status == 200 else {}

                async with session.get(f"https://groups.roblox.com/v2/users/{r_id}/groups/roles") as resp:
                    g_data = await resp.json() if resp.status == 200 else {}

                async with session.get(f"https://games.roblox.com/v2/users/{r_id}/games?sortOrder=Desc&limit=10") as resp:
                    games_data = await resp.json() if resp.status == 200 else {}

                async with session.get(f"https://badges.roblox.com/v1/users/{r_id}/badges?limit=5&sortOrder=Desc") as resp:
                    badges_data = await resp.json() if resp.status == 200 else {}

                async with session.get(f"https://users.roblox.com/v1/users/{r_id}/username-history?limit=10") as resp:
                    hist_data = await resp.json() if resp.status == 200 else {}

                async with session.get(f"https://thumbnails.roblox.com/v1/users/avatar?userIds={r_id}&size=720x720&format=Png&isCircular=false") as resp:
                    thumb_data = await resp.json() if resp.status == 200 else {}

                # Parse user profile details
                display_name = p_data.get("displayName", username)
                created_raw = p_data.get("created", "")
                created_str = "Unknown"
                age_str = "Unknown"

                if created_raw:
                    try:
                        dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                        created_str = dt.strftime("%B %d, %Y")
                        days = (datetime.now(timezone.utc) - dt).days
                        age_str = f"{days:,} days ({days // 365} yrs)"
                    except Exception:
                        pass

                # Parse groups & check membership in Template Engine Roblox Group
                groups_list = g_data.get("data", [])
                formatted_groups = []
                giveaway_indicators = []

                te_group_id = str(os.getenv("ROBLOX_GROUP_ID", "")).strip()
                in_te_group = False
                te_role_name = None

                gw_keywords = ["robux", "giveaway", "rbx", "free", "rewards", "payout", "claim"]

                for item in groups_list:
                    grp = item.get("group", {})
                    g_name = grp.get("name", "Group")
                    g_id = str(grp.get("id", ""))
                    role = item.get("role", {}).get("name", "Member")
                    g_desc = grp.get("description", "").lower()
                    g_shout = (grp.get("shout") or {}).get("body", "").lower() if isinstance(grp.get("shout"), dict) else ""

                    # Check if this group matches the Template Engine Group ID
                    if te_group_id and g_id == te_group_id:
                        in_te_group = True
                        te_role_name = role

                    # Check for giveaway indicators
                    matches = [kw for kw in gw_keywords if kw in g_name.lower() or kw in g_desc or kw in g_shout]
                    if matches:
                        giveaway_indicators.append(f"• **{g_name}** — Keyword detected: `{', '.join(set(matches))}`")

                # Format Template Engine Group field
                if te_group_id:
                    if in_te_group:
                        formatted_groups.append(f"• **[Template Engine Group](https://www.roblox.com/groups/{te_group_id})**\n   └─ Status: `Member` ✅\n   └─ Role: `{te_role_name}`")
                    else:
                        formatted_groups.append(f"• **[Template Engine Group](https://www.roblox.com/groups/{te_group_id})**\n   └─ Status: `Not a Member` ❌")
                else:
                    formatted_groups.append("• *ROBLOX_GROUP_ID not configured in environment.*")

                # Parse experiences
                exp_list = games_data.get("data", [])
                formatted_exp = []
                for game in exp_list[:3]:
                    g_title = game.get("name", "Experience")
                    visits = game.get("placeVisits", 0)
                    formatted_exp.append(f"• **{g_title}**\n   └─ Visits: `{visits:,}`")

                # Parse badges
                badge_list = badges_data.get("data", [])
                formatted_badges = [f"• {b.get('name')}" for b in badge_list[:3]]

                # Parse username history
                names_hist = hist_data.get("data", [])
                formatted_hist = [f"• {n.get('name')}" for n in names_hist[:5]]

                # Build final output embed
                avatar_url = ""
                if thumb_data.get("data"):
                    avatar_url = thumb_data["data"][0].get("imageUrl", "")

                embed = discord.Embed(
                    title="🔎 Roblox Profile Lookup",
                    color=0x8B5CF6
                )
                if avatar_url:
                    embed.set_thumbnail(url=avatar_url)

                user_field = (
                    f"**Username:** {username}\n"
                    f"**Display Name:** {display_name}\n"
                    f"**User ID:** `{r_id}`\n"
                    f"**Account Created:** {created_str}\n"
                    f"**Account Age:** {age_str}"
                )
                embed.add_field(name="👤 USER", value=user_field, inline=False)

                if formatted_groups:
                    embed.add_field(name="👥 GROUPS", value="\n".join(formatted_groups), inline=False)

                if giveaway_indicators:
                    embed.add_field(name="💰 GIVEAWAY INDICATORS", value="\n".join(giveaway_indicators), inline=False)

                if formatted_exp:
                    embed.add_field(name="🎮 EXPERIENCES", value="\n".join(formatted_exp), inline=False)

                if formatted_badges:
                    embed.add_field(name="🏆 BADGES", value="\n".join(formatted_badges), inline=False)

                if formatted_hist:
                    embed.add_field(name="📜 USERNAME HISTORY", value="\n".join(formatted_hist), inline=False)

                embed.set_footer(text="Template Engine Roblox Intelligence Audit")
                await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"[Roblox Search Command Error] {e}")
            await interaction.followup.send(f"An error occurred while searching Roblox profile: `{str(e)}`")

    # -------------------------------------------------------------
    # SLASH COMMAND: GIVEAWAY SYSTEM (CREATE / EDIT / END)
    # -------------------------------------------------------------
    @app_commands.command(name="giveaway", description="Create, edit, or end server giveaways with Roblox group shout integration.")
    @app_commands.describe(
        action="Action to perform: create, edit, or end.",
        channel="Target channel to post or announce the giveaway.",
        duration="Giveaway duration e.g. 1h, 1d, 7d.",
        prize="The prize being given away.",
        description="Custom details for the giveaway.",
        message_link="Message ID or link for editing or ending a giveaway."
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
            embed = discord.Embed(
                title="Permission Denied",
                description="This command is strictly restricted to the bot owner.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        target_channel = channel or interaction.channel

        if action.value == "create":
            dur_td = parse_duration_to_timedelta(duration or "1d")
            end_dt = datetime.now(timezone.utc) + dur_td
            end_ts = int(end_dt.timestamp())

            embed = discord.Embed(
                title=f"🎉 GIVEAWAY: {prize}",
                description=f"{description}\n\n**Ends:** <t:{end_ts}:R>\n**Hosted By:** {interaction.user.mention}",
                color=0x8B5CF6
            )
            embed.set_image(url=GIVEAWAY_BANNER_URL)
            embed.set_footer(text="Entries: 0 | Hosted by Template Engine")

            view = GiveawayPanelView()
            gw_msg = await target_channel.send(embed=embed, view=view)

            gw_data = {
                "message_id": str(gw_msg.id),
                "channel_id": str(target_channel.id),
                "guild_id": str(interaction.guild_id),
                "prize": prize,
                "description": description,
                "end_timestamp": end_ts,
                "entries": [],
                "created_at": datetime.now(timezone.utc).isoformat()
            }

            ACTIVE_GIVEAWAYS[str(gw_msg.id)] = gw_data
            if db_giveaways is not None:
                try:
                    db_giveaways.insert_one(gw_data)
                except Exception as e:
                    print(f"[Mongo Save Giveaway Error] {e}")

            # Post Roblox group shout announcement
            roblox_group_id = os.getenv("ROBLOX_GROUP_ID", "").strip()
            if roblox_group_id:
                shout_msg = f"🎉 NEW GIVEAWAY: {prize}! Join our community server to enter!"
                asyncio.create_task(post_roblox_group_shout(roblox_group_id, shout_msg))

            confirm_embed = discord.Embed(
                title="Giveaway Created!",
                description=f"Giveaway successfully announced in {target_channel.mention}!",
                color=0x2ECC71
            )
            await interaction.followup.send(embed=confirm_embed, ephemeral=True)

        elif action.value == "edit":
            if not message_link:
                await interaction.followup.send("Please provide the message ID or link to edit the giveaway.", ephemeral=True)
                return

            msg_id = message_link.split("/")[-1].strip()
            gw_data = ACTIVE_GIVEAWAYS.get(msg_id)
            if not gw_data and db_giveaways is not None:
                gw_data = db_giveaways.find_one({"message_id": msg_id})

            if not gw_data:
                await interaction.followup.send(f"Could not find an active giveaway matching ID `{msg_id}`.", ephemeral=True)
                return

            end_ts = gw_data.get("end_timestamp", int(datetime.now(timezone.utc).timestamp()))
            gw_channel_id = int(gw_data.get("channel_id", target_channel.id))
            gw_channel = self.bot.get_channel(gw_channel_id) or target_channel

            try:
                target_msg = await gw_channel.fetch_message(int(msg_id))
                embed = target_msg.embeds[0]
                embed.title = f"🎉 GIVEAWAY: {prize}"
                embed.description = f"{description}\n\n**Ends:** <t:{end_ts}:R>\n**Hosted By:** {interaction.user.mention}"
                embed.set_image(url=GIVEAWAY_BANNER_URL)
                await target_msg.edit(embed=embed)

                gw_data["prize"] = prize
                gw_data["description"] = description
                ACTIVE_GIVEAWAYS[msg_id] = gw_data

                if db_giveaways is not None:
                    db_giveaways.update_one({"message_id": msg_id}, {"$set": {"prize": prize, "description": description}})

                await interaction.followup.send("Giveaway details updated successfully!", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Failed to edit giveaway message: `{str(e)}`", ephemeral=True)

        elif action.value == "end":
            if not message_link:
                await interaction.followup.send("Please provide the message ID or link to end the giveaway.", ephemeral=True)
                return

            msg_id = message_link.split("/")[-1].strip()
            gw_data = ACTIVE_GIVEAWAYS.get(msg_id)
            if not gw_data and db_giveaways is not None:
                gw_data = db_giveaways.find_one({"message_id": msg_id})

            if not gw_data:
                await interaction.followup.send(f"Could not find an active giveaway matching ID `{msg_id}`.", ephemeral=True)
                return

            await self.end_giveaway_logic(gw_data)
            await interaction.followup.send("Giveaway ended early successfully!", ephemeral=True)

    async def end_giveaway_logic(self, gw_data: dict):
        msg_id = str(gw_data.get("message_id"))
        channel_id = int(gw_data.get("channel_id", 0))
        entries = gw_data.get("entries", [])
        prize = gw_data.get("prize", "Prize")

        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None

        winner_mention = "No valid entrants"
        if entries:
            winner_id = random.choice(entries)
            winner_mention = f"<@{winner_id}>"

        if channel:
            try:
                target_msg = await channel.fetch_message(int(msg_id))
                embed = target_msg.embeds[0]
                embed.title = f"🎉 GIVEAWAY ENDED: {prize}"
                embed.description = f"**Winner:** {winner_mention}\n**Total Entries:** {len(entries)}"
                embed.color = 0x2ECC71
                embed.set_image(url=GIVEAWAY_BANNER_URL)
                await target_msg.edit(embed=embed, view=None)

                if entries:
                    await channel.send(f"Congratulations {winner_mention}! You won **{prize}**!")
            except Exception as e:
                print(f"[End Giveaway Logic Error] {e}")

        ACTIVE_GIVEAWAYS.pop(msg_id, None)
        if db_giveaways is not None:
            try:
                db_giveaways.delete_one({"message_id": msg_id})
            except Exception as e:
                print(f"[Mongo Delete Giveaway Error] {e}")

    # -------------------------------------------------------------
    # SLASH COMMAND: SET VERIFICATION OR UNSYNC INTERACTIVE PANELS
    # -------------------------------------------------------------
    @app_commands.command(name="set", description="Deploy verification or un-sync interactive embed panels into a channel.")
    @app_commands.describe(
        action="Select whether to deploy the verification or un-sync panel.",
        channel="The target channel to send the embed panel into.",
        description="Custom description text for the panel."
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
            embed = discord.Embed(
                title="Permission Denied",
                description="This command is strictly restricted to the bot owner.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
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

            confirm_embed = discord.Embed(
                title="Panel Successfully Deployed",
                description=f"Deployed **{action.name}** into {channel.mention}.",
                color=0x2ECC71
            )
            await interaction.followup.send(embed=confirm_embed, ephemeral=True)

        except Exception as e:
            print(f"[Set Command Error] {e}")
            await interaction.followup.send(f"Failed to deploy panel: `{str(e)}`", ephemeral=True)

    # -------------------------------------------------------------
    # EVENT LISTENER: WELCOME BONUS (20 POINTS) & WELCOME MESSAGE
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        add_user_points(str(member.id), 20)
        print(f"[Welcome Bonus] Awarded 20 points to new member: {member.name} ({member.id})")

        # Check for configured welcome channel
        guild_id_str = str(member.guild.id)
        settings = LOCAL_GUILD_SETTINGS.get(guild_id_str)

        if not settings and db_guild_settings is not None:
            try:
                settings = db_guild_settings.find_one({"guild_id": guild_id_str})
                if settings:
                    LOCAL_GUILD_SETTINGS[guild_id_str] = settings
            except Exception as e:
                print(f"[Mongo Fetch Welcome Error] {e}")

        if settings and settings.get("welcome_channel_id"):
            channel_id = int(settings["welcome_channel_id"])
            channel = member.guild.get_channel(channel_id)

            if channel:
                raw_msg = settings.get("welcome_message", "Welcome {user} to **{server}**!")
                formatted_msg = raw_msg.replace("{user}", member.mention).replace("{username}", member.name).replace("{server}", member.guild.name)

                embed = discord.Embed(
                    title=f"Welcome to {member.guild.name}!",
                    description=formatted_msg,
                    color=0x8B5CF6
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=f"Member #{member.guild.member_count}")

                try:
                    await channel.send(embed=embed)
                except Exception as send_err:
                    print(f"[Welcome Send Error] {send_err}")

    # -------------------------------------------------------------
    # SLASH COMMAND: SET WELCOME CHANNEL AND MESSAGE
    # -------------------------------------------------------------
    @app_commands.command(name="welcome-set", description="Configure the automated welcome greeting channel and custom message.")
    @app_commands.describe(
        channel="The channel where welcome greetings will be posted.",
        message="Custom message template. Variables: {user}, {username}, {server}."
    )
    async def welcome_set_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: Optional[str] = "Welcome {user} to **{server}**! Enjoy your stay!"
    ):
        await interaction.response.defer(ephemeral=True)

        if interaction.user.id != BOT_OWNER_ID:
            embed = discord.Embed(
                title="Permission Denied",
                description="This command is strictly restricted to the bot owner.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        guild_id_str = str(interaction.guild_id)
        settings_payload = {
            "guild_id": guild_id_str,
            "welcome_channel_id": str(channel.id),
            "welcome_message": message
        }

        LOCAL_GUILD_SETTINGS[guild_id_str] = settings_payload

        if db_guild_settings is not None:
            try:
                db_guild_settings.update_one(
                    {"guild_id": guild_id_str},
                    {"$set": settings_payload},
                    upsert=True
                )
            except Exception as e:
                print(f"[Mongo Save Welcome Error] {e}")

        embed = discord.Embed(
            title="Welcome Greeting Configured",
            description=f"Automated welcome greetings will now be sent to {channel.mention}.\n\n**Template:**\n```{message}```\n**Supported Tags:** `{user}` (mention), `{username}` (name), `{server}` (server name)",
            color=0x2ECC71
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------
    # SLASH COMMAND: CHECK USER POINTS
    # -------------------------------------------------------------
    @app_commands.command(name="points", description="Check your or another member's current point balance.")
    @app_commands.describe(user="Optional user to check points for (defaults to yourself).")
    async def points_cmd(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await interaction.response.defer(ephemeral=False)
        target_user = user or interaction.user
        pts = get_user_points(str(target_user.id))

        embed = discord.Embed(
            title="Point Balance",
            description=f"{target_user.mention} currently has {pts:,} points.",
            color=0x8B5CF6
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------
    # SLASH COMMAND: LEADERBOARD
    # -------------------------------------------------------------
    @app_commands.command(name="leaderboard", description="View the top point earners in the server.")
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        top_users = get_top_leaderboard(10)

        if not top_users:
            embed = discord.Embed(
                title="Point Leaderboard",
                description="No active point rankings recorded yet!",
                color=0x8B5CF6
            )
            await interaction.followup.send(embed=embed)
            return

        description_lines = []
        for idx, entry in enumerate(top_users):
            rank_str = f"#{idx + 1}"
            uid = entry["discord_id"]
            pts = entry["points"]
            description_lines.append(f"{rank_str} <@{uid}> - {pts:,} pts")

        embed = discord.Embed(
            title="Point Leaderboard",
            description="\n".join(description_lines),
            color=0xF1C40F
        )
        embed.set_footer(text="Earn +2 points per message | +20 on join")
        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------
    # SLASH COMMAND: SET / MODIFY USER POINTS
    # -------------------------------------------------------------
    @app_commands.command(name="points-set", description="Add or deduct points for a specified user.")
    @app_commands.describe(
        user="The target user to modify points for.",
        action="Select whether to add or deduct points.",
        amount="The amount of points to add or deduct."
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
            embed = discord.Embed(
                title="Permission Denied",
                description="This command is strictly restricted to the bot owner.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if amount <= 0:
            embed = discord.Embed(
                title="Invalid Amount",
                description="Please enter a positive point value.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        delta = amount if action.value == "add" else -amount
        new_total = add_user_points(str(user.id), delta)

        action_label = "added to" if action.value == "add" else "deducted from"
        color = 0x2ECC71 if action.value == "add" else 0xF43F5E

        embed = discord.Embed(
            title="Points Balance Updated",
            description=f"Successfully {action_label} {user.mention}!\n\nAmount Changed: {'+' if delta > 0 else ''}{delta:,} pts\nNew Balance: {new_total:,} pts",
            color=color
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------
    # SLASH COMMAND: SET / REMOVE PREMIUM STATUS
    # -------------------------------------------------------------
    @app_commands.command(name="premium-set", description="Grant or revoke TE Premium subscription status for a user.")
    @app_commands.describe(
        user="Target user to modify premium status for.",
        action="Select whether to grant or revoke premium.",
        duration="Duration format e.g. 1d, 7d, 30d, 1y, inf (Defaults to 30d)."
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
            embed = discord.Embed(
                title="Permission Denied",
                description="This command is strictly restricted to the bot owner.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            if action.value == "add":
                duration_td = parse_duration_to_timedelta(duration or "30d")
                set_user_premium(str(user.id), duration_td)
                exp_date = datetime.now(timezone.utc) + duration_td
                exp_ts = int(exp_date.timestamp())

                embed = discord.Embed(
                    title="TE Premium Granted",
                    description=f"Successfully granted TE Premium to {user.mention}!\n\nDuration: {duration}\nExpires: <t:{exp_ts}:F> (<t:{exp_ts}:R>)",
                    color=0xF1C40F
                )
            else:
                remove_user_premium(str(user.id))
                embed = discord.Embed(
                    title="TE Premium Revoked",
                    description=f"Successfully revoked TE Premium access from {user.mention}.",
                    color=0xF43F5E
                )

            embed.set_thumbnail(url=user.display_avatar.url)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[Premium Set Error] {e}")
            embed = discord.Embed(
                title="Error Modifying Premium Status",
                description=f"An error occurred: {str(e)}",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

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

        if not is_premium_submission and str(message.channel.id) != str(PREMIUM_DESIGN_CHANNEL_ID):
            print(f"[Frontend Direct DM] Initiating direct DM with 5 retries to submitter {submitting_user_id}...")
            await self.send_non_premium_verification_dm(submitting_user_id, target_guild_id, layout, guild.name, message.channel)
        else:
            print(f"[Auto-Build] Premium deployment for {guild.name} ({guild.id})")
            await self.execute_auto_build(guild, layout)

    async def send_non_premium_verification_dm(self, user_id: str, target_guild_id: str, layout: dict, guild_name: str, fallback_channel: Optional[discord.TextChannel] = None):
        dev_product_url = "https://www.roblox.com/games/6094950399/Template-Engine#!/store"
        user_pts = get_user_points(user_id)

        embed = discord.Embed(
            title="Server Deployment Options",
            description=f"<@{user_id}> Your blueprint for **{guild_name}** has been submitted!\n\nTo build this layout, select one of the deployment options below:",
            color=0x8B5CF6
        )
        embed.add_field(
            name="Option 1: Buy Dev Product",
            value="Purchase the single-use Roblox Developer Product, then click **Verify Purchase**.",
            inline=False
        )
        embed.add_field(
            name="Option 2: Redeem Points",
            value=f"Use **100 server points** to deploy immediately.\nYour Points: {user_pts:,} pts",
            inline=False
        )

        view = NonPremiumDeploymentView(
            bot=self.bot,
            submitting_user_id=user_id,
            target_guild_id=target_guild_id,
            layout=layout,
            dev_product_url=dev_product_url
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

        # Fallback 1: Target Guild
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

        # Fallback 2: Submission Channel
        if fallback_channel:
            try:
                await fallback_channel.send(content=f"<@{user_id}>", embed=embed, view=view)
                print(f"[Fallback Success] Posted deployment embed in submission channel #{fallback_channel.name}")
            except Exception as chan_err:
                print(f"[Fallback Channel Send Error] Could not post to submission channel: {chan_err}")

    async def notify_missing_bot(self, user_id: str, target_guild_id: str, layout: dict = None):
        try:
            user = await self.bot.fetch_user(int(user_id))
            if user:
                embed = discord.Embed(
                    title="Bot Missing in Target Server",
                    description=f"Auto-deployment paused. The bot is not present in target server ID `{target_guild_id}`.\n\nPlease add the bot using the link below, then click **Build**.",
                    color=0xF43F5E
                )
                view = PremiumMissingBotView(bot=self.bot, submitting_user_id=user_id, target_guild_id=target_guild_id, layout=layout)
                await user.send(embed=embed, view=view)
        except Exception as e:
            print(f"[DM Warning] Failed sending DM to {user_id}: {e}")

    async def nuke_guild_structure(self, guild: discord.Guild):
        """Deletes existing channels, categories, and custom roles prior to auto-building."""
        for channel in guild.channels:
            try:
                await channel.delete(reason="Template Engine Nuke Before Auto-Build")
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"[Nuke Channel Warning] Failed to delete {channel.name}: {e}")

        for role in guild.roles:
            if not role.is_default() and not role.managed and role.position < guild.me.top_role.position:
                try:
                    await role.delete(reason="Template Engine Nuke Before Auto-Build")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    print(f"[Nuke Role Warning] Failed to delete role {role.name}: {e}")

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

                try:
                    role = await guild.create_role(
                        name=role_name,
                        color=discord.Color(color_int),
                        reason="Template Engine Auto-Build"
                    )
                    created_roles[role_name] = role
                    await asyncio.sleep(0.2)
                except Exception as e:
                    print(f"[Auto-Build Role Error] {role_name}: {e}")

            categories = layout.get("categories", [])
            for cat_data in categories:
                cat_raw_name = cat_data.get("name", "CATEGORY")
                cat_clean_name = strip_emojis(cat_raw_name) or cat_raw_name

                try:
                    category = await guild.create_category(
                        name=cat_clean_name,
                        reason="Template Engine Auto-Build"
                    )
                    await asyncio.sleep(0.2)

                    for chan in cat_data.get("channels", []):
                        chan_raw_name = chan.get("name") or chan.get("clean_name") or "channel"
                        chan_clean_name = format_channel_name(chan_raw_name)
                        chan_type = chan.get("type", "text")
                        chan_topic = chan.get("topic", "")

                        if chan_type == "voice":
                            await category.create_voice_channel(
                                name=chan_clean_name,
                                reason="Template Engine Auto-Build"
                            )
                        elif chan_type == "stage":
                            await category.create_stage_channel(
                                name=chan_clean_name,
                                topic=chan_topic,
                                reason="Template Engine Auto-Build"
                            )
                        elif chan_type == "forum":
                            await category.create_forum_channel(
                                name=chan_clean_name,
                                topic=chan_topic,
                                reason="Template Engine Auto-Build"
                            )
                        else:
                            await category.create_text_channel(
                                name=chan_clean_name,
                                topic=chan_topic,
                                reason="Template Engine Auto-Build"
                            )
                        await asyncio.sleep(0.2)
                except Exception as e:
                    print(f"[Auto-Build Cat Error] {cat_clean_name}: {e}")
        except Exception as top_err:
            print(f"[Auto-Build Execution Error] {top_err}")


async def setup(bot: commands.Bot):
    # Register persistent views for static panels across redeployments
    bot.add_view(RobloxVerificationView())
    bot.add_view(RobloxUnsyncView())
    bot.add_view(GiveawayPanelView())
    bot.add_view(NonPremiumDeploymentView(bot=bot))
    bot.add_view(PremiumMissingBotView(bot=bot))
    await bot.add_cog(BotCog(bot))
