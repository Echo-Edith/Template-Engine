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

PREMIUM_SCAN_CHANNEL_ID = 1563544108696477802
BOT_OWNER_ID = 1219266886143967245

MONGO_URI = os.getenv("MONGO_URI")
db_premium = None
db_build_access = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client["static_studio_db"]
        db_premium = db["premium_users"]
        db_build_access = db["build_authorized_users"]
    except Exception as e:
        print(f"[Bot Mongo Warning] Could not connect to MongoDB: {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_AUTHORIZED_BUILDERS = set()

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


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_premium_expirations.start()

    def cog_unload(self):
        self.check_premium_expirations.cancel()

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
                print(f"[Role Creation Error] Could not create gold role in {guild.name}: {e}")
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
        if manage_permission is not None:
            ow.manage_channels = manage_permission
        if embed_permission is not None:
            ow.embed_links = embed_permission
        if attach_permission is not None:
            ow.attach_files = attach_permission
        if history_permission is not None:
            ow.read_message_history = history_permission

        return ow

    # --- UPDATED PUBLIC TE PREMIUM INFORMATION COMMAND ---
    @app_commands.command(name="premium", description="View updated features, perks, and pricing for TE Premium status.")
    async def premium_info(self, interaction: discord.Interaction):
        website_url = os.getenv("WEBSITE_URL", os.getenv("DASHBOARD_URL", "https://templateengine-xi.vercel.app"))
        
        embed = discord.Embed(
            title="👑 Echo Studio — TE Premium Membership",
            description=(
                "Supercharge your Discord architecture workflow with instant background builds, stylized typography, and zero queue wait times!\n\n"
                "💰 **Pricing:** `299 Robux / Month`"
            ),
            color=0xF1C40F,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="⚡ 1. Fully Automated Background Auto-Builds",
            value="Skip manual staff reviews! Layouts auto-build instantly into your target server upon submission.",
            inline=False
        )
        embed.add_field(
            name="✨ 2. Exclusive Channel Font Engine",
            value="Customize your channels using 𝔊𝔬𝔱𝔥𝔦𝔠, 𐌱𝒶𝓃𝒸𝓎, 𝙼𝚘𝚗𝚘𝚜𝚙𝚊𝚌𝚎, 🅂🅀🅄🄰🅁🄴🄳, S L A S H, and ⓒⓘⓡⓒⓛⓔⓓ fonts.",
            inline=False
        )
        embed.add_field(
            name="🧵 3. Automated Thread & Nested Discussions",
            value="Enable auto-thread creation on support and topic channels directly from the generator UI.",
            inline=False
        )
        embed.add_field(
            name="🎨 4. Premium Design Presets & Sleek Minimalist",
            value="Access Minimalist (No Emoji), Corporate, Community, and Gaming presets out of the box.",
            inline=False
        )
        embed.add_field(
            name="🌟 5. Exclusive Gold Role Badge",
            value=f"Receive the **{PREMIUM_ROLE_NAME}** gold role across all official Echo Studio servers.",
            inline=False
        )
        embed.set_footer(text="Echo Studio Infrastructure • Upgrade Today")

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Studio Dashboard", url=website_url, style=discord.ButtonStyle.link))
        
        await interaction.response.send_message(embed=embed, view=view)

    # --- TOGGLE TE PREMIUM STATUS (GRANT / REVOKE) ---
    @app_commands.command(name="premium-set", description="[ADMIN ONLY] Toggle TE Premium status for a user (Grants or Revokes).")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user="User to toggle TE Premium status for", length="Duration if granting (e.g. 30d, 60d). Default: 30d")
    async def premium_set(self, interaction: discord.Interaction, user: discord.User, length: str = "30d"):
        await interaction.response.defer(thinking=True)
        user_id_str = str(user.id)
        
        if is_user_premium(user_id_str):
            remove_user_premium(user_id_str)
            await self.remove_premium_role(user_id_str)
            embed = discord.Embed(
                title="[TE Premium Revoked]",
                description=f"Revoked TE Premium status from {user.mention} and removed the gold role.",
                color=0xf43f5e
            )
        else:
            td = parse_duration_to_timedelta(length)
            set_user_premium(user_id_str, td)
            exp_date = datetime.now(timezone.utc) + td
            await self.assign_premium_role(user_id_str)
            embed = discord.Embed(
                title="[TE Premium Activated]",
                description=f"Granted TE Premium status to {user.mention} and assigned the gold role!",
                color=0xF1C40F
            )
            embed.add_field(name="Expires On", value=f"<t:{int(exp_date.timestamp())}:F>", inline=True)

        embed.set_footer(text="TE Premium Infrastructure")
        await interaction.followup.send(embed=embed)

    # --- TOGGLE BUILD AUTHORIZATION (OWNER ONLY) ---
    @app_commands.command(name="build-allow", description="[OWNER ONLY] Toggle /build permission for a user ID.")
    @app_commands.describe(user="User to grant or revoke /build command permission")
    async def build_allow_toggle(self, interaction: discord.Interaction, user: discord.User):
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ This command is restricted strictly to the bot owner.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        user_id_str = str(user.id)
        is_now_authorized = toggle_user_build_authorization(user_id_str)

        if is_now_authorized:
            embed = discord.Embed(
                title="[Build Permission Granted]",
                description=f"✅ User {user.mention} (`{user_id_str}`) is now authorized to run `/build` commands.",
                color=0x2ecc71
            )
        else:
            embed = discord.Embed(
                title="[Build Permission Revoked]",
                description=f"🚫 User {user.mention} (`{user_id_str}`) permission for `/build` has been revoked.",
                color=0xf43f5e
            )

        embed.set_footer(text="Echo Studio Infrastructure")
        await interaction.followup.send(embed=embed)

    # --- FULLY FEATURED MANUAL CHANNEL MANAGEMENT COMMANDS ---
    @app_commands.command(name="create-channel", description="Create a text, voice, or category channel with granular permission options.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.choices(type=[
        app_commands.Choice(name="Text Channel", value="text"),
        app_commands.Choice(name="Voice Channel", value="voice"),
        app_commands.Choice(name="Category Channel", value="category")
    ])
    @app_commands.describe(
        name="Mandatory channel name",
        category="Category to place channel in (Required for Text/Voice)",
        type="Channel type (text, voice, category)",
        emoji="Optional emoji prefix",
        topic="Optional text channel topic description",
        view_permission="Optional: Allow @everyone to view channel (True/False)",
        send_permission="Optional: Allow @everyone to send messages or connect (True/False)",
        manage_permission="Optional: Allow @everyone to manage channel (True/False)",
        embed_permission="Optional: Allow @everyone to embed links (True/False)",
        attach_permission="Optional: Allow @everyone to attach files (True/False)",
        history_permission="Optional: Allow @everyone to read message history (True/False)"
    )
    async def create_channel(
        self,
        interaction: discord.Interaction,
        name: str,
        category: discord.CategoryChannel,
        type: app_commands.Choice[str] = None,
        emoji: str = None,
        topic: str = None,
        view_permission: bool = None,
        send_permission: bool = None,
        manage_permission: bool = None,
        embed_permission: bool = None,
        attach_permission: bool = None,
        history_permission: bool = None
    ):
        await interaction.response.defer(thinking=True)
        c_type = (type.value if type else "text").lower().strip()
        formatted_name = self.slugify_channel_name(name, emoji) if c_type != "category" else f"{emoji or ''} {name}".strip()

        overwrites = {}
        has_perm_options = any(x is not None for x in [
            view_permission, send_permission, manage_permission,
            embed_permission, attach_permission, history_permission
        ])

        if has_perm_options:
            everyone_role = interaction.guild.default_role
            ow = self.build_permission_overwrites(
                c_type=c_type,
                view_permission=view_permission,
                send_permission=send_permission,
                manage_permission=manage_permission,
                embed_permission=embed_permission,
                attach_permission=attach_permission,
                history_permission=history_permission
            )
            overwrites[everyone_role] = ow

        try:
            if c_type == "voice":
                ch = await interaction.guild.create_voice_channel(name=formatted_name, category=category, overwrites=overwrites)
            elif c_type == "category":
                ch = await interaction.guild.create_category(name=formatted_name, overwrites=overwrites)
            else:
                ch = await interaction.guild.create_text_channel(name=formatted_name, category=category, topic=topic or "", overwrites=overwrites)

            embed = discord.Embed(
                title="✅ Channel Created",
                description=f"Successfully created channel {ch.mention} (`{ch.id}`) in **{category.name if category else 'Root'}**.",
                color=0x2ecc71
            )
            if topic: embed.add_field(name="Topic", value=topic, inline=False)
            if view_permission is not None: embed.add_field(name="View", value=str(view_permission), inline=True)
            if send_permission is not None: embed.add_field(name="Send/Connect", value=str(send_permission), inline=True)
            if manage_permission is not None: embed.add_field(name="Manage", value=str(manage_permission), inline=True)
            if embed_permission is not None: embed.add_field(name="Embed Links", value=str(embed_permission), inline=True)
            if attach_permission is not None: embed.add_field(name="Attach Files", value=str(attach_permission), inline=True)
            if history_permission is not None: embed.add_field(name="Read History", value=str(history_permission), inline=True)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(embed=discord.Embed(title="❌ Creation Error", description=f"Error creating channel: {e}", color=0xf43f5e))

    @app_commands.command(name="edit-channel", description="Edit an existing channel's name, topic, category, or granular permissions.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.describe(
        channel="Target channel to edit",
        new_name="Optional new channel name",
        category="Optional category to move the channel to",
        topic="Optional new topic (text channels only)",
        emoji="Optional new emoji prefix",
        view_permission="Optional: Allow @everyone to view channel (True/False)",
        send_permission="Optional: Allow @everyone to send messages or connect (True/False)",
        manage_permission="Optional: Allow @everyone to manage channel (True/False)",
        embed_permission="Optional: Allow @everyone to embed links (True/False)",
        attach_permission="Optional: Allow @everyone to attach files (True/False)",
        history_permission="Optional: Allow @everyone to read message history (True/False)"
    )
    async def edit_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        new_name: str = None,
        category: discord.CategoryChannel = None,
        topic: str = None,
        emoji: str = None,
        view_permission: bool = None,
        send_permission: bool = None,
        manage_permission: bool = None,
        embed_permission: bool = None,
        attach_permission: bool = None,
        history_permission: bool = None
    ):
        await interaction.response.defer(thinking=True)
        try:
            kwargs = {}
            if new_name or emoji:
                base_name = new_name or channel.name
                kwargs["name"] = self.slugify_channel_name(base_name, emoji or "") if not isinstance(channel, discord.CategoryChannel) else f"{emoji or ''} {base_name}".strip()
            if category is not None and not isinstance(channel, discord.CategoryChannel):
                kwargs["category"] = category
            if topic is not None and isinstance(channel, discord.TextChannel):
                kwargs["topic"] = topic

            has_perm_options = any(x is not None for x in [
                view_permission, send_permission, manage_permission,
                embed_permission, attach_permission, history_permission
            ])

            if has_perm_options:
                everyone_role = interaction.guild.default_role
                current_overwrites = channel.overwrites_for(everyone_role)
                c_type = "voice" if isinstance(channel, discord.VoiceChannel) else "text"
                updated_ow = self.build_permission_overwrites(
                    base_overwrite=current_overwrites,
                    c_type=c_type,
                    view_permission=view_permission,
                    send_permission=send_permission,
                    manage_permission=manage_permission,
                    embed_permission=embed_permission,
                    attach_permission=attach_permission,
                    history_permission=history_permission
                )
                kwargs["overwrites"] = {**channel.overwrites, everyone_role: updated_ow}

            await channel.edit(**kwargs)
            embed = discord.Embed(
                title="✅ Channel Updated",
                description=f"Successfully edited channel {channel.mention}.",
                color=0x2ecc71
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(embed=discord.Embed(title="❌ Edit Error", description=f"Error editing channel: {e}", color=0xf43f5e))

    @app_commands.command(name="temp-channel", description="Create a temporary text or voice channel with granular permissions and auto-delete timer.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.choices(type=[
        app_commands.Choice(name="Text Channel", value="text"),
        app_commands.Choice(name="Voice Channel", value="voice")
    ])
    @app_commands.describe(
        name="Mandatory temporary channel name",
        category="Category to place temporary channel in",
        type="Channel type (text or voice)",
        duration_minutes="Auto-delete timer in minutes (Default: 10)",
        emoji="Optional emoji prefix",
        topic="Optional topic description",
        view_permission="Optional: Allow @everyone to view channel (True/False)",
        send_permission="Optional: Allow @everyone to send messages or connect (True/False)",
        manage_permission="Optional: Allow @everyone to manage channel (True/False)",
        embed_permission="Optional: Allow @everyone to embed links (True/False)",
        attach_permission="Optional: Allow @everyone to attach files (True/False)",
        history_permission="Optional: Allow @everyone to read message history (True/False)"
    )
    async def temp_channel(
        self,
        interaction: discord.Interaction,
        name: str,
        category: discord.CategoryChannel,
        type: app_commands.Choice[str] = None,
        duration_minutes: int = 10,
        emoji: str = "⏳",
        topic: str = None,
        view_permission: bool = None,
        send_permission: bool = None,
        manage_permission: bool = None,
        embed_permission: bool = None,
        attach_permission: bool = None,
        history_permission: bool = None
    ):
        await interaction.response.defer(thinking=True)
        c_type = (type.value if type else "text").lower().strip()
        formatted_name = self.slugify_channel_name(name, emoji)

        overwrites = {}
        has_perm_options = any(x is not None for x in [
            view_permission, send_permission, manage_permission,
            embed_permission, attach_permission, history_permission
        ])

        if has_perm_options:
            everyone_role = interaction.guild.default_role
            ow = self.build_permission_overwrites(
                c_type=c_type,
                view_permission=view_permission,
                send_permission=send_permission,
                manage_permission=manage_permission,
                embed_permission=embed_permission,
                attach_permission=attach_permission,
                history_permission=history_permission
            )
            overwrites[everyone_role] = ow

        try:
            desc_topic = topic or f"Temporary channel. Auto-deletes in {duration_minutes} minute(s)."
            if c_type == "voice":
                ch = await interaction.guild.create_voice_channel(name=formatted_name, category=category, overwrites=overwrites)
            else:
                ch = await interaction.guild.create_text_channel(name=formatted_name, category=category, topic=desc_topic, overwrites=overwrites)

            embed = discord.Embed(
                title="⏳ Temporary Channel Active",
                description=f"Created temporary channel {ch.mention} in **{category.name}**. It will auto-delete in **{duration_minutes} minute(s)**.",
                color=0xeab308
            )
            await interaction.followup.send(embed=embed)

            async def auto_delete():
                await asyncio.sleep(duration_minutes * 60)
                try:
                    await ch.delete(reason="Temporary channel duration expired")
                except Exception: pass

            asyncio.create_task(auto_delete())
        except Exception as e:
            await interaction.followup.send(embed=discord.Embed(title="❌ Error", description=f"Error creating temp channel: {e}", color=0xf43f5e))

    # --- RESTRICTED BUILD COMMAND ---
    @app_commands.command(name="build", description="Nukes the server and deploys layout blueprint (Requires authorization).")
    async def build_server(self, interaction: discord.Interaction, url: str = None, file: discord.Attachment = None, server_id: str = None):
        user_id_str = str(interaction.user.id)
        if not (interaction.user.guild_permissions.administrator or is_user_authorized_for_build(user_id_str)):
            await interaction.response.send_message("❌ You are not authorized to execute `/build`. Contact the owner to grant permissions via `/build-allow`.", ephemeral=True)
            return

        await interaction.response.send_message(embed=discord.Embed(title="[Building...]", description="Deploying blueprint...", color=0xeab308))
        blueprint_data = None
        try:
            async with aiohttp.ClientSession() as session:
                target_url = file.url if file else url
                if target_url:
                    async with session.get(target_url) as resp:
                        if resp.status == 200: blueprint_data = await resp.json()
        except Exception as e:
            await interaction.followup.send(embed=discord.Embed(title="[Fetch Error]", description=f"Error: {e}", color=0xf43f5e))
            return

        if not blueprint_data:
            await interaction.followup.send(embed=discord.Embed(title="[Invalid Input]", description="Valid URL or file required.", color=0xf43f5e))
            return

        target_guild_id = int(server_id.strip()) if server_id else interaction.guild_id
        guild = self.bot.get_guild(target_guild_id)
        if not guild:
            await interaction.followup.send(embed=discord.Embed(title="[Guild Not Found]", description="Bot is not in target server.", color=0xf43f5e))
            return

        await self.execute_blueprint_build(guild, blueprint_data)
        await interaction.followup.send(embed=discord.Embed(title="[Server Rebuilt]", description=f"Rebuilt server **{guild.name}**.", color=0x2ecc71))

    # --- DIRECT HANDSHAKE & UTILITIES ---
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
                    await target_channel.send(embed=discord.Embed(
                        title="⚠️ [Auto-Build Pending - Bot Missing]",
                        description=f"Bot isn't in target server `{target_server_id}`! Direct message sent to user with invite link.",
                        color=0xeab308
                    ))
                except Exception: pass

            if user_id and str(user_id).isdigit():
                try:
                    usr = await self.bot.fetch_user(int(user_id))
                    if usr:
                        dm_embed = discord.Embed(
                            title="⚠️ Action Required: Add Bot to Complete Server Build",
                            description=f"Your design for server ID `{target_server_id}` is ready!\n\n👉 **[Click Here to Add Bot & Build]({invite_url})**",
                            color=0xeab308
                        )
                        await usr.send(embed=dm_embed)
                except Exception as e:
                    print(f"[DM Error] {e}")
            return

        try:
            await self.execute_blueprint_build(guild, blueprint_data)
            if target_channel:
                await target_channel.send(embed=discord.Embed(
                    title="⚡ [Auto-Build Complete]",
                    description=f"✅ Successfully built server **{guild.name}** (`{target_server_id}`) automatically!",
                    color=0x2ecc71
                ))
        except Exception as build_err:
            if target_channel:
                await target_channel.send(embed=discord.Embed(
                    title="❌ [Auto-Build Failed]",
                    description=f"Error deploying to `{target_server_id}`: `{str(build_err)}`",
                    color=0xf43f5e
                ))

    def slugify_channel_name(self, name: str, emoji: str = "") -> str:
        clean_name = re.sub(r'[^a-zA-Z0-9\s\-_]', '', name or "").strip().lower()
        clean_name = re.sub(r'[\s_]+', '-', clean_name).strip("-") or "unnamed-channel"
        return f"{emoji.strip()}│{clean_name}" if emoji and emoji.strip() else clean_name

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

    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=discord.Embed(title="[Pong]", description=f"Latency: {round(self.bot.latency * 1000)}ms", color=0x3b82f6))

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_commands(self, ctx: commands.Context):
        synced = await self.bot.tree.sync()
        await ctx.send(f"✅ Registered & synced {len(synced)} command(s).")


async def setup(bot):
    cog = BotCog(bot)
    await bot.add_cog(cog)
