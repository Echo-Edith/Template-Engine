import os
import re
import json
import asyncio
import aiohttp
import discord
from datetime import datetime, timezone, timedelta
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient

# Target Log, Welcome, and Analytics Channels
SUBSCRIPTION_LOG_CHANNEL_ID = 1564262413514182739
WELCOME_CHANNEL_ID = 1564262763310616799
MONTHLY_ANALYTICS_CHANNEL_ID = 1564263447057670154
PREMIUM_SCAN_CHANNEL_ID = 1563544108696477802
BOT_OWNER_ID = 1219266886143967245

MONGO_URI = os.getenv("MONGO_URI")
ROBLOX_OPEN_CLOUD_KEY = (os.getenv("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()

db_premium = None
db_build_access = None
db_users = None
db_stats = None

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
        db_stats = db["stats"]
    except Exception as e:
        print(f"[Bot Mongo Warning] Could not connect to MongoDB: {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_AUTHORIZED_BUILDERS = set()
LOCAL_PERSISTENT_USERS = {}
PENDING_VERIFICATION_CODES = {}

PREMIUM_ROLE_NAME = "Template Engine Premium"
PREMIUM_ROLE_COLOR = discord.Color(0xF1C40F)


def parse_duration_to_timedelta(duration_str: str) -> timedelta:
    if not duration_str:
        return timedelta(days=30)
    match = re.match(r"^(\d+)\s*([smhdSMHD])?$", duration_str.strip())
    if not match:
        return timedelta(days=30)
    val, unit = match.groups()
    num = int(val)
    unit = (unit or "d").lower()
    if unit == "s": return timedelta(seconds=num)
    elif unit == "m": return timedelta(minutes=num)
    elif unit == "h": return timedelta(hours=num)
    elif unit == "d": return timedelta(days=num)
    return timedelta(days=30)


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
    else:
        exp = LOCAL_PREMIUM_USERS.get(user_id_str)
        return exp > now if exp else False


def set_user_premium(user_id: str, duration_td: timedelta):
    user_id_str = str(user_id)
    now = datetime.now(timezone.utc)
    expires_at = now + duration_td

    if db_premium is not None:
        db_premium.update_one(
            {"user_id": user_id_str},
            {"$set": {"user_id": user_id_str, "expires_at": expires_at.isoformat(), "updated_at": now.isoformat()}},
            upsert=True
        )
    else:
        LOCAL_PREMIUM_USERS[user_id_str] = expires_at


def remove_user_premium(user_id: str):
    user_id_str = str(user_id)
    if db_premium is not None:
        db_premium.delete_one({"user_id": user_id_str})
    else:
        LOCAL_PREMIUM_USERS.pop(user_id_str, None)


def save_user_roblox_link(discord_id: str, discord_username: str, roblox_id: str, roblox_username: str):
    discord_id_str = str(discord_id)
    now = datetime.now(timezone.utc).isoformat()
    roblox_data = {"id": str(roblox_id), "username": roblox_username}

    if db_users is not None:
        try:
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
        except Exception as e:
            print(f"[Mongo User Sync Error] {e}")
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
        try:
            db_users.update_one(
                {"discord_id": discord_id_str},
                {"$unset": {"roblox_data": ""}}
            )
        except Exception as e:
            print(f"[Mongo User Unlink Error] {e}")
    else:
        if discord_id_str in LOCAL_PERSISTENT_USERS:
            LOCAL_PERSISTENT_USERS[discord_id_str].pop("roblox_data", None)


def is_user_authorized_for_build(user_id: str) -> bool:
    user_id_str = str(user_id)
    if int(user_id_str) == BOT_OWNER_ID:
        return True
    if is_user_premium(user_id_str):
        return True
    if db_build_access is not None:
        return db_build_access.find_one({"user_id": user_id_str}) is not None
    return user_id_str in LOCAL_AUTHORIZED_BUILDERS


def toggle_user_build_authorization(user_id: str) -> bool:
    user_id_str = str(user_id)
    
    if db_build_access is not None:
        exists = db_build_access.find_one({"user_id": user_id_str}) is not None
        if exists:
            db_build_access.delete_one({"user_id": user_id_str})
            return False
        else:
            db_build_access.update_one(
                {"user_id": user_id_str},
                {"$set": {"user_id": user_id_str, "added_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True
            )
            return True
    else:
        if user_id_str in LOCAL_AUTHORIZED_BUILDERS:
            LOCAL_AUTHORIZED_BUILDERS.remove(user_id_str)
            return False
        else:
            LOCAL_AUTHORIZED_BUILDERS.add(user_id_str)
            return True


# --- MODAL FOR ROBLOX VERIFICATION INPUT ---
class RobloxVerifyModal(discord.ui.Modal, title="Roblox Bio Verification"):
    roblox_username = discord.ui.TextInput(
        label="Exact Roblox Username",
        placeholder="e.g. Builderman",
        required=True,
        min_length=3,
        max_length=30
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id_str = str(interaction.user.id)
        code_data = PENDING_VERIFICATION_CODES.get(user_id_str)

        if not code_data:
            err_embed = discord.Embed(
                title="[CODE NOT FOUND]",
                description="Please click **Generate Code** first before verifying.",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Template Engine Infrastructure")
            await interaction.followup.send(embed=err_embed, ephemeral=True)
            return

        code = code_data["code"]
        target_username = self.roblox_username.value.strip()

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with aiohttp.ClientSession(headers=headers) as session:
                # Step 1: Resolve Roblox Username to User ID
                async with session.post(
                    "https://users.roblox.com/v1/usernames/users",
                    json={"usernames": [target_username], "excludeBannedUsers": False}
                ) as res:
                    res_data = await res.json()

                if not res_data.get("data") or len(res_data["data"]) == 0:
                    err = discord.Embed(
                        title="[ROBLOX USER NOT FOUND]",
                        description=f"Roblox account `@{target_username}` was not found.",
                        color=0xf43f5e
                    )
                    err.set_footer(text="Template Engine Infrastructure")
                    await interaction.followup.send(embed=err, ephemeral=True)
                    return

                roblox_id = res_data["data"][0]["id"]
                exact_username = res_data["data"][0]["name"]

                # Step 2: Fetch Profile Bio
                async with session.get(f"https://users.roblox.com/v1/users/{roblox_id}") as profile_res:
                    profile_data = await profile_res.json()
                    bio_text = profile_data.get("description", "")

                # Step 3: Check Verification Code
                if code not in bio_text:
                    err = discord.Embed(
                        title="[VERIFICATION FAILED]",
                        description=(
                            f"Code `{code}` was not found in the bio for `@{exact_username}`.\n\n"
                            f"Please ensure you saved the code in your Roblox profile bio and try again."
                        ),
                        color=0xf43f5e
                    )
                    err.set_footer(text="Template Engine Infrastructure")
                    await interaction.followup.send(embed=err, ephemeral=True)
                    return

                # Step 4: Link Accounts & Update Nickname
                save_user_roblox_link(
                    discord_id=user_id_str,
                    discord_username=interaction.user.name,
                    roblox_id=str(roblox_id),
                    roblox_username=exact_username
                )
                
                now = datetime.now(timezone.utc)
                expires_at = now + timedelta(days=30)
                set_user_premium(user_id_str, timedelta(days=30))

                new_nickname = f"{interaction.user.name} ({exact_username})"
                if len(new_nickname) > 32:
                    new_nickname = new_nickname[:29] + "..."

                nickname_updated = False
                try:
                    await interaction.user.edit(nick=new_nickname)
                    nickname_updated = True
                except Exception:
                    pass

                # Direct DM to user
                try:
                    dm_embed = discord.Embed(
                        title="👑 TE Premium Activated!",
                        description=(
                            f"Thank you for linking your Roblox account **@{exact_username}**!\n\n"
                            f"**Perks Unlocked:**\n"
                            f"• Instant 1-click server deployments\n"
                            f"• Gold Glassmorphic website theme\n"
                            f"• Advanced AI server architecture building"
                        ),
                        color=0xF1C40F
                    )
                    dm_embed.set_footer(text="Template Engine Infrastructure")
                    await interaction.user.send(embed=dm_embed)
                except Exception:
                    pass

                # Log to Subscription Channel
                log_channel = interaction.guild.get_channel(SUBSCRIPTION_LOG_CHANNEL_ID) if interaction.guild else None
                if log_channel:
                    log_embed = discord.Embed(
                        title="💎 Account Verified & Premium Linked",
                        description=f"User {interaction.user.mention} (`{interaction.user.name}`) verified via Roblox Bio!",
                        color=0xF1C40F,
                        timestamp=now
                    )
                    log_embed.add_field(name="Roblox Profile", value=f"@{exact_username} (`{roblox_id}`)", inline=True)
                    log_embed.add_field(name="Expires At", value=f"<t:{int(expires_at.timestamp())}:R>", inline=True)
                    log_embed.set_footer(text="Template Engine Audit Log")
                    await log_channel.send(embed=log_embed)

                success_embed = discord.Embed(
                    title="[ROBLOX ACCOUNT VERIFIED]",
                    description=(
                        f"Successfully verified and synced Roblox profile **@{exact_username}** (`{roblox_id}`) "
                        f"with your Discord account!\n\n"
                        f"• **TE Premium:** Activated (30 Days)\n"
                        f"• **Nickname Updated:** `{new_nickname if nickname_updated else 'Requires Manage Nicknames permission'}`\n"
                        f"• **Website Status:** Both accounts are now synced."
                    ),
                    color=0x2ecc71
                )
                success_embed.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=success_embed, ephemeral=True)

        except Exception as e:
            err = discord.Embed(
                title="[VERIFICATION ERROR]",
                description=f"An error occurred while contacting Roblox servers: {e}",
                color=0xf43f5e
            )
            err.set_footer(text="Template Engine Infrastructure")
            await interaction.followup.send(embed=err, ephemeral=True)


# --- INTERACTIVE VIEWS FOR DISCORD VERIFICATION ---
class VerificationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Code", style=discord.ButtonStyle.primary, custom_id="btn_gen_code", emoji="🔑")
    async def generate_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        import random
        code = f"TE-{random.randint(100000, 999999)}"
        PENDING_VERIFICATION_CODES[user_id_str] = {"code": code, "created_at": datetime.now(timezone.utc)}

        embed = discord.Embed(
            title="[YOUR VERIFICATION CODE]",
            description=(
                f"Copy and paste this code into your Roblox profile **About** (Bio) section:\n\n"
                f"```\n{code}\n```\n"
                f"Once saved on Roblox, click the **Verify Bio** button to complete linking."
            ),
            color=0xF1C40F
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Verify Bio", style=discord.ButtonStyle.success, custom_id="btn_verify_bio", emoji="✅")
    async def verify_bio_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RobloxVerifyModal())

    @discord.ui.button(label="Help & Instructions", style=discord.ButtonStyle.secondary, custom_id="btn_help_bio", emoji="❓")
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        help_embed = discord.Embed(
            title="[HOW TO VERIFY YOUR ROBLOX ACCOUNT]",
            description=(
                "Follow these simple steps to link your Roblox account with Discord:\n\n"
                "1️⃣ Click **Generate Code** to receive your unique `TE-XXXXXX` code.\n"
                "2️⃣ Go to your **Roblox Profile** → Edit Profile → **About**.\n"
                "3️⃣ Paste the code into your bio and click **Save**.\n"
                "4️⃣ Return here, click **Verify Bio**, and type your Roblox username.\n\n"
                "✨ *Once verified, your accounts sync automatically on both Discord and the website!*"
            ),
            color=0x3b82f6
        )
        help_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=help_embed, ephemeral=True)


class UnsyncPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Unsync Account", style=discord.ButtonStyle.danger, custom_id="btn_unsync_acc", emoji="🔗")
    async def unsync_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        remove_user_roblox_link(user_id_str)

        try:
            await interaction.user.edit(nick=None)
        except Exception:
            pass

        embed = discord.Embed(
            title="[ACCOUNT UNSYNCED]",
            description="Your Roblox account link has been removed. Server nickname reset.",
            color=0x2ecc71
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_premium_expirations.start()
        self.monthly_summary_task.start()

    def cog_unload(self):
        self.check_premium_expirations.cancel()
        self.monthly_summary_task.cancel()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if not channel:
            channel = member.guild.system_channel
        if channel:
            try:
                await channel.send(f"Welcome, {member.mention}")
            except Exception as e:
                print(f"[Welcome Error] {e}")

    async def get_or_create_premium_role(self, guild: discord.Guild) -> discord.Role:
        role = discord.utils.get(guild.roles, name=PREMIUM_ROLE_NAME)
        if not role:
            try:
                role = await guild.create_role(
                    name=PREMIUM_ROLE_NAME,
                    color=PREMIUM_ROLE_COLOR,
                    hoist=True,
                    reason="Automated TE Premium system setup"
                )
            except Exception as e:
                print(f"[Role Creation Error] Could not create role in {guild.name}: {e}")
                return None
        return role

    async def assign_premium_role(self, user_id: str):
        for guild in self.bot.guilds:
            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None
            if member:
                role = await self.get_or_create_premium_role(guild)
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason="TE Premium activated")
                    except Exception as e:
                        print(f"[Role Assign Error] Failed to assign role to {member} in {guild.name}: {e}")

    async def remove_premium_role(self, user_id: str):
        for guild in self.bot.guilds:
            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None
            if member:
                role = discord.utils.get(guild.roles, name=PREMIUM_ROLE_NAME)
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="TE Premium expired or revoked")
                    except Exception as e:
                        print(f"[Role Remove Error] Failed to remove role from {member} in {guild.name}: {e}")

    @tasks.loop(minutes=1.0)
    async def check_premium_expirations(self):
        now = datetime.now(timezone.utc)
        expired_ids = []
        if db_premium is not None:
            docs = db_premium.find()
            for doc in docs:
                uid = doc.get("user_id")
                exp_raw = doc.get("expires_at")
                if uid and exp_raw:
                    exp = datetime.fromisoformat(exp_raw) if isinstance(exp_raw, str) else exp_raw
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if exp <= now:
                        expired_ids.append(uid)
        else:
            for uid, exp in list(LOCAL_PREMIUM_USERS.items()):
                if exp <= now:
                    expired_ids.append(uid)

        for uid in expired_ids:
            remove_user_premium(uid)
            await self.remove_premium_role(uid)

    @check_premium_expirations.before_loop
    async def before_check_expirations(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24.0)
    async def monthly_summary_task(self):
        now = datetime.now(timezone.utc)
        if now.day == 1:
            channel = self.bot.get_channel(MONTHLY_ANALYTICS_CHANNEL_ID)
            if not channel:
                return

            active_prem = db_premium.count_documents({"expires_at": {"$gt": now.isoformat()}}) if db_premium is not None else len(LOCAL_PREMIUM_USERS)
            total_blueprints = db_stats.find_one({"metric": "global"}).get("blueprints_generated", 0) if db_stats is not None else 0
            
            total_earnings_robux = active_prem * 299
            total_earnings_usd = round(total_earnings_robux * 0.0035, 2)

            embed = discord.Embed(
                title=f"📊 Monthly Analytics Summary — {now.strftime('%B %Y')}",
                color=0xF1C40F,
                timestamp=now
            )
            embed.add_field(name="Active Premium Subscribers", value=f"`{active_prem}`", inline=True)
            embed.add_field(name="Estimated Monthly Revenue", value=f"`{total_earnings_robux} Robux` (~`${total_earnings_usd} USD`)", inline=True)
            embed.add_field(name="Total Server Layouts Generated", value=f"`{total_blueprints}`", inline=False)
            embed.set_footer(text="Template Engine Automated Analytics")

            await channel.send(embed=embed)

    @monthly_summary_task.before_loop
    async def before_monthly_summary(self):
        await self.bot.wait_until_ready()

    def build_permission_overwrites(
        self,
        base_overwrite: discord.PermissionOverwrite = None,
        c_type: str = "text",
        view_permission: bool = None,
        send_permission: bool = None,
        manage_permission: bool = None,
        embed_permission: bool = None,
        attach_permission: bool = None,
        history_permission: bool = None
    ) -> discord.PermissionOverwrite:
        ow = base_overwrite or discord.PermissionOverwrite()

        if view_permission is not None:
            ow.read_messages = view_permission
            ow.view_channel = view_permission
        if send_permission is not None:
            if c_type == "voice":
                ow.connect = send_permission
                ow.speak = send_permission
            else:
                ow.send_messages = send_permission
                ow.send_messages_in_threads = send_permission
        if manage_permission is not None:
            ow.manage_channels = manage_permission
            ow.manage_threads = manage_permission
        if embed_permission is not None:
            ow.embed_links = embed_permission
        if attach_permission is not None:
            ow.attach_files = attach_permission
        if history_permission is not None:
            ow.read_message_history = history_permission

        return ow

    def slugify_channel_name(self, name: str, emoji: str = "") -> str:
        clean_name = re.sub(r'[^a-zA-Z0-9\s\-_]', '', name or "").strip().lower()
        clean_name = re.sub(r'[\s_]+', '-', clean_name).strip("-") or "unnamed-channel"
        if emoji and emoji.strip():
            clean_emoji = re.sub(r'[^a-zA-Z0-9\s\-_]', '', emoji.strip()).lower()
            if clean_emoji:
                return f"{clean_emoji}-{clean_name}"
        return clean_name

    # --- 1. /verification-set COMMAND (OWNER ONLY) ---
    @app_commands.command(name="verification-set", description="Post the Roblox verification panel in a channel (Owner restricted).")
    @app_commands.describe(
        location="Target channel to post verification panel",
        title="Custom title for the verification panel",
        description="Custom body text for the verification panel"
    )
    async def verification_set(
        self,
        interaction: discord.Interaction,
        location: discord.TextChannel,
        title: str = "Roblox Account Verification",
        description: str = "Click Generate Code to link your Roblox profile and sync account benefits."
    ):
        if interaction.user.id != BOT_OWNER_ID:
            err_owner = discord.Embed(
                title="[RESTRICTED COMMAND]",
                description="Restricted command. Bot owner access required.",
                color=0xf43f5e
            )
            err_owner.set_footer(text="Template Engine Infrastructure")
            await interaction.response.send_message(embed=err_owner, ephemeral=True)
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=0x8b5cf6
        )
        embed.set_footer(text="Template Engine Infrastructure")

        await location.send(embed=embed, view=VerificationPanelView())

        confirm_embed = discord.Embed(
            title="[VERIFICATION PANEL DEPLOYED]",
            description=f"Successfully deployed verification panel to {location.mention}.",
            color=0x2ecc71
        )
        confirm_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

    # --- 2. /un-sync-set COMMAND (OWNER ONLY) ---
    @app_commands.command(name="un-sync-set", description="Post the Roblox account unsync panel in a channel (Owner restricted).")
    @app_commands.describe(
        location="Target channel to post unsync panel",
        title="Custom title for the unsync panel",
        description="Custom body text for the unsync panel"
    )
    async def unsync_set(
        self,
        interaction: discord.Interaction,
        location: discord.TextChannel,
        title: str = "Unsync Roblox Account",
        description: str = "Click the button below to unlink your Roblox account and reset your nickname."
    ):
        if interaction.user.id != BOT_OWNER_ID:
            err_owner = discord.Embed(
                title="[RESTRICTED COMMAND]",
                description="Restricted command. Bot owner access required.",
                color=0xf43f5e
            )
            err_owner.set_footer(text="Template Engine Infrastructure")
            await interaction.response.send_message(embed=err_owner, ephemeral=True)
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=0xf43f5e
        )
        embed.set_footer(text="Template Engine Infrastructure")

        await location.send(embed=embed, view=UnsyncPanelView())

        confirm_embed = discord.Embed(
            title="[UNSYNC PANEL DEPLOYED]",
            description=f"Successfully deployed unsync panel to {location.mention}.",
            color=0x2ecc71
        )
        confirm_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

    # --- 3. /channel COMMAND (TEMPORARY CHANNEL CONFIGURATION WITH NOTIFICATIONS) ---
    @app_commands.command(name="channel", description="Configure channel type or set a temporary channel timer.")
    @app_commands.describe(
        type="Select channel behavior",
        duration="Duration for temporary channels (e.g., 10m, 2h, 1d)"
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="Temporary Channel", value="temporary"),
        app_commands.Choice(name="Standard Channel", value="standard")
    ])
    async def set_channel_type(self, interaction: discord.Interaction, type: app_commands.Choice[str], duration: str = "1h"):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("Manage Channels permission required.", ephemeral=True)
            return

        ch = interaction.channel
        if type.value == "temporary":
            td = parse_duration_to_timedelta(duration)
            expires_at = datetime.now(timezone.utc) + td

            init_embed = discord.Embed(
                title="⏳ Temporary Channel Configured",
                description=(
                    f"This channel has been set to **Temporary** and will automatically expire in **{duration}**.\n\n"
                    f"**Scheduled Expiration:** <t:{int(expires_at.timestamp())}:F> (<t:{int(expires_at.timestamp())}:R>)"
                ),
                color=0xeab308
            )
            init_embed.set_footer(text="Template Engine Channel Manager")
            await interaction.response.send_message(embed=init_embed)

            # Asynchronous timer for expiration warning & deletion
            async def handle_temp_channel_lifecycle():
                total_seconds = td.total_seconds()

                # Warning notification 1 minute prior to deletion if duration is over 2 mins
                if total_seconds > 120:
                    await asyncio.sleep(total_seconds - 60)
                    warn_embed = discord.Embed(
                        title="⚠️ Channel Expiring Soon!",
                        description="This temporary channel will be automatically deleted in **1 minute**.",
                        color=0xf43f5e
                    )
                    warn_embed.set_footer(text="Template Engine Channel Manager")
                    try:
                        await ch.send(embed=warn_embed)
                    except Exception:
                        pass
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(total_seconds)

                # Final Expiration & Deletion
                try:
                    del_embed = discord.Embed(
                        title="💥 Temporary Channel Expired",
                        description="Channel lifetime reached. Deleting channel now...",
                        color=0xf43f5e
                    )
                    await ch.send(embed=del_embed)
                    await asyncio.sleep(3)
                    await ch.delete(reason="Temporary channel expired")
                except Exception as e:
                    print(f"[Temp Channel Deletion Error] {e}")

            asyncio.create_task(handle_temp_channel_lifecycle())

        else:
            std_embed = discord.Embed(
                title="📌 Standard Channel Active",
                description="This channel is set to **Standard** and will persist indefinitely.",
                color=0x2ecc71
            )
            std_embed.set_footer(text="Template Engine Channel Manager")
            await interaction.response.send_message(embed=std_embed)

    # --- 4. /build COMMAND (AUTHORIZED USER IDs ONLY) ---
    @app_commands.command(name="build", description="Deploys layout blueprint (Requires build authorization).")
    @app_commands.describe(
        url="Optional URL to blueprint JSON",
        file="Optional JSON attachment file",
        server_id="Optional target Discord server ID"
    )
    async def build_server(self, interaction: discord.Interaction, url: str = None, file: discord.Attachment = None, server_id: str = None):
        user_id_str = str(interaction.user.id)
        if not is_user_authorized_for_build(user_id_str):
            err_embed = discord.Embed(
                title="[ACCESS DENIED]",
                description="You are not authorized to execute the /build command. Request permission via /build-allow.",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Template Engine Infrastructure")
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        init_embed = discord.Embed(
            title="[BUILD INITIATED]",
            description="Processing and deploying server blueprint...",
            color=0xeab308
        )
        init_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=init_embed)

        blueprint_data = None
        try:
            async with aiohttp.ClientSession() as session:
                target_url = file.url if file else url
                if target_url:
                    async with session.get(target_url) as resp:
                        if resp.status == 200:
                            blueprint_data = await resp.json()
        except Exception as e:
            err_fetch = discord.Embed(
                title="[FETCH ERROR]",
                description=f"Error reading blueprint payload: {e}",
                color=0xf43f5e
            )
            err_fetch.set_footer(text="Template Engine Infrastructure")
            await interaction.followup.send(embed=err_fetch)
            return

        if not blueprint_data:
            err_invalid = discord.Embed(
                title="[INVALID INPUT]",
                description="Valid JSON URL or attached blueprint file required.",
                color=0xf43f5e
            )
            err_invalid.set_footer(text="Template Engine Infrastructure")
            await interaction.followup.send(embed=err_invalid)
            return

        target_guild_id = int(server_id.strip()) if server_id and server_id.strip().isdigit() else interaction.guild_id
        guild = self.bot.get_guild(target_guild_id)
        if not guild:
            err_guild = discord.Embed(
                title="[GUILD NOT FOUND]",
                description=f"Bot is not present in target server ID {target_guild_id}.",
                color=0xf43f5e
            )
            err_guild.set_footer(text="Template Engine Infrastructure")
            await interaction.followup.send(embed=err_guild)
            return

        await self.execute_blueprint_build(guild, blueprint_data)

        done_embed = discord.Embed(
            title="[BUILD COMPLETE]",
            description=f"Successfully built server layout for {guild.name} ({guild.id}).",
            color=0x2ecc71
        )
        done_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.followup.send(embed=done_embed)

    # --- 5. /build-allow COMMAND (OWNER ONLY) ---
    @app_commands.command(name="build-allow", description="Toggle /build command authorization for a specific user.")
    @app_commands.describe(user="Target user to grant or revoke authorization")
    async def build_allow_toggle(self, interaction: discord.Interaction, user: discord.User):
        if interaction.user.id != BOT_OWNER_ID:
            err_owner = discord.Embed(
                title="[RESTRICTED COMMAND]",
                description="Restricted command. Bot owner access required.",
                color=0xf43f5e
            )
            err_owner.set_footer(text="Template Engine Infrastructure")
            await interaction.response.send_message(embed=err_owner, ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        user_id_str = str(user.id)
        is_now_authorized = toggle_user_build_authorization(user_id_str)

        if is_now_authorized:
            embed = discord.Embed(
                title="[BUILD AUTHORIZATION GRANTED]",
                description=f"User {user.mention} ({user_id_str}) is now authorized to run /build commands.",
                color=0x2ecc71
            )
        else:
            embed = discord.Embed(
                title="[BUILD AUTHORIZATION REVOKED]",
                description=f"User {user.mention} ({user_id_str}) build permission has been revoked.",
                color=0xf43f5e
            )

        embed.set_footer(text="Template Engine Infrastructure")
        await interaction.followup.send(embed=embed)

    # --- 6. /premium COMMAND (PUBLIC) ---
    @app_commands.command(name="premium", description="View features, perks, and subscription link for TE Premium.")
    async def premium_info(self, interaction: discord.Interaction):
        roblox_place_id = os.getenv("ROBLOX_PLACE_ID", "6094950399")
        roblox_store_url = os.getenv(
            "ROBLOX_STORE_URL",
            f"https://www.roblox.com/games/{roblox_place_id}/store"
        )
        
        embed = discord.Embed(
            title="[TE PREMIUM MEMBERSHIP]",
            description=(
                "Upgrade your Discord architecture workflow with instant background builds, stylized typography, and zero queue wait times.\n\n"
                "Pricing: 299 Robux / Month"
            ),
            color=0xF1C40F,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="1. Fully Automated Auto-Builds", value="Skip staff reviews. Layouts auto-build instantly into your server.", inline=False)
        embed.add_field(name="2. Exclusive Typography Engine", value="Access Gothic, Fancy, Monospace, Squared, Slash, and Circled font options.", inline=False)
        embed.add_field(name="3. Thread & Forum Automation", value="Enable auto-thread creation on support and topic channels.", inline=False)
        embed.add_field(name="4. Premium Design Presets", value="Access Minimalist, Corporate, Community, and Gaming presets.", inline=False)
        embed.add_field(name="5. Exclusive Gold Role Badge", value=f"Receive the {PREMIUM_ROLE_NAME} gold role across official servers.", inline=False)
        embed.set_footer(text="Template Engine Infrastructure")

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Subscribe via Roblox Store", url=roblox_store_url, style=discord.ButtonStyle.link))
        
        await interaction.response.send_message(embed=embed, view=view)

    # --- 7. /premium-set COMMAND (OWNER ONLY) ---
    @app_commands.command(name="premium-set", description="Grant or remove TE Premium status for a user.")
    @app_commands.describe(
        user="Target user to modify TE Premium status for",
        action="Select add to grant or remove to revoke premium",
        duration="Duration if granting (e.g. 30d, 60d). Default: 30d"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add Premium", value="add"),
        app_commands.Choice(name="Remove Premium", value="remove")
    ])
    async def premium_set(self, interaction: discord.Interaction, user: discord.User, action: app_commands.Choice[str], duration: str = "30d"):
        if interaction.user.id != BOT_OWNER_ID:
            err_owner = discord.Embed(
                title="[RESTRICTED COMMAND]",
                description="Restricted command. Bot owner access required.",
                color=0xf43f5e
            )
            err_owner.set_footer(text="Template Engine Infrastructure")
            await interaction.response.send_message(embed=err_owner, ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        user_id_str = str(user.id)
        act_val = action.value.lower()

        if act_val == "remove":
            remove_user_premium(user_id_str)
            await self.remove_premium_role(user_id_str)
            embed = discord.Embed(
                title="[TE PREMIUM REVOKED]",
                description=f"Revoked TE Premium status from {user.mention} ({user_id_str}) and removed gold role.",
                color=0xf43f5e
            )
        else:
            td = parse_duration_to_timedelta(duration)
            set_user_premium(user_id_str, td)
            exp_date = datetime.now(timezone.utc) + td
            await self.assign_premium_role(user_id_str)
            embed = discord.Embed(
                title="[TE PREMIUM ACTIVATED]",
                description=f"Granted TE Premium status to {user.mention} ({user_id_str}) and assigned gold role.",
                color=0xF1C40F
            )
            embed.add_field(name="Expires On", value=f"<t:{int(exp_date.timestamp())}:F>", inline=True)

        embed.set_footer(text="TE Premium Infrastructure")
        await interaction.followup.send(embed=embed)

    # --- 8. /ping COMMAND (PUBLIC) ---
    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        embed = discord.Embed(
            title="[PONG]",
            description=f"Latency: {latency_ms} ms",
            color=0x3b82f6
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=embed)

    # --- 9. /nuke COMMAND (OWNER ONLY) ---
    @app_commands.command(name="nuke", description="Delete all channels and roles in the server (Owner restricted).")
    async def nuke_server(self, interaction: discord.Interaction):
        if interaction.user.id != BOT_OWNER_ID:
            err_owner = discord.Embed(
                title="[RESTRICTED COMMAND]",
                description="Restricted command. Bot owner access required.",
                color=0xf43f5e
            )
            err_owner.set_footer(text="Template Engine Infrastructure")
            await interaction.response.send_message(embed=err_owner, ephemeral=True)
            return

        nuke_embed = discord.Embed(
            title="[NUKE INITIATED]",
            description="Wiping guild channels and non-default roles...",
            color=0xf43f5e
        )
        nuke_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=nuke_embed)
        await self.wipe_guild_infrastructure(interaction.guild)

    # --- 10. /website COMMAND (PUBLIC) ---
    @app_commands.command(name="website", description="Get the official Template Engine website button.")
    async def website_link(self, interaction: discord.Interaction):
        website_url = os.getenv("WEBSITE_URL", os.getenv("DASHBOARD_URL", "https://templateengine-xi.vercel.app"))
        
        embed = discord.Embed(
            title="[TEMPLATE ENGINE WEBSITE]",
            description="Click the button below to access the online layout generator and server architecture dashboard.",
            color=0x3b82f6
        )
        embed.set_footer(text="Template Engine Infrastructure")
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Website", url=website_url, style=discord.ButtonStyle.link, emoji="🌐"))
        
        await interaction.response.send_message(embed=embed, view=view)

    # --- DIRECT HANDSHAKE & UTILITY METHODS ---
    async def handle_direct_handshake_build(self, user_id: str, target_server_id: str, blueprint_url: str, blueprint_data: dict, log_channel_id: str):
        await self.bot.wait_until_ready()
        target_channel = self.bot.get_channel(int(log_channel_id)) if log_channel_id and str(log_channel_id).isdigit() else self.bot.get_channel(PREMIUM_SCAN_CHANNEL_ID)

        guild = None
        try:
            guild_id = int(target_server_id)
            guild = self.bot.get_guild(guild_id) or await self.bot.fetch_guild(guild_id)
        except Exception:
            guild = None

        if not guild:
            client_id = os.getenv("DISCORD_CLIENT_ID", str(self.bot.user.id if self.bot.user else ""))
            invite_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot%20applications.commands&guild_id={target_server_id}"

            if target_channel:
                try:
                    ch_embed = discord.Embed(
                        title="[AUTO-BUILD PENDING - BOT MISSING]",
                        description=f"Bot is missing from target server ID {target_server_id}. Direct message sent to user.",
                        color=0xeab308
                    )
                    ch_embed.set_footer(text="Template Engine Infrastructure")
                    await target_channel.send(embed=ch_embed)
                except Exception: pass

            if user_id and str(user_id).isdigit():
                try:
                    usr = await self.bot.fetch_user(int(user_id))
                    if usr:
                        dm_embed = discord.Embed(
                            title="[ACTION REQUIRED: ADD BOT TO SERVER]",
                            description=f"Your server design for ID {target_server_id} is ready.\n\nAdd bot link: {invite_url}",
                            color=0xeab308
                        )
                        dm_embed.set_footer(text="Template Engine Infrastructure")
                        await usr.send(embed=dm_embed)
                except Exception as e:
                    print(f"[DM Error] {e}")
            return

        try:
            await self.execute_blueprint_build(guild, blueprint_data)
            if target_channel:
                done_embed = discord.Embed(
                    title="[AUTO-BUILD COMPLETE]",
                    description=f"Successfully built server layout for {guild.name} ({target_server_id}).",
                    color=0x2ecc71
                )
                done_embed.set_footer(text="Template Engine Infrastructure")
                await target_channel.send(embed=done_embed)
        except Exception as build_err:
            if target_channel:
                fail_embed = discord.Embed(
                    title="[AUTO-BUILD FAILED]",
                    description=f"Error deploying to target server {target_server_id}: {str(build_err)}",
                    color=0xf43f5e
                )
                fail_embed.set_footer(text="Template Engine Infrastructure")
                await target_channel.send(embed=fail_embed)

    async def wipe_guild_infrastructure(self, guild: discord.Guild):
        for channel in guild.channels:
            try: await channel.delete(reason="Rebuild Execution")
            except Exception: pass
        for role in guild.roles:
            if not role.is_default() and not role.is_bot_managed() and not role.is_premium_subscriber():
                try: await role.delete(reason="Rebuild Execution")
                except Exception: pass

    async def execute_blueprint_build(self, guild: discord.Guild, blueprint_data: dict):
        await self.wipe_guild_infrastructure(guild)
        for r_cfg in blueprint_data.get("roles", []):
            color_hex = str(r_cfg.get("color", "#8b5cf6")).replace("#", "")
            try:
                await guild.create_role(name=r_cfg["name"], color=discord.Color(int(color_hex, 16) if color_hex else 0x8b5cf6))
            except Exception: pass

        for cat_cfg in blueprint_data.get("categories", []):
            try:
                category = await guild.create_category(name=str(cat_cfg.get("name", "CATEGORY")).strip())
                for chan_cfg in cat_cfg.get("channels", []):
                    c_name = self.slugify_channel_name(chan_cfg.get("name", "channel"), chan_cfg.get("emoji", ""))
                    if chan_cfg.get("type") == "voice":
                        await guild.create_voice_channel(name=c_name, category=category)
                    else:
                        await guild.create_text_channel(name=c_name, category=category, topic=chan_cfg.get("topic", ""))
            except Exception: pass

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_commands(self, ctx: commands.Context):
        synced = await self.bot.tree.sync()
        sync_embed = discord.Embed(
            title="[SYNC COMPLETE]",
            description=f"Registered and synced {len(synced)} global slash command(s).",
            color=0x3b82f6
        )
        sync_embed.set_footer(text="Template Engine Infrastructure")
        await ctx.send(embed=sync_embed)


async def setup(bot):
    # Register persistent UI views on bot startup so buttons survive redeployments
    bot.add_view(VerificationPanelView())
    bot.add_view(UnsyncPanelView())
    cog = BotCog(bot)
    await bot.add_cog(cog)
