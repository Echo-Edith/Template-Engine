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

PREMIUM_SCAN_CHANNEL_ID = "1563544108696477802"

MONGO_URI = os.getenv("MONGO_URI")

db_premium = None
db_authorized_builders = None
db_users = None
db_stats = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        try:
            db = mongo_client.get_default_database()
        except Exception:
            db = mongo_client.get_database("static_studio_db")
        db_premium = db["premium_users"]
        db_authorized_builders = db["build_authorized_users"]
        db_users = db["persistent_users"]
        db_stats = db["stats"]
    except Exception as e:
        print(f"[MongoDB Warning] Could not connect to Mongo: {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_AUTHORIZED_BUILDERS = set()
LOCAL_PERSISTENT_USERS = {}

def is_user_premium(user_id: str) -> bool:
    if not user_id:
        return False
    user_id_str = str(user_id)
    now = datetime.now(timezone.utc)

    if db_premium is not None:
        try:
            doc = db_premium.find_one({"user_id": user_id_str})
            if doc and "expires_at" in doc:
                expires_at = doc["expires_at"]
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                return expires_at > now
        except Exception:
            return False
        return False
    else:
        exp = LOCAL_PREMIUM_USERS.get(user_id_str)
        return exp > now if exp else False

class BotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Scan premium design log channel (1563544108696477802)
        if str(message.channel.id) == PREMIUM_SCAN_CHANNEL_ID:
            json_attachment = None
            for attachment in message.attachments:
                if attachment.filename.endswith(".json"):
                    json_attachment = attachment
                    break

            if json_attachment:
                try:
                    content_bytes = await json_attachment.read()
                    payload = json.loads(content_bytes.decode('utf-8'))

                    target_guild_id = payload.get("target_server_id")
                    user_id = payload.get("submitting_user_id")
                    layout = payload.get("layout", {})

                    if not target_guild_id or target_guild_id == "N/A" or target_guild_id == "Unknown":
                        return

                    guild = self.bot.get_guild(int(target_guild_id))

                    # IF BOT IS NOT IN TARGET GUILD
                    if not guild:
                        print(f"[Auto-Build Warning] Bot is not in target guild: {target_guild_id}")
                        if user_id:
                            try:
                                user_obj = await self.bot.fetch_user(int(user_id))
                                if user_obj:
                                    invite_url = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands&guild_id={target_guild_id}"
                                    
                                    dm_embed = discord.Embed(
                                        title="Bot Required to Auto-Build Server",
                                        description=f"The Template Engine bot is not currently in server `{target_guild_id}`.\n\nPlease add the bot to your server using the button below and click **Retry Auto-Deploy**.",
                                        color=discord.Color.gold()
                                    )
                                    
                                    view = discord.ui.View()
                                    view.add_item(discord.ui.Button(label="Add Bot to Server", url=invite_url, style=discord.ButtonStyle.link))
                                    await user_obj.send(embed=dm_embed, view=view)
                            except Exception as dm_err:
                                print(f"[DM Error] Could not DM user: {dm_err}")
                        return

                    # BOT IS IN GUILD -> EXECUTE AUTOMATIC BUILDING
                    print(f"[Auto-Build] Starting layout deployment for guild {guild.name} ({guild.id})")
                    
                    # 1. Create Roles
                    role_map = {}
                    for r_data in layout.get("roles", []):
                        r_name = r_data.get("name")
                        r_color = r_data.get("color", "#8b5cf6")
                        try:
                            hex_val = int(r_color.lstrip('#'), 16)
                            color_obj = discord.Color(hex_val)
                        except Exception:
                            color_obj = discord.Color.purple()
                        
                        existing_role = discord.utils.get(guild.roles, name=r_name)
                        if not existing_role:
                            new_role = await guild.create_role(name=r_name, color=color_obj)
                            role_map[r_name] = new_role
                        else:
                            role_map[r_name] = existing_role

                    # 2. Create Categories & Channels
                    for cat_data in layout.get("categories", []):
                        cat_name = cat_data.get("name", "CATEGORY")
                        category = await guild.create_category(name=cat_name)

                        for chan_data in cat_data.get("channels", []):
                            ch_name = chan_data.get("name", "channel")
                            ch_type = chan_data.get("type", "text")
                            ch_topic = chan_data.get("topic", "")

                            if ch_type == "voice":
                                await guild.create_voice_channel(name=ch_name, category=category)
                            else:
                                await guild.create_text_channel(name=ch_name, category=category, topic=ch_topic)

                    print(f"[Auto-Build Complete] Successfully built layout for {guild.name}")

                except Exception as build_err:
                    print(f"[Auto-Build Failure] Error building layout: {build_err}")

async def setup(bot: commands.Bot):
    await bot.add_cog(BotCog(bot))
