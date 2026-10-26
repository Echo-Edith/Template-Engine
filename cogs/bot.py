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
db_giveaways = None

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
    except Exception as e:
        print(f"[Bot Mongo Warning] Could not connect to MongoDB: {e}")

LOCAL_PREMIUM_USERS = {}
LOCAL_AUTHORIZED_BUILDERS = set()
LOCAL_PERSISTENT_USERS = {}
PENDING_VERIFICATION_CODES = {}
LOCAL_GIVEAWAYS = {}

PREMIUM_ROLE_NAME = "Template Engine Premium"
PREMIUM_ROLE_COLOR = discord.Color(0xF1C40F)


def parse_duration_to_timedelta(duration_str: str) -> timedelta:
    if not duration_str or duration_str.lower() in ["inf", "infinite"]:
        return timedelta(days=36500) # ~100 years
    match = re.match(r"^(\d+)\s*([smhdSMHD])?$", duration_str.strip())
    if not match:
        return timedelta(days=1)
    val, unit = match.groups()
    num = int(val)
    unit = (unit or "d").lower()
    if unit == "s": return timedelta(seconds=num)
    elif unit == "m": return timedelta(minutes=num)
    elif unit == "h": return timedelta(hours=num)
    elif unit == "d": return timedelta(days=num)
    return timedelta(days=1)


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


# --- GIVEAWAY STORAGE HELPERS ---
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


# --- GIVEAWAY ENTRIES PAGINATION VIEW ---
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
            color=0x3b82f6
        )
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_entries = self.entries[start:end]

        if page_entries:
            lines = [f"{start + idx + 1}. <@{uid}>" for idx, uid in enumerate(page_entries)]
            embed.description = "\n".join(lines)
        else:
            embed.description = "No entries yet."

        embed.set_footer(text=f"Page {self.current_page + 1} of {self.max_pages}")
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


# --- PERSISTENT GIVEAWAY VIEW ---
class GiveawayPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.success, custom_id="btn_gw_join", emoji="🎉")
    async def join_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        data = get_giveaway_data(msg_id)
        if not data or data.get("ended"):
            await interaction.response.send_message("This giveaway has ended or does not exist.", ephemeral=True)
            return

        user = interaction.user
        user_id = str(user.id)
        entries_dict = data.get("entries", {})

        # Check unallowed roles
        unallowed_role_ids = data.get("unallowed_role_ids", [])
        if any(role.id in unallowed_role_ids for role in user.roles):
            await interaction.response.send_message("❌ You have a role that is excluded from entering this giveaway.", ephemeral=True)
            return

        # Check allowed roles
        allowed_role_ids = data.get("allowed_role_ids", [])
        if allowed_role_ids and not any(role.id in allowed_role_ids for role in user.roles):
            await interaction.response.send_message("❌ You do not have the required role to enter this giveaway.", ephemeral=True)
            return

        # Toggle leave if already entered
        if user_id in entries_dict:
            entries_dict.pop(user_id)
            data["entries"] = entries_dict
            save_giveaway_data(msg_id, data)
            await interaction.response.send_message("❌ You have left the giveaway.", ephemeral=True)
            return

        # Determine entries count
        max_entry_count = data.get("max_entry", 1)
        bonus_role_ids = data.get("bonus_role_ids", [])
        has_bonus = any(role.id in bonus_role_ids for role in user.roles) or is_user_premium(user_id)
        entry_weight = max_entry_count if has_bonus else 1

        entries_dict[user_id] = entry_weight
        data["entries"] = entries_dict
        save_giveaway_data(msg_id, data)

        msg = f"✅ You joined the giveaway! (Entries: **{entry_weight}**)"
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="View Entries", style=discord.ButtonStyle.secondary, custom_id="btn_gw_entries", emoji="📜")
    async def view_entries_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        data = get_giveaway_data(msg_id)
        entries_dict = data.get("entries", {})
        unique_users = list(entries_dict.keys())

        pag_view = GiveawayEntriesPaginationView(unique_users)
        await interaction.response.send_message(embed=pag_view.get_embed(), view=pag_view, ephemeral=True)


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


# --- INTERACTIVE VIEWS FOR DISCORD VERIFICATION ---
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
        self.check_premium_expirations.start()
        self.check_active_giveaways.start()

    def cog_unload(self):
        self.check_premium_expirations.cancel()
        self.check_active_giveaways.cancel()

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

        # Create weighted list based on entry count
        weighted_pool = []
        for uid, weight in entries_dict.items():
            weighted_pool.extend([uid] * weight)

        winners = []
        if weighted_pool:
            selected_set = set()
            attempts = 0
            while len(selected_set) < winner_count and attempts < 1000 and len(selected_set) < len(entries_dict):
                chosen = random.choice(weighted_pool)
                selected_set.add(chosen)
                attempts += 1
            winners = list(selected_set)

        if winners:
            winner_pings = ", ".join([f"<@{w}>" for w in winners])
            result_desc = f"🎉 **Congratulations** {winner_pings}! You won **{prize}**!"
        else:
            result_desc = "No valid entries. No winners selected."

        if msg:
            embed = msg.embeds[0] if msg.embeds else discord.Embed(title=gw_data.get("title"))
            embed.color = 0x2ecc71
            embed.description = f"**GIVEAWAY ENDED**\n\n**Prize:** {prize}\n**Winner(s):** {winner_pings if winners else 'None'}"
            await msg.edit(embed=embed, view=None)

        end_embed = discord.Embed(
            title="🎉 Giveaway Ended!",
            description=result_desc,
            color=0x2ecc71
        )
        end_embed.set_footer(text="Template Engine Infrastructure")
        await channel.send(embed=end_embed)

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

    # --- GIVEAWAY COMMANDS ---
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
            await interaction.response.send_message("Manage Events permission required.", ephemeral=True)
            return

        td = parse_duration_to_timedelta(duration)
        end_time = datetime.now(timezone.utc) + td

        allowed_ids = [int(x.strip()) for x in allowed_roles.split(",") if x.strip().isdigit()] if allowed_roles else []
        unallowed_ids = [int(x.strip()) for x in unallowed_roles.split(",") if x.strip().isdigit()] if unallowed_roles else []
        bonus_ids = [int(x.strip()) for x in allowed_roles_max_entry.split(",") if x.strip().isdigit()] if allowed_roles_max_entry else []

        embed = discord.Embed(
            title=f"🎉 {title}",
            description=description or f"Click **Enter Giveaway** below to join!\n\n**Prize:** {prize or title}",
            color=0x8b5cf6,
            timestamp=end_time
        )
        embed.add_field(name="Winners", value=f"`{winners}`", inline=True)
        embed.add_field(name="Ends At", value=f"<t:{int(end_time.timestamp())}:F> (<t:{int(end_time.timestamp())}:R>)", inline=True)
        if bonus_ids:
            embed.add_field(name="Bonus Entry Multiplier", value=f"Bonus Roles / TE Premium get **{maximum_entry}** entries!", inline=False)

        embed.set_footer(text="Template Engine Giveaways")

        view = GiveawayPanelView()
        msg = await channel.send(embed=embed, view=view)

        gw_data = {
            "message_id": str(msg.id),
            "channel_id": str(channel.id),
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
            await interaction.response.send_message("Manage Events permission required.", ephemeral=True)
            return

        msg_id = message_id_or_link.split("/")[-1].strip()
        gw_data = get_giveaway_data(msg_id)
        if not gw_data:
            await interaction.response.send_message("Giveaway message not found.", ephemeral=True)
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
                    if title: embed.title = f"🎉 {title}"
                    await msg.edit(embed=embed)
            except Exception: pass

        await interaction.response.send_message("✅ Giveaway updated successfully.", ephemeral=True)

    @app_commands.command(name="giveaway-end", description="Force-end an active giveaway immediately.")
    @app_commands.describe(message_id_or_link="Target giveaway message ID or URL")
    async def giveaway_end(self, interaction: discord.Interaction, message_id_or_link: str):
        if not interaction.user.guild_permissions.manage_events and interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("Manage Events permission required.", ephemeral=True)
            return

        msg_id = message_id_or_link.split("/")[-1].strip()
        gw_data = get_giveaway_data(msg_id)
        if not gw_data or gw_data.get("ended"):
            await interaction.response.send_message("Giveaway not found or already ended.", ephemeral=True)
            return

        await self.finalize_giveaway(gw_data)
        await interaction.response.send_message("💥 Giveaway ended early.", ephemeral=True)

    # --- UTILITY COMMANDS ---
    @app_commands.command(name="verification-set", description="Post Roblox verification panel.")
    async def verification_set(self, interaction: discord.Interaction, location: discord.TextChannel, title: str = "Roblox Account Verification", description: str = "Click Generate Code to link your Roblox profile."):
        if interaction.user.id != BOT_OWNER_ID: return
        embed = discord.Embed(title=title, description=description, color=0x8b5cf6)
        await location.send(embed=embed, view=VerificationPanelView())
        await interaction.response.send_message("Verification panel deployed.", ephemeral=True)

    @app_commands.command(name="un-sync-set", description="Post Roblox unsync panel.")
    async def unsync_set(self, interaction: discord.Interaction, location: discord.TextChannel, title: str = "Unsync Roblox Account", description: str = "Unlink your Roblox account."):
        if interaction.user.id != BOT_OWNER_ID: return
        embed = discord.Embed(title=title, description=description, color=0xf43f5e)
        await location.send(embed=embed, view=UnsyncPanelView())
        await interaction.response.send_message("Unsync panel deployed.", ephemeral=True)

    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Pong! `{round(self.bot.latency * 1000)}ms`")

    @app_commands.command(name="website", description="Get website button.")
    async def website_link(self, interaction: discord.Interaction):
        website_url = os.getenv("WEBSITE_URL", "https://templateengine-xi.vercel.app")
        embed = discord.Embed(title="[TEMPLATE ENGINE WEBSITE]", description="Access generator online:", color=0x3b82f6)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Website", url=website_url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot):
    bot.add_view(VerificationPanelView())
    bot.add_view(UnsyncPanelView())
    bot.add_view(GiveawayPanelView())
    cog = BotCog(bot)
    await bot.add_cog(cog)
