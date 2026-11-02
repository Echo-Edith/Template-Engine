import os
import re
import json
import random
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient

# List of known Roblox giveaway/influencer group indicators for heuristic detection
GIVEAWAY_KEYWORDS = ["giveaway", "robux", "rbx", "free robux", "rewards", "payout", "claim", "spin", "win"]

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

        cog = bot_inst.get_cog("BotCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)


# -------------------------------------------------------------
# INTERACTIVE UI FOR NON-PREMIUM VERIFICATION & POINTS PURCHASE
# -------------------------------------------------------------
class NonPremiumDeploymentView(discord.ui.View):
    def __init__(self, bot=None, submitting_user_id: str = "0", target_guild_id: str = "0", layout: dict = None, dev_product_url: str = ""):
        super().__init__(timeout=None)
        self.bot = bot
        self.submitting_user_id = str(submitting_user_id)
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}

        final_url = dev_product_url or "https://www.roblox.com/games/6094950399/Template-Engine#!/store"
        self.add_item(discord.ui.Button(label="Buy Roblox Dev Product", url=final_url, style=discord.ButtonStyle.link))

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
# DISCORD BOT COG IMPLEMENTATION
# -------------------------------------------------------------
class BotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

        is_owner = (interaction.user.id == BOT_OWNER_ID)
        is_admin = interaction.permissions.administrator if interaction.permissions else False

        if not (is_owner or is_admin):
            embed = discord.Embed(
                title="Permission Denied",
                description="You need Administrator permissions to deploy system panels.",
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
    # EVENT LISTENER: WELCOME BONUS (20 POINTS)
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        add_user_points(str(member.id), 20)
        print(f"[Welcome Bonus] Awarded 20 points to new member: {member.name} ({member.id})")

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

        is_owner = (interaction.user.id == BOT_OWNER_ID)
        is_admin = interaction.permissions.administrator if interaction.permissions else False

        if not (is_owner or is_admin):
            embed = discord.Embed(
                title="Permission Denied",
                description="You need Administrator permissions to modify user points.",
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

        is_owner = (interaction.user.id == BOT_OWNER_ID)
        is_admin = interaction.permissions.administrator if interaction.permissions else False

        if not (is_owner or is_admin):
            embed = discord.Embed(
                title="Permission Denied",
                description="You need Administrator permissions to manage TE Premium memberships.",
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
    # SLASH COMMAND: ROBLOX USER INTELLIGENCE SEARCH
    # -------------------------------------------------------------
    @app_commands.command(name="search", description="Lookup detailed Roblox profile info, groups, giveaway risk, games, badges, and history.")
    @app_commands.describe(username_or_id="Exact Roblox username or numeric Roblox User ID to search.")
    async def search_cmd(self, interaction: discord.Interaction, username_or_id: str):
        await interaction.response.defer(ephemeral=False)
        query = username_or_id.strip()

        roblox_id = None
        username = ""
        display_name = ""
        created_at_str = "Unknown"
        account_age_str = "Unknown"

        async with aiohttp.ClientSession() as session:
            # 1. Resolve Username or User ID
            if query.isdigit():
                roblox_id = query
            else:
                async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [query], "excludeBannedUsers": False}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        user_list = data.get("data", [])
                        if user_list:
                            roblox_id = str(user_list[0].get("id"))
                            username = user_list[0].get("name")
                            display_name = user_list[0].get("displayName", username)

            if not roblox_id:
                embed = discord.Embed(
                    title="User Not Found",
                    description=f"Could not locate Roblox account for `{query}`.",
                    color=0xF43F5E
                )
                await interaction.followup.send(embed=embed)
                return

            # 2. Fetch Detailed User Details
            async with session.get(f"https://users.roblox.com/v1/users/{roblox_id}") as resp:
                if resp.status == 200:
                    u_data = await resp.json()
                    username = u_data.get("name", username)
                    display_name = u_data.get("displayName", username)
                    created_raw = u_data.get("created", "")
                    if created_raw:
                        try:
                            dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                            created_at_str = dt.strftime("%B %d, %Y")
                            days_old = (datetime.now(timezone.utc) - dt).days
                            years = days_old // 365
                            rem_days = days_old % 365
                            account_age_str = f"{years} yr{'s' if years != 1 else ''}, {rem_days} day{'s' if rem_days != 1 else ''} ({days_old} days)"
                        except Exception:
                            pass

            # 3. Fetch Avatar Headshot Thumbnail
            avatar_url = ""
            async with session.get(f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={roblox_id}&size=420x420&format=Png&isCircular=false") as resp:
                if resp.status == 200:
                    thumb_data = await resp.json()
                    if thumb_data.get("data"):
                        avatar_url = thumb_data["data"][0].get("imageUrl", "")

            # 4. Fetch User Groups & Giveaway Risk Analysis
            groups_lines = []
            giveaway_indicators = []
            async with session.get(f"https://groups.roblox.com/v2/users/{roblox_id}/groups/roles") as resp:
                if resp.status == 200:
                    g_data = await resp.json()
                    g_list = g_data.get("data", [])[:10]
                    for idx, g in enumerate(g_list, start=1):
                        group_info = g.get("group", {})
                        g_id = group_info.get("id")
                        g_name = group_info.get("name", "Group")
                        role_name = g.get("role", {}).get("name", "Member")
                        g_desc = group_info.get("description", "").lower()
                        g_shout = (group_info.get("shout") or {}).get("body", "").lower()
                        owner_info = group_info.get("owner", {})
                        owner_name = owner_info.get("username", "Unknown") if owner_info else "No Owner"

                        # Hyperlink group name to Roblox Group URL
                        g_link = f"[{g_name}](https://www.roblox.com/groups/{g_id})"
                        groups_lines.append(f"**{idx}. {g_link}**\n└─ **Role:** {role_name}\n└─ **Owner:** {owner_name}")

                        # Giveaway Keyword Detection Heuristics
                        matched_kw = [kw for kw in GIVEAWAY_KEYWORDS if kw in g_name.lower() or kw in g_desc or kw in g_shout]
                        if matched_kw:
                            giveaway_indicators.append(f"- **{g_name}** — Giveaway keywords detected ({', '.join(matched_kw[:3])})")

            # 5. Fetch User Experiences / Games Created
            experiences_lines = []
            async with session.get(f"https://games.roblox.com/v2/users/{roblox_id}/games?accessFilter=Public&limit=5") as resp:
                if resp.status == 200:
                    game_data = await resp.json()
                    games = game_data.get("data", [])
                    for game in games[:3]:
                        g_title = game.get("name", "Experience")
                        visits = game.get("placeVisits", 0)
                        creator_name = game.get("creator", {}).get("name", username)
                        experiences_lines.append(f"- **{g_title}**\n  └─ **Visits:** {visits:,}\n  └─ **Creator:** {creator_name}")

            # 6. Fetch Badges
            badges_lines = []
            async with session.get(f"https://badges.roblox.com/v1/users/{roblox_id}/badges?limit=5&sortOrder=Desc") as resp:
                if resp.status == 200:
                    badge_data = await resp.json()
                    b_list = badge_data.get("data", [])
                    for b in b_list[:3]:
                        b_name = b.get("name", "Badge")
                        badges_lines.append(f"- {b_name}")

            # 7. Fetch Username History
            history_lines = []
            async with session.get(f"https://users.roblox.com/v1/users/{roblox_id}/username-history?limit=10&sortOrder=Desc") as resp:
                if resp.status == 200:
                    h_data = await resp.json()
                    h_list = h_data.get("data", [])
                    for h in h_list:
                        history_lines.append(f"- {h.get('name')}")

        # Construct Structured Response
        embed = discord.Embed(
            title="🔎 Roblox Lookup",
            description="━━━━━━━━━━━━━━━━",
            color=0x8B5CF6
        )

        user_block = (
            f"**Username:** {username}\n"
            f"**Display Name:** {display_name}\n"
            f"**User ID:** `{roblox_id}`\n"
            f"**Account Created:** {created_at_str}\n"
            f"**Account Age:** {account_age_str}"
        )
        embed.add_field(name="👤 USER", value=user_block, inline=False)

        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        groups_block = "\n\n".join(groups_lines) if groups_lines else "No public groups found."
        embed.add_field(name="👥 GROUPS", value=groups_block[:1024], inline=False)

        giveaway_block = "\n".join(giveaway_indicators) if giveaway_indicators else "None detected."
        embed.add_field(name="💰 GIVEAWAY INDICATORS", value=giveaway_block[:1024], inline=False)

        exp_block = "\n".join(experiences_lines) if experiences_lines else "No public experiences created."
        embed.add_field(name="🎮 EXPERIENCES", value=exp_block[:1024], inline=False)

        badges_block = "\n".join(badges_lines) if badges_lines else "No recent badges."
        embed.add_field(name="🏆 BADGES", value=badges_block[:1024], inline=False)

        history_block = "\n".join(history_lines) if history_lines else "No previous usernames recorded."
        embed.add_field(name="📜 HISTORY\nPrevious usernames:", value=history_block[:1024], inline=False)

        await interaction.followup.send(embed=embed)

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

        if not is_premium_submission and str(message.channel.id) != str(PREMIUM_DESIGN_CHANNEL_ID):
            print(f"[Frontend Direct DM] Initiating direct DM with 5 retries to submitter {submitting_user_id} before logging...")
            await self.send_non_premium_verification_dm(submitting_user_id, target_guild_id, layout, guild.name, message.channel)
        else:
            print(f"[Auto-Build] Premium deployment for {guild.name} ({guild.id})")
            await self.execute_auto_build(guild, layout)

    async def send_non_premium_verification_dm(self, user_id: str, target_guild_id: str, layout: dict, guild_name: str, fallback_channel: Optional[discord.TextChannel] = None):
        dev_product_url = os.getenv("ROBLOX_DEV_PRODUCT_URL", "https://www.roblox.com/games/6094950399/Template-Engine#!/store").strip()
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
                        elif chan_type in ["forum", "stage"]:
                            await category.create_text_channel(
                                name=chan_clean_name,
                                topic=f"[{chan_type.upper()}] {chan_topic}",
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
    # Register persistent views for static panels
    bot.add_view(RobloxVerificationView())
    bot.add_view(RobloxUnsyncView())
    bot.add_view(PremiumMissingBotView())
    await bot.add_cog(BotCog(bot))
