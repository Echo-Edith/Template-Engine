import os
import re
import json
import random
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient

# --- CONSTANTS & CONFIGURATION ---
BOT_OWNER_ID = 1219266886143967245
MAIN_OWNER_GUILD_ID = 1284513257574629458

PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("PREMIUM_DESIGN_CHANNEL_ID", "1563544108696477802"))
NON_PREMIUM_DESIGN_CHANNEL_ID = int(os.getenv("NON_PREMIUM_DESIGN_CHANNEL_ID", "1563524101522464900"))

SUBSCRIPTION_LOG_CHANNEL_ID = 1564262413514182739
MONTHLY_ANALYTICS_CHANNEL_ID = 1564263447057670154

SYSTEM_LOG_WEBHOOK_URL = (os.environ.get("SYSTEM_LOG_WEBHOOK_URL") or "").strip()
DESIGN_WEBHOOK_URL = (os.environ.get("DESIGN_WEBHOOK_URL") or "").strip()
MONGO_URI = os.getenv("MONGO_URI")

ROBLOX_DEV_PRODUCT_ID = (os.environ.get("ROBLOX_DEV_PRODUCT_ID") or "").strip()
ROBLOX_OPEN_CLOUD_KEY = (os.environ.get("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_UNIVERSE_ID = (os.environ.get("ROBLOX_UNIVERSE_ID") or "").strip()

# --- DATABASE / IN-MEMORY FALLBACK STORAGE ---
db_users = None
db_points = None
db_premium = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_users = db["persistent_users"]
        db_points = db["user_points"]
        db_premium = db["premium_users"]
        print("[MongoDB] BotCog connected to database successfully.")
    except Exception as e:
        print(f"[MongoDB Error in BotCog] {e}")

LOCAL_USER_POINTS = {}
LOCAL_PREMIUM_USERS = {}
PENDING_VERIFICATION_CODES = {}
USED_DEV_PRODUCT_RECEIPTS = set()

# Anti-Spam & Cooldown Tracking
USER_LAST_MESSAGE_TIME = {}
USER_SPAM_COUNTS = {}
USER_PENALTY_UNTIL = {}

# --- HELPER FUNCTIONS FOR POINTS & PREMIUM ---
def get_user_points(discord_id: str) -> int:
    if db_points is not None:
        try:
            doc = db_points.find_one({"discord_id": str(discord_id)})
            if doc:
                return int(doc.get("points", 0))
        except Exception as e:
            print(f"[Mongo Get Points Error] {e}")
    return LOCAL_USER_POINTS.get(str(discord_id), 0)


def set_user_points(discord_id: str, points: int):
    points = max(0, points)
    if db_points is not None:
        try:
            db_points.update_one(
                {"discord_id": str(discord_id)},
                {"$set": {"points": points, "updated_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True
            )
        except Exception as e:
            print(f"[Mongo Set Points Error] {e}")
    LOCAL_USER_POINTS[str(discord_id)] = points


def add_user_points(discord_id: str, amount: int) -> int:
    current = get_user_points(discord_id)
    new_total = max(0, current + amount)
    set_user_points(discord_id, new_total)
    return new_total


def is_user_premium(discord_id: str) -> bool:
    if db_premium is not None:
        try:
            doc = db_premium.find_one({"discord_id": str(discord_id)})
            if doc:
                return True
        except Exception as e:
            print(f"[Mongo Check Premium Error] {e}")
    return str(discord_id) in LOCAL_PREMIUM_USERS


def save_user_roblox_link(discord_id: str, discord_username: str, roblox_id: str, roblox_username: str):
    data = {
        "discord_id": str(discord_id),
        "discord_username": discord_username,
        "roblox_data": {
            "roblox_id": str(roblox_id),
            "roblox_username": roblox_username,
            "linked_at": datetime.now(timezone.utc).isoformat()
        }
    }
    if db_users is not None:
        try:
            db_users.update_one({"discord_id": str(discord_id)}, {"$set": data}, upsert=True)
        except Exception as e:
            print(f"[Mongo Save Roblox Link Error] {e}")


def remove_user_roblox_link(discord_id: str):
    if db_users is not None:
        try:
            db_users.update_one({"discord_id": str(discord_id)}, {"$unset": {"roblox_data": ""}})
        except Exception as e:
            print(f"[Mongo Remove Roblox Link Error] {e}")


def strip_emojis(text: str) -> str:
    """Strips unicode emojis from text."""
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


# --- INTERACTIVE VIEWS & MODALS ---

class RobloxVerifyModal(discord.ui.Modal, title="Verify Roblox Account"):
    roblox_username = discord.ui.TextInput(
        label="Exact Roblox Username",
        placeholder="Enter your exact Roblox username...",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        entered_username = self.roblox_username.value.strip()

        expected_code = PENDING_VERIFICATION_CODES.get(user_id)
        if not expected_code:
            await interaction.followup.send(
                "No active verification code found. Please click 'Generate Code' first!",
                ephemeral=True
            )
            return

        async with aiohttp.ClientSession() as session:
            try:
                # 1. Fetch User ID from Roblox API
                usr_url = "https://users.roblox.com/v1/usernames/users"
                payload = {"usernames": [entered_username], "excludeBannedUsers": True}
                async with session.post(usr_url, json=payload) as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Failed to query Roblox API. Try again later.", ephemeral=True)
                        return
                    res_data = await resp.json()
                    users_list = res_data.get("data", [])
                    if not users_list:
                        await interaction.followup.send(f"Roblox user `{entered_username}` was not found.", ephemeral=True)
                        return
                    r_id = users_list[0].get("id")
                    r_name = users_list[0].get("name")

                # 2. Fetch User Bio
                bio_url = f"https://users.roblox.com/v1/users/{r_id}"
                async with session.get(bio_url) as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Failed to retrieve Roblox profile details.", ephemeral=True)
                        return
                    profile_data = await resp.json()
                    description = profile_data.get("description", "")

                if expected_code in description:
                    save_user_roblox_link(user_id, str(interaction.user), str(r_id), r_name)
                    PENDING_VERIFICATION_CODES.pop(user_id, None)

                    embed = discord.Embed(
                        title="Verification Successful",
                        description=f"Your Discord account has been linked to Roblox account **{r_name}** (`{r_id}`).",
                        color=0x10B981
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(
                        f"Code `{expected_code}` was not found in profile bio for `{r_name}`.\n\n"
                        f"Current Bio:\n```{description[:200]}```\n"
                        "Please save the code in your Roblox bio and click Verify again.",
                        ephemeral=True
                    )
            except Exception as e:
                print(f"[Roblox Verification Error] {e}")
                await interaction.followup.send(f"An unexpected error occurred during verification: `{e}`", ephemeral=True)


class RobloxVerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Code", style=discord.ButtonStyle.primary, custom_id="btn_verify_gen_code")
    async def generate_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        code = f"TE-{random.randint(10000, 99999)}"
        PENDING_VERIFICATION_CODES[user_id] = code

        embed = discord.Embed(
            title="Your Verification Code",
            description=(
                f"Your unique code is: **`{code}`**\n\n"
                "**Instructions:**\n"
                "1. Go to your Roblox Profile Settings.\n"
                "2. Paste **`" + code + "`** anywhere inside your **About / Bio** section.\n"
                "3. Click **Verify Account** below and type your exact Roblox username."
            ),
            color=0x8B5CF6
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Verify Account", style=discord.ButtonStyle.success, custom_id="btn_verify_submit")
    async def verify_submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        if user_id not in PENDING_VERIFICATION_CODES:
            await interaction.response.send_message("Please click 'Generate Code' first before verifying!", ephemeral=True)
            return
        await interaction.response.send_modal(RobloxVerifyModal())

    @discord.ui.button(label="Help & Instructions", style=discord.ButtonStyle.secondary, custom_id="btn_verify_help")
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        help_text = (
            "**Verification Guide:**\n"
            "1. Click **Generate Code** to create your `TE-XXXXX` token.\n"
            "2. Edit your Roblox Profile Bio on roblox.com and paste the token.\n"
            "3. Click **Verify Account** and input your username.\n"
            "4. Once linked, server permissions and features auto-sync."
        )
        await interaction.response.send_message(help_text, ephemeral=True)


class RobloxUnsyncView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Un-sync Account", style=discord.ButtonStyle.danger, custom_id="btn_unsync_account")
    async def unsync_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        remove_user_roblox_link(user_id)
        embed = discord.Embed(
            title="Account Unlinked",
            description="Your Roblox account link has been cleared from database records.",
            color=0xEF4444
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class NonPremiumDeploymentView(discord.ui.View):
    def __init__(self, bot: commands.Bot = None, submitting_user_id: str = "0", target_guild_id: str = "0", layout: dict = None, dev_product_url: str = ""):
        super().__init__(timeout=None)
        self.bot = bot
        self.submitting_user_id = str(submitting_user_id)
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}
        self.dev_product_url = dev_product_url

        if dev_product_url:
            self.add_item(discord.ui.Button(label="Buy Dev Product", url=dev_product_url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="Verify Purchase", style=discord.ButtonStyle.primary, custom_id="btn_verify_dev_product")
    async def verify_purchase_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        receipt_token = f"RECEIPT_{user_id}_{self.target_guild_id}"

        if receipt_token in USED_DEV_PRODUCT_RECEIPTS:
            await interaction.followup.send(
                "This developer product receipt token has already been redeemed! Each purchase can only be used once.",
                ephemeral=True
            )
            return

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
            await interaction.followup.send("Error: The bot is missing from the target server.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Purchase Verified! Deploying Layout...",
            description=f"Single-use purchase receipt verified! Now building **{guild.name}**...",
            color=0x10B981
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        cog = bot_inst.get_cog("BotCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)

    @discord.ui.button(label="Use Points (100 Pts)", style=discord.ButtonStyle.success, custom_id="btn_use_points_build")
    async def use_points_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        current_points = get_user_points(user_id)

        if current_points < 100:
            missing = 100 - current_points
            embed = discord.Embed(
                title="Insufficient Points",
                description=f"You need at least 100 points to deploy this layout.\n\n**Your Balance:** {current_points} pts\n**Missing:** {missing} pts",
                color=0xEF4444
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
            description=f"Deducted 100 points!\n\n**Updated Balance:** {new_total:,} pts\nNow building **{guild.name}**...",
            color=0x8B5CF6
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        cog = bot_inst.get_cog("BotCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)


# -------------------------------------------------------------
# INTERACTIVE UI FOR PREMIUM MISSING BOT RETRY
# -------------------------------------------------------------
class PremiumMissingBotView(discord.ui.View):
    def __init__(self, bot=None, target_guild_id: str = "0", layout: dict = None, invite_url: str = ""):
        super().__init__(timeout=None)
        self.bot = bot
        self.target_guild_id = str(target_guild_id)
        self.layout = layout or {}

        if invite_url:
            self.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link))

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
                description=f"Re-build failed because the bot is still missing from target server ID `{self.target_guild_id}`.\n\nPlease add the bot using the **Add Bot to Server** button above and try again.",
                color=0xEF4444
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Bot Detected! Starting Auto-Build...",
            description=f"The bot has joined **{guild.name}**! Nuking server structure and deploying layout now...",
            color=0x10B981
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        cog = bot_inst.get_cog("BotCog")
        if cog:
            await cog.execute_auto_build(guild, self.layout)


# -------------------------------------------------------------
# DISCORD BOT COG IMPLEMENTATION
# -------------------------------------------------------------
class BotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- AUTO BUILD DEPLOYMENT LOGIC ---
    async def execute_auto_build(self, guild: discord.Guild, layout: dict):
        print(f"[Auto-Build Engine] Starting server build for target guild {guild.name} ({guild.id})")

        # 1. Clear existing channels and non-managed roles
        try:
            for ch in guild.channels:
                try:
                    await ch.delete(reason="Auto-Build layout reset")
                    await asyncio.sleep(0.1)
                except Exception as ce:
                    print(f"[Delete Channel Error] {ce}")

            for role in guild.roles:
                if not role.is_default() and not role.managed and role < guild.me.top_role:
                    try:
                        await role.delete(reason="Auto-Build layout reset")
                        await asyncio.sleep(0.1)
                    except Exception as re_err:
                        print(f"[Delete Role Error] {re_err}")
        except Exception as e:
            print(f"[Nuke Step Warning] {e}")

        # 2. Build Roles
        roles_data = layout.get("roles", [])
        created_roles = {}
        for r_item in roles_data:
            r_name = strip_emojis(r_item.get("name", "Member")) or str(r_item.get("name", "Member"))
            r_color_str = str(r_item.get("color", "#8B5CF6")).lstrip('#')
            try:
                color_val = int(r_color_str, 16)
            except ValueError:
                color_val = 0x8B5CF6

            try:
                new_role = await guild.create_role(
                    name=r_name,
                    color=discord.Color(color_val),
                    reason="Auto-Build Deployment"
                )
                created_roles[r_name.lower()] = new_role
                await asyncio.sleep(0.2)
            except Exception as e:
                print(f"[Create Role Error] {e}")

        # 3. Build Categories and Channels
        categories_data = layout.get("categories", [])
        for cat_item in categories_data:
            cat_raw_name = cat_item.get("name", "GENERAL")
            cat_clean_name = strip_emojis(cat_raw_name) or cat_raw_name

            try:
                dis_cat = await guild.create_category(name=cat_clean_name, reason="Auto-Build Deployment")
                await asyncio.sleep(0.2)

                for ch_item in cat_item.get("channels", []):
                    ch_raw_name = ch_item.get("name") or ch_item.get("clean_name") or "channel"
                    ch_clean_name = format_channel_name(ch_raw_name)
                    ch_type = ch_item.get("type", "text")
                    ch_topic = ch_item.get("topic", "")

                    if ch_type == "voice":
                        await guild.create_voice_channel(name=ch_clean_name, category=dis_cat, reason="Auto-Build Deployment")
                    else:
                        await guild.create_text_channel(name=ch_clean_name, category=dis_cat, topic=ch_topic, reason="Auto-Build Deployment")
                    await asyncio.sleep(0.2)
            except Exception as e:
                print(f"[Create Category Error] {e}")

        print(f"[Auto-Build Engine] Server build complete for {guild.name}!")

    # --- DM DISPATCH WITH 5 RETRIES AND SERVER FALLBACKS ---
    async def send_non_premium_verification_dm(self, user_id: str, target_guild_id: str, layout: dict, guild_name: str, fallback_channel: Optional[discord.TextChannel] = None):
        dev_product_url = os.getenv("ROBLOX_DEV_PRODUCT_URL", "https://www.roblox.com/games/refer?PlaceId=123456789")
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

        # RETRY DM ATTEMPTS (UP TO 5 TIMES)
        dm_sent = False
        user = None

        try:
            user = await self.bot.fetch_user(int(user_id))
        except Exception as e:
            print(f"[DM Error] Failed to fetch user {user_id}: {e}")

        if user:
            for attempt in range(1, 6):
                try:
                    dm_chan = user.dm_channel or await user.create_dm()
                    await dm_chan.send(embed=embed, view=view)
                    print(f"[DM Success] DM delivered on attempt {attempt}/5 to {user.name} ({user_id})")
                    dm_sent = True
                    break
                except discord.Forbidden:
                    print(f"[DM Blocked] Direct Messages are blocked for user {user_id}. Stopping retries.")
                    break
                except Exception as attempt_err:
                    print(f"[DM Retry {attempt}/5] Failed to send DM to {user_id}: {attempt_err}")
                    if attempt < 5:
                        await asyncio.sleep(1.5)

        if dm_sent:
            return

        # FALLBACK 1: SEND TO TARGET GUILD (IF BOT IS PRESENT)
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
                    print(f"[Fallback Success] Posted deployment embed in target server channel #{target_channel.name} ({target_guild.name})")
                    return
                except Exception as t_err:
                    print(f"[Target Server Send Error] Failed to post in target server {target_guild.name}: {t_err}")

        # FALLBACK 2: SUBMISSION CHANNEL IN MAIN SERVER
        if fallback_channel:
            try:
                await fallback_channel.send(content=f"<@{user_id}>", embed=embed, view=view)
                print(f"[Fallback Success] Posted deployment embed in fallback submission channel #{fallback_channel.name}")
            except Exception as chan_err:
                print(f"[Fallback Channel Send Error] Could not post to submission channel: {chan_err}")

    async def notify_missing_bot(self, user_id: str, target_guild_id: str, layout: dict = None):
        try:
            user = await self.bot.fetch_user(int(user_id))
            if user:
                embed = discord.Embed(
                    title="Bot Missing in Target Server",
                    description=(
                        f"<@{user_id}> Auto-deployment paused. The bot is not present in target server ID `{target_guild_id}`.\n\n"
                        f"**Instructions:**\n"
                        f"1. Click **Add Bot to Server** below to invite the bot.\n"
                        f"2. Once added, click **Build** to re-attempt the auto-build!"
                    ),
                    color=0xEF4444
                )
                invite_url = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands&guild_id={target_guild_id}"
                view = PremiumMissingBotView(
                    bot=self.bot,
                    target_guild_id=target_guild_id,
                    layout=layout or {},
                    invite_url=invite_url
                )
                dm_channel = user.dm_channel or await user.create_dm()
                await dm_channel.send(embed=embed, view=view)
                print(f"[Missing Bot DM] Sent build & invite options to user {user_id}")
        except Exception as e:
            print(f"[DM Warning] Failed sending DM to {user_id}: {e}")

    # --- EVENT LISTENERS ---

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        user_id = str(member.id)
        add_user_points(user_id, 20)
        print(f"[Welcome Bonus] Granted 20 welcome bonus points to {member.name} ({user_id})")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        is_design_channel = str(message.channel.id) in [str(PREMIUM_DESIGN_CHANNEL_ID), str(NON_PREMIUM_DESIGN_CHANNEL_ID)]

        # Ignore normal messages from bots, unless it is a layout JSON submission
        if message.author.bot and not (is_design_channel and message.attachments):
            return

        # 💬 Chat Points & Anti-Spam Logic
        if not message.author.bot and message.guild:
            user_id = str(message.author.id)
            now = datetime.now(timezone.utc)

            penalty_until = USER_PENALTY_UNTIL.get(user_id)
            if penalty_until and now < penalty_until:
                pass
            else:
                last_time = USER_LAST_MESSAGE_TIME.get(user_id)
                spam_count = USER_SPAM_COUNTS.get(user_id, 0)

                if last_time and (now - last_time).total_seconds() < 30:
                    spam_count += 1
                    USER_SPAM_COUNTS[user_id] = spam_count

                    if spam_count > 5:
                        USER_LAST_MESSAGE_TIME[user_id] = now
                        USER_PENALTY_UNTIL[user_id] = now + timedelta(minutes=30)
                        print(f"[Anti-Spam Penalty] Paused points for 30m for user {user_id}")
                else:
                    USER_SPAM_COUNTS[user_id] = 1
                    USER_LAST_MESSAGE_TIME[user_id] = now
                    add_user_points(user_id, 2)

        # Process Web Layout Attachments
        if not is_design_channel or not message.attachments:
            return

        attachment = message.attachments[0]
        if not attachment.filename.endswith(".json"):
            return

        try:
            content = await attachment.read()
            data = json.loads(content.decode("utf-8"))
        except Exception as e:
            print(f"[Blueprint Parse Error] {e}")
            return

        submitting_user_id = str(data.get("submitting_user_id", "0"))
        target_guild_id = str(data.get("target_server_id", "0")).strip()
        is_premium_submission = bool(data.get("is_premium", False))
        layout = data.get("layout", {})

        target_gid = int(target_guild_id) if target_guild_id and target_guild_id != "0" else 0
        guild = self.bot.get_guild(target_gid) if target_gid else None

        if not guild and target_gid:
            try:
                guild = await self.bot.fetch_guild(target_gid)
            except Exception:
                guild = None

        if not guild:
            await self.notify_missing_bot(submitting_user_id, target_guild_id, layout)
            return

        if is_premium_submission or (str(message.channel.id) == str(PREMIUM_DESIGN_CHANNEL_ID)):
            print(f"[Auto-Build] Premium deployment for {guild.name} ({guild.id})")
            await self.execute_auto_build(guild, layout)
        else:
            print(f"[Non-Premium] Triggering deployment DM for user {submitting_user_id}...")
            await self.send_non_premium_verification_dm(submitting_user_id, target_guild_id, layout, guild.name, message.channel)

    # --- SLASH COMMANDS ---

    @app_commands.command(name="points", description="Check your current server points balance.")
    async def points_command(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target_user = user or interaction.user
        pts = get_user_points(str(target_user.id))

        embed = discord.Embed(
            title="Points Balance",
            description=f"**{target_user.mention}** currently has **{pts:,}** points.",
            color=0x8B5CF6
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="View top point earners across the server.")
    async def leaderboard_command(self, interaction: discord.Interaction):
        if db_points is not None:
            top_docs = list(db_points.find().sort("points", -1).limit(10))
            description_lines = []
            for idx, doc in enumerate(top_docs, 1):
                uid = doc.get("discord_id")
                pts = doc.get("points", 0)
                description_lines.append(f"**#{idx}** <@{uid}> — `{pts:,}` pts")
        else:
            sorted_local = sorted(LOCAL_USER_POINTS.items(), key=lambda x: x[1], reverse=True)[:10]
            description_lines = [f"**#{idx}** <@{uid}> — `{pts:,}` pts" for idx, (uid, pts) in enumerate(sorted_local, 1)]

        embed = discord.Embed(
            title="Points Leaderboard",
            description="\n".join(description_lines) if description_lines else "No points recorded yet.",
            color=0x8B5CF6
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="points-set", description="Add or deduct points for a user (Admin Only).")
    @app_commands.describe(user="The user to modify points for", amount="Points amount (use negative numbers to deduct)")
    async def points_set_command(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if interaction.user.id != BOT_OWNER_ID and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permissions required.", ephemeral=True)
            return

        new_total = add_user_points(str(user.id), amount)
        embed = discord.Embed(
            title="Points Updated",
            description=f"Updated points for **{user.mention}**.\n**Adjustment:** `{amount:+d}`\n**New Total:** `{new_total:,}` pts",
            color=0x10B981
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="set", description="Deploy interactive UI panels (Verification or Un-sync).")
    @app_commands.choices(action=[
        app_commands.Choice(name="verification", value="verification"),
        app_commands.Choice(name="un-sync", value="un-sync")
    ])
    async def set_panel_command(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel,
        description: Optional[str] = None
    ):
        if interaction.user.id != BOT_OWNER_ID and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permissions required.", ephemeral=True)
            return

        if action.value == "verification":
            embed = discord.Embed(
                title="Roblox Account Verification",
                description=description or "Click **Generate Code** to receive your code, place it in your Roblox bio, and click **Verify Account** to link.",
                color=0x8B5CF6
            )
            view = RobloxVerificationView()
        else:
            embed = discord.Embed(
                title="Roblox Account Un-sync",
                description=description or "Click **Un-sync Account** to unlink your Roblox account and clear saved metrics.",
                color=0xEF4444
            )
            view = RobloxUnsyncView()

        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"Successfully posted **{action.name}** panel in {channel.mention}!", ephemeral=True)


async def setup(bot: commands.Bot):
    # Register persistent views so buttons remain active across bot redeployments
    bot.add_view(RobloxVerificationView())
    bot.add_view(RobloxUnsyncView())

    await bot.add_cog(BotCog(bot))
    print("[Setup] BotCog loaded with persistent verification & un-sync views registered.")
