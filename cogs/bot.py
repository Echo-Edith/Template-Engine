import os
import re
import json
import asyncio
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def slugify_channel_name(self, name: str, emoji: str = "") -> str:
        """Converts raw name input into a Discord-compliant slug (kebab-case) with optional emoji prefix."""
        if not name:
            name = ""

        emoji_pattern = re.compile(
            r'[\U00010000-\U0010ffff'
            r'\u2600-\u27BF'
            r'\u2300-\u23FF'
            r'\u2B05-\u2B07'
            r'\u2934-\u2935'
            r'\u3297-\u3299]+',
            flags=re.UNICODE
        )
        clean_name = emoji_pattern.sub('', name)
        clean_name = re.sub(r'[^a-zA-Z0-9\s\-_]', '', clean_name).strip().lower()
        clean_name = re.sub(r'[\s_]+', '-', clean_name).strip("-") or "unnamed-channel"

        if emoji and emoji.strip():
            return f"{emoji.strip()}│{clean_name}"
        return clean_name

    def parse_duration_seconds(self, duration_str: str) -> int:
        """Parses duration strings like '30s', '15m', '2h', '1d' into total seconds."""
        if not duration_str:
            return 0
        
        match = re.match(r"^(\d+)\s*([smhdSMHD])?$", duration_str.strip())
        if not match:
            return 0
            
        value, unit = match.groups()
        num = int(value)
        unit = (unit or "s").lower()

        if unit == "s":
            return num
        elif unit == "m":
            return num * 60
        elif unit == "h":
            return num * 3600
        elif unit == "d":
            return num * 86400
        return 0

    async def schedule_channel_deletion(
        self,
        channel: discord.abc.GuildChannel,
        delay_seconds: int,
        origin_channel: discord.abc.GuildChannel = None,
        executor: discord.User = None
    ):
        """Helper task to automatically delete a channel after specified delay in seconds and log it."""
        await asyncio.sleep(delay_seconds)
        channel_name = channel.name
        channel_id = channel.id
        try:
            await channel.delete(reason="Temporary Channel Expiration Auto-Delete")
            
            # Send deletion log to the channel where the command was executed
            if origin_channel:
                log_embed = discord.Embed(
                    title="[Temporary Channel Deleted]",
                    description=f"The temporary channel **#{channel_name}** (`{channel_id}`) has expired and was automatically deleted.",
                    color=0xf43f5e
                )
                if executor:
                    log_embed.add_field(name="Created By", value=executor.mention, inline=True)
                log_embed.set_footer(text="Echo Studio Infrastructure")
                
                try:
                    await origin_channel.send(embed=log_embed)
                except Exception as e:
                    print(f"Failed to send temp channel deletion log to origin channel: {e}")

        except discord.NotFound:
            pass  # Channel was already deleted manually
        except Exception as e:
            print(f"Failed to auto-delete temp channel {channel_id}: {e}")

    async def wipe_guild_infrastructure(self, guild: discord.Guild, leave_fallback_channel: bool = False):
        """Helper method to completely purge channels, categories, and roles from a server."""
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
                await guild.create_text_channel(
                    name="nuked",
                    topic="Server wiped clean by Owner execution."
                )
            except Exception as e:
                print(f"Failed to create fallback channel: {e}")

    # --- OWNER SYNC COMMAND ---
    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_commands(self, ctx: commands.Context):
        """Manual prefix command (!sync) to purge duplicate guild overrides and sync global commands."""
        embed = discord.Embed(
            title="[Syncing Commands]",
            description="Cleaning guild command overrides and syncing global commands...",
            color=0x3b82f6
        )
        embed.set_footer(text="Echo Studio Infrastructure")
        msg = await ctx.send(embed=embed)

        try:
            self.bot.tree.clear_commands(guild=ctx.guild)
            await self.bot.tree.sync(guild=ctx.guild)
            synced = await self.bot.tree.sync()

            success_embed = discord.Embed(
                title="[Sync Complete]",
                description=f"Successfully cleaned duplicate guild commands and globally synced {len(synced)} command(s).",
                color=0x2ecc71
            )
            success_embed.set_footer(text="Echo Studio Infrastructure")
            await msg.edit(embed=success_embed)
        except Exception as e:
            err_embed = discord.Embed(
                title="[Sync Error]",
                description=f"Failed to sync commands: {e}",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await msg.edit(embed=err_embed)

    # --- PING COMMAND ---
    @app_commands.command(
        name="ping",
        description="Check the bot response latency."
    )
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        embed = discord.Embed(
            title="[Pong]",
            description=f"Bot Latency: {latency}ms",
            color=0x3b82f6
        )
        embed.set_footer(text="Echo Studio Infrastructure")
        await interaction.response.send_message(embed=embed)

    # --- WEBSITE COMMAND ---
    @app_commands.command(
        name="website",
        description="Get the link to the dashboard and website."
    )
    async def website(self, interaction: discord.Interaction):
        website_url = os.getenv("WEBSITE_URL", os.getenv("DASHBOARD_URL", "https://templateengine-xi.vercel.app"))

        embed = discord.Embed(
            title="[Echo Studio Portal]",
            description="Click the button below to access the dashboard",
            color=0x8b5cf6
        )
        embed.set_footer(text="Echo Studio Dashboard")

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Visit Website", url=website_url, style=discord.ButtonStyle.link))

        await interaction.response.send_message(embed=embed, view=view)

    # --- ADMIN CREATE SINGLE CHANNEL COMMAND ---
    @app_commands.command(
        name="create_channel",
        description="[ADMIN ONLY] Create a single custom channel under a category with granular permissions."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        name="Name of the channel to create",
        type="Channel type: text, voice, or announcement",
        category="Target category to place this channel under",
        emoji="Optional emoji prefix (e.g. 💬)",
        topic="Optional topic/description for the channel",
        target_role="Role to apply these permission overrides to (defaults to @everyone)",
        view_channel="Allow viewing channel",
        send_messages="Allow sending messages",
        embed_links="Allow embedding links",
        attach_files="Allow attaching files/media",
        read_message_history="Allow viewing past messages",
        mention_everyone="Allow mentioning @everyone and @here",
        manage_messages="Allow deleting/pinning messages",
        manage_channel="Allow editing channel settings",
        connect="Allow connecting to voice",
        speak="Allow speaking in voice",
        stream="Allow sharing video/screen stream",
        use_voice_activation="Allow voice activation vs push-to-talk"
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="Text Channel", value="text"),
        app_commands.Choice(name="Voice Channel", value="voice"),
        app_commands.Choice(name="Announcement Channel", value="announcement")
    ])
    async def create_channel(
        self,
        interaction: discord.Interaction,
        name: str,
        type: str,
        category: discord.CategoryChannel = None,
        emoji: str = "",
        topic: str = "",
        target_role: discord.Role = None,
        view_channel: bool = None,
        send_messages: bool = None,
        embed_links: bool = None,
        attach_files: bool = None,
        read_message_history: bool = None,
        mention_everyone: bool = None,
        manage_messages: bool = None,
        manage_channel: bool = None,
        connect: bool = None,
        speak: bool = None,
        stream: bool = None,
        use_voice_activation: bool = None
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
        if mention_everyone is not None: perm_kwargs["mention_everyone"] = mention_everyone
        if manage_messages is not None: perm_kwargs["manage_messages"] = manage_messages
        if manage_channel is not None: perm_kwargs["manage_channels"] = manage_channel
        if connect is not None: perm_kwargs["connect"] = connect
        if speak is not None: perm_kwargs["speak"] = speak
        if stream is not None: perm_kwargs["stream"] = stream
        if use_voice_activation is not None: perm_kwargs["use_voice_activation"] = use_voice_activation

        if type == "announcement" and send_messages is None and role == interaction.guild.default_role:
            perm_kwargs["send_messages"] = False

        overwrites = {}
        if perm_kwargs:
            overwrites[role] = discord.PermissionOverwrite(**perm_kwargs)

        try:
            if type == "voice":
                new_chan = await interaction.guild.create_voice_channel(
                    name=clean_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Admin single channel creation by {interaction.user}"
                )
            elif type == "announcement":
                new_chan = await interaction.guild.create_text_channel(
                    name=clean_name,
                    category=category,
                    topic=topic,
                    overwrites=overwrites,
                    reason=f"Admin single channel creation by {interaction.user}"
                )
                if "COMMUNITY" in interaction.guild.features:
                    try:
                        await new_chan.edit(type=discord.ChannelType.news)
                    except Exception:
                        pass
            else:
                new_chan = await interaction.guild.create_text_channel(
                    name=clean_name,
                    category=category,
                    topic=topic,
                    overwrites=overwrites,
                    reason=f"Admin single channel creation by {interaction.user}"
                )

            embed = discord.Embed(
                title="[Channel Created]",
                description=f"Successfully created single channel {new_chan.mention} in **{interaction.guild.name}**.",
                color=0x2ecc71
            )
            embed.add_field(name="Category", value=category.name if category else "None", inline=True)
            embed.add_field(name="Type", value=type.capitalize(), inline=True)
            embed.add_field(name="Target Role", value=role.mention, inline=True)
            embed.set_footer(text="Echo Studio Infrastructure")

            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            err_embed = discord.Embed(
                title="[Channel Creation Failed]",
                description=f"An error occurred while creating channel: `{e}`",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed, ephemeral=False)

    # --- ADMIN TEMPORARY CHANNEL COMMAND ---
    @app_commands.command(
        name="temp_channel",
        description="[ADMIN ONLY] Create a temporary channel that is automatically deleted after a set duration."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        name="Name of the temporary channel",
        duration="Duration before auto-deletion (e.g., 30s, 15m, 2h, 1d)",
        type="Channel type: text, voice, or announcement",
        category="Target category to place this channel under",
        emoji="Optional emoji prefix (e.g. ⏳)",
        topic="Optional topic/description for the channel",
        target_role="Role to apply permission overrides to (defaults to @everyone)",
        view_channel="Allow viewing channel",
        send_messages="Allow sending messages",
        embed_links="Allow embedding links",
        attach_files="Allow attaching files/media",
        read_message_history="Allow viewing past messages",
        mention_everyone="Allow mentioning @everyone and @here",
        manage_messages="Allow deleting/pinning messages",
        manage_channel="Allow editing channel settings",
        connect="Allow connecting to voice",
        speak="Allow speaking in voice",
        stream="Allow sharing video/screen stream",
        use_voice_activation="Allow voice activation vs push-to-talk"
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="Text Channel", value="text"),
        app_commands.Choice(name="Voice Channel", value="voice"),
        app_commands.Choice(name="Announcement Channel", value="announcement")
    ])
    async def temp_channel(
        self,
        interaction: discord.Interaction,
        name: str,
        duration: str,
        type: str,
        category: discord.CategoryChannel = None,
        emoji: str = "⏳",
        topic: str = "",
        target_role: discord.Role = None,
        view_channel: bool = None,
        send_messages: bool = None,
        embed_links: bool = None,
        attach_files: bool = None,
        read_message_history: bool = None,
        mention_everyone: bool = None,
        manage_messages: bool = None,
        manage_channel: bool = None,
        connect: bool = None,
        speak: bool = None,
        stream: bool = None,
        use_voice_activation: bool = None
    ):
        await interaction.response.defer(thinking=True, ephemeral=False)

        delay_seconds = self.parse_duration_seconds(duration)
        if not delay_seconds or delay_seconds <= 0:
            err_embed = discord.Embed(
                title="[Invalid Duration]",
                description="Please enter a valid time format, e.g. `30s` (30 sec), `15m` (15 min), `2h` (2 hours), or `1d` (1 day).",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed, ephemeral=False)
            return

        if delay_seconds > 604800:  # Cap at 7 days
            err_embed = discord.Embed(
                title="[Duration Exceeded]",
                description="Temporary channel duration cannot exceed 7 days (`7d`).",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed, ephemeral=False)
            return

        clean_name = self.slugify_channel_name(name, emoji)
        role = target_role or interaction.guild.default_role

        perm_kwargs = {}
        if view_channel is not None: perm_kwargs["view_channel"] = view_channel
        if send_messages is not None: perm_kwargs["send_messages"] = send_messages
        if embed_links is not None: perm_kwargs["embed_links"] = embed_links
        if attach_files is not None: perm_kwargs["attach_files"] = attach_files
        if read_message_history is not None: perm_kwargs["read_message_history"] = read_message_history
        if mention_everyone is not None: perm_kwargs["mention_everyone"] = mention_everyone
        if manage_messages is not None: perm_kwargs["manage_messages"] = manage_messages
        if manage_channel is not None: perm_kwargs["manage_channels"] = manage_channel
        if connect is not None: perm_kwargs["connect"] = connect
        if speak is not None: perm_kwargs["speak"] = speak
        if stream is not None: perm_kwargs["stream"] = stream
        if use_voice_activation is not None: perm_kwargs["use_voice_activation"] = use_voice_activation

        if type == "announcement" and send_messages is None and role == interaction.guild.default_role:
            perm_kwargs["send_messages"] = False

        overwrites = {}
        if perm_kwargs:
            overwrites[role] = discord.PermissionOverwrite(**perm_kwargs)

        try:
            auto_delete_topic = f"⏱️ Temporary channel. Self-destructs after {duration}."
            full_topic = f"{topic}\n\n{auto_delete_topic}".strip() if topic else auto_delete_topic

            if type == "voice":
                new_chan = await interaction.guild.create_voice_channel(
                    name=clean_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Admin temp channel creation by {interaction.user} (expires in {duration})"
                )
            elif type == "announcement":
                new_chan = await interaction.guild.create_text_channel(
                    name=clean_name,
                    category=category,
                    topic=full_topic,
                    overwrites=overwrites,
                    reason=f"Admin temp channel creation by {interaction.user} (expires in {duration})"
                )
                if "COMMUNITY" in interaction.guild.features:
                    try:
                        await new_chan.edit(type=discord.ChannelType.news)
                    except Exception:
                        pass
            else:
                new_chan = await interaction.guild.create_text_channel(
                    name=clean_name,
                    category=category,
                    topic=full_topic,
                    overwrites=overwrites,
                    reason=f"Admin temp channel creation by {interaction.user} (expires in {duration})"
                )

            # Schedule background deletion task with origin channel reference for logging
            asyncio.create_task(
                self.schedule_channel_deletion(
                    channel=new_chan,
                    delay_seconds=delay_seconds,
                    origin_channel=interaction.channel,
                    executor=interaction.user
                )
            )

            embed = discord.Embed(
                title="[Temporary Channel Created]",
                description=f"Successfully created temporary channel {new_chan.mention} in **{interaction.guild.name}**.",
                color=0xeab308
            )
            embed.add_field(name="Auto-Delete Duration", value=f"`{duration}` ({delay_seconds} seconds)", inline=True)
            embed.add_field(name="Category", value=category.name if category else "None", inline=True)
            embed.add_field(name="Type", value=type.capitalize(), inline=True)
            embed.set_footer(text="Echo Studio Infrastructure")

            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            err_embed = discord.Embed(
                title="[Temp Channel Creation Failed]",
                description=f"An error occurred while creating temporary channel: `{e}`",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed, ephemeral=False)

    # --- ADMIN EDIT SINGLE CHANNEL COMMAND ---
    @app_commands.command(
        name="edit_channel",
        description="[ADMIN ONLY] Personalize or edit an existing channel's name, topic, category, or permissions."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Select the target channel to edit",
        new_name="New channel name",
        emoji="Emoji prefix to apply or update",
        new_topic="New channel topic/description",
        category="Move channel to a different category",
        target_role="Role to configure permissions for (defaults to @everyone)",
        view_channel="Allow/deny viewing channel",
        send_messages="Allow/deny sending messages",
        embed_links="Allow/deny embedding links",
        attach_files="Allow/deny attaching files",
        read_message_history="Allow/deny reading message history",
        mention_everyone="Allow/deny mentioning @everyone",
        manage_messages="Allow/deny managing messages",
        manage_channel="Allow/deny managing channel",
        connect="Allow/deny connecting to voice",
        speak="Allow/deny speaking in voice",
        stream="Allow/deny streaming/video",
        use_voice_activation="Allow/deny voice activation"
    )
    async def edit_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        new_name: str = None,
        emoji: str = "",
        new_topic: str = None,
        category: discord.CategoryChannel = None,
        target_role: discord.Role = None,
        view_channel: bool = None,
        send_messages: bool = None,
        embed_links: bool = None,
        attach_files: bool = None,
        read_message_history: bool = None,
        mention_everyone: bool = None,
        manage_messages: bool = None,
        manage_channel: bool = None,
        connect: bool = None,
        speak: bool = None,
        stream: bool = None,
        use_voice_activation: bool = None
    ):
        await interaction.response.defer(thinking=True, ephemeral=False)

        try:
            kwargs = {}
            target_name = new_name or channel.name
            if new_name or emoji:
                kwargs["name"] = self.slugify_channel_name(target_name, emoji)

            if category is not None:
                kwargs["category"] = category

            if new_topic is not None and isinstance(channel, (discord.TextChannel, discord.StageChannel)):
                kwargs["topic"] = new_topic

            if kwargs:
                await channel.edit(**kwargs, reason=f"Edited by {interaction.user}")

            role = target_role or interaction.guild.default_role
            overwrites = channel.overwrites_for(role)

            if view_channel is not None: overwrites.view_channel = view_channel
            if send_messages is not None: overwrites.send_messages = send_messages
            if embed_links is not None: overwrites.embed_links = embed_links
            if attach_files is not None: overwrites.attach_files = attach_files
            if read_message_history is not None: overwrites.read_message_history = read_message_history
            if mention_everyone is not None: overwrites.mention_everyone = mention_everyone
            if manage_messages is not None: overwrites.manage_messages = manage_messages
            if manage_channel is not None: overwrites.manage_channels = manage_channel
            if connect is not None: overwrites.connect = connect
            if speak is not None: overwrites.speak = speak
            if stream is not None: overwrites.stream = stream
            if use_voice_activation is not None: overwrites.use_voice_activation = use_voice_activation

            await channel.set_permissions(
                role,
                overwrite=overwrites,
                reason=f"Permissions updated by {interaction.user}"
            )

            embed = discord.Embed(
                title="[Channel Updated]",
                description=f"Successfully updated settings and permissions for {channel.mention}.",
                color=0x8b5cf6
            )
            embed.set_footer(text="Echo Studio Infrastructure")

            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            err_embed = discord.Embed(
                title="[Channel Edit Failed]",
                description=f"An error occurred while editing channel: `{e}`",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed, ephemeral=False)

    # --- BUILD COMMAND ---
    @app_commands.command(
        name="build",
        description="Nukes the server completely and rebuilds layout from a JSON blueprint file or URL."
    )
    @app_commands.describe(
        url="URL to blueprint JSON file",
        file="Uploaded JSON blueprint file",
        server_id="Target Server/Guild ID (Optional, defaults to blueprint metadata)"
    )
    async def build_server(
        self,
        interaction: discord.Interaction,
        url: str = None,
        file: discord.Attachment = None,
        server_id: str = None
    ):
        init_embed = discord.Embed(
            title="[Building...]",
            description="Initiating infrastructure wipe and deploying blueprint...",
            color=0xeab308
        )
        init_embed.set_footer(text="Echo Studio Infrastructure")
        await interaction.response.send_message(embed=init_embed)

        blueprint_data = None

        try:
            async with aiohttp.ClientSession() as session:
                if file:
                    async with session.get(file.url) as resp:
                        if resp.status == 200:
                            blueprint_data = await resp.json()
                elif url:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            blueprint_data = await resp.json()
        except Exception as e:
            err_embed = discord.Embed(
                title="[Fetch Error]",
                description=f"Failed to fetch blueprint data: {e}",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed)
            return

        if not blueprint_data:
            err_embed = discord.Embed(
                title="[Invalid Input]",
                description="Please provide a valid url link or upload a file attachment.",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed)
            return

        target_guild_id = None
        if server_id:
            target_guild_id = int(server_id.strip())
        elif "build_meta" in blueprint_data and blueprint_data["build_meta"].get("target_server_id"):
            raw_id = str(blueprint_data["build_meta"]["target_server_id"]).strip()
            target_guild_id = int(raw_id) if raw_id.isdigit() else interaction.guild_id
        else:
            target_guild_id = interaction.guild_id

        guild = self.bot.get_guild(target_guild_id)
        if not guild:
            try:
                guild = await self.bot.fetch_guild(target_guild_id)
            except Exception:
                guild = None

        if not guild:
            err_embed = discord.Embed(
                title="[Target Guild Not Found]",
                description=f"Bot is not in target server ({target_guild_id}). Please invite the bot to the server first.",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.followup.send(embed=err_embed)
            return

        await self.wipe_guild_infrastructure(guild, leave_fallback_channel=False)

        created_roles = {}
        for r_cfg in blueprint_data.get("roles", []):
            color_hex = str(r_cfg.get("color", "#8b5cf6")).replace("#", "")
            color_val = int(color_hex, 16) if color_hex else 0x8b5cf6
            try:
                role = await guild.create_role(
                    name=r_cfg["name"],
                    color=discord.Color(color_val),
                    reason="Echo Studio Infrastructure Automated Build"
                )
                created_roles[r_cfg["name"]] = role
            except Exception as e:
                print(f"Error creating role {r_cfg.get('name')}: {e}")

        for cat_cfg in blueprint_data.get("categories", []):
            try:
                cat_name = str(cat_cfg.get("name", "CATEGORY")).strip()
                category = await guild.create_category(name=cat_name)

                for chan_cfg in cat_cfg.get("channels", []):
                    c_raw_name = chan_cfg.get("name", "channel")
                    c_emoji = chan_cfg.get("emoji", "")
                    c_name = self.slugify_channel_name(c_raw_name, c_emoji)
                    c_type = chan_cfg.get("type", "text")
                    c_topic = chan_cfg.get("topic", "")

                    perms_dict = chan_cfg.get("permissions", {})
                    read_only = chan_cfg.get("read_only", False)

                    overwrites = {}
                    if read_only or c_type == "announcement":
                        overwrites[guild.default_role] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=False
                        )
                    elif perms_dict:
                        overwrites[guild.default_role] = discord.PermissionOverwrite(
                            view_channel=perms_dict.get("view", True),
                            send_messages=perms_dict.get("send", True),
                            embed_links=perms_dict.get("embed", True),
                            attach_files=perms_dict.get("attach", True),
                            read_message_history=perms_dict.get("history", True),
                            connect=perms_dict.get("connect", True),
                            speak=perms_dict.get("speak", True)
                        )

                    if c_type == "voice":
                        await guild.create_voice_channel(
                            name=c_name,
                            category=category,
                            overwrites=overwrites
                        )
                    elif c_type == "announcement":
                        new_chan = await guild.create_text_channel(
                            name=c_name,
                            category=category,
                            topic=c_topic,
                            overwrites=overwrites
                        )
                        if "COMMUNITY" in guild.features:
                            try:
                                await new_chan.edit(type=discord.ChannelType.news)
                            except Exception:
                                pass
                    else:
                        await guild.create_text_channel(
                            name=c_name,
                            category=category,
                            topic=c_topic,
                            overwrites=overwrites
                        )
            except Exception as e:
                print(f"Error creating category/channel: {e}")

        embed = discord.Embed(
            title="[Server Rebuilt]",
            description=f"Successfully nuked previous layout and deployed new blueprint to {guild.name} ({guild.id}).",
            color=0x2ecc71
        )
        embed.set_footer(text="Echo Studio Infrastructure")

        try:
            await interaction.followup.send(embed=embed)
        except Exception:
            if interaction.user:
                await interaction.user.send(embed=embed)

    # --- OWNER-ONLY NUKE COMMAND ---
    @app_commands.command(
        name="nuke",
        description="[OWNER ONLY] Deletes channels and roles. Leaves 1 channel unless optional server_id is specified."
    )
    @app_commands.describe(
        server_id="Optional target Server/Guild ID. If provided, wipes EVERYTHING without leaving a fallback channel."
    )
    async def nuke_server(
        self,
        interaction: discord.Interaction,
        server_id: str = None
    ):
        if not await self.bot.is_owner(interaction.user):
            err_embed = discord.Embed(
                title="[Access Denied]",
                description="Only the Bot Owner can execute /nuke.",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        target_guild_id = int(server_id.strip()) if server_id else interaction.guild_id
        guild = self.bot.get_guild(target_guild_id)

        if not guild:
            try:
                guild = await self.bot.fetch_guild(target_guild_id)
            except Exception:
                guild = None

        if not guild:
            err_embed = discord.Embed(
                title="[Guild Not Found]",
                description=f"Guild ID {target_guild_id} not found or bot is not present.",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Echo Studio Infrastructure")
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        init_embed = discord.Embed(
            title="[Nuking...]",
            description=f"Initiating complete server nuke on {guild.name} ({guild.id})...",
            color=0xeab308
        )
        init_embed.set_footer(text="Echo Studio Infrastructure")
        await interaction.response.send_message(embed=init_embed)

        leave_channel = False if server_id else True
        await self.wipe_guild_infrastructure(guild, leave_fallback_channel=leave_channel)

        if server_id:
            try:
                dm_embed = discord.Embed(
                    title="[Server Nuked]",
                    description=f"Complete server nuke executed on {guild.name} ({guild.id}). All channels and roles deleted.",
                    color=0x2ecc71
                )
                dm_embed.set_footer(text="Echo Studio Infrastructure")
                await interaction.user.send(embed=dm_embed)
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(BotCog(bot))
