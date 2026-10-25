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

# MongoDB Client Setup for Premium persistence in Bot Cog
MONGO_URI = os.getenv("MONGO_URI")
db_premium = None
if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client["static_studio_db"]
        db_premium = db["premium_users"]
    except Exception as e:
        print(f"[Bot Mongo Warning] Could not connect to MongoDB for Premium: {e}")

LOCAL_PREMIUM_USERS = {}
PREMIUM_ROLE_NAME = "Template Engine Premium"
PREMIUM_ROLE_COLOR = discord.Color(0xF1C40F)  # Vibrant Gold Color


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


def get_all_active_premiums():
    now = datetime.now(timezone.utc)
    active = []
    if db_premium is not None:
        docs = db_premium.find()
        for doc in docs:
            uid = doc.get("user_id")
            exp_raw = doc.get("expires_at")
            if uid and exp_raw:
                exp = datetime.fromisoformat(exp_raw) if isinstance(exp_raw, str) else exp_raw
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp > now:
                    active.append((uid, exp))
    else:
        for uid, exp in LOCAL_PREMIUM_USERS.items():
            if exp > now:
                active.append((uid, exp))
    return active


def get_all_expired_premiums():
    now = datetime.now(timezone.utc)
    expired = []
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
                    expired.append(uid)
    else:
        for uid, exp in LOCAL_PREMIUM_USERS.items():
            if exp <= now:
                expired.append(uid)
    return expired


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_premium_expirations.start()

    def cog_unload(self):
        self.check_premium_expirations.cancel()

    async def get_or_create_premium_role(self, guild: discord.Guild) -> discord.Role:
        """Finds or creates the gold 'Template Engine Premium' role in the specified guild."""
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
        """Assigns the gold TE Premium role to a user across all mutual guilds."""
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
        """Removes the gold TE Premium role from a user across all mutual guilds."""
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
        """Background loop that checks for expired subscriptions every minute and strips the role."""
        expired_ids = get_all_expired_premiums()
        for uid in expired_ids:
            remove_user_premium(uid)
            await self.remove_premium_role(uid)

    @check_premium_expirations.before_loop
    async def before_check_expirations(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Listens for 'One-Click Build' button interactions sent via webhooks."""
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("buildbtn_"):
            return

        parts = custom_id.split("_", 2)
        if len(parts) < 3:
            await interaction.response.send_message("❌ Invalid build button payload.", ephemeral=True)
            return

        target_server_id = parts[1]
        blueprint_url = parts[2]

        guild = None
        try:
            guild_id = int(target_server_id)
            guild = self.bot.get_guild(guild_id)
            if not guild:
                guild = await self.bot.fetch_guild(guild_id)
        except Exception:
            guild = None

        if not guild:
            client_id = os.getenv("DISCORD_CLIENT_ID", str(self.bot.user.id if self.bot.user else ""))
            invite_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot%20applications.commands&guild_id={target_server_id}"

            await interaction.response.send_message(
                embed=discord.Embed(
                    title="[Build Error]",
                    description=f"❌ The bot isn't in the server! Please invite the bot first.\nTarget Server ID: `{target_server_id}`",
                    color=0xf43f5e
                ),
                ephemeral=True
            )

            try:
                dm_embed = discord.Embed(
                    title="⚠️ Bot Missing From Target Server",
                    description=(
                        f"You attempted a One-Click Build for server ID `{target_server_id}`, but the bot has not been added to that server yet.\n\n"
                        f"👉 **[Click Here to Add Bot to Target Server]({invite_url})**"
                    ),
                    color=0xeab308
                )
                dm_embed.set_footer(text="Echo Studio Infrastructure")
                await interaction.user.send(embed=dm_embed)
            except Exception as e:
                print(f"[DM Error] Could not send direct message to user {interaction.user}: {e}")
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="[One-Click Build Initiated]",
                description=f"⚡ Starting server build for **{guild.name}** (`{target_server_id}`)...",
                color=0xeab308
            ),
            ephemeral=True
        )

        blueprint_data = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(blueprint_url) as resp:
                    if resp.status == 200:
                        blueprint_data = await resp.json()
        except Exception as e:
            print(f"Failed to fetch blueprint file: {e}")

        if not blueprint_data:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="[Build Error]",
                    description="❌ Failed to load blueprint JSON file from server.",
                    color=0xf43f5e
                ),
                ephemeral=True
            )
            return

        asyncio.create_task(self.execute_blueprint_build(guild, blueprint_data))
        await interaction.followup.send(
            embed=discord.Embed(
                title="[Build Executing]",
                description=f"✅ Successfully queued build for server **{guild.name}**! Channels and roles are being generated now.",
                color=0x2ecc71
            ),
            ephemeral=True
        )

    def slugify_channel_name(self, name: str, emoji: str = "") -> str:
        if not name:
            name = ""
        emoji_pattern = re.compile(
            r'[\U00010000-\U0010ffff\u2600-\u27BF\u2300-\u23FF\u2B05-\u2B07\u2934-\u2935\u3297-\u3299]+',
            flags=re.UNICODE
        )
        clean_name = emoji_pattern.sub('', name)
        clean_name = re.sub(r'[^a-zA-Z0-9\s\-_]', '', clean_name).strip().lower()
        clean_name = re.sub(r'[\s_]+', '-', clean_name).strip("-") or "unnamed-channel"

        if emoji and emoji.strip():
            return f"{emoji.strip()}│{clean_name}"
        return clean_name

    def parse_duration_seconds(self, duration_str: str) -> int:
        if not duration_str:
            return 0
        match = re.match(r"^(\d+)\s*([smhdSMHD])?$", duration_str.strip())
        if not match:
            return 0
        value, unit = match.groups()
        num = int(value)
        unit = (unit or "s").lower()
        if unit == "s": return num
        elif unit == "m": return num * 60
        elif unit == "h": return num * 3600
        elif unit == "d": return num * 84600
        return 0

    async def wipe_guild_infrastructure(self, guild: discord.Guild, leave_fallback_channel: bool = False):
        for channel in guild.channels:
            try:
                await channel.delete(reason="Server Nuke/Rebuild Execution")
            except Exception as e:
                print(f"Failed to delete channel {channel.name}: {e}")

        for role in guild.roles:
            if not role.is_default() and not role.is_bot_managed() and not role.is_premium_subscriber():
                try:
                    await role.delete(reason="Server Nuke/Rebuild Execution")
                except Exception as e:
                    print(f"Failed to delete role {role.name}: {e}")

        if leave_fallback_channel:
            try:
                await guild.create_text_channel(name="nuked", topic="Server wiped clean by Owner execution.")
            except Exception as e:
                print(f"Failed to create fallback channel: {e}")

    # --- TE PREMIUM COMMANDS ---
    premium_group = app_commands.Group(name="premium", description="[ADMIN ONLY] Manage TE Premium Subscriptions")

    @premium_group.command(name="add", description="Grant TE Premium to a user & assign gold role (Default: 30 days)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user="User to grant TE Premium", length="Duration (e.g. 30d, 60d, 1y). Default: 30d")
    async def premium_add(self, interaction: discord.Interaction, user: discord.User, length: str = "30d"):
        await interaction.response.defer(thinking=True)
        td = parse_duration_to_timedelta(length)
        set_user_premium(str(user.id), td)
        exp_date = datetime.now(timezone.utc) + td

        await self.assign_premium_role(str(user.id))

        embed = discord.Embed(
            title="[TE Premium Activated]",
            description=f"Granted TE Premium status to {user.mention} and assigned the **{PREMIUM_ROLE_NAME}** gold role!",
            color=0xF1C40F
        )
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Expires On", value=f"<t:{int(exp_date.timestamp())}:F>", inline=True)
        embed.set_footer(text="TE Premium Infrastructure")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @premium_group.command(name="remove", description="Revoke TE Premium status & remove gold role from a user")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user="User to revoke TE Premium from")
    async def premium_remove(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(thinking=True)
        remove_user_premium(str(user.id))

        await self.remove_premium_role(str(user.id))

        embed = discord.Embed(
            title="[TE Premium Revoked]",
            description=f"Revoked TE Premium status from {user.mention} and removed the **{PREMIUM_ROLE_NAME}** role.",
            color=0xf43f5e
        )
        embed.set_footer(text="TE Premium Infrastructure")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @premium_group.command(name="check", description="Check TE Premium status of a user")
    @app_commands.describe(user="User to check")
    async def premium_check(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        is_prem = is_user_premium(str(target.id))

        if is_prem:
            embed = discord.Embed(
                title="[TE Premium Active]",
                description=f"{target.mention} holds active TE Premium membership! 👑",
                color=0xF1C40F
            )
        else:
            embed = discord.Embed(
                title="[Standard User]",
                description=f"{target.mention} does not have active TE Premium.",
                color=0x95a5a6
            )
        embed.set_footer(text="TE Premium Infrastructure")
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="premiums", description="[ADMIN ONLY] List all active TE Premium subscribers.")
    @app_commands.checks.has_permissions(administrator=True)
    async def premiums_list(self, interaction: discord.Interaction):
        active_list = get_all_active_premiums()

        if not active_list:
            embed = discord.Embed(
                title="[Active TE Premiums]",
                description="There are currently no active TE Premium subscribers.",
                color=0x3b82f6
            )
            embed.set_footer(text="TE Premium Infrastructure")
            await interaction.response.send_message(embed=embed, ephemeral=False)
            return

        desc_lines = []
        for uid, exp in active_list:
            desc_lines.append(f"• <@{uid}> — Expires: <t:{int(exp.timestamp())}:R>")

        embed = discord.Embed(
            title=f"[Active TE Premiums ({len(active_list)})]",
            description="\n".join(desc_lines[:25]),
            color=0xF1C40F
        )
        embed.set_footer(text="TE Premium Infrastructure")
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # --- OWNER SYNC COMMAND ---
    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_commands(self, ctx: commands.Context):
        embed = discord.Embed(title="[Syncing Commands]", description="Syncing commands...", color=0x3b82f6)
        msg = await ctx.send(embed=embed)
        try:
            self.bot.tree.add_command(self.premium_group)
        except Exception:
            pass
        try:
            self.bot.tree.clear_commands(guild=ctx.guild)
            await self.bot.tree.sync(guild=ctx.guild)
            synced = await self.bot.tree.sync()
            success_embed = discord.Embed(title="[Sync Complete]", description=f"Synced {len(synced)} global command(s).", color=0x2ecc71)
            await msg.edit(embed=success_embed)
        except Exception as e:
            err_embed = discord.Embed(title="[Sync Error]", description=f"Failed to sync: {e}", color=0xf43f5e)
            await msg.edit(embed=err_embed)

    # --- PING & WEBSITE COMMANDS ---
    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=discord.Embed(title="[Pong]", description=f"Latency: {round(self.bot.latency * 1000)}ms", color=0x3b82f6))

    @app_commands.command(name="website", description="Get the link to the dashboard.")
    async def website(self, interaction: discord.Interaction):
        website_url = os.getenv("WEBSITE_URL", os.getenv("DASHBOARD_URL", "https://templateengine-xi.vercel.app"))
        embed = discord.Embed(title="[Echo Studio Portal]", description="Click button below to access dashboard", color=0x8b5cf6)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Visit Website", url=website_url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view)

    # --- ADMIN CHANNEL CREATION & TEMPORARY CHANNELS ---
    @app_commands.command(name="create_channel", description="[ADMIN ONLY] Create a single custom channel.")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_channel(
        self, interaction: discord.Interaction, name: str, type: str, category: discord.CategoryChannel = None,
        emoji: str = "", topic: str = "", target_role: discord.Role = None, view_channel: bool = None,
        send_messages: bool = None, embed_links: bool = None, attach_files: bool = None, read_message_history: bool = None
    ):
        await interaction.response.defer(thinking=True, ephemeral=False)
        clean_name = self.slugify_channel_name(name, emoji)
        role = target_role or interaction.guild.default_role
        perm_kwargs = {}
        if view_channel is not None: perm_kwargs["view_channel"] = view_channel
        if send_messages is not None: perm_kwargs["send_messages"] = send_messages
        if embed_links is not None: perm_kwargs["embed_links"] = embed_links
        if attach_files is not None: perm_kwargs["attach_files"] = attach_files
        if read_message_history is not None: perm_kwargs["read_message_history"] = read_message_history
        overwrites = {role: discord.PermissionOverwrite(**perm_kwargs)} if perm_kwargs else {}

        try:
            if type == "voice":
                new_chan = await interaction.guild.create_voice_channel(name=clean_name, category=category, overwrites=overwrites)
            else:
                new_chan = await interaction.guild.create_text_channel(name=clean_name, category=category, topic=topic, overwrites=overwrites)
            await interaction.followup.send(embed=discord.Embed(title="[Channel Created]", description=f"Created {new_chan.mention}", color=0x2ecc71))
        except Exception as e:
            await interaction.followup.send(embed=discord.Embed(title="[Creation Failed]", description=f"Error: `{e}`", color=0xf43f5e))

    # --- BUILD COMMAND ---
    @app_commands.command(name="build", description="Nukes the server and deploys layout blueprint.")
    async def build_server(self, interaction: discord.Interaction, url: str = None, file: discord.Attachment = None, server_id: str = None):
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

    async def execute_blueprint_build(self, guild: discord.Guild, blueprint_data: dict):
        await self.wipe_guild_infrastructure(guild, leave_fallback_channel=False)
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
                    c_type = chan_cfg.get("type", "text")
                    if c_type == "voice":
                        await guild.create_voice_channel(name=c_name, category=category)
                    else:
                        await guild.create_text_channel(name=c_name, category=category, topic=chan_cfg.get("topic", ""))
            except Exception: pass

    # --- OWNER-ONLY NUKE COMMAND ---
    @app_commands.command(name="nuke", description="[OWNER ONLY] Deletes channels and roles.")
    async def nuke_server(self, interaction: discord.Interaction, server_id: str = None):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(embed=discord.Embed(title="[Access Denied]", description="Owner only.", color=0xf43f5e), ephemeral=True)
            return

        target_guild_id = int(server_id.strip()) if server_id else interaction.guild_id
        guild = self.bot.get_guild(target_guild_id)
        if not guild:
            await interaction.response.send_message(embed=discord.Embed(title="[Access Denied]", description="Guild not found.", color=0xf43f5e), ephemeral=True)
            return

        await interaction.response.send_message(embed=discord.Embed(title="[Nuking...]", description=f"Nuking {guild.name}...", color=0xeab308))
        await self.wipe_guild_infrastructure(guild, leave_fallback_channel=not bool(server_id))


async def setup(bot):
    cog = BotCog(bot)
    await bot.add_cog(cog)
    try:
        bot.tree.add_command(cog.premium_group)
    except Exception:
        pass
