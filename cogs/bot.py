import json
import asyncio
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

CHANNEL_LOG_PREMIUM_DESIGNS = 1563544108696477802

class RetryAutoBuildView(discord.ui.View):
    def __init__(self, client_id: str, target_guild_id: str, payload: dict):
        super().__init__(timeout=None)
        invite_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot%20applications.commands&guild_id={target_guild_id}"
        self.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link))
        self.payload = payload
        self.target_guild_id = target_guild_id

    @discord.ui.button(label="Retry Auto-Deploy", style=discord.ButtonStyle.primary, custom_id="btn_retry_autobuild")
    async def retry_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.client.get_guild(int(self.target_guild_id))
        if not guild:
            await interaction.followup.send("Bot is still not present in the target server. Please invite the bot first!", ephemeral=True)
            return

        await interaction.followup.send("Bot detected in server! Starting layout auto-build...", ephemeral=True)
        bot_cog = interaction.client.get_cog("BotCog")
        if bot_cog:
            await bot_cog.execute_autobuild(guild, self.payload)


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def execute_autobuild(self, guild: discord.Guild, payload: dict):
        layout = payload.get("layout", {})
        roles_data = layout.get("roles", [])
        categories_data = layout.get("categories", [])

        # 1. Create Roles
        created_roles = {}
        for r in roles_data:
            r_name = r.get("name")
            if not r_name:
                continue
            r_color_hex = r.get("color", "#8b5cf6").lstrip("#")
            try:
                color_int = int(r_color_hex, 16)
            except Exception:
                color_int = 0x8b5cf6

            existing = discord.utils.get(guild.roles, name=r_name)
            if not existing:
                try:
                    role_obj = await guild.create_role(name=r_name, color=discord.Color(color_int))
                    created_roles[r_name] = role_obj
                except Exception as e:
                    print(f"[Role Creation Error] {e}")
            else:
                created_roles[r_name] = existing

        # 2. Create Categories & Channels
        for cat_data in categories_data:
            cat_name = cat_data.get("name", "CATEGORY")
            emoji = cat_data.get("emoji", "")
            full_cat_name = f"{emoji} {cat_name}".strip() if emoji else cat_name

            try:
                cat_obj = await guild.create_category(name=full_cat_name)
            except Exception as e:
                print(f"[Category Creation Error] {e}")
                continue

            for ch_data in cat_data.get("channels", []):
                ch_name = ch_data.get("name", "channel").lower().replace(" ", "-")
                ch_emoji = ch_data.get("emoji", "")
                full_ch_name = f"{ch_emoji} {ch_name}".strip() if ch_emoji else ch_name
                ch_type = ch_data.get("type", "text")
                topic = ch_data.get("topic", "")
                is_readonly = ch_data.get("read_only", False)

                overwrites = {}
                if is_readonly:
                    overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)

                try:
                    if ch_type == "voice":
                        await cat_obj.create_voice_channel(name=full_ch_name, overwrites=overwrites)
                    else:
                        await cat_obj.create_text_channel(name=full_ch_name, topic=topic, overwrites=overwrites)
                except Exception as e:
                    print(f"[Channel Creation Error] {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Intercept attached design blueprints in the Premium Log channel
        if message.channel.id == CHANNEL_LOG_PREMIUM_DESIGNS and message.attachments:
            for attachment in message.attachments:
                if attachment.filename == "blueprint.json":
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(attachment.url) as resp:
                                if resp.status == 200:
                                    payload_bytes = await resp.read()
                                    payload = json.loads(payload_bytes.decode("utf-8"))

                                    target_guild_id = str(payload.get("target_server_id", ""))
                                    user_id = str(payload.get("submitting_user_id", ""))

                                    if not target_guild_id.isdigit():
                                        return

                                    guild = self.bot.get_guild(int(target_guild_id))

                                    # Target server found -> Run instant auto-build
                                    if guild:
                                        await self.execute_autobuild(guild, payload)
                                        confirm_embed = discord.Embed(
                                            title="Auto-Deploy Completed",
                                            description=f"Successfully built server architecture for **{guild.name}** (`{guild.id}`).",
                                            color=0x2ecc71
                                        )
                                        await message.channel.send(embed=confirm_embed)
                                    else:
                                        # Bot is not present in target guild -> DM user & alert log channel
                                        missing_embed = discord.Embed(
                                            title="Auto-Deploy Pending (Bot Missing)",
                                            description=f"The bot is not present in Target Guild ID `{target_guild_id}`. A direct message has been sent to the user with an invite link.",
                                            color=0xE74C3C
                                        )
                                        await message.channel.send(embed=missing_embed)

                                        # Direct message user with Add Bot & Retry buttons
                                        if user_id.isdigit():
                                            user = await self.bot.fetch_user(int(user_id))
                                            if user:
                                                dm_embed = discord.Embed(
                                                    title="Action Required: Add Bot to Auto-Deploy Layout",
                                                    description=f"You requested an instant auto-deploy for server ID `{target_guild_id}`, but the bot is not present in that server.\n\nPlease click **Add Bot to Server** below and then click **Retry Auto-Deploy**.",
                                                    color=0xF1C40F
                                                )
                                                view = RetryAutoBuildView(
                                                    client_id=str(self.bot.user.id),
                                                    target_guild_id=target_guild_id,
                                                    payload=payload
                                                )
                                                try:
                                                    await user.send(embed=dm_embed, view=view)
                                                except Exception as err:
                                                    print(f"[DM User Error] {err}")
                    except Exception as ex:
                        print(f"[Auto-Build Execution Exception] {ex}")

async def setup(bot):
    await bot.add_cog(BotCog(bot))
