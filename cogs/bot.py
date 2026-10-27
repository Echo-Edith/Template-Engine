import os
import re
import json
import random
import asyncio
import aiohttp
import discord
from datetime import datetime, timezone, timedelta
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient

# Base Configuration & Constants
BOT_OWNER_ID = 1219266886143967245
MAIN_OWNER_GUILD_ID = 1284513257574629458

SUBSCRIPTION_LOG_CHANNEL_ID = 1564262413514182739
MONTHLY_ANALYTICS_CHANNEL_ID = 1564263447057670154
PREMIUM_SCAN_CHANNEL_ID = 1563544108696477802

GIVEAWAY_BANNER_URL = "https://res.cloudinary.com/dtz0urit6/image/upload/f_png,q_auto/cloudinary-tools-uploads/qxpfnwtebf6jqybaayjo.png"

# Webhooks for Non-Premium logging
SYSTEM_LOG_WEBHOOK_URL = (os.getenv("SYSTEM_LOG_WEBHOOK_URL") or "").strip()
WEBSITE_WEBHOOK_URL = (os.getenv("WEBSITE_WEBHOOK_URL") or SYSTEM_LOG_WEBHOOK_URL).strip()
DESIGN_WEBHOOK_URL = (os.getenv("DESIGN_WEBHOOK_URL") or SYSTEM_LOG_WEBHOOK_URL).strip()

MONGO_URI = os.getenv("MONGO_URI")
ROBLOX_OPEN_CLOUD_KEY = (os.getenv("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_COOKIE = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip()
ROBLOX_SUBSCRIPTION_ID = (os.getenv("SUBSCRIPTION_ID") or "EXP-4378714879071224384").strip()
ROBLOX_PLACE_ID = (os.getenv("ROBLOX_PLACE_ID") or "6094950399").strip()
ROBLOX_UNIVERSE_ID = (os.getenv("ROBLOX_UNIVERSE_ID") or "").strip()
ROBLOX_GROUP_ID = (os.getenv("ROBLOX_GROUP_ID") or "").strip()

db_premium = None
db_build_access = None
db_users = None
db_stats = None
db_giveaways = None
db_settings = None
db_premium_config = None

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
        db_giveaways = db["giveaways"]
        db_settings = db["guild_settings"]
        db_premium_config = db["premium_config"]
    except Exception as e:
        print(f"[Bot Mongo Warning] Could not connect to MongoDB: {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_AUTHORIZED_BUILDERS = set()
LOCAL_PERSISTENT_USERS = {}
PENDING_VERIFICATION_CODES = {}
LOCAL_GIVEAWAYS = {}
LOCAL_GUILD_SETTINGS = {}
LOCAL_PREMIUM_CONFIG = {}

PREMIUM_ROLE_NAME = "Template Engine Premium"
PREMIUM_ROLE_COLOR = discord.Color(0xF1C40F)


def parse_duration_to_timedelta(duration_str: str) -> timedelta:
    if not duration_str or duration_str.lower() in ["inf", "infinite"]:
        return timedelta(days=36500)
    
    match = re.match(r"^(\d+)\s*(s|h|d|mo|y)$", duration_str.strip().lower())
    if not match:
        return timedelta(days=30)
    
    val, unit = match.groups()
    num = int(val)
    if unit == "s":
        return timedelta(seconds=num)
    elif unit == "h":
        return timedelta(hours=num)
    elif unit == "d":
        return timedelta(days=num)
    elif unit == "mo":
        return timedelta(days=num * 30)
    elif unit == "y":
        return timedelta(days=num * 365)
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


async def check_roblox_subscription_status(roblox_id: str) -> bool:
    if not ROBLOX_OPEN_CLOUD_KEY or not ROBLOX_SUBSCRIPTION_ID:
        return False
    url = f"https://apis.roblox.com/cloud/v2/users/{roblox_id}/subscriptions/{ROBLOX_SUBSCRIPTION_ID}"
    headers = {"x-api-key": ROBLOX_OPEN_CLOUD_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    state = data.get("state", "").upper()
                    return state in ["ACTIVE", "RENEWING"]
    except Exception as e:
        print(f"[Roblox Open Cloud Sub Check Error] {e}")
    return False


async def fetch_roblox_revenue_data(timeframe: str = "1d") -> dict:
    """
    Fetch Roblox revenue summary for a given timeframe (1d, 7d, 30d, 1y).
    Tries economy.roblox.com endpoints via Cookie or Open Cloud API.
    """
    timeframe_map = {
        "1d": "Day",
        "7d": "Week",
        "30d": "Month",
        "1y": "Year",
        "365d": "Year"
    }
    tf_param = timeframe_map.get(timeframe.lower(), "Day")

    headers = {}
    if ROBLOX_COOKIE:
        clean_cookie = ROBLOX_COOKIE if ROBLOX_COOKIE.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={ROBLOX_COOKIE}"
        headers["Cookie"] = clean_cookie
    if ROBLOX_OPEN_CLOUD_KEY:
        headers["x-api-key"] = ROBLOX_OPEN_CLOUD_KEY

    urls = []
    if ROBLOX_GROUP_ID:
        urls.append(f"https://economy.roblox.com/v1/groups/{ROBLOX_GROUP_ID}/revenue/summary/{tf_param}")
    if ROBLOX_UNIVERSE_ID:
        urls.append(f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/economy/revenue/summary/{tf_param}")

    if not urls:
        return {"error": "Missing ROBLOX_GROUP_ID or ROBLOX_UNIVERSE_ID in environment variables."}

    try:
        async with aiohttp.ClientSession() as session:
            for url in urls:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return {
                            "sale_of_goods": data.get("itemSaleRevenue", data.get("itemSale", 0)),
                            "advance_rebates": data.get("publishingAdvanceRebates", 0),
                            "commissions": data.get("commissionRevenue", data.get("comission", 0)),
                            "payouts": data.get("groupPayouts", data.get("groupPayout", 0)),
                            "creator_rewards": data.get("creatorRewards", 0),
                            "total": data.get("totalRevenue", data.get("total", 0)),
                            "pending_robux": data.get("pendingRobux", 0)
                        }
            
            return {
                "sale_of_goods": 0,
                "advance_rebates": 0,
                "commissions": 0,
                "payouts": 0,
                "creator_rewards": 0,
                "total": 0,
                "pending_robux": 0,
                "note": "Roblox returned 404/403. Ensure `ROBLOX_GROUP_ID` and `ROBLOX_COOKIE` are set in your environment variables."
            }
    except Exception as e:
        return {"error": str(e)}


def set_guild_welcome_setting(guild_id: str, channel_id: str = None, message_template: str = None):
    g_id = str(guild_id)
    update_data = {}
    if channel_id is not None: update_data["welcome_channel_id"] = str(channel_id)
    if message_template is not None: update_data["welcome_message"] = message_template

    if db_settings is not None:
        db_settings.update_one({"guild_id": g_id}, {"$set": update_data}, upsert=True)
    else:
        current = LOCAL_GUILD_SETTINGS.get(g_id, {})
        current.update(update_data)
        LOCAL_GUILD_SETTINGS[g_id] = current


def get_guild_settings(guild_id: str) -> dict:
    g_id = str(guild_id)
    if db_settings is not None:
        return db_settings.find_one({"guild_id": g_id}) or {}
    return LOCAL_GUILD_SETTINGS.get(g_id, {})


def set_premium_embed_config(title: str = None, description: str = None, perks: list = None):
    update_data = {}
    if title: update_data["title"] = title
    if description: update_data["description"] = description
    if perks is not None: update_data["perks"] = perks

    if db_premium_config is not None:
        db_premium_config.update_one({"config_key": "global"}, {"$set": update_data}, upsert=True)
    else:
        LOCAL_PREMIUM_CONFIG.update(update_data)


def get_premium_embed_config() -> dict:
    if db_premium_config is not None:
        return db_premium_config.find_one({"config_key": "global"}) or {}
    return LOCAL_PREMIUM_CONFIG


def save_giveaway_data(message_id: str, data: dict):
    msg_id_str = str(message_id)
    if db_giveaways is not None:
        db_giveaways.update_one({"message_id": msg_id_str}, {"$set": data}, upsert=True)
    else:
        LOCAL_GIVEAWAYS[msg_id_str] = data


def get_giveaway_data(message_id: str) -> dict:
    msg_id_str = str(message_id)
    if db_giveaways is not None:
        return db_giveaways.find_one({"message_id": msg_id_str}) or {}
    return LOCAL_GIVEAWAYS.get(msg_id_str, {})


async def publish_roblox_place_payload(interaction: discord.Interaction, lua_code: str):
    if not ROBLOX_OPEN_CLOUD_KEY or not ROBLOX_UNIVERSE_ID or not ROBLOX_PLACE_ID:
        err = discord.Embed(
            title="[CONFIGURATION ERROR]",
            description=(
                "Missing Open Cloud Credentials in Environment Variables!\n\n"
                "Ensure `ROBLOX_OPEN_CLOUD_KEY`, `ROBLOX_UNIVERSE_ID`, and `ROBLOX_PLACE_ID` are configured."
            ),
            color=0xf43f5e
        )
        err.set_footer(text="Template Engine Infrastructure")
        await interaction.followup.send(embed=err, ephemeral=True)
        return

    rbxlx_template = f"""<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">
    <External>null</External>
    <External>nil</External>
    <Item class="Workspace" referent="RBX0">
        <Properties>
            <string name="Name">Workspace</string>
            <float name="FallenPartsDestroyHeight">-100000</float>
        </Properties>
        <Item class="Part" referent="RBX_BASEPLATE">
            <Properties>
                <string name="Name">Baseplate</string>
                <bool name="Anchored">true</bool>
                <bool name="CanCollide">true</bool>
                <CoordinateFrame name="CFrame">
                    <X>0</X>
                    <Y>0</Y>
                    <Z>0</Z>
                    <R00>1</R00><R01>0</R01><R02>0</R02>
                    <R10>0</R10><R11>1</R11><R12>0</R12>
                    <R20>0</R20><R21>0</R21><R22>1</R22>
                </CoordinateFrame>
                <Vector3 name="size">
                    <X>500</X>
                    <Y>20</Y>
                    <Z>500</Z>
                </Vector3>
                <Color3uint8 name="Color3uint8">10724259</Color3uint8>
                <float name="Transparency">0</float>
            </Properties>
        </Item>
        <Item class="SpawnLocation" referent="RBX_SPAWN">
            <Properties>
                <string name="Name">SpawnLocation</string>
                <bool name="Anchored">true</bool>
                <bool name="CanCollide">true</bool>
                <CoordinateFrame name="CFrame">
                    <X>0</X>
                    <Y>11</Y>
                    <Z>0</Z>
                    <R00>1</R00><R01>0</R01><R02>0</R02>
                    <R10>0</R10><R11>1</R11><R12>0</R12>
                    <R20>0</R20><R21>0</R21><R22>1</R22>
                </CoordinateFrame>
                <Vector3 name="size">
                    <X>12</X>
                    <Y>2</Y>
                    <Z>12</Z>
                </Vector3>
                <float name="Transparency">0</float>
            </Properties>
        </Item>
    </Item>
    <Item class="StarterGui" referent="RBX_STARTER_GUI">
        <Properties>
            <string name="Name">StarterGui</string>
            <bool name="ResetPlayerGuiOnSpawn">false</bool>
        </Properties>
        <Item class="LocalScript" referent="RBX_LOCKSCREEN_SCRIPT">
            <Properties>
                <string name="Name">StoreLockscreenScript</string>
                <ProtectedString name="Source"><![CDATA[{lua_code}]]></ProtectedString>
            </Properties>
        </Item>
    </Item>
</roblox>"""

    url = f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/places/{ROBLOX_PLACE_ID}/versions?versionType=Published"
    headers = {
        "x-api-key": ROBLOX_OPEN_CLOUD_KEY,
        "Content-Type": "application/xml"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=rbxlx_template.encode('utf-8')) as resp:
                resp_text = await resp.text()
                try:
                    resp_data = json.loads(resp_text)
                except Exception:
                    resp_data = resp_text

                if resp.status == 200:
                    version_num = resp_data.get("versionNumber", "N/A") if isinstance(resp_data, dict) else "N/A"
                    embed = discord.Embed(
                        title="[PLACE PUBLISHED TO ROBLOX]",
                        description=(
                            f"Successfully published `.rbxlx` place payload with StarterGui UI execution to Roblox!\n\n"
                            f"• **Place ID:** `{ROBLOX_PLACE_ID}`\n"
                            f"• **Universe ID:** `{ROBLOX_UNIVERSE_ID}`\n"
                            f"• **New Published Version:** `{version_num}`\n"
                            f"• **Payload Size:** `{len(lua_code)} characters`"
                        ),
                        color=0x2ecc71
                    )
                else:
                    embed = discord.Embed(
                        title="[OPEN CLOUD PUBLISH FAILED]",
                        description=f"Roblox Open Cloud returned status code `{resp.status}`:\n```json\n{resp_text}\n```",
                        color=0xf43f5e
                    )

        embed.set_footer(text="Template Engine Infrastructure")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        err_embed = discord.Embed(
            title="[PUBLISH EXCEPTION]",
            description=f"Failed to communicate with Roblox Open Cloud API: {e}",
            color=0xf43f5e
        )
        err_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.followup.send(embed=err_embed, ephemeral=True)


class PremiumConfigModal(discord.ui.Modal, title="Configure /premium Embed"):
    embed_title = discord.ui.TextInput(
        label="Embed Title",
        placeholder="👑 Template Engine Premium",
        required=False,
        max_length=256
    )
    embed_description = discord.ui.TextInput(
        label="Embed Description",
        style=discord.TextStyle.paragraph,
        placeholder="Enter custom description body text...",
        required=False,
        max_length=2000
    )
    embed_perks = discord.ui.TextInput(
        label="Perks List (One perk per line)",
        style=discord.TextStyle.paragraph,
        placeholder="Instant server layout auto-deployments\nGold Glassmorphism web dashboard access",
        required=False,
        max_length=2000
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        title_val = self.embed_title.value.strip() or None
        desc_val = self.embed_description.value.strip() or None
        perks_val = [line.strip() for line in self.embed_perks.value.split("\n") if line.strip()] if self.embed_perks.value.strip() else None

        set_premium_embed_config(title=title_val, description=desc_val, perks=perks_val)

        embed = discord.Embed(
            title="[PREMIUM CONFIG UPDATED]",
            description="Successfully updated the `/premium` embed configuration.",
            color=0x2ecc71
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await interaction.followup.send(embed=embed, ephemeral=True)


class PremiumConfigTriggerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Open Premium Embed Modal", style=discord.ButtonStyle.primary, emoji="✏️")
    async def open_modal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PremiumConfigModal())


class GiveawayEntriesPaginationView(discord.ui.View):
    def __init__(self, entries: list, current_page: int = 0):
        super().__init__(timeout=120)
        self.entries = entries
        self.current_page = current_page
        self.per_page = 10
        self.max_pages = max(1, (len(self.entries) + self.per_page - 1) // self.per_page)
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = (self.current_page == 0)
        self.next_btn.disabled = (self.current_page >= self.max_pages - 1)

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎉 Giveaway Entries ({len(self.entries)} Total)",
            color=0xF1C40F
        )
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_entries = self.entries[start:end]

        if page_entries:
            lines = [f"{start + idx + 1}. <@{uid}>" for idx, uid in enumerate(page_entries)]
            embed.description = "\n".join(lines)
        else:
            embed.description = "No entries yet."

        embed.set_footer(text=f"Template Engine Giveaways • Page {self.current_page + 1} of {self.max_pages}")
        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, custom_id="gw_page_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="gw_page_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.max_pages - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)


class GiveawayPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, custom_id="btn_gw_join", emoji="🎉")
    async def join_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        data = get_giveaway_data(msg_id)
        if not data or data.get("ended"):
            err_embed = discord.Embed(title="[GIVEAWAY ENDED]", description="This giveaway has ended or no longer exists.", color=0xf43f5e)
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        user = interaction.user
        user_id = str(user.id)
        entries_dict = data.get("entries", {})

        unallowed_role_ids = data.get("unallowed_role_ids", [])
        if any(role.id in unallowed_role_ids for role in user.roles):
            err_embed = discord.Embed(title="[ENTRY RESTRICTED]", description="❌ You have an excluded role for this giveaway.", color=0xf43f5e)
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        allowed_role_ids = data.get("allowed_role_ids", [])
        if allowed_role_ids and not any(role.id in allowed_role_ids for role in user.roles):
            err_embed = discord.Embed(title="[ROLE REQUIRED]", description="❌ You do not have the required role to enter.", color=0xf43f5e)
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            return

        if user_id in entries_dict:
            entries_dict.pop(user_id)
            data["entries"] = entries_dict
            save_giveaway_data(msg_id, data)
            leave_embed = discord.Embed(title="[GIVEAWAY LEFT]", description="❌ You have left the giveaway.", color=0xf43f5e)
            await interaction.response.send_message(embed=leave_embed, ephemeral=True)
            return

        max_entry_count = data.get("max_entry", 1)
        bonus_role_ids = data.get("bonus_role_ids", [])
        has_bonus = any(role.id in bonus_role_ids for role in user.roles) or is_user_premium(user_id)
        entry_weight = max_entry_count if has_bonus else 1

        entries_dict[user_id] = entry_weight
        data["entries"] = entries_dict
        save_giveaway_data(msg_id, data)

        join_embed = discord.Embed(
            title="[GIVEAWAY JOINED]",
            description=f"✅ Successfully entered the giveaway! (Total Entries Granted: **{entry_weight}**)",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=join_embed, ephemeral=True)

    @discord.ui.button(label="Entries", style=discord.ButtonStyle.secondary, custom_id="btn_gw_entries", emoji="📜")
    async def view_entries_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        data = get_giveaway_data(msg_id)
        entries_dict = data.get("entries", {})
        unique_users = list(entries_dict.keys())

        pag_view = GiveawayEntriesPaginationView(unique_users)
        await interaction.response.send_message(embed=pag_view.get_embed(), view=pag_view, ephemeral=True)


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
        now = datetime.now(timezone.utc)

        if not code_data:
            err_embed = discord.Embed(
                title="[CODE NOT FOUND]",
                description="Please click **Generate Code** first before verifying.",
                color=0xf43f5e
            )
            err_embed.set_footer(text="Template Engine Infrastructure")
            await interaction.followup.send(embed=err_embed, ephemeral=True)
            return

        created_at = code_data.get("created_at", now)
        if (now - created_at).total_seconds() > 600:
            PENDING_VERIFICATION_CODES.pop(user_id_str, None)
            exp_embed = discord.Embed(
                title="[CODE EXPIRED]",
                description="Your verification code has expired (10-minute limit). Please click **Generate Code** again.",
                color=0xf43f5e
            )
            exp_embed.set_footer(text="Template Engine Infrastructure")
            await interaction.followup.send(embed=exp_embed, ephemeral=True)
            return

        code = code_data["code"]
        target_username = self.roblox_username.value.strip()

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with aiohttp.ClientSession(headers=headers) as session:
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

                async with session.get(f"https://users.roblox.com/v1/users/{roblox_id}") as profile_res:
                    profile_data = await profile_res.json()
                    bio_text = profile_data.get("description", "")

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

                PENDING_VERIFICATION_CODES.pop(user_id_str, None)

                save_user_roblox_link(
                    discord_id=user_id_str,
                    discord_username=interaction.user.name,
                    roblox_id=str(roblox_id),
                    roblox_username=exact_username
                )

                has_active_sub = await check_roblox_subscription_status(str(roblox_id))
                
                if has_active_sub:
                    set_user_premium(user_id_str, timedelta(days=30))
                else:
                    remove_user_premium(user_id_str)

                new_nickname = f"{interaction.user.name} ({exact_username})"
                if len(new_nickname) > 32:
                    new_nickname = new_nickname[:29] + "..."

                try:
                    await interaction.user.edit(nick=new_nickname)
                except Exception:
                    pass

                status_msg = (
                    f"Successfully verified and linked Roblox profile **@{exact_username}** (`{roblox_id}`) "
                    f"with your Discord account!\n\n"
                    f"• **TE Premium:** {'Activated (30 Days)' if has_active_sub else 'Inactive (Subscribe via /premium for perks)'}\n"
                    f"• **Website Status:** Accounts synced."
                )

                success_embed = discord.Embed(
                    title="[ROBLOX ACCOUNT LINKED]",
                    description=status_msg,
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


class RobloxScriptUploadModal(discord.ui.Modal, title="Deploy Place Script via Open Cloud"):
    script_content = discord.ui.TextInput(
        label="Lua Script Code (Client or Server)",
        style=discord.TextStyle.paragraph,
        placeholder="Paste your script here...",
        required=True,
        min_length=5,
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await publish_roblox_place_payload(interaction, self.script_content.value)


class VerificationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Generate Code", style=discord.ButtonStyle.primary, custom_id="btn_gen_code", emoji="🔑")
    async def generate_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        code = f"TE-{random.randint(100000, 999999)}"
        PENDING_VERIFICATION_CODES[user_id_str] = {"code": code, "created_at": datetime.now(timezone.utc)}

        embed = discord.Embed(
            title="[YOUR VERIFICATION CODE]",
            description=(
                f"Copy and paste this code into your Roblox profile **About** (Bio) section:\n\n"
                f"`{code}`\n\n"
                f"⏳ **Expires in 10 minutes.**\n"
                f"Once saved on Roblox, click **Verify Bio** to complete linking."
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
                "1️⃣ Click **Generate Code** to receive your single-tap code (`TE-XXXXXX`).\n"
                "2️⃣ Go to your **Roblox Profile** → Edit Profile → **About**.\n"
                "3️⃣ Paste the code into your bio and click **Save**.\n"
                "4️⃣ Return here within 10 minutes, click **Verify Bio**, and enter your Roblox username."
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
        remove_user_premium(user_id_str)

        try:
            await interaction.user.edit(nick=None)
        except Exception:
            pass

        embed = discord.Embed(
            title="[ACCOUNT UNSYNCED]",
            description="Your Roblox account link has been removed and server nickname reset.",
            color=0x2ecc71
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_weekly_sent = None
        self.last_monthly_sent = None
        self.check_premium_expirations.start()
        self.check_active_giveaways.start()
        self.check_analytics_reports.start()

    def cog_unload(self):
        self.check_premium_expirations.cancel()
        self.check_active_giveaways.cancel()
        self.check_analytics_reports.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            synced = await self.bot.tree.sync()
            print(f"[BotCog OnReady] Automatically synchronized {len(synced)} slash commands globally.")
        except Exception as e:
            print(f"[BotCog Sync Error] Failed auto-sync: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = str(member.guild.id)
        settings = get_guild_settings(guild_id)

        target_ch_id = settings.get("welcome_channel_id")
        welcome_tpl = settings.get("welcome_message") or "Welcome, {user}.\nPlease read channel."

        channel = None
        if target_ch_id and str(target_ch_id).isdigit():
            channel = member.guild.get_channel(int(target_ch_id))
        
        if not channel:
            channel = member.guild.system_channel

        if channel:
            try:
                formatted_msg = welcome_tpl.replace("{user}", member.mention).replace("{server}", member.guild.name)
                embed = discord.Embed(
                    title="👋 Welcome!",
                    description=formatted_msg,
                    color=0x3b82f6
                )
                embed.set_footer(text=f"Welcome to {member.guild.name}")
                await channel.send(content=member.mention, embed=embed)
            except Exception as e:
                print(f"[Welcome Listener Error] {e}")

    @tasks.loop(seconds=10.0)
    async def check_active_giveaways(self):
        now = datetime.now(timezone.utc)
        active_gw = []

        if db_giveaways is not None:
            active_gw = list(db_giveaways.find({"ended": False}))
        else:
            active_gw = [v for v in LOCAL_GIVEAWAYS.values() if not v.get("ended")]

        for gw in active_gw:
            end_time_raw = gw.get("end_time")
            if not end_time_raw:
                continue
            end_time = datetime.fromisoformat(end_time_raw) if isinstance(end_time_raw, str) else end_time_raw
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)

            if now >= end_time:
                await self.finalize_giveaway(gw)

    @check_active_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()

    async def finalize_giveaway(self, gw_data: dict):
        msg_id = gw_data.get("message_id")
        channel_id = gw_data.get("channel_id")
        gw_data["ended"] = True
        save_giveaway_data(msg_id, gw_data)

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            return

        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception:
            msg = None

        entries_dict = gw_data.get("entries", {})
        winner_count = gw_data.get("winners", 1)
        prize = gw_data.get("prize") or gw_data.get("title", "Prize")

        weighted_pool = []
        for uid, weight in entries_dict.items():
            weighted_pool.extend([uid] * weight)

        winners = []
        if weighted_pool:
            unique_entrants = list(entries_dict.keys())
            actual_winners_count = min(winner_count, len(unique_entrants))
            winners = random.sample(unique_entrants, actual_winners_count)

        winner_pings = " ".join([f"<@{w}>" for w in winners]) if winners else "None"

        if msg:
            end_embed = discord.Embed(
                title=f"🎉 {gw_data.get('title')}",
                description=(
                    f"**Prize:** {prize}\n\n"
                    f"**Winners:** {winner_pings}\n"
                    f"**Hosted By:** <@{gw_data.get('host_id')}>"
                ),
                color=0xF1C40F
            )
            end_embed.set_image(url=GIVEAWAY_BANNER_URL)
            end_embed.set_footer(text=f"{winner_count} winners | Ended")
            await msg.edit(embed=end_embed, view=None)

        announcement_embed = discord.Embed(
            title="🎉 Giveaway Ended!",
            description=f"Congratulations {winner_pings}! You won **{prize}**!",
            color=0xF1C40F
        )
        announcement_embed.set_footer(text="Template Engine Giveaways")

        await channel.send(content=f"🎉 {winner_pings}" if winners else None, embed=announcement_embed)

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

        for uid, data in list(PENDING_VERIFICATION_CODES.items()):
            c_time = data.get("created_at")
            if c_time and (now - c_time).total_seconds() > 600:
                PENDING_VERIFICATION_CODES.pop(uid, None)

    @check_premium_expirations.before_loop
    async def before_check_expirations(self):
        await self.bot.wait_until_ready()

    # --- AUTOMATED WEEKLY & MONTHLY ANALYTICS REPORTS ---
    @tasks.loop(hours=1.0)
    async def check_analytics_reports(self):
        now = datetime.now(timezone.utc)
        ch_id = int(MONTHLY_ANALYTICS_CHANNEL_ID)
        channel = self.bot.get_channel(ch_id)
        if not channel:
            return

        # Weekly Report (Sent every Sunday at 00:00 UTC)
        if now.weekday() == 6 and now.hour == 0 and self.last_weekly_sent != now.strftime("%Y-%U"):
            self.last_weekly_sent = now.strftime("%Y-%U")
            rev_7d = await fetch_roblox_revenue_data("7d")
            
            active_subs = 0
            if db_premium is not None:
                active_subs = db_premium.count_documents({"expires_at": {"$gt": now.isoformat()}})
            else:
                active_subs = len([u for u, exp in LOCAL_PREMIUM_USERS.items() if exp > now])

            linked_accs = db_users.count_documents({"roblox_data": {"$exists": True}}) if db_users is not None else len(LOCAL_PERSISTENT_USERS)

            embed = discord.Embed(
                title="📊 Weekly Analytics Summary",
                description="Here is your automated 7-day server and revenue report.",
                color=0x3b82f6,
                timestamp=now
            )
            embed.add_field(name="👑 Active Premium Members", value=f"`{active_subs}`", inline=True)
            embed.add_field(name="🔗 Synced Roblox Accounts", value=f"`{linked_accs}`", inline=True)
            embed.add_field(name="💰 7-Day Revenue Total", value=f"R$ `{rev_7d.get('total', 0):,}`", inline=False)
            embed.add_field(name="⏳ Pending Robux", value=f"R$ `{rev_7d.get('pending_robux', 0):,}`", inline=True)
            embed.set_footer(text="Template Engine Automated Weekly Analytics")
            await channel.send(embed=embed)

        # Monthly Report (Sent on 1st of every month at 00:00 UTC)
        if now.day == 1 and now.hour == 0 and self.last_monthly_sent != now.strftime("%Y-%m"):
            self.last_monthly_sent = now.strftime("%Y-%m")
            rev_30d = await fetch_roblox_revenue_data("30d")

            active_subs = 0
            if db_premium is not None:
                active_subs = db_premium.count_documents({"expires_at": {"$gt": now.isoformat()}})
            else:
                active_subs = len([u for u, exp in LOCAL_PREMIUM_USERS.items() if exp > now])

            linked_accs = db_users.count_documents({"roblox_data": {"$exists": True}}) if db_users is not None else len(LOCAL_PERSISTENT_USERS)

            embed = discord.Embed(
                title="📈 Monthly Financial & Platform Executive Summary",
                description="Automated monthly report generated for Template Engine.",
                color=0xF1C40F,
                timestamp=now
            )
            embed.add_field(name="👑 Total Premium Subscribers", value=f"`{active_subs}`", inline=True)
            embed.add_field(name="🔗 Synced Roblox Accounts", value=f"`{linked_accs}`", inline=True)
            embed.add_field(name="🛒 Sale of Goods Revenue", value=f"R$ `{rev_30d.get('sale_of_goods', 0):,}`", inline=False)
            embed.add_field(name="💵 Commissions Revenue", value=f"R$ `{rev_30d.get('commissions', 0):,}`", inline=True)
            embed.add_field(name="💰 Total Monthly Revenue", value=f"R$ `{rev_30d.get('total', 0):,}`", inline=False)
            embed.add_field(name="⏳ Pending Robux Balance", value=f"R$ `{rev_30d.get('pending_robux', 0):,}`", inline=True)
            embed.set_footer(text="Template Engine Executive Analytics")
            await channel.send(embed=embed)

    @check_analytics_reports.before_loop
    async def before_analytics_reports(self):
        await self.bot.wait_until_ready()

    # --- PREFIX COMMANDS (OWNER RESTRICTED) ---
    @commands.command(name="rev")
    async def revenue_prefix_command(self, ctx: commands.Context, timeframe: str = "1d"):
        """Check Roblox Revenue Summary for a specified timeframe (Owner restricted). Usage: !rev 1d / !rev 7d / !rev 30d / !rev 1y"""
        if ctx.author.id != BOT_OWNER_ID:
            return

        tf_clean = timeframe.lower().strip()
        if tf_clean not in ["1d", "7d", "30d", "1y", "365d"]:
            err_embed = discord.Embed(
                title="[INVALID TIMEFRAME]",
                description="Valid options: `1d` (Past Day), `7d` (Past Week), `30d` (Past Month), `1y` (Past Year)",
                color=0xf43f5e
            )
            await ctx.send(embed=err_embed)
            return

        loading_msg = await ctx.send("🔎 *Fetching revenue summary from Roblox...*")
        rev_data = await fetch_roblox_revenue_data(tf_clean)

        if "error" in rev_data and rev_data.get("total", 0) == 0 and "note" not in rev_data:
            err_embed = discord.Embed(
                title="[REVENUE FETCH ERROR]",
                description=f"Failed to fetch revenue data: {rev_data.get('error', 'Unknown Error')}",
                color=0xf43f5e
            )
            await loading_msg.edit(content=None, embed=err_embed)
            return

        tf_labels = {"1d": "Past Day", "7d": "Past Week", "30d": "Past Month", "1y": "Past Year", "365d": "Past Year"}
        time_label = tf_labels.get(tf_clean, tf_clean)

        embed = discord.Embed(
            title=f"Configure Template Engine Revenue Summary ({time_label})",
            color=0x2ecc71,
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="Sale of Goods", value=f"R$ `{rev_data.get('sale_of_goods', 0):,}`", inline=False)
        embed.add_field(name="Publishing Advance Rebates", value=f"R$ `{rev_data.get('advance_rebates', 0):,}`", inline=False)
        embed.add_field(name="Commissions", value=f"R$ `{rev_data.get('commissions', 0):,}`", inline=False)
        embed.add_field(name="Community Revenue Payouts", value=f"R$ `{rev_data.get('payouts', 0):,}`", inline=False)
        embed.add_field(name="Community Creator Rewards", value=f"R$ `{rev_data.get('creator_rewards', 0):,}`", inline=False)
        embed.add_field(name="Total", value=f"R$ **`{rev_data.get('total', 0):,}`**", inline=False)
        embed.add_field(name="Pending Robux", value=f"R$ `{rev_data.get('pending_robux', 0):,}`", inline=False)

        if "note" in rev_data:
            embed.set_footer(text=rev_data["note"])
        else:
            embed.set_footer(text="Template Engine Revenue Analytics")

        await loading_msg.edit(content=None, embed=embed)

    @commands.command(name="pset")
    async def pset_prefix_command(self, ctx: commands.Context, user: discord.User = None, duration: str = "30d"):
        """Grant or revoke TE Premium for a user (Owner restricted). Usage: !pset @User 1s/1h/1d/1mo/1y or !pset @User remove"""
        if ctx.author.id != BOT_OWNER_ID:
            return

        if not user:
            embed = discord.Embed(title="[INVALID SYNTAX]", description="Usage: `!pset @User <1s|1h|1d|1mo|1y|remove>`", color=0xf43f5e)
            await ctx.send(embed=embed)
            return

        user_id_str = str(user.id)
        if duration.lower() == "remove":
            remove_user_premium(user_id_str)
            embed = discord.Embed(title="[TE PREMIUM REVOKED]", description=f"Revoked TE Premium status from {user.mention}.", color=0xf43f5e)
        else:
            td = parse_duration_to_timedelta(duration)
            set_user_premium(user_id_str, td)
            exp_time = datetime.now(timezone.utc) + td
            embed = discord.Embed(
                title="[TE PREMIUM GRANTED]",
                description=f"Successfully granted TE Premium to {user.mention}.\nExpires: <t:{int(exp_time.timestamp())}:F> (<t:{int(exp_time.timestamp())}:R>)",
                color=0x2ecc71
            )

        embed.set_footer(text="Template Engine Premium Manager")
        await ctx.send(embed=embed)

    @commands.command(name="pdes")
    async def pdes_prefix_command(self, ctx: commands.Context):
        """Send a button that opens a Modal UI to configure /premium embed text (Owner restricted)."""
        if ctx.author.id != BOT_OWNER_ID:
            return

        embed = discord.Embed(
            title="[PREMIUM EMBED CONFIGURATION]",
            description="Click the button below to open the configuration modal and update the `/premium` embed details.",
            color=0xF1C40F
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await ctx.send(embed=embed, view=PremiumConfigTriggerView())

    @commands.command(name="ba")
    async def ba_prefix_command(self, ctx: commands.Context, user: discord.User = None):
        """Toggle /build authorization for a mentioned user (Owner restricted). Usage: !ba @User"""
        if ctx.author.id != BOT_OWNER_ID:
            return

        if not user:
            embed = discord.Embed(title="[INVALID SYNTAX]", description="Usage: `!ba @User`", color=0xf43f5e)
            await ctx.send(embed=embed)
            return

        user_id_str = str(user.id)
        is_now_authorized = toggle_user_build_authorization(user_id_str)

        if is_now_authorized:
            embed = discord.Embed(title="[BUILD AUTHORIZATION GRANTED]", description=f"User {user.mention} ({user_id_str}) is now authorized to run build commands.", color=0x2ecc71)
        else:
            embed = discord.Embed(title="[BUILD AUTHORIZATION REVOKED]", description=f"User {user.mention} ({user_id_str}) build permission has been revoked.", color=0xf43f5e)

        embed.set_footer(text="Template Engine Infrastructure")
        await ctx.send(embed=embed)

    @commands.command(name="cmds")
    async def cmds_prefix_command(self, ctx: commands.Context):
        """List all prefix commands (1 command per line)."""
        cmd_lines = [
            "`!rev <1d|7d|30d|1y>` - Check Roblox revenue summary for specified timeframe",
            "`!pset @User <1s|1h|1d|1mo|1y|remove>` - Grant or revoke TE Premium for a user",
            "`!pdes` - Open Modal UI to customize /premium embed",
            "`!ba @User` - Toggle build authorization for a user",
            "`!sync` - Synchronize slash commands across Discord",
            "`!cmds` - Display this list of prefix commands"
        ]
        
        embed = discord.Embed(
            title="🤖 Echo Studio - Prefix Commands",
            description="\n".join(cmd_lines),
            color=0x3b82f6
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await ctx.send(embed=embed)

    @commands.command(name="sync")
    async def sync_prefix_command(self, ctx: commands.Context):
        """Synchronize global slash commands across Discord (Owner restricted)."""
        if ctx.author.id != BOT_OWNER_ID:
            return

        synced_global = await self.bot.tree.sync()

        embed = discord.Embed(
            title="[SLASH COMMANDS SYNCED]",
            description=f"Successfully synchronized **{len(synced_global)}** global slash commands.",
            color=0x2ecc71
        )
        embed.set_footer(text="Template Engine Infrastructure")
        await ctx.send(embed=embed)

    # --- OWNER-ONLY & ADMIN SLASH COMMANDS ---
    @app_commands.command(name="welcome-set", description="Set welcome channel and custom welcome message format for this server (Admins).")
    @app_commands.describe(
        channel="Target text channel for welcome messages",
        message="Custom message template (Use {user} for user ping, {server} for server name)"
    )
    async def welcome_set(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Welcome, {user}.\nPlease read channel."):
        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator and interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[ACCESS DENIED]", description="Manage Server or Administrator permission required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        set_guild_welcome_setting(guild_id=str(interaction.guild.id), channel_id=str(channel.id), message_template=message)

        embed = discord.Embed(
            title="[WELCOME SETTINGS UPDATED]",
            description=f"Welcome messages in **{interaction.guild.name}** will be sent to {channel.mention}.\n\n**Template:**\n`{message}`",
            color=0x2ecc71
        )
        embed.set_footer(text="Template Engine Configuration")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="premiums", description="List all active TE Premium members and expiration dates (Owner restricted).")
    async def premiums_list(self, interaction: discord.Interaction):
        if interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[ACCESS DENIED]", description="Restricted command. Bot owner access required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        now = datetime.now(timezone.utc)
        active_subscribers = []

        if db_premium is not None:
            docs = list(db_premium.find())
            for doc in docs:
                uid = doc.get("user_id")
                exp_raw = doc.get("expires_at")
                if uid and exp_raw:
                    exp = datetime.fromisoformat(exp_raw) if isinstance(exp_raw, str) else exp_raw
                    if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
                    if exp > now: active_subscribers.append((uid, exp))
        else:
            for uid, exp in LOCAL_PREMIUM_USERS.items():
                if exp > now: active_subscribers.append((uid, exp))

        embed = discord.Embed(
            title=f"👑 Active TE Premium Members ({len(active_subscribers)})",
            color=0xF1C40F,
            timestamp=now
        )

        if active_subscribers:
            lines = [f"{idx}. <@{uid}> (`{uid}`) — Expires: <t:{int(exp.timestamp())}:F> (<t:{int(exp.timestamp())}:R>)" for idx, (uid, exp) in enumerate(active_subscribers, start=1)]
            full_desc = "\n".join(lines)
            embed.description = full_desc if len(full_desc) <= 4000 else full_desc[:3950] + "\n\n...[Truncated]"
        else:
            embed.description = "No active TE Premium subscribers found."

        embed.set_footer(text="Template Engine Premium Audit Log")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="verification-set", description="Post Roblox verification panel (Owner restricted).")
    async def verification_set(self, interaction: discord.Interaction, location: discord.TextChannel, title: str = "Roblox Account Verification", description: str = "Click Generate Code to link your Roblox profile."):
        if interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[ACCESS DENIED]", description="Restricted command. Bot owner access required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        embed = discord.Embed(title=title, description=description, color=0x8b5cf6)
        embed.set_footer(text="Template Engine Infrastructure")
        await location.send(embed=embed, view=VerificationPanelView())
        confirm = discord.Embed(title="[PANEL DEPLOYED]", description="Verification panel deployed.", color=0x2ecc71)
        await interaction.response.send_message(embed=confirm, ephemeral=True)

    @app_commands.command(name="un-sync-set", description="Post Roblox unsync panel (Owner restricted).")
    async def unsync_set(self, interaction: discord.Interaction, location: discord.TextChannel, title: str = "Unsync Roblox Account", description: str = "Unlink your Roblox account."):
        if interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[ACCESS DENIED]", description="Restricted command. Bot owner access required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        embed = discord.Embed(title=title, description=description, color=0xf43f5e)
        embed.set_footer(text="Template Engine Infrastructure")
        await location.send(embed=embed, view=UnsyncPanelView())
        confirm = discord.Embed(title="[PANEL DEPLOYED]", description="Unsync panel deployed.", color=0x2ecc71)
        await interaction.response.send_message(embed=confirm, ephemeral=True)

    @app_commands.command(name="nuke", description="Delete all channels and roles in the server (Owner restricted).")
    async def nuke_server(self, interaction: discord.Interaction):
        if interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[ACCESS DENIED]", description="Restricted command. Bot owner access required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        nuke_embed = discord.Embed(title="[NUKE INITIATED]", description="Wiping guild channels and non-default roles...", color=0xf43f5e)
        nuke_embed.set_footer(text="Template Engine Infrastructure")
        await interaction.response.send_message(embed=nuke_embed, ephemeral=True)

        for channel in interaction.guild.channels:
            try: await channel.delete(reason="Rebuild Execution")
            except Exception: pass
        for role in interaction.guild.roles:
            if not role.is_default() and not role.is_bot_managed() and not role.is_premium_subscriber():
                try: await role.delete(reason="Rebuild Execution")
                except Exception: pass

    @app_commands.command(name="roblox-game", description="Publish place script directly to Roblox game via Open Cloud (File or Modal).")
    @app_commands.describe(file="Optional .lua or .txt file attachment containing your complete Luau script")
    async def roblox_game_publish(self, interaction: discord.Interaction, file: discord.Attachment = None):
        if interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[ACCESS DENIED]", description="Restricted command. Bot owner access required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        if file:
            await interaction.response.defer(ephemeral=True)
            try:
                script_bytes = await file.read()
                lua_code = script_bytes.decode('utf-8')
                await publish_roblox_place_payload(interaction, lua_code)
            except Exception as e:
                err = discord.Embed(title="[FILE READ ERROR]", description=f"Could not read uploaded script file: {e}", color=0xf43f5e)
                await interaction.followup.send(embed=err, ephemeral=True)
        else:
            await interaction.response.send_modal(RobloxScriptUploadModal())

    # --- PUBLIC MEMBERS COMMANDS ---
    @app_commands.command(name="premium", description="View TE Premium features, perks, and subscription options.")
    async def premium_public(self, interaction: discord.Interaction):
        roblox_store_url = os.getenv("ROBLOX_STORE_URL", f"https://www.roblox.com/games/{ROBLOX_PLACE_ID}/store")
        cfg = get_premium_embed_config()

        title = cfg.get("title") or "👑 Template Engine Premium"
        base_desc = cfg.get("description") or "Upgrade to TE Premium to unlock automated server deployments, VIP badges, and custom themes."
        perks_list = cfg.get("perks") or [
            "Instant server layout auto-deployments",
            "Gold Glassmorphism web dashboard access",
            "Multi-entry multiplier on all server giveaways",
            "Exclusive TE Premium gold role in Discord"
        ]

        perks_formatted = "\n".join([f"• {p}" for p in perks_list])
        full_desc = f"{base_desc}\n\n**Perks Included:**\n{perks_formatted}"

        embed = discord.Embed(
            title=title,
            description=full_desc,
            color=0xF1C40F
        )
        embed.set_footer(text="Template Engine Infrastructure")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Get Premium on Roblox (299 Robux)", url=roblox_store_url, style=discord.ButtonStyle.link, emoji="🛒"))
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="channel", description="Create, edit, or set temporary channel behavior.")
    @app_commands.describe(
        action="Select channel operation mode",
        name="Channel name (for create or edit)",
        type="Channel type (for create: text or voice)",
        category="Target category for channel",
        topic="Channel topic description",
        duration="Duration for temporary channels (e.g., 10m, 2h, 1d)"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Create Channel", value="create"),
            app_commands.Choice(name="Edit Current Channel", value="edit"),
            app_commands.Choice(name="Temporary Channel Timer", value="temporary")
        ],
        type=[
            app_commands.Choice(name="Text Channel", value="text"),
            app_commands.Choice(name="Voice Channel", value="voice")
        ]
    )
    async def channel_manage(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        name: str = None,
        type: app_commands.Choice[str] = None,
        category: discord.CategoryChannel = None,
        topic: str = None,
        duration: str = "1h"
    ):
        if not interaction.user.guild_permissions.manage_channels and interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[PERMISSION DENIED]", description="Manage Channels permission required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        act = action.value.lower()
        guild = interaction.guild

        if act == "create":
            if not name:
                err = discord.Embed(title="[MISSING PARAMETER]", description="❌ Channel name is required to create a channel.", color=0xf43f5e)
                await interaction.response.send_message(embed=err, ephemeral=True)
                return

            c_type = type.value if type else "text"
            if c_type == "voice":
                new_ch = await guild.create_voice_channel(name=name, category=category)
            else:
                new_ch = await guild.create_text_channel(name=name, category=category, topic=topic or "")

            embed = discord.Embed(
                title="[CHANNEL CREATED]",
                description=f"Successfully created channel {new_ch.mention} in category `{category.name if category else 'None'}`.",
                color=0x2ecc71
            )
            embed.set_footer(text="Template Engine Channel Manager")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif act == "edit":
            ch = interaction.channel
            kwargs = {}
            if name: kwargs["name"] = name
            if category: kwargs["category"] = category
            if topic and isinstance(ch, discord.TextChannel): kwargs["topic"] = topic

            if not kwargs:
                err = discord.Embed(title="[MISSING PARAMETER]", description="❌ Provide at least one field (name, category, or topic) to edit.", color=0xf43f5e)
                await interaction.response.send_message(embed=err, ephemeral=True)
                return

            await ch.edit(**kwargs)
            embed = discord.Embed(
                title="[CHANNEL EDITED]",
                description=f"Updated settings for {ch.mention}.",
                color=0x3b82f6
            )
            embed.set_footer(text="Template Engine Channel Manager")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif act == "temporary":
            ch = interaction.channel
            td = parse_duration_to_timedelta(duration)
            expires_at = datetime.now(timezone.utc) + td

            init_embed = discord.Embed(
                title="⏳ Temporary Channel Active",
                description=(
                    f"This channel has been set to **Temporary** and will automatically expire in **{duration}**.\n\n"
                    f"**Scheduled Expiration:** <t:{int(expires_at.timestamp())}:F> (<t:{int(expires_at.timestamp())}:R>)"
                ),
                color=0xeab308
            )
            init_embed.set_footer(text="Template Engine Channel Manager")
            await interaction.response.send_message(embed=init_embed)

            async def handle_temp_channel_lifecycle():
                total_seconds = td.total_seconds()
                if total_seconds > 120:
                    await asyncio.sleep(total_seconds - 60)
                    warn_embed = discord.Embed(
                        title="⚠️ Channel Expiring Soon!",
                        description="This temporary channel will be automatically deleted in **1 minute**.",
                        color=0xf43f5e
                    )
                    warn_embed.set_footer(text="Template Engine Channel Manager")
                    try: await ch.send(embed=warn_embed)
                    except Exception: pass
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(total_seconds)

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

    @app_commands.command(name="giveaway-start", description="Start a customizable giveaway with multi-entry multipliers.")
    @app_commands.describe(
        channel="Channel to post giveaway",
        winners="Number of winners",
        duration="Duration (e.g. 10m, 2h, 1d, inf)",
        title="Giveaway title",
        prize="Prize description",
        description="Optional body description",
        allowed_roles="Role requirement string (Comma-separated Role IDs)",
        unallowed_roles="Excluded roles (Comma-separated Role IDs)",
        maximum_entry="Max entries granted for bonus role members (Default: 2)",
        allowed_roles_max_entry="Bonus role IDs for maximum entries (Comma-separated IDs)"
    )
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        winners: int,
        duration: str,
        title: str,
        prize: str = None,
        description: str = None,
        allowed_roles: str = None,
        unallowed_roles: str = None,
        maximum_entry: int = 2,
        allowed_roles_max_entry: str = None
    ):
        if not interaction.user.guild_permissions.manage_events and interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[PERMISSION DENIED]", description="Manage Events permission required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        td = parse_duration_to_timedelta(duration)
        end_time = datetime.now(timezone.utc) + td

        allowed_ids = [int(x.strip()) for x in allowed_roles.split(",") if x.strip().isdigit()] if allowed_roles else []
        unallowed_ids = [int(x.strip()) for x in unallowed_roles.split(",") if x.strip().isdigit()] if unallowed_roles else []
        bonus_ids = [int(x.strip()) for x in allowed_roles_max_entry.split(",") if x.strip().isdigit()] if allowed_roles_max_entry else []

        embed_desc = ""
        if prize:
            embed_desc += f"**{prize}**\n"
        if description:
            embed_desc += f"{description}\n"
        if embed_desc:
            embed_desc += "\n"

        embed_desc += (
            f"**Time Remaining**\n"
            f"<t:{int(end_time.timestamp())}:R> (<t:{int(end_time.timestamp())}:F>)\n\n"
            f"**Hosted By**\n"
            f"{interaction.user.mention}"
        )

        embed = discord.Embed(
            title=title,
            description=embed_desc,
            color=0xF1C40F
        )
        embed.set_image(url=GIVEAWAY_BANNER_URL)
        embed.set_footer(text=f"{winners} winners | Ends At | {end_time.strftime('%Y/%m/%d %I:%M %p')}")

        view = GiveawayPanelView()
        msg = await channel.send(embed=embed, view=view)

        gw_data = {
            "message_id": str(msg.id),
            "channel_id": str(channel.id),
            "host_id": str(interaction.user.id),
            "title": title,
            "prize": prize or title,
            "winners": winners,
            "end_time": end_time.isoformat(),
            "allowed_role_ids": allowed_ids,
            "unallowed_role_ids": unallowed_ids,
            "bonus_role_ids": bonus_ids,
            "max_entry": maximum_entry,
            "entries": {},
            "ended": False
        }
        save_giveaway_data(str(msg.id), gw_data)

        confirm = discord.Embed(
            title="[GIVEAWAY STARTED]",
            description=f"Giveaway deployed to {channel.mention}.\nMessage Link: {msg.jump_url}",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=confirm, ephemeral=True)

    @app_commands.command(name="giveaway-edit", description="Edit an existing giveaway using message ID or link.")
    @app_commands.describe(
        message_id_or_link="Target giveaway message ID or URL",
        title="Updated title",
        prize="Updated prize",
        winners="Updated winner count"
    )
    async def giveaway_edit(
        self,
        interaction: discord.Interaction,
        message_id_or_link: str,
        title: str = None,
        prize: str = None,
        winners: int = None
    ):
        if not interaction.user.guild_permissions.manage_events and interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[PERMISSION DENIED]", description="Manage Events permission required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        msg_id = message_id_or_link.split("/")[-1].strip()
        gw_data = get_giveaway_data(msg_id)
        if not gw_data:
            err = discord.Embed(title="[NOT FOUND]", description="Giveaway message not found.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        if title: gw_data["title"] = title
        if prize: gw_data["prize"] = prize
        if winners: gw_data["winners"] = winners

        save_giveaway_data(msg_id, gw_data)

        channel = self.bot.get_channel(int(gw_data["channel_id"]))
        if channel:
            try:
                msg = await channel.fetch_message(int(msg_id))
                if msg and msg.embeds:
                    embed = msg.embeds[0]
                    if title: embed.title = title
                    await msg.edit(embed=embed)
            except Exception: pass

        confirm = discord.Embed(title="[GIVEAWAY UPDATED]", description="✅ Giveaway updated successfully.", color=0x2ecc71)
        await interaction.response.send_message(embed=confirm, ephemeral=True)

    @app_commands.command(name="giveaway-end", description="Force-end an active giveaway immediately.")
    @app_commands.describe(message_id_or_link="Target giveaway message ID or URL")
    async def giveaway_end(self, interaction: discord.Interaction, message_id_or_link: str):
        if not interaction.user.guild_permissions.manage_events and interaction.user.id != BOT_OWNER_ID:
            err = discord.Embed(title="[PERMISSION DENIED]", description="Manage Events permission required.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        msg_id = message_id_or_link.split("/")[-1].strip()
        gw_data = get_giveaway_data(msg_id)
        if not gw_data or gw_data.get("ended"):
            err = discord.Embed(title="[NOT FOUND]", description="Giveaway not found or already ended.", color=0xf43f5e)
            await interaction.response.send_message(embed=err, ephemeral=True)
            return

        await self.finalize_giveaway(gw_data)
        confirm = discord.Embed(title="[GIVEAWAY ENDED]", description="💥 Giveaway ended early.", color=0x2ecc71)
        await interaction.response.send_message(embed=confirm, ephemeral=True)

    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction):
        embed = discord.Embed(title="[PONG]", description=f"Latency: `{round(self.bot.latency * 1000)}ms`", color=0x3b82f6)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="website", description="Get website button.")
    async def website_link(self, interaction: discord.Interaction):
        website_url = os.getenv("WEBSITE_URL", "https://templateengine-xi.vercel.app")
        embed = discord.Embed(title="[TEMPLATE ENGINE WEBSITE]", description="Access layout generator online:", color=0x3b82f6)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Website", url=website_url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot):
    bot.add_view(VerificationPanelView())
    bot.add_view(UnsyncPanelView())
    bot.add_view(GiveawayPanelView())
    await bot.add_cog(BotCog(bot))
