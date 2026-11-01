import os
import re
import json
import random
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient

BOT_OWNER_ID = 1219266886143967245
MAIN_GUILD_ID = 1284513257574629458

PREMIUM_DESIGN_CHANNEL_ID = 1563544108696477802
NON_PREMIUM_DESIGN_CHANNEL_ID = 1563524101522464900
WEBSITE_LOGIN_CHANNEL_ID = 1563524100276617248
SUBSCRIPTION_LOG_CHANNEL_ID = 1564262413514182739

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
                {"$set": {"discord_id": str(user_id), "expires_at": iso_exp, "updated_at": datetime.now(timezone.utc).isoformat()}},
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
    if not text:
        return ""
    clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff]', '', str(text))
    return clean.strip()


def format_channel_name(name: str) -> str:
    clean = strip_emojis(name)
    if not clean:
        clean = str(name).strip()
    return clean.lower().replace(" ", "-")


class BotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------
    # SLASH COMMAND: SET / REVOKE PREMIUM STATUS
    # -------------------------------------------------------------
    @app_commands.command(name="premium-set", description="Grant or revoke TE Premium status for a user.")
    @app_commands.describe(
        user="The user to update premium status for",
        action="Choose whether to grant ('add') or revoke ('remove') premium",
        duration="Duration if adding (e.g. 1d, 7d, 30d, 1y, inf). Default is 30d."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Grant Premium", value="add"),
        app_commands.Choice(name="Revoke Premium", value="remove")
    ])
    async def premium_set(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        action: app_commands.Choice[str],
        duration: str = "30d"
    ):
        # Immediately defer to prevent Discord 3-second timeout ("Application did not respond")
        await interaction.response.defer(ephemeral=True)

        try:
            # Permission check: Only Bot Owner or Users with Manage Server / Administrator permissions
            is_owner = (interaction.user.id == BOT_OWNER_ID)
            is_admin = interaction.permissions.administrator or interaction.permissions.manage_guild

            if not (is_owner or is_admin):
                await interaction.followup.send("❌ You do not have permission to use this command.", ephemeral=True)
                return

            user_id_str = str(user.id)

            if action.value == "add":
                parsed_td = parse_duration_to_timedelta(duration)
                set_user_premium(user_id_str, parsed_td)

                if parsed_td == timedelta(days=36500):
                    exp_str = "Lifetime (Permanent)"
                else:
                    exp_date = datetime.now(timezone.utc) + parsed_td
                    exp_str = f"<t:{int(exp_date.timestamp())}:R>"

                embed = discord.Embed(
                    title="⭐ TE Premium Granted",
                    description=f"Successfully granted **TE Premium** to {user.mention} (`{user.id}`).\n\n**Duration:** `{duration}`\n**Expires:** {exp_str}",
                    color=0xF1C40F
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

            elif action.value == "remove":
                remove_user_premium(user_id_str)
                embed = discord.Embed(
                    title="🗑️ TE Premium Revoked",
                    description=f"Successfully revoked **TE Premium** from {user.mention} (`{user.id}`).",
                    color=0xF43F5E
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"[Premium Set Command Error] {e}")
            await interaction.followup.send(f"❌ An error occurred while executing the command: `{e}`", ephemeral=True)

    # -------------------------------------------------------------
    # SUB-ROUTINE: NUKE TARGET SERVER BEFORE AUTO-BUILDING
    # -------------------------------------------------------------
    async def nuke_guild_structure(self, guild: discord.Guild):
        """Removes all existing channels, categories, and custom roles."""
        print(f"[Nuke Engine] Nuking channels and roles for guild: {guild.name} ({guild.id})")
        for channel in list(guild.channels):
            try:
                await channel.delete(reason="Template Engine Auto-Build Server Reset")
                await asyncio.sleep(0.2)
            except Exception as e:
                print(f"[Nuke Warning] Could not delete channel {channel.name}: {e}")

        for role in list(guild.roles):
            if role.is_default() or role.managed:
                continue
            try:
                await role.delete(reason="Template Engine Auto-Build Server Reset")
                await asyncio.sleep(0.2)
            except Exception as e:
                print(f"[Nuke Warning] Could not delete role {role.name}: {e}")

    # -------------------------------------------------------------
    # LISTEN FOR PREMIUM AUTO-BUILD LOG MESSAGES
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if str(message.channel.id) != str(PREMIUM_DESIGN_CHANNEL_ID):
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

        if not target_guild_id or target_guild_id == "0":
            print("[Auto-Build Error] Target server ID is missing or 0.")
            return

        # 1. Fetch Target Guild via Cache or Discord API
        guild = self.bot.get_guild(int(target_guild_id))
        if not guild:
            try:
                guild = await self.bot.fetch_guild(int(target_guild_id))
            except Exception as fetch_err:
                print(f"[Auto-Build Fetch Error] Could not fetch target guild {target_guild_id}: {fetch_err}")
                guild = None

        if not guild:
            await self.notify_missing_bot(submitting_user_id, target_guild_id)
            return

        # 2. Verify User Admin / Owner Rights in Target Guild
        try:
            member = await guild.fetch_member(int(submitting_user_id))
        except Exception:
            member = None

        if not member:
            await self.notify_user_permission_failed(submitting_user_id, target_guild_id, "User is not a member of the target server.")
            return

        is_owner = (guild.owner_id == member.id)
        is_admin = member.guild_permissions.administrator

        if not (is_owner or is_admin):
            await self.notify_user_permission_failed(submitting_user_id, target_guild_id, "User lacks Administrator or Server Owner permissions in target server.")
            return

        # 3. Execute Nuke and Clean Auto-Build Deployment
        print(f"[Auto-Build] Deploying layout to {guild.name} ({guild.id}) for user {submitting_user_id}")
        await self.execute_auto_build(guild, layout)

    async def notify_missing_bot(self, user_id: str, target_guild_id: str):
        try:
            user = await self.bot.fetch_user(int(user_id))
            if user:
                embed = discord.Embed(
                    title="Bot Missing in Target Server",
                    description=f"Auto-deployment paused. The bot is not present in target server ID `{target_guild_id}`.\n\nPlease add the bot using the link below.",
                    color=0xF43F5E
                )
                invite_url = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands&guild_id={target_guild_id}"
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link))
                await user.send(embed=embed, view=view)
        except Exception as e:
            print(f"[DM Warning] Failed sending DM to {user_id}: {e}")

    async def notify_user_permission_failed(self, user_id: str, target_guild_id: str, reason: str):
        try:
            user = await self.bot.fetch_user(int(user_id))
            if user:
                embed = discord.Embed(
                    title="Auto-Build Deployment Cancelled",
                    description=f"Cannot deploy layout to target server ID `{target_guild_id}`.\n\n**Reason:** {reason}",
                    color=0xF43F5E
                )
                await user.send(embed=embed)
        except Exception as e:
            print(f"[DM Warning] Failed sending DM to {user_id}: {e}")

    async def execute_auto_build(self, guild: discord.Guild, layout: dict):
        try:
            # Step 1: Nuke target server
            await self.nuke_guild_structure(guild)

            created_roles = {}
            roles_data = layout.get("roles", [])

            # Step 2: Create Roles
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

            # Step 3: Create Categories & Channels
            categories = layout.get("categories", [])
            for cat_data in categories:
                cat_raw_name = cat_data.get("name", "CATEGORY")
                cat_emoji = cat_data.get("emoji", "")
                cat_clean_name = strip_emojis(cat_raw_name) or cat_raw_name
                cat_display = f"{cat_emoji} {cat_clean_name}".strip() if cat_emoji else cat_clean_name

                try:
                    category = await guild.create_category(
                        name=cat_display,
                        reason="Template Engine Auto-Build"
                    )
                    await asyncio.sleep(0.2)

                    for chan in cat_data.get("channels", []):
                        chan_raw_name = chan.get("name") or chan.get("clean_name") or "channel"
                        chan_emoji = chan.get("emoji", "")
                        chan_clean_name = format_channel_name(chan_raw_name)
                        chan_type = chan.get("type", "text")
                        chan_topic = chan.get("topic", "")

                        if chan_type == "voice":
                            chan_display = f"{chan_emoji} {chan_clean_name}".strip() if chan_emoji else chan_clean_name
                            await category.create_voice_channel(
                                name=chan_display,
                                reason="Template Engine Auto-Build"
                            )
                        else:
                            chan_display = f"{chan_emoji}-{chan_clean_name}".strip('-') if chan_emoji else chan_clean_name
                            await category.create_text_channel(
                                name=chan_display,
                                topic=chan_topic,
                                reason="Template Engine Auto-Build"
                            )
                        await asyncio.sleep(0.2)
                except Exception as e:
                    print(f"[Auto-Build Cat Error] {cat_display}: {e}")
        except Exception as top_err:
            print(f"[Auto-Build Execution Error] {top_err}")


async def setup(bot: commands.Bot):
    await bot.add_cog(BotCog(bot))
