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

    # --- 1. /build COMMAND (AUTHORIZED USER IDs ONLY) ---
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

    # --- 2. /build-allow COMMAND (OWNER ONLY) ---
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

    # --- 3. UNIFIED /channel COMMAND (SERVER ADMINS ONLY) ---
    @app_commands.command(name="channel", description="Create, edit, or set temporary channels with permission controls.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Create Channel", value="create"),
            app_commands.Choice(name="Edit Channel", value="edit"),
            app_commands.Choice(name="Temporary Channel", value="temporary")
        ],
        type=[
            app_commands.Choice(name="Text Channel", value="text"),
            app_commands.Choice(name="Voice Channel", value="voice"),
            app_commands.Choice(name="Announcement Channel", value="announcement"),
            app_commands.Choice(name="Thread Channel", value="threads")
        ]
    )
    @app_commands.describe(
        action="Action type: create, edit, or temporary",
        name="Channel name",
        category="Category channel placement",
        channel="Target channel (Required for edit action)",
        type="Channel type (text, voice, announcement, threads)",
        emoji="Optional prefix string (Leave empty for none)",
        topic="Channel topic description",
        duration_minutes="Temporary auto-delete timer in minutes (Default: 10)",
        view_permission="Allow @everyone to view channel (True/False)",
        send_permission="Allow @everyone to send messages/connect (True/False)",
        manage_permission="Allow @everyone to manage channel (True/False)",
        embed_permission="Allow @everyone to embed links (True/False)",
        attach_permission="Allow @everyone to attach files (True/False)",
        history_permission="Allow @everyone to read message history (True/False)"
    )
    async def channel_management(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        name: str = None,
        category: discord.CategoryChannel = None,
        channel: discord.abc.GuildChannel = None,
        type: app_commands.Choice[str] = None,
        emoji: str = None,
        topic: str = None,
        duration_minutes: int = 10,
        view_permission: bool = None,
        send_permission: bool = None,
        manage_permission: bool = None,
        embed_permission: bool = None,
        attach_permission: bool = None,
        history_permission: bool = None
    ):
        await interaction.response.defer(thinking=True)
        act_val = action.value.lower()
        c_type = (type.value if type else "text").lower().strip()

        overwrites = {}
        has_perm_options = any(x is not None for x in [
            view_permission, send_permission, manage_permission,
            embed_permission, attach_permission, history_permission
        ])

        if has_perm_options:
            everyone_role = interaction.guild.default_role
            base_ow = channel.overwrites_for(everyone_role) if channel else None
            ow = self.build_permission_overwrites(
                base_overwrite=base_ow,
                c_type=c_type,
                view_permission=view_permission,
                send_permission=send_permission,
                manage_permission=manage_permission,
                embed_permission=embed_permission,
                attach_permission=attach_permission,
                history_permission=history_permission
            )
            overwrites[everyone_role] = ow

        # ACTION: CREATE
        if act_val == "create":
            if not name:
                err = discord.Embed(title="[MISSING PARAMETER]", description="Channel name parameter is required for creation.", color=0xf43f5e)
                err.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=err)
                return

            formatted_name = self.slugify_channel_name(name, emoji or "")

            try:
                if c_type == "voice":
                    created_ch = await interaction.guild.create_voice_channel(name=formatted_name, category=category, overwrites=overwrites)
                elif c_type == "announcement":
                    created_ch = await interaction.guild.create_text_channel(name=formatted_name, category=category, topic=topic or "", news=True, overwrites=overwrites)
                elif c_type == "threads":
                    target_parent = channel if isinstance(channel, discord.TextChannel) else await interaction.guild.create_text_channel(name=formatted_name, category=category, topic=topic or "", overwrites=overwrites)
                    created_ch = await target_parent.create_thread(name=formatted_name, auto_archive_duration=1440)
                else:
                    created_ch = await interaction.guild.create_text_channel(name=formatted_name, category=category, topic=topic or "", overwrites=overwrites)

                embed = discord.Embed(
                    title="[CHANNEL CREATED]",
                    description=f"Successfully created channel {created_ch.mention} ({created_ch.id}).",
                    color=0x2ecc71
                )
                if topic: embed.add_field(name="Topic", value=topic, inline=False)
                embed.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                err_ch = discord.Embed(title="[CREATION ERROR]", description=f"Failed to create channel: {e}", color=0xf43f5e)
                err_ch.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=err_ch)

        # ACTION: EDIT
        elif act_val == "edit":
            if not channel:
                err = discord.Embed(title="[MISSING PARAMETER]", description="Target channel parameter is required for edit action.", color=0xf43f5e)
                err.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=err)
                return

            try:
                kwargs = {}
                if name or emoji:
                    base_n = name or channel.name
                    kwargs["name"] = self.slugify_channel_name(base_n, emoji or "")
                if category is not None and not isinstance(channel, discord.CategoryChannel):
                    kwargs["category"] = category
                if topic is not None and isinstance(channel, discord.TextChannel):
                    kwargs["topic"] = topic

                if has_perm_options:
                    everyone_role = interaction.guild.default_role
                    updated_ow = overwrites[everyone_role]
                    kwargs["overwrites"] = {**channel.overwrites, everyone_role: updated_ow}

                await channel.edit(**kwargs)
                embed = discord.Embed(title="[CHANNEL UPDATED]", description=f"Successfully edited channel {channel.mention}.", color=0x2ecc71)
                embed.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                err_edit = discord.Embed(title="[EDIT ERROR]", description=f"Failed to edit channel: {e}", color=0xf43f5e)
                err_edit.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=err_edit)

        # ACTION: TEMPORARY
        elif act_val == "temporary":
            if not name:
                err = discord.Embed(title="[MISSING PARAMETER]", description="Channel name parameter is required for temporary channels.", color=0xf43f5e)
                err.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=err)
                return

            formatted_name = self.slugify_channel_name(name, emoji or "temp")
            desc_topic = topic or f"Temporary channel. Auto-deletes in {duration_minutes} minute(s)."

            try:
                if c_type == "voice":
                    temp_ch = await interaction.guild.create_voice_channel(name=formatted_name, category=category, overwrites=overwrites)
                else:
                    temp_ch = await interaction.guild.create_text_channel(name=formatted_name, category=category, topic=desc_topic, overwrites=overwrites)

                embed = discord.Embed(
                    title="[TEMPORARY CHANNEL CREATED]",
                    description=f"Created temporary channel {temp_ch.mention}. Auto-deletes in {duration_minutes} minute(s).",
                    color=0xeab308
                )
                embed.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=embed)

                async def auto_delete():
                    await asyncio.sleep(duration_minutes * 60)
                    try:
                        await temp_ch.delete(reason="Temporary channel duration expired")
                    except Exception: pass

                asyncio.create_task(auto_delete())
            except Exception as e:
                err_temp = discord.Embed(title="[TEMP ERROR]", description=f"Failed to create temporary channel: {e}", color=0xf43f5e)
                err_temp.set_footer(text="Template Engine Infrastructure")
                await interaction.followup.send(embed=err_temp)

    @channel_management.error
    async def channel_management_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            err_perm = discord.Embed(
                title="[PERMISSION DENIED]",
                description="Administrator permission 'Manage Channels' is required to execute this command.",
                color=0xf43f5e
            )
            err_perm.set_footer(text="Template Engine Infrastructure")
            if interaction.response.is_done():
                await interaction.followup.send(embed=err_perm, ephemeral=True)
            else:
                await interaction.response.send_message(embed=err_perm, ephemeral=True)

    # --- 4. /premium COMMAND (PUBLIC) ---
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

    # --- 5. /premium-set COMMAND (OWNER ONLY) ---
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

    # --- 6. /ping COMMAND (PUBLIC) ---
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

    # --- 7. /nuke COMMAND (OWNER ONLY) ---
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

    # --- 8. /website COMMAND (PUBLIC) ---
    @app_commands.command(name="website", description="Get the official Template Engine website link.")
    async def website_link(self, interaction: discord.Interaction):
        website_url = os.getenv("WEBSITE_URL", os.getenv("DASHBOARD_URL", "https://templateengine-xi.vercel.app"))
        
        embed = discord.Embed(
            title="[TEMPLATE ENGINE WEBSITE]",
            description=f"Access the interactive layout generator and dashboard online:\n{website_url}",
            color=0x3b82f6
        )
        embed.set_footer(text="Template Engine Infrastructure")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Website", url=website_url, style=discord.ButtonStyle.link))
        
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
    cog = BotCog(bot)
    await bot.add_cog(cog)
