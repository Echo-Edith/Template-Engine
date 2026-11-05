import os
import re
import json
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

from cogs.features import (
    BOT_OWNER_ID,
    RobloxVerificationView,
    RobloxUnsyncView,
    GiveawayView,
    giveaway_payload,
    giveaway_reward_text,
    GIVEAWAYS,
    GIVEAWAY_DEBUG,
    MOD_ACTION_LABELS,
    MAX_REMINDERS_PER_USER,
    COLOR_PURPLE,
    COLOR_GOLD,
    get_roblox_cookie_header,
    get_user_points,
    add_user_points,
    get_top_leaderboard,
    parse_duration_to_timedelta,
    parse_duration_strict,
    format_timedelta,
    set_user_premium,
    remove_user_premium,
    embed_ok,
    embed_err,
    embed_info,
    build_pages,
    send_pages,
    add_case,
    get_case,
    get_cases,
    delete_case,
    delete_cases,
    dm_moderation_notice,
    set_afk,
    count_user_reminders,
    add_reminder,
    save_giveaway,
    delete_giveaway_record,
    build_giveaway_embed,
    finish_giveaway,
    reroll_giveaway,
    get_giveaway_message,
    COLOR_RED,
    COLOR_GREEN,
    get_prices,
    set_price,
    PROMO_CODE_RE,
    normalize_promo_code,
    create_promo,
    get_promo,
    list_promos,
    redeem_promo,
    update_promo,
    delete_promo,
    send_promo_log,
    add_staff,
    remove_staff,
    is_staff,
    list_staff,
    HARDCODED_STAFF,
    COMMAND_ADMINS,
    extend_user_premium,
    set_staff_role,
    get_staff_role,
)

# -------------------------------------------------------------
# WHO CAN USE WHAT (edit these sets to change access)
#   PUBLIC_COMMANDS : anyone
#   ADMIN_COMMANDS  : server members with the Administrator permission
#   anything else   : bot owner only (set, points-set, premium-set, game, revenue, cookie-check)
# -------------------------------------------------------------
STAFF_COMMANDS = {"staff", "staff-list"}  # only HARDCODED_STAFF may run these

PUBLIC_COMMANDS = {
    "points", "leaderboard", "roblox-user", "gameinfo", "timestamp", "remind",
    "afk", "whois", "permissions", "rolelist", "channelstats", "redeem", "promo",
}
ADMIN_COMMANDS = {
    "groupcheck", "slowmode", "nick", "role", "say", "embed", "announce",
    "warn", "clear", "purge", "mute", "unmute", "kick", "ban", "unban",
    "lock", "unlock", "modlog", "giveaway",
}


# -------------------------------------------------------------
# /game SETTINGS & HELPERS (Roblox Luau Execution API)
# -------------------------------------------------------------
GAME_DEFAULT_PLACE_ID = "6094950399"  # Template Engine
GAME_API_BASE = "https://apis.roblox.com/cloud/v2"
GAME_MAX_FILE_BYTES = 4 * 1024 * 1024
GAME_ALLOWED_EXTENSIONS = (".lua", ".luau", ".txt")
GAME_POLL_INTERVAL = 2      # seconds between status checks
GAME_POLL_TIMEOUT = 90      # seconds before we stop waiting
GAME_TERMINAL_STATES = {"COMPLETE", "FAILED", "CANCELLED"}


def _game_block(text: str, limit: int = 1000) -> str:
    """Wraps text in a code block that fits inside an embed field (1024 chars)."""
    text = str(text).replace("```", "`​``")
    if len(text) > limit:
        text = text[: limit - 15] + "\n...(truncated)"
    return f"```\n{text}\n```"


def _game_error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=0xF43F5E)


class GameCodeModal(discord.ui.Modal, title="Run Luau Code"):
    code_input = discord.ui.TextInput(
        label="Luau code",
        style=discord.TextStyle.paragraph,
        placeholder='print("Server is running")\nreturn 1 + 1',
        required=True,
        max_length=4000
    )

    def __init__(self, cog: "CommandsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.run_game_code(interaction, self.code_input.value)


# -------------------------------------------------------------
# SHARED HELPERS FOR THE EXTRA COMMANDS
# -------------------------------------------------------------
NO_PINGS = discord.AllowedMentions.none()


async def roblox_get(session: aiohttp.ClientSession, url: str, headers: Optional[dict] = None, params: Optional[dict] = None):
    """GET a Roblox endpoint. Returns (status, json_or_None). Status 0 means the request itself failed."""
    try:
        async with session.get(url, headers=headers, params=params) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            return resp.status, data
    except Exception as e:
        print(f"[Roblox GET Error] {url}: {e}")
        return 0, None


async def resolve_roblox_user(session: aiohttp.ClientSession, username: str) -> Optional[dict]:
    """Roblox username -> {"id", "name", "displayName"} (banned users included)."""
    try:
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username.strip()], "excludeBannedUsers": False}
        ) as resp:
            if resp.status != 200:
                return None
            rows = (await resp.json()).get("data", [])
            return rows[0] if rows else None
    except Exception as e:
        print(f"[Roblox Resolve Error] {e}")
        return None


def iso_to_ts(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        cleaned = re.sub(r"\.\d+", "", value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(cleaned).timestamp())
    except Exception:
        return None


def parse_hex_color(text: Optional[str], default: int = COLOR_PURPLE) -> Optional[int]:
    """Returns the color as an int, the default for empty text, or None if the text is not a hex color."""
    if not text or not text.strip():
        return default
    cleaned = text.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", cleaned):
        return int(cleaned, 16)
    return None


def pretty_perm(name: str) -> str:
    return name.replace("_", " ").title()


def check_hierarchy(interaction: discord.Interaction, target: discord.Member) -> Optional[str]:
    """Returns a reason why the action is not allowed, or None if it is fine."""
    guild = interaction.guild
    if target.id == interaction.user.id:
        return "You cannot use this on yourself."
    if target.id == guild.me.id:
        return "I cannot use this on myself."
    if target.id == guild.owner_id:
        return "You cannot use this on the server owner."
    if guild.me.top_role <= target.top_role:
        return "My highest role is not above that member's highest role. Move my role higher."
    if interaction.user.id not in (BOT_OWNER_ID, guild.owner_id) and interaction.user.top_role <= target.top_role:
        return "That member's highest role is the same as or above yours."
    return None


# -------------------------------------------------------------
# MODALS: /embed and /announce
# -------------------------------------------------------------
class EmbedBuilderModal(discord.ui.Modal, title="Create Embed"):
    title_input = discord.ui.TextInput(label="Title", required=False, max_length=256)
    description_input = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=True, max_length=4000)
    color_input = discord.ui.TextInput(label="Color (hex, e.g. 8B5CF6)", required=False, max_length=7)
    image_input = discord.ui.TextInput(label="Image URL", required=False, max_length=500)
    footer_input = discord.ui.TextInput(label="Footer", required=False, max_length=200)

    def __init__(self, channel: discord.abc.Messageable):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        color = parse_hex_color(self.color_input.value)
        if color is None:
            await interaction.followup.send(embed=embed_err("Invalid Color", "Use a 6 digit hex color such as `8B5CF6`."), ephemeral=True)
            return

        embed = discord.Embed(title=self.title_input.value or None, description=self.description_input.value, color=color)
        image = (self.image_input.value or "").strip()
        if image:
            if not re.match(r"^https?://\S+$", image):
                await interaction.followup.send(embed=embed_err("Invalid Image URL", "The image URL must start with `http://` or `https://`."), ephemeral=True)
                return
            embed.set_image(url=image)
        if self.footer_input.value:
            embed.set_footer(text=self.footer_input.value)

        try:
            await self.channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except Exception as e:
            await interaction.followup.send(embed=embed_err("Could Not Send Embed", f"`{e}`"), ephemeral=True)
            return
        await interaction.followup.send(embed=embed_ok("Embed Sent", f"Posted in {getattr(self.channel, 'mention', 'the channel')}."), ephemeral=True)


class AnnounceModal(discord.ui.Modal, title="Create Announcement"):
    title_input = discord.ui.TextInput(label="Title", required=True, max_length=256)
    message_input = discord.ui.TextInput(label="Announcement", style=discord.TextStyle.paragraph, required=True, max_length=4000)

    def __init__(self, channel: discord.TextChannel, ping: str, role: Optional[discord.Role]):
        super().__init__()
        self.channel = channel
        self.ping = ping
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title=self.title_input.value, description=self.message_input.value, color=COLOR_PURPLE)
        embed.set_footer(text=f"Announcement | {interaction.guild.name}")
        embed.timestamp = datetime.now(timezone.utc)

        parts = []
        if self.ping == "everyone":
            parts.append("@everyone")
        elif self.ping == "here":
            parts.append("@here")
        if self.role:
            parts.append(self.role.mention)
        mentions = discord.AllowedMentions(
            everyone=self.ping in ("everyone", "here"),
            roles=[self.role] if self.role else False,
            users=False
        )
        try:
            await self.channel.send(content=" ".join(parts) or None, embed=embed, allowed_mentions=mentions)
        except Exception as e:
            await interaction.followup.send(embed=embed_err("Could Not Send Announcement", f"`{e}`"), ephemeral=True)
            return
        await interaction.followup.send(embed=embed_ok("Announcement Sent", f"Posted in {self.channel.mention}."), ephemeral=True)


# -------------------------------------------------------------
# PRICE CHANGES (/change-price)
# -------------------------------------------------------------
PRICE_LABELS = {
    "dev_product": ("Dev Product", "R$"),
    "subscription": ("Subscription", "R$"),
    "points": ("Points (build cost)", "pts"),
}
PRICE_SHOWN_IN = {
    "dev_product": "the deployment DMs",
    "subscription": "the website (Get TE Premium button and the build notice)",
    "points": "the deployment DMs and the Use Points button",
}
PRICE_MAX = 1_000_000


def fmt_price(kind: str, value: Optional[int]) -> str:
    if value is None:
        return "`Unknown`"
    return f"`{int(value):,} {PRICE_LABELS[kind][1]}`"


async def resolve_universe_id(session: aiohttp.ClientSession) -> Optional[str]:
    universe_id = os.getenv("ROBLOX_UNIVERSE_ID", "").strip()
    if universe_id:
        return universe_id
    place_id = os.getenv("ROBLOX_PLACE_ID", "").strip() or GAME_DEFAULT_PLACE_ID
    status, data = await roblox_get(session, f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
    if status == 200 and isinstance(data, dict) and data.get("universeId"):
        return str(data["universeId"])
    return None


async def fetch_dev_product_price(session: aiohttp.ClientSession, api_key: str, universe_id: str, product_id: str):
    """Returns (price, error). The API key needs the developer-product:read permission."""
    status, data = await roblox_get(
        session,
        f"https://apis.roblox.com/developer-products/v2/universes/{universe_id}/developer-products/{product_id}/creator",
        headers={"x-api-key": api_key}
    )
    if status == 200 and isinstance(data, dict):
        price = (data.get("priceInformation") or {}).get("defaultPriceInRobux")
        if isinstance(price, int):
            return price, None
        return None, "Roblox did not return a price for this product"
    return None, f"HTTP {status}"


async def update_dev_product_price(session: aiohttp.ClientSession, api_key: str, universe_id: str, product_id: str, price: int):
    """Returns (ok, error). The API key needs the developer-product:write permission."""
    form = aiohttp.FormData()
    form.add_field("price", str(int(price)))
    try:
        async with session.patch(
            f"https://apis.roblox.com/developer-products/v2/universes/{universe_id}/developer-products/{product_id}",
            data=form, headers={"x-api-key": api_key}
        ) as resp:
            if resp.status in (200, 204):
                return True, ""
            body = await resp.text()
            return False, f"HTTP {resp.status}: {body[:300]}"
    except Exception as e:
        return False, str(e)


class PriceChangeConfirmView(discord.ui.View):
    """Shows old and new price. Nothing changes until the owner presses Confirm."""

    def __init__(self, owner_id: int, kind: str, old: Optional[int], new: int, api: Optional[dict] = None):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.kind = kind
        self.old = old
        self.new = new
        self.api = api

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(embed=embed_err("Not Yours", "Only the person who ran the command can use these buttons."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass
        embed = await self.apply()
        await interaction.edit_original_response(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=embed_info("Price Change Cancelled", "Nothing was changed."), view=None)
        self.stop()

    async def apply(self) -> discord.Embed:
        label = PRICE_LABELS[self.kind][0]
        saved = True

        if self.kind == "dev_product":
            async with aiohttp.ClientSession() as session:
                ok, err = await update_dev_product_price(session, self.api["key"], self.api["universe"], self.api["product"], self.new)
            if not ok:
                hint = "\nCheck that the API key has the `developer-product:write` permission for this experience."
                return embed_err("Roblox Rejected the Change", f"The dev product price was **not** changed.\n{_game_block(err, 700)}{hint}")
            saved = set_price("dev_product_robux", self.new)
        elif self.kind == "subscription":
            saved = set_price("subscription_robux", self.new)
        else:
            saved = set_price("points_cost", self.new)

        embed = embed_ok("Price Updated", f"**{label}** price changed.")
        embed.add_field(name="Old", value=fmt_price(self.kind, self.old), inline=True)
        embed.add_field(name="New", value=fmt_price(self.kind, self.new), inline=True)
        embed.add_field(name="Now Shown In", value=PRICE_SHOWN_IN[self.kind], inline=False)
        if self.kind == "dev_product":
            embed.add_field(name="Changed On Roblox", value="`Yes`", inline=True)
        elif self.kind == "subscription":
            embed.add_field(
                name="Changed On Roblox",
                value="`No` - only the shown price changed. Change the real price in Creator Hub.",
                inline=False
            )
        if not saved:
            embed.add_field(name="Warning", value="The database could not be reached. The new price only lasts until the bot restarts.", inline=False)
        return embed


# -------------------------------------------------------------
# PROMO CODE PANEL (/manage codes)
# -------------------------------------------------------------
def promo_uses_text(promo: dict) -> str:
    limit = promo.get("max_uses")
    return f"{promo.get('uses', 0)}/{limit}" if limit is not None else f"{promo.get('uses', 0)}/unlimited"


def promo_reward_text(promo: dict) -> str:
    parts = []
    if int(promo.get("points", 0)) > 0:
        parts.append(f"{int(promo['points']):,} pts")
    if int(promo.get("premium_days", 0)) > 0:
        parts.append(f"{int(promo['premium_days'])}d TE Premium")
    return " + ".join(parts) or "nothing"


def promo_line(promo: dict) -> str:
    expires = promo.get("expires_at")
    expires_text = f"<t:{int(expires)}:R>" if expires else "never"
    return f"`{promo['code']}` | `{promo_reward_text(promo)}` | uses `{promo_uses_text(promo)}` | expires {expires_text}"


def promo_detail_embed(promo: dict, notice: Optional[str] = None) -> discord.Embed:
    embed = embed_info(f"Promo Code | {promo['code']}", notice)
    embed.add_field(name="Code", value=f"`{promo['code']}`", inline=True)
    embed.add_field(name="Reward", value=f"`{promo_reward_text(promo)}`", inline=True)
    embed.add_field(name="Uses", value=f"`{promo_uses_text(promo)}`", inline=True)
    expires = promo.get("expires_at")
    embed.add_field(name="Expires", value=f"<t:{int(expires)}:F> (<t:{int(expires)}:R>)" if expires else "`Never`", inline=False)
    embed.add_field(name="Created", value=f"<t:{int(promo.get('created_at', 0))}:R> by <@{promo.get('created_by', 0)}>", inline=False)
    return embed


class ManageCodesView(discord.ui.View):
    """Pick a code from the menu, then edit its points, limits or delete it."""
    PAGE_SIZE = 25

    def __init__(self, owner_id: int, page: int = 0, selected: Optional[str] = None, confirm_delete: bool = False):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        codes = list_promos(active_only=True)
        self.total = len(codes)
        pages = max(1, -(-len(codes) // self.PAGE_SIZE))
        self.page = max(0, min(page, pages - 1))
        self.selected = selected if selected and any(c["code"] == selected for c in codes) else None
        self.confirm_delete = bool(confirm_delete and self.selected)

        chunk = codes[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]
        if chunk:
            self.select_menu = discord.ui.Select(
                placeholder="Choose a code to manage...",
                options=[
                    discord.SelectOption(
                        label=c["code"], value=c["code"],
                        description=f"{int(c['points']):,} pts | uses {promo_uses_text(c)}"[:100],
                        default=(c["code"] == self.selected)
                    ) for c in chunk
                ],
                row=0
            )
            self.select_menu.callback = self._on_select
            self.add_item(self.select_menu)

        if pages > 1:
            prev_btn = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
            next_btn = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= pages - 1, row=1)
            prev_btn.callback = self._on_prev
            next_btn.callback = self._on_next
            self.add_item(prev_btn)
            self.add_item(next_btn)

        if self.selected and self.confirm_delete:
            yes_btn = discord.ui.Button(label="Confirm Delete", style=discord.ButtonStyle.danger, row=2)
            no_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
            yes_btn.callback = self._on_confirm_delete
            no_btn.callback = self._on_cancel_delete
            self.add_item(yes_btn)
            self.add_item(no_btn)
        elif self.selected:
            points_btn = discord.ui.Button(label="Edit Points", style=discord.ButtonStyle.primary, row=2)
            limits_btn = discord.ui.Button(label="Edit Limit / Expiry", style=discord.ButtonStyle.primary, row=2)
            delete_btn = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, row=2)
            points_btn.callback = self._on_edit_points
            limits_btn.callback = self._on_edit_limits
            delete_btn.callback = self._on_delete
            self.add_item(points_btn)
            self.add_item(limits_btn)
            self.add_item(delete_btn)

    def build_embed(self, notice: Optional[str] = None) -> discord.Embed:
        if self.selected:
            promo = get_promo(self.selected)
            if promo:
                embed = promo_detail_embed(promo, notice)
                if self.confirm_delete:
                    embed.color = COLOR_RED
                    embed.add_field(name="Delete this code?", value="This cannot be undone. Press **Confirm Delete** to remove it.", inline=False)
                return embed
        if self.total:
            text = f"Active codes: `{self.total}`\nChoose a code from the menu to edit or delete it."
        else:
            text = "There are no active promo codes. Create one with `/set code`."
        return embed_info("Manage Promo Codes", f"{notice}\n\n{text}" if notice else text)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(embed=embed_err("Not Yours", "Only the person who opened this panel can use it."), ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction, page: int, selected: Optional[str], confirm: bool = False, notice: Optional[str] = None):
        view = ManageCodesView(self.owner_id, page, selected, confirm)
        await interaction.response.edit_message(embed=view.build_embed(notice), view=view)

    async def _on_select(self, interaction: discord.Interaction):
        await self._show(interaction, self.page, self.select_menu.values[0])

    async def _on_prev(self, interaction: discord.Interaction):
        await self._show(interaction, self.page - 1, None)

    async def _on_next(self, interaction: discord.Interaction):
        await self._show(interaction, self.page + 1, None)

    async def _on_edit_points(self, interaction: discord.Interaction):
        promo = get_promo(self.selected)
        if not promo:
            await self._show(interaction, self.page, None, notice="That code no longer exists.")
            return
        await interaction.response.send_modal(EditPromoPointsModal(self.owner_id, self.page, promo))

    async def _on_edit_limits(self, interaction: discord.Interaction):
        promo = get_promo(self.selected)
        if not promo:
            await self._show(interaction, self.page, None, notice="That code no longer exists.")
            return
        await interaction.response.send_modal(EditPromoLimitsModal(self.owner_id, self.page, promo))

    async def _on_delete(self, interaction: discord.Interaction):
        await self._show(interaction, self.page, self.selected, confirm=True)

    async def _on_cancel_delete(self, interaction: discord.Interaction):
        await self._show(interaction, self.page, self.selected)

    async def _on_confirm_delete(self, interaction: discord.Interaction):
        code = self.selected
        removed = delete_promo(code)
        notice = f"Code `{code}` was deleted." if removed else f"Code `{code}` was already gone."
        await self._show(interaction, self.page, None, notice=notice)


class EditPromoPointsModal(discord.ui.Modal, title="Edit Reward"):
    points_input = discord.ui.TextInput(label="Reward points (0 = none)", required=True, max_length=9)
    premium_input = discord.ui.TextInput(label="TE Premium days (0 = none, 30 = one month)", required=True, max_length=4)

    def __init__(self, owner_id: int, page: int, promo: dict):
        super().__init__()
        self.owner_id = owner_id
        self.page = page
        self.code = promo["code"]
        self.points_input.default = str(int(promo.get("points", 0)))
        self.premium_input.default = str(int(promo.get("premium_days", 0)))

    async def on_submit(self, interaction: discord.Interaction):
        points_text = self.points_input.value.strip().replace(",", "")
        days_text = self.premium_input.value.strip()
        if (not points_text.isdigit() or int(points_text) > 1_000_000
                or not days_text.isdigit() or int(days_text) > 3650):
            await interaction.response.send_message(
                embed=embed_err("Invalid Reward", "Points must be `0` to `1,000,000`. Premium days must be `0` to `3,650`."), ephemeral=True)
            return
        points, days = int(points_text), int(days_text)
        if points == 0 and days == 0:
            await interaction.response.send_message(embed=embed_err("Invalid Reward", "A code must give points or premium (or both)."), ephemeral=True)
            return
        promo = update_promo(self.code, {"points": points, "premium_days": days})
        if not promo:
            await interaction.response.send_message(embed=embed_err("Code Not Found", f"`{self.code}` no longer exists."), ephemeral=True)
            return
        view = ManageCodesView(self.owner_id, self.page, self.code)
        await interaction.response.edit_message(embed=view.build_embed(f"Reward changed to `{promo_reward_text(promo)}`. People who already used the code keep what they got."), view=view)


class EditPromoLimitsModal(discord.ui.Modal, title="Edit Usage Limit and Expiry"):
    limit_input = discord.ui.TextInput(label="Usage limit (empty = unlimited)", required=False, max_length=9)
    expires_input = discord.ui.TextInput(label="Expires in (e.g. 7d, 12h). Empty = never", required=False, max_length=30)

    def __init__(self, owner_id: int, page: int, promo: dict):
        super().__init__()
        self.owner_id = owner_id
        self.page = page
        self.code = promo["code"]
        if promo.get("max_uses") is not None:
            self.limit_input.default = str(int(promo["max_uses"]))
        expires = promo.get("expires_at")
        if expires:
            remaining = int(expires - datetime.now(timezone.utc).timestamp())
            if remaining > 0:
                self.expires_input.default = format_timedelta(timedelta(seconds=remaining))

    async def on_submit(self, interaction: discord.Interaction):
        limit_text = self.limit_input.value.strip().lower().replace(",", "")
        if limit_text in ("", "none", "unlimited", "0"):
            max_uses = None
        elif limit_text.isdigit() and 1 <= int(limit_text) <= 1_000_000:
            max_uses = int(limit_text)
        else:
            await interaction.response.send_message(embed=embed_err("Invalid Usage Limit", "Enter a whole number from `1` to `1,000,000`, or leave it empty for unlimited."), ephemeral=True)
            return

        expires_text = self.expires_input.value.strip().lower()
        if expires_text in ("", "none", "never"):
            expires_at = None
        else:
            delta = parse_duration_strict(expires_text)
            if delta is None:
                await interaction.response.send_message(embed=embed_err("Invalid Expiry", "Use a time like `12h`, `7d` or `2w`, or leave it empty for no expiry."), ephemeral=True)
                return
            expires_at = datetime.now(timezone.utc).timestamp() + delta.total_seconds()

        promo = update_promo(self.code, {"max_uses": max_uses, "expires_at": expires_at})
        if not promo:
            await interaction.response.send_message(embed=embed_err("Code Not Found", f"`{self.code}` no longer exists."), ephemeral=True)
            return

        view = ManageCodesView(self.owner_id, self.page, self.code)
        notice = "Limit and expiry updated."
        if not view.selected:
            notice = f"Limit and expiry updated. `{self.code}` is no longer active (used up or expired), so it left this list."
        await interaction.response.edit_message(embed=view.build_embed(notice), view=view)


# -------------------------------------------------------------
# STAFF ROLE PICKER (shown after /staff add)
# -------------------------------------------------------------
class StaffListView(discord.ui.View):
    """/staff-list: staff members (paged) + a role picker. The chosen role is saved and given by /staff add."""
    PER_PAGE = 15

    def __init__(self, owner_id: int, page: int = 0):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.ids = list_staff()
        self.pages = max(1, -(-len(self.ids) // self.PER_PAGE))
        self.page = max(0, min(page, self.pages - 1))
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1
        if self.pages == 1:
            self.remove_item(self.prev_btn)
            self.remove_item(self.next_btn)

    def build_embed(self, notice: Optional[str] = None) -> discord.Embed:
        chunk = self.ids[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]
        lines = "\n".join(f"<@{uid}> (`{uid}`)" for uid in chunk) or "No staff members yet. Use `/staff` to add one."
        role_info = get_staff_role()
        role_text = f"<@&{role_info['role_id']}>" if role_info else "`Not set`"
        embed = embed_info("Staff List", f"{notice}\n\n" if notice else "")
        embed.description = (embed.description or "") + f"Staff members: `{len(self.ids)}`\n**Role given by `/staff add`:** {role_text}\n\n{lines}"
        if self.pages > 1:
            embed.set_footer(text=f"Page {self.page + 1}/{self.pages}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(embed=embed_err("Not Yours", "Only the person who ran the command can use this."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = StaffListView(self.owner_id, self.page - 1)
        await interaction.response.edit_message(embed=view.build_embed(), view=view, allowed_mentions=NO_PINGS)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = StaffListView(self.owner_id, self.page + 1)
        await interaction.response.edit_message(embed=view.build_embed(), view=view, allowed_mentions=NO_PINGS)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Choose the role new staff get...", min_values=1, max_values=1, row=0)
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=embed_err("Server Only", "Use this inside a server."), ephemeral=True)
            return
        if role.is_default() or role.managed:
            await interaction.response.send_message(embed=embed_err("Cannot Use This Role", "Pick a normal role (not @everyone and not a bot role)."), ephemeral=True)
            return
        if role >= guild.me.top_role:
            await interaction.response.send_message(embed=embed_err("Role Too High", f"{role.mention} is higher than my top role. Move my role above it first."), ephemeral=True, allowed_mentions=NO_PINGS)
            return
        saved = set_staff_role(guild.id, role.id)
        notice = f"Saved. New staff will get {role.mention} and TE Premium."
        if not saved:
            notice += "\nThe database could not be reached, so this only lasts until the bot restarts."
        view = StaffListView(self.owner_id, self.page)
        await interaction.response.edit_message(embed=view.build_embed(notice), view=view, allowed_mentions=NO_PINGS)


# -------------------------------------------------------------
# COMMANDS COG: all slash commands
# -------------------------------------------------------------
class CommandsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------
    # ACCESS LEVELS: bot owner can use everything, see PUBLIC_COMMANDS / ADMIN_COMMANDS above
    # -------------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cmd = interaction.command
        root_name = (cmd.root_parent or cmd).name if cmd else ""

        # Staff commands: ONLY the hard-coded staff IDs (checked before anything else)
        if root_name in STAFF_COMMANDS:
            if str(interaction.user.id) in COMMAND_ADMINS:
                return True
            denied = embed_err("Permission Denied", "Only the main staff can use this command.")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=denied, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=denied, ephemeral=True)
            except Exception as e:
                print(f"[Staff Check Error] {e}")
            return False

        # Main staff (the two hard-coded IDs) can use every command
        if interaction.user.id == BOT_OWNER_ID or str(interaction.user.id) in COMMAND_ADMINS:
            return True

        # Public commands: anyone can use these
        if root_name in PUBLIC_COMMANDS:
            return True

        # Admin commands: server Administrator permission required
        if root_name in ADMIN_COMMANDS:
            if interaction.guild is not None and interaction.permissions.administrator:
                return True
            embed = embed_err(
                "Permission Denied",
                "This command needs the `Administrator` permission and can only be used inside a server."
            )
        else:
            embed = discord.Embed(
                title="Permission Denied",
                description="These commands can only be used by the main staff.",
                color=0xF43F5E
            )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"[Owner Check Error] {e}")
        return False

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # The owner check already replied to the user, so ignore that failure quietly.
        if isinstance(error, app_commands.CheckFailure):
            return
        if isinstance(error, app_commands.CommandOnCooldown):
            cooldown_embed = embed_err("Slow Down", f"Try again in `{error.retry_after:.1f}s`.")
            try:
                await interaction.response.send_message(embed=cooldown_embed, ephemeral=True)
            except Exception:
                pass
            return
        print(f"[App Command Error] {error}")
        # Tell the user instead of leaving them with "application did not respond"
        embed = embed_err("Command Failed", f"Something went wrong:\n`{str(error)[:500]}`")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    # -------------------------------------------------------------
    # SLASH COMMAND: SET VERIFICATION OR UNSYNC INTERACTIVE PANELS
    # -------------------------------------------------------------
    set_group = app_commands.Group(name="set", description="Owner setup tools: panels and promo codes.")

    @set_group.command(name="panel", description="Deploy verification or un-sync interactive embed panels into a channel.")
    @app_commands.describe(
        action="Select whether to deploy the verification or un-sync panel.",
        channel="The target channel to send the embed panel into.",
        description="Custom description text for the panel."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Verification Panel", value="verification"),
        app_commands.Choice(name="Un-Sync Panel", value="un-sync")
    ])
    async def set_panel_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel,
        description: str
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            if action.value == "verification":
                embed = discord.Embed(
                    title="Roblox Account Verification",
                    description=description,
                    color=0x8B5CF6
                )
                embed.set_footer(text="Template Engine Roblox Synchronization System")
                view = RobloxVerificationView()
            else:
                embed = discord.Embed(
                    title="Un-Sync Roblox Account",
                    description=description,
                    color=0xF43F5E
                )
                embed.set_footer(text="Template Engine Account Management")
                view = RobloxUnsyncView()

            await channel.send(embed=embed, view=view)

            confirm_embed = discord.Embed(
                title="Panel Successfully Deployed",
                description=f"Deployed **{action.name}** into {channel.mention}.",
                color=0x2ECC71
            )
            await interaction.followup.send(embed=confirm_embed, ephemeral=True)

        except Exception as e:
            print(f"[Set Command Error] {e}")
            await interaction.followup.send(f"Failed to deploy panel: `{str(e)}`", ephemeral=True)

    # -------------------------------------------------------------
    # SLASH COMMAND: CHECK USER POINTS
    # -------------------------------------------------------------
    @app_commands.command(name="points", description="Check your or another member's current point balance.")
    @app_commands.describe(user="Optional user to check points for (defaults to yourself).")
    async def points_cmd(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await interaction.response.defer(ephemeral=False)
        target_user = user or interaction.user
        pts = get_user_points(str(target_user.id))

        embed = discord.Embed(
            title="Point Balance",
            description=f"{target_user.mention} currently has {pts:,} points.",
            color=0x8B5CF6
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------
    # SLASH COMMAND: LEADERBOARD
    # -------------------------------------------------------------
    @app_commands.command(name="leaderboard", description="View the top point earners in the server.")
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        top_users = get_top_leaderboard(10)

        if not top_users:
            embed = discord.Embed(
                title="Point Leaderboard",
                description="No active point rankings recorded yet!",
                color=0x8B5CF6
            )
            await interaction.followup.send(embed=embed)
            return

        description_lines = []
        for idx, entry in enumerate(top_users):
            rank_str = f"#{idx + 1}"
            uid = entry["discord_id"]
            pts = entry["points"]
            description_lines.append(f"{rank_str} <@{uid}> - {pts:,} pts")

        embed = discord.Embed(
            title="Point Leaderboard",
            description="\n".join(description_lines),
            color=0xF1C40F
        )
        embed.set_footer(text="Earn +2 points per message | +20 on join")
        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------
    # SLASH COMMAND: SET / MODIFY USER POINTS
    # -------------------------------------------------------------
    @app_commands.command(name="points-set", description="Add or deduct points for a specified user.")
    @app_commands.describe(
        user="The target user to modify points for.",
        action="Select whether to add or deduct points.",
        amount="The amount of points to add or deduct."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add Points", value="add"),
        app_commands.Choice(name="Deduct Points", value="deduct")
    ])
    async def points_set_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        action: app_commands.Choice[str],
        amount: int
    ):
        await interaction.response.defer(ephemeral=True)

        if amount <= 0:
            embed = discord.Embed(
                title="Invalid Amount",
                description="Please enter a positive point value.",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        delta = amount if action.value == "add" else -amount
        new_total = add_user_points(str(user.id), delta)

        action_label = "added to" if action.value == "add" else "deducted from"
        color = 0x2ECC71 if action.value == "add" else 0xF43F5E

        embed = discord.Embed(
            title="Points Balance Updated",
            description=f"Successfully {action_label} {user.mention}!\n\nAmount Changed: {'+' if delta > 0 else ''}{delta:,} pts\nNew Balance: {new_total:,} pts",
            color=color
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------
    # SLASH COMMAND: SET / REMOVE PREMIUM STATUS
    # -------------------------------------------------------------
    @app_commands.command(name="premium-set", description="Grant or revoke TE Premium subscription status for a user.")
    @app_commands.describe(
        user="Target user to modify premium status for.",
        action="Select whether to grant or revoke premium.",
        duration="Duration format e.g. 1d, 7d, 30d, 1y, inf (Defaults to 30d)."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Grant Premium", value="add"),
        app_commands.Choice(name="Revoke Premium", value="remove")
    ])
    async def premium_set_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        action: app_commands.Choice[str],
        duration: Optional[str] = "30d"
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            if action.value == "add":
                duration_td = parse_duration_to_timedelta(duration or "30d")
                set_user_premium(str(user.id), duration_td)
                exp_date = datetime.now(timezone.utc) + duration_td
                exp_ts = int(exp_date.timestamp())

                embed = discord.Embed(
                    title="TE Premium Granted",
                    description=f"Successfully granted TE Premium to {user.mention}!\n\nDuration: {duration}\nExpires: <t:{exp_ts}:F> (<t:{exp_ts}:R>)",
                    color=0xF1C40F
                )
            else:
                remove_user_premium(str(user.id))
                embed = discord.Embed(
                    title="TE Premium Revoked",
                    description=f"Successfully revoked TE Premium access from {user.mention}.",
                    color=0xF43F5E
                )

            embed.set_thumbnail(url=user.display_avatar.url)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[Premium Set Error] {e}")
            embed = discord.Embed(
                title="Error Modifying Premium Status",
                description=f"An error occurred: {str(e)}",
                color=0xF43F5E
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------
    # SLASH COMMAND: RUN LUAU CODE ON ROBLOX SERVERS
    # -------------------------------------------------------------
    @app_commands.command(name="game", description="Run Luau code on your Roblox game's servers (owner only).")
    @app_commands.describe(file="Optional .lua, .luau or .txt file (max 4 MB). Leave empty to type code in a window.")
    async def game_cmd(self, interaction: discord.Interaction, file: Optional[discord.Attachment] = None):
        # No file: open the code window
        if file is None:
            await interaction.response.send_modal(GameCodeModal(self))
            return

        await interaction.response.defer(ephemeral=True)

        if not file.filename.lower().endswith(GAME_ALLOWED_EXTENSIONS):
            await interaction.followup.send(
                embed=_game_error_embed("Wrong File Type", "Please attach a `.lua`, `.luau` or `.txt` file."),
                ephemeral=True
            )
            return

        if file.size > GAME_MAX_FILE_BYTES:
            await interaction.followup.send(
                embed=_game_error_embed("File Too Large", "The file must be 4 MB or smaller."),
                ephemeral=True
            )
            return

        try:
            raw = await file.read()
            code = raw.decode("utf-8-sig")
        except Exception as e:
            await interaction.followup.send(
                embed=_game_error_embed("Could Not Read File", f"The file must be UTF-8 text.\n`{e}`"),
                ephemeral=True
            )
            return

        if not code.strip():
            await interaction.followup.send(
                embed=_game_error_embed("Empty File", "The file has no code in it."),
                ephemeral=True
            )
            return

        await self.run_game_code(interaction, code)

    async def run_game_code(self, interaction: discord.Interaction, code: str):
        """Sends code to the Luau Execution API, waits for the task, then shows the result.
        The interaction must already be deferred (ephemeral)."""
        api_key = os.getenv("ROBLOX_OPEN_CLOUD_KEY", "").strip()
        if not api_key:
            await interaction.followup.send(
                embed=_game_error_embed("Missing API Key", "`ROBLOX_OPEN_CLOUD_KEY` is not set."),
                ephemeral=True
            )
            return

        place_id = os.getenv("ROBLOX_PLACE_ID", "").strip() or GAME_DEFAULT_PLACE_ID
        universe_id = os.getenv("ROBLOX_UNIVERSE_ID", "").strip()

        try:
            async with aiohttp.ClientSession(headers={"x-api-key": api_key}) as session:
                # Find the universe ID from the place ID if it was not set
                if not universe_id:
                    async with session.get(f"https://apis.roblox.com/universes/v1/places/{place_id}/universe") as resp:
                        if resp.status != 200:
                            await interaction.followup.send(
                                embed=_game_error_embed(
                                    "Universe Lookup Failed",
                                    f"Could not find the universe for place `{place_id}` (HTTP `{resp.status}`).\nSet `ROBLOX_UNIVERSE_ID` to skip this lookup."
                                ),
                                ephemeral=True
                            )
                            return
                        universe_id = str((await resp.json()).get("universeId") or "")
                    if not universe_id:
                        await interaction.followup.send(
                            embed=_game_error_embed("Universe Lookup Failed", f"No universe ID was returned for place `{place_id}`."),
                            ephemeral=True
                        )
                        return

                # Start the task
                create_url = f"{GAME_API_BASE}/universes/{universe_id}/places/{place_id}/luau-execution-session-tasks"
                async with session.post(create_url, json={"script": code}) as resp:
                    body = await resp.text()
                    if resp.status not in (200, 201):
                        await interaction.followup.send(
                            embed=_game_error_embed(
                                "Roblox Rejected the Request",
                                f"HTTP `{resp.status}`\n{_game_block(body, 900)}"
                            ),
                            ephemeral=True
                        )
                        return
                    task = json.loads(body)

                task_path = task.get("path", "")
                state = task.get("state", "")
                if not task_path:
                    await interaction.followup.send(
                        embed=_game_error_embed("Unexpected Response", f"Roblox did not return a task ID.\n{_game_block(body, 900)}"),
                        ephemeral=True
                    )
                    return

                # Check the task every 2 seconds, up to 90 seconds
                loop = asyncio.get_running_loop()
                deadline = loop.time() + GAME_POLL_TIMEOUT
                while state not in GAME_TERMINAL_STATES and loop.time() < deadline:
                    await asyncio.sleep(GAME_POLL_INTERVAL)
                    async with session.get(f"{GAME_API_BASE}/{task_path}") as resp:
                        if resp.status != 200:
                            await interaction.followup.send(
                                embed=_game_error_embed(
                                    "Status Check Failed",
                                    f"HTTP `{resp.status}` while checking task `{task_path.split('/')[-1]}`."
                                ),
                                ephemeral=True
                            )
                            return
                        task = await resp.json()
                        state = task.get("state", "")

                # Read the logs (anything the code printed)
                log_lines = []
                page_token = ""
                for _ in range(5):
                    params = {"pageToken": page_token} if page_token else None
                    async with session.get(f"{GAME_API_BASE}/{task_path}/logs", params=params) as resp:
                        if resp.status != 200:
                            break
                        log_data = await resp.json()
                    for entry in log_data.get("luauExecutionSessionTaskLogs", []):
                        log_lines.extend(entry.get("messages", []))
                    page_token = log_data.get("nextPageToken", "")
                    if not page_token:
                        break

        except Exception as e:
            print(f"[Game Command Error] {e}")
            await interaction.followup.send(
                embed=_game_error_embed("Game Command Error", f"`{e}`"),
                ephemeral=True
            )
            return

        # Build the result embed
        task_id = task_path.split("/")[-1]
        finished = state in GAME_TERMINAL_STATES
        state_label = state if finished else "Still Running"
        color = 0x2ECC71 if state == "COMPLETE" else (0xF43F5E if finished else 0xF1C40F)

        results = (task.get("output") or {}).get("results", [])
        results_text = "\n".join(json.dumps(r, ensure_ascii=False) for r in results)

        embed = discord.Embed(title="Luau Execution Result", color=color)
        embed.add_field(name="Output", value=_game_block("\n".join(log_lines) or "(no output)"), inline=False)
        embed.add_field(name="Return Values", value=_game_block(results_text or "(none)"), inline=False)

        err = task.get("error")
        if err:
            err_text = f"{err.get('code', 'ERROR')}: {err.get('message', '')}" if isinstance(err, dict) else str(err)
            embed.add_field(name="Error", value=_game_block(err_text), inline=False)

        embed.add_field(name="Task ID", value=f"`{task_id}`", inline=True)
        embed.add_field(name="State", value=f"`{state_label}`", inline=True)
        if not finished:
            embed.set_footer(text="Took longer than 90 seconds. The task may still finish on Roblox.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # =============================================================
    # ROBLOX COMMANDS
    # =============================================================
    @app_commands.command(name="revenue", description="Show your Roblox group's revenue (owner only).")
    @app_commands.describe(period="Time period to show (default: Month).")
    @app_commands.choices(period=[
        app_commands.Choice(name="Last 24 hours", value="Day"),
        app_commands.Choice(name="Last 7 days", value="Week"),
        app_commands.Choice(name="Last 30 days", value="Month"),
        app_commands.Choice(name="Last 365 days", value="Year"),
    ])
    async def revenue_cmd(self, interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(ephemeral=True)

        group_id = os.getenv("ROBLOX_GROUP_ID", "").strip()
        cookie_headers = get_roblox_cookie_header()
        if not group_id:
            await interaction.followup.send(embed=embed_err("Missing Group ID", "`ROBLOX_GROUP_ID` is not set."), ephemeral=True)
            return
        if not cookie_headers:
            await interaction.followup.send(embed=embed_err("Missing Cookie", "`ROBLOX_COOKIE` is not set."), ephemeral=True)
            return

        frame = period.value if period else "Month"
        frame_label = period.name if period else "Last 30 days"

        async with aiohttp.ClientSession() as session:
            status, summary = await roblox_get(session, f"https://economy.roblox.com/v1/groups/{group_id}/revenue/summary/{frame}", headers=cookie_headers)
            _, funds = await roblox_get(session, f"https://economy.roblox.com/v1/groups/{group_id}/currency", headers=cookie_headers)
            _, info = await roblox_get(session, f"https://groups.roblox.com/v1/groups/{group_id}")

        if status != 200 or not isinstance(summary, dict):
            hint = " The cookie may be expired, or that account cannot view group revenue." if status in (401, 403) else ""
            await interaction.followup.send(embed=embed_err("Could Not Fetch Revenue", f"Roblox returned HTTP `{status}`.{hint}"), ephemeral=True)
            return

        group_name = (info or {}).get("name") or f"Group {group_id}"
        embed = discord.Embed(title=f"{group_name} | Revenue", description=f"Period: **{frame_label}**", color=COLOR_GOLD)

        for key, value in summary.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            label = re.sub(r"(?<!^)(?=[A-Z])", " ", key).replace(" Robux", "").title()
            embed.add_field(name=label, value=f"`{value:,} R$`", inline=True)

        if isinstance(funds, dict) and isinstance(funds.get("robux"), (int, float)):
            embed.add_field(name="Group Funds", value=f"`{funds['robux']:,} R$`", inline=False)
        embed.add_field(name="Group ID", value=f"`{group_id}`", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="roblox-user", description="Look up a Roblox account by username.")
    @app_commands.describe(username="The Roblox username to look up.")
    async def roblox_user_cmd(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer()

        async with aiohttp.ClientSession() as session:
            found = await resolve_roblox_user(session, username)
            if not found:
                await interaction.followup.send(embed=embed_err("User Not Found", f"No Roblox user named `{username[:50]}`."))
                return

            uid = found["id"]
            _, profile = await roblox_get(session, f"https://users.roblox.com/v1/users/{uid}")
            _, friends = await roblox_get(session, f"https://friends.roblox.com/v1/users/{uid}/friends/count")
            _, followers = await roblox_get(session, f"https://friends.roblox.com/v1/users/{uid}/followers/count")
            _, following = await roblox_get(session, f"https://friends.roblox.com/v1/users/{uid}/followings/count")
            _, thumb = await roblox_get(
                session, "https://thumbnails.roblox.com/v1/users/avatar-headshot",
                params={"userIds": uid, "size": "420x420", "format": "Png"}
            )

        if not isinstance(profile, dict):
            await interaction.followup.send(embed=embed_err("Lookup Failed", "Roblox did not return the profile. Try again later."))
            return

        name = profile.get("name", found.get("name", username))
        display = profile.get("displayName") or name
        bio = (profile.get("description") or "").strip() or "No bio"
        created = iso_to_ts(profile.get("created"))

        def count(data):
            return f"`{data['count']:,}`" if isinstance(data, dict) and "count" in data else "`Unknown`"

        embed = discord.Embed(
            title=f"{display} (@{name})",
            url=f"https://www.roblox.com/users/{uid}/profile",
            description=bio[:1000],
            color=COLOR_PURPLE
        )
        embed.add_field(name="User ID", value=f"`{uid}`", inline=True)
        embed.add_field(name="Username", value=f"`{name}`", inline=True)
        embed.add_field(name="Display Name", value=f"`{display}`", inline=True)
        embed.add_field(name="Banned", value="`Yes`" if profile.get("isBanned") else "`No`", inline=True)
        embed.add_field(name="Created", value=f"<t:{created}:F> (<t:{created}:R>)" if created else "`Unknown`", inline=False)
        embed.add_field(name="Friends", value=count(friends), inline=True)
        embed.add_field(name="Followers", value=count(followers), inline=True)
        embed.add_field(name="Following", value=count(following), inline=True)

        try:
            image_url = thumb["data"][0].get("imageUrl")
            if image_url:
                embed.set_thumbnail(url=image_url)
        except Exception:
            pass
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="cookie-check", description="Check if the saved Roblox cookie is still valid (owner only).")
    async def cookie_check_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        cookie_headers = get_roblox_cookie_header()
        if not cookie_headers:
            await interaction.followup.send(embed=embed_err("Missing Cookie", "`ROBLOX_COOKIE` is not set."), ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            status, me = await roblox_get(session, "https://users.roblox.com/v1/users/authenticated", headers=cookie_headers)
            robux = None
            if status == 200 and isinstance(me, dict) and me.get("id"):
                _, bal = await roblox_get(session, f"https://economy.roblox.com/v1/users/{me['id']}/currency", headers=cookie_headers)
                if isinstance(bal, dict):
                    robux = bal.get("robux")

        if status == 200 and isinstance(me, dict) and me.get("id"):
            embed = embed_ok("Cookie Is Valid", "The saved cookie is working.")
            embed.add_field(name="Account", value=f"`{me.get('name')}`", inline=True)
            embed.add_field(name="Display Name", value=f"`{me.get('displayName')}`", inline=True)
            embed.add_field(name="User ID", value=f"`{me.get('id')}`", inline=True)
            if robux is not None:
                embed.add_field(name="Robux", value=f"`{robux:,} R$`", inline=True)
            embed.add_field(name="Group ID", value=f"`{os.getenv('ROBLOX_GROUP_ID', '').strip() or 'not set'}`", inline=True)
        elif status in (401, 403):
            embed = embed_err("Cookie Is Invalid", f"Roblox rejected the cookie (HTTP `{status}`). It is expired or logged out. Set a new `ROBLOX_COOKIE`.")
        else:
            embed = embed_err("Could Not Check Cookie", f"Roblox returned HTTP `{status}`. Try again later.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="groupcheck", description="Check if a Roblox user is in your group.")
    @app_commands.describe(user="The Roblox username to check.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def groupcheck_cmd(self, interaction: discord.Interaction, user: str):
        await interaction.response.defer(ephemeral=True)

        group_id = os.getenv("ROBLOX_GROUP_ID", "").strip()
        if not group_id:
            await interaction.followup.send(embed=embed_err("Missing Group ID", "`ROBLOX_GROUP_ID` is not set."), ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            found = await resolve_roblox_user(session, user)
            if not found:
                await interaction.followup.send(embed=embed_err("User Not Found", f"No Roblox user named `{user[:50]}`."), ephemeral=True)
                return
            uid = found["id"]
            status, data = await roblox_get(
                session, f"https://groups.roblox.com/v2/users/{uid}/groups/roles",
                headers=get_roblox_cookie_header() or None
            )
            _, info = await roblox_get(session, f"https://groups.roblox.com/v1/groups/{group_id}")

        if status != 200 or not isinstance(data, dict):
            await interaction.followup.send(embed=embed_err("Lookup Failed", f"Roblox returned HTTP `{status}`."), ephemeral=True)
            return

        group_name = (info or {}).get("name") or f"Group {group_id}"
        match = next((g for g in data.get("data", []) if str(g.get("group", {}).get("id")) == group_id), None)

        if match:
            role = match.get("role", {})
            embed = embed_ok("Group Member", f"**{found['name']}** is in **{group_name}**.")
            embed.add_field(name="Role", value=f"`{role.get('name', 'Unknown')}`", inline=True)
            embed.add_field(name="Rank", value=f"`{role.get('rank', '?')}`", inline=True)
        else:
            embed = embed_err("Not In Group", f"**{found['name']}** is not in **{group_name}**.")
        embed.add_field(name="User ID", value=f"`{uid}`", inline=True)
        embed.add_field(name="Group ID", value=f"`{group_id}`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="gameinfo", description="Show info about a Roblox game.")
    @app_commands.describe(game="Game name, place ID, universe ID or a roblox.com/games link.")
    async def gameinfo_cmd(self, interaction: discord.Interaction, game: str):
        await interaction.response.defer()

        query = game.strip()
        link = re.search(r"games/(\d+)", query)
        if link:
            query = link.group(1)

        async with aiohttp.ClientSession() as session:
            universe_id = None
            if query.isdigit():
                status, data = await roblox_get(session, f"https://apis.roblox.com/universes/v1/places/{query}/universe")
                if status == 200 and isinstance(data, dict) and data.get("universeId"):
                    universe_id = str(data["universeId"])
                else:
                    universe_id = query  # maybe it is already a universe ID
            else:
                _, data = await roblox_get(
                    session, "https://apis.roblox.com/search-api/omni-search",
                    params={"searchQuery": query, "sessionId": str(uuid.uuid4()), "pageType": "all"}
                )
                for group in (data or {}).get("searchResults", []):
                    for item in group.get("contents", []):
                        if item.get("universeId"):
                            universe_id = str(item["universeId"])
                            break
                    if universe_id:
                        break

            if not universe_id:
                await interaction.followup.send(embed=embed_err("Game Not Found", f"No game found for `{query[:80]}`."))
                return

            _, details = await roblox_get(session, "https://games.roblox.com/v1/games", params={"universeIds": universe_id})
            _, votes = await roblox_get(session, "https://games.roblox.com/v1/games/votes", params={"universeIds": universe_id})

        try:
            g = details["data"][0]
        except Exception:
            await interaction.followup.send(embed=embed_err("Game Not Found", f"No game found for `{query[:80]}`."))
            return

        place_id = g.get("rootPlaceId")
        embed = discord.Embed(
            title=g.get("name", "Unknown Game"),
            url=f"https://www.roblox.com/games/{place_id}" if place_id else None,
            description=(g.get("description") or "No description")[:500],
            color=COLOR_PURPLE
        )
        embed.add_field(name="Playing", value=f"`{g.get('playing', 0):,}`", inline=True)
        embed.add_field(name="Visits", value=f"`{g.get('visits', 0):,}`", inline=True)
        embed.add_field(name="Favorites", value=f"`{g.get('favoritedCount', 0):,}`", inline=True)

        try:
            up, down = votes["data"][0]["upVotes"], votes["data"][0]["downVotes"]
            total = up + down
            ratio = f"`{round(up / total * 100)}%` ({up:,} up / {down:,} down)" if total else "`No votes`"
            embed.add_field(name="Rating", value=ratio, inline=False)
        except Exception:
            pass

        embed.add_field(name="Max Players", value=f"`{g.get('maxPlayers', '?')}`", inline=True)
        creator = g.get("creator") or {}
        embed.add_field(name="Creator", value=f"`{creator.get('name', 'Unknown')}` ({creator.get('type', '?')})", inline=True)
        created, updated = iso_to_ts(g.get("created")), iso_to_ts(g.get("updated"))
        if created:
            embed.add_field(name="Created", value=f"<t:{created}:D>", inline=True)
        if updated:
            embed.add_field(name="Last Updated", value=f"<t:{updated}:R>", inline=True)
        embed.add_field(name="Universe ID", value=f"`{universe_id}`", inline=True)
        if place_id:
            embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
        await interaction.followup.send(embed=embed)

    # =============================================================
    # UTILITY COMMANDS
    # =============================================================
    @app_commands.command(name="remind", description="Set a reminder. The bot will DM you when it is time.")
    @app_commands.describe(time="When to remind you, e.g. 10m, 2h, 1d, 1h30m.", message="What to remind you about.")
    async def remind_cmd(self, interaction: discord.Interaction, time: str, message: app_commands.Range[str, 1, 1500]):
        delta = parse_duration_strict(time)
        if delta is None or delta < timedelta(seconds=10) or delta > timedelta(days=365):
            await interaction.response.send_message(
                embed=embed_err("Invalid Time", "Use a time between `10s` and `1y`, for example `10m`, `2h`, `1d` or `1h30m`."),
                ephemeral=True
            )
            return
        if count_user_reminders(interaction.user.id) >= MAX_REMINDERS_PER_USER:
            await interaction.response.send_message(
                embed=embed_err("Too Many Reminders", f"You can have at most `{MAX_REMINDERS_PER_USER}` active reminders."),
                ephemeral=True
            )
            return

        due_ts = datetime.now(timezone.utc).timestamp() + delta.total_seconds()
        reminder = add_reminder(interaction.user.id, interaction.channel_id or 0, message, due_ts)
        embed = embed_ok("Reminder Set", f"I will remind you <t:{int(due_ts)}:R> (in `{format_timedelta(delta)}`).")
        embed.add_field(name="Message", value=message[:1000], inline=False)
        embed.add_field(name="Reminder ID", value=f"`{reminder['id']}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="afk", description="Set yourself as AFK. Pinging you shows your reason; talking removes AFK.")
    @app_commands.describe(reason="Why you are AFK.")
    @app_commands.guild_only()
    async def afk_cmd(self, interaction: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 200]] = "AFK"):
        set_afk(interaction.guild.id, interaction.user.id, reason or "AFK")
        embed = embed_info("You Are Now AFK", f"{interaction.user.mention} is now AFK: {reason}")
        embed.set_footer(text="Send any message to remove your AFK.")
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    @app_commands.command(name="timestamp", description="Get Discord timestamp codes you can copy.")
    @app_commands.describe(time="Optional: 2h, 1d, a UTC date like 2026-12-25 18:00, or a unix time. Empty = now.")
    async def timestamp_cmd(self, interaction: discord.Interaction, time: Optional[str] = None):
        now = datetime.now(timezone.utc)
        target = now
        if time and time.strip():
            text = time.strip()
            delta = parse_duration_strict(text)
            if delta is not None:
                target = now + delta
            elif text.isdigit():
                try:
                    target = datetime.fromtimestamp(int(text), tz=timezone.utc)
                except Exception:
                    target = None
            else:
                target = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        target = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
            if target is None:
                await interaction.response.send_message(
                    embed=embed_err("Invalid Time", "Use a duration like `2h`, a date like `2026-12-25 18:00` (UTC), or a unix time."),
                    ephemeral=True
                )
                return

        ts = int(target.timestamp())
        styles = [("t", "Short time"), ("T", "Long time"), ("d", "Short date"), ("D", "Long date"),
                  ("f", "Short date/time"), ("F", "Long date/time"), ("R", "Relative")]
        lines = [f"**{label}**: <t:{ts}:{code}>\n`<t:{ts}:{code}>`" for code, label in styles]
        embed = embed_info("Discord Timestamps", "\n\n".join(lines))
        embed.add_field(name="Unix Time", value=f"`{ts}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="whois", description="Show information about a member.")
    @app_commands.describe(user="The member to look up (defaults to you).")
    @app_commands.guild_only()
    async def whois_cmd(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        member = user or interaction.user
        embed = discord.Embed(title=str(member), description=member.mention, color=member.color if member.color.value else COLOR_PURPLE)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Username", value=f"`{member.name}`", inline=True)
        embed.add_field(name="Nickname", value=f"`{member.nick}`" if member.nick else "`None`", inline=True)
        embed.add_field(name="Bot", value="`Yes`" if member.bot else "`No`", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(member.created_at.timestamp())}:F>\n(<t:{int(member.created_at.timestamp())}:R>)", inline=False)
        if member.joined_at:
            embed.add_field(name="Joined", value=f"<t:{int(member.joined_at.timestamp())}:F>\n(<t:{int(member.joined_at.timestamp())}:R>)", inline=False)
        if member.is_timed_out():
            embed.add_field(name="Timed Out Until", value=f"<t:{int(member.timed_out_until.timestamp())}:R>", inline=False)

        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        roles_text = ", ".join(roles) if roles else "None"
        if len(roles_text) > 1000:
            roles_text = roles_text[:1000].rsplit(",", 1)[0] + ", ..."
        embed.add_field(name=f"Roles ({len(roles)})", value=roles_text, inline=False)
        if not member.top_role.is_default():
            embed.add_field(name="Highest Role", value=member.top_role.mention, inline=True)
        if member.guild_permissions.administrator:
            embed.add_field(name="Key Permission", value="`Administrator`", inline=True)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    @app_commands.command(name="permissions", description="Show a member's permissions in this channel.")
    @app_commands.describe(user="The member to check (defaults to you).")
    @app_commands.guild_only()
    async def permissions_cmd(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        member = user or interaction.user
        perms = interaction.channel.permissions_for(member)
        granted = sorted(pretty_perm(name) for name, value in perms if value)
        denied = sorted(pretty_perm(name) for name, value in perms if not value)

        embed = embed_info(f"Permissions | {member.display_name}", f"{member.mention} in {interaction.channel.mention}")
        if perms.administrator:
            embed.add_field(name="Administrator", value="This member has every permission.", inline=False)
        embed.add_field(name=f"Allowed ({len(granted)})", value="```\n" + ("\n".join(granted) or "None")[:950] + "\n```", inline=False)
        embed.add_field(name=f"Denied ({len(denied)})", value="```\n" + ("\n".join(denied) or "None")[:950] + "\n```", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="rolelist", description="List every role in the server, one per line.")
    @app_commands.guild_only()
    async def rolelist_cmd(self, interaction: discord.Interaction):
        roles = [r for r in reversed(interaction.guild.roles) if not r.is_default()]
        lines = [f"{r.mention} | `{r.id}` | {len(r.members)} members" for r in roles]
        pages = build_pages(f"Roles in {interaction.guild.name}", lines, per_page=20, header=f"Total roles: `{len(roles)}`\n\n")
        await send_pages(interaction, pages, ephemeral=False)

    @app_commands.command(name="channelstats", description="Show channel counts for this server.")
    @app_commands.guild_only()
    async def channelstats_cmd(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        embed = embed_info(f"Channel Stats | {guild.name}")
        embed.add_field(name="Total Channels", value=f"`{len(guild.channels)}`", inline=True)
        embed.add_field(name="Categories", value=f"`{len(guild.categories)}`", inline=True)
        embed.add_field(name="Text", value=f"`{len(guild.text_channels)}`", inline=True)
        embed.add_field(name="Voice", value=f"`{len(guild.voice_channels)}`", inline=True)
        embed.add_field(name="Stage", value=f"`{len(guild.stage_channels)}`", inline=True)
        embed.add_field(name="Forum", value=f"`{len(guild.forums)}`", inline=True)
        embed.add_field(name="Active Threads", value=f"`{len(guild.threads)}`", inline=True)

        details = [f"Channel: {channel.mention}", f"ID: `{channel.id}`"]
        if getattr(channel, "created_at", None):
            details.append(f"Created: <t:{int(channel.created_at.timestamp())}:R>")
        if isinstance(channel, discord.TextChannel):
            details.append(f"Slowmode: `{channel.slowmode_delay}s`")
            details.append(f"NSFW: `{'Yes' if channel.is_nsfw() else 'No'}`")
            if channel.category:
                details.append(f"Category: `{channel.category.name}`")
        embed.add_field(name="This Channel", value="\n".join(details), inline=False)
        await interaction.response.send_message(embed=embed)

    # =============================================================
    # ADMIN UTILITY COMMANDS
    # =============================================================
    @app_commands.command(name="slowmode", description="Set slowmode for a channel. Use off or 0 to disable.")
    @app_commands.describe(duration="e.g. 5s, 30s, 2m, 1h (max 6h) or off.", channel="Channel (defaults to this one).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def slowmode_cmd(self, interaction: discord.Interaction, duration: str, channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=embed_err("Wrong Channel", "Slowmode can only be set on text channels."), ephemeral=True)
            return

        if duration.strip().lower() in ("off", "0", "none", "disable"):
            seconds = 0
        else:
            delta = parse_duration_strict(duration)
            if delta is None or delta.total_seconds() > 21600:
                await interaction.response.send_message(embed=embed_err("Invalid Duration", "Use `5s`, `30s`, `2m`, `1h` (max `6h`) or `off`."), ephemeral=True)
                return
            seconds = int(delta.total_seconds())

        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I need `Manage Channels` in that channel."), ephemeral=True)
            return
        text = f"Slowmode in {channel.mention} is now `{format_timedelta(timedelta(seconds=seconds))}`." if seconds else f"Slowmode in {channel.mention} is now off."
        await interaction.response.send_message(embed=embed_ok("Slowmode Updated", text), ephemeral=True)

    @app_commands.command(name="nick", description="Change or reset a member's nickname.")
    @app_commands.describe(user="The member to rename.", nickname="New nickname. Leave empty to reset.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def nick_cmd(self, interaction: discord.Interaction, user: discord.Member, nickname: Optional[app_commands.Range[str, 1, 32]] = None):
        problem = None if user.id == interaction.user.id else check_hierarchy(interaction, user)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Change Nickname", problem), ephemeral=True)
            return
        try:
            await user.edit(nick=nickname, reason=f"Nickname changed by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I cannot change that member's nickname."), ephemeral=True)
            return
        text = f"{user.mention}'s nickname is now `{nickname}`." if nickname else f"{user.mention}'s nickname was reset."
        await interaction.response.send_message(embed=embed_ok("Nickname Updated", text), ephemeral=True, allowed_mentions=NO_PINGS)

    role_group = app_commands.Group(
        name="role", description="Add or remove roles.", guild_only=True,
        default_permissions=discord.Permissions(administrator=True)
    )

    def _role_problem(self, interaction: discord.Interaction, role: discord.Role) -> Optional[str]:
        guild = interaction.guild
        if role.is_default():
            return "You cannot use `@everyone`."
        if role.managed:
            return "That role is managed by an integration and cannot be assigned."
        if role >= guild.me.top_role:
            return "That role is above or equal to my highest role. Move my role higher."
        if interaction.user.id not in (BOT_OWNER_ID, guild.owner_id) and role >= interaction.user.top_role:
            return "That role is the same as or above your highest role."
        return None

    @role_group.command(name="add", description="Give a role to a member.")
    @app_commands.describe(user="The member.", role="The role to give.")
    async def role_add_cmd(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        problem = self._role_problem(interaction, role)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Add Role", problem), ephemeral=True)
            return
        if role in user.roles:
            await interaction.response.send_message(embed=embed_err("Already Has Role", f"{user.mention} already has {role.mention}."), ephemeral=True, allowed_mentions=NO_PINGS)
            return
        try:
            await user.add_roles(role, reason=f"Role added by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I could not add that role."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_ok("Role Added", f"Gave {role.mention} to {user.mention}."), ephemeral=True, allowed_mentions=NO_PINGS)

    @role_group.command(name="remove", description="Remove a role from a member.")
    @app_commands.describe(user="The member.", role="The role to remove.")
    async def role_remove_cmd(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        problem = self._role_problem(interaction, role)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Remove Role", problem), ephemeral=True)
            return
        if role not in user.roles:
            await interaction.response.send_message(embed=embed_err("Does Not Have Role", f"{user.mention} does not have {role.mention}."), ephemeral=True, allowed_mentions=NO_PINGS)
            return
        try:
            await user.remove_roles(role, reason=f"Role removed by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I could not remove that role."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_ok("Role Removed", f"Removed {role.mention} from {user.mention}."), ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="say", description="Send a message as the bot.")
    @app_commands.describe(message="What the bot should say.", channel="Channel (defaults to this one).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def say_cmd(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 2000], channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        except Exception as e:
            await interaction.response.send_message(embed=embed_err("Could Not Send", f"`{e}`"), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_ok("Message Sent", f"Posted in {channel.mention}."), ephemeral=True)

    @app_commands.command(name="embed", description="Create and send a custom embed.")
    @app_commands.describe(channel="Channel to send the embed to (defaults to this one).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def embed_cmd(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await interaction.response.send_modal(EmbedBuilderModal(channel or interaction.channel))

    @app_commands.command(name="announce", description="Send an announcement embed, with an optional ping.")
    @app_commands.describe(channel="Where to post the announcement.", ping="Optional @everyone or @here ping.", role="Optional role to ping.")
    @app_commands.choices(ping=[
        app_commands.Choice(name="No ping", value="none"),
        app_commands.Choice(name="@everyone", value="everyone"),
        app_commands.Choice(name="@here", value="here"),
    ])
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def announce_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        ping: Optional[app_commands.Choice[str]] = None,
        role: Optional[discord.Role] = None
    ):
        await interaction.response.send_modal(AnnounceModal(channel, ping.value if ping else "none", role))

    # =============================================================
    # MODERATION COMMANDS (server Administrator only)
    # The moderator is never shown to the moderated user (DM has no moderator).
    # =============================================================
    def _mod_result(self, title: str, user, case: dict, dm_sent: Optional[bool], extra: Optional[dict] = None) -> discord.Embed:
        embed = embed_ok(title, f"{user.mention} (`{user.id}`)")
        embed.add_field(name="Case ID", value=f"`#{case['case_id']}`", inline=True)
        for name, value in (extra or {}).items():
            embed.add_field(name=name, value=value, inline=True)
        embed.add_field(name="Reason", value=case["reason"][:1000], inline=False)
        if dm_sent is not None:
            embed.set_footer(text="User was notified by DM." if dm_sent else "Could not DM the user (DMs are closed).")
        return embed

    @app_commands.command(name="warn", description="Warn a member. They get a DM and the warning is logged.")
    @app_commands.describe(user="The member to warn.", reason="Why they are being warned.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def warn_cmd(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 500]):
        problem = check_hierarchy(interaction, user)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Warn", problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = add_case(interaction.guild.id, user.id, "warn", reason, interaction.user.id)
        dm_sent = await dm_moderation_notice(user, interaction.guild, "warn", reason, case["case_id"])
        await interaction.followup.send(embed=self._mod_result("Member Warned", user, case, dm_sent), ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="mute", description="Time out a member for a set duration.")
    @app_commands.describe(user="The member to mute.", duration="e.g. 10m, 2h, 1d (max 28d).", reason="Why they are being muted.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def mute_cmd(
        self, interaction: discord.Interaction, user: discord.Member, duration: str,
        reason: Optional[app_commands.Range[str, 1, 500]] = "No reason provided"
    ):
        delta = parse_duration_strict(duration)
        if delta is None or delta > timedelta(days=28):
            await interaction.response.send_message(embed=embed_err("Invalid Duration", "Use a duration from `1s` to `28d`, for example `10m`, `2h` or `1d`."), ephemeral=True)
            return
        problem = check_hierarchy(interaction, user)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Mute", problem), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await user.timeout(delta, reason=f"{reason} | by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need the `Moderate Members` permission."), ephemeral=True)
            return

        case = add_case(interaction.guild.id, user.id, "mute", reason, interaction.user.id, duration=format_timedelta(delta))
        dm_sent = await dm_moderation_notice(user, interaction.guild, "mute", reason, case["case_id"], duration=format_timedelta(delta))
        until = int((datetime.now(timezone.utc) + delta).timestamp())
        await interaction.followup.send(
            embed=self._mod_result("Member Muted", user, case, dm_sent, {"Duration": f"`{format_timedelta(delta)}`", "Ends": f"<t:{until}:R>"}),
            ephemeral=True, allowed_mentions=NO_PINGS
        )

    @app_commands.command(name="unmute", description="Remove a member's timeout.")
    @app_commands.describe(user="The member to unmute.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def unmute_cmd(self, interaction: discord.Interaction, user: discord.Member):
        if not user.is_timed_out():
            await interaction.response.send_message(embed=embed_err("Not Muted", f"{user.mention} is not muted."), ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await user.timeout(None, reason=f"Unmuted by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need the `Moderate Members` permission."), ephemeral=True)
            return
        case = add_case(interaction.guild.id, user.id, "unmute", "Mute removed", interaction.user.id)
        dm_sent = await dm_moderation_notice(user, interaction.guild, "unmute", "Your mute was removed.", case["case_id"])
        await interaction.followup.send(embed=self._mod_result("Member Unmuted", user, case, dm_sent), ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="kick", description="Kick a member. They get a DM first.")
    @app_commands.describe(user="The member to kick.", reason="Why they are being kicked.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def kick_cmd(self, interaction: discord.Interaction, user: discord.Member, reason: Optional[app_commands.Range[str, 1, 500]] = "No reason provided"):
        problem = check_hierarchy(interaction, user)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Kick", problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        case_id = None
        # DM first: after a kick the bot may no longer be able to reach the user
        case = add_case(interaction.guild.id, user.id, "kick", reason, interaction.user.id)
        case_id = case["case_id"]
        dm_sent = await dm_moderation_notice(user, interaction.guild, "kick", reason, case_id)
        try:
            await user.kick(reason=f"{reason} | by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            delete_cases(interaction.guild.id, user.id, "kick", case_id)
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need the `Kick Members` permission."), ephemeral=True)
            return
        await interaction.followup.send(embed=self._mod_result("Member Kicked", user, case, dm_sent), ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="ban", description="Ban a user. They get a DM first.")
    @app_commands.describe(user="The user to ban.", reason="Why they are being banned.", delete_days="Delete their messages from the last 0-7 days.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def ban_cmd(
        self, interaction: discord.Interaction, user: discord.User,
        reason: Optional[app_commands.Range[str, 1, 500]] = "No reason provided",
        delete_days: Optional[app_commands.Range[int, 0, 7]] = 0
    ):
        member = interaction.guild.get_member(user.id)
        if member:
            problem = check_hierarchy(interaction, member)
            if problem:
                await interaction.response.send_message(embed=embed_err("Cannot Ban", problem), ephemeral=True)
                return
        elif user.id in (interaction.user.id, interaction.guild.me.id):
            await interaction.response.send_message(embed=embed_err("Cannot Ban", "You cannot use this on that user."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        case = add_case(interaction.guild.id, user.id, "ban", reason, interaction.user.id)
        dm_sent = await dm_moderation_notice(user, interaction.guild, "ban", reason, case["case_id"]) if member else None
        try:
            await interaction.guild.ban(
                user, reason=f"{reason} | by {interaction.user} ({interaction.user.id})",
                delete_message_seconds=int(delete_days or 0) * 86400
            )
        except discord.Forbidden:
            delete_cases(interaction.guild.id, user.id, "ban", case["case_id"])
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need the `Ban Members` permission."), ephemeral=True)
            return
        await interaction.followup.send(embed=self._mod_result("User Banned", user, case, dm_sent), ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="unban", description="Unban a user by ID or username.")
    @app_commands.describe(user="The user ID (best) or username of the banned user.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def unban_cmd(self, interaction: discord.Interaction, user: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        wanted = user.strip().lower().lstrip("@")
        target = None
        try:
            async for entry in guild.bans(limit=None):
                u = entry.user
                if str(u.id) == wanted or u.name.lower() == wanted or str(u).lower() == wanted:
                    target = u
                    break
        except discord.Forbidden:
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need the `Ban Members` permission to read the ban list."), ephemeral=True)
            return

        if target is None:
            await interaction.followup.send(embed=embed_err("Not Banned", f"No banned user matches `{user[:50]}`."), ephemeral=True)
            return

        try:
            await guild.unban(target, reason=f"Unbanned by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(embed=embed_err("Missing Permission", "I could not unban that user."), ephemeral=True)
            return
        case = add_case(guild.id, target.id, "unban", "Ban removed", interaction.user.id)
        await interaction.followup.send(embed=self._mod_result("User Unbanned", target, case, None), ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="clear", description="Remove a moderation case from the log by its case ID.")
    @app_commands.describe(case_id="The case ID from /modlog or from the moderation result, e.g. 12.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def clear_cmd(self, interaction: discord.Interaction, case_id: app_commands.Range[int, 1, 100000000]):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        case = get_case(guild.id, case_id)
        if not case:
            await interaction.followup.send(embed=embed_err("Case Not Found", f"This server has no case with ID `#{case_id}`."), ephemeral=True)
            return

        if not delete_case(guild.id, case_id):
            await interaction.followup.send(embed=embed_err("Could Not Remove Case", f"Case `#{case_id}` could not be removed. Try again."), ephemeral=True)
            return

        label = MOD_ACTION_LABELS.get(case["action"], case["action"].title())
        embed = embed_ok("Case Removed", f"Case `#{case_id}` (**{label}**) for <@{case['user_id']}> was removed from the log.")
        embed.add_field(name="User ID", value=f"`{case['user_id']}`", inline=True)
        embed.add_field(name="Reason", value=str(case["reason"])[:500] or "None", inline=False)

        # Removing a record does not undo the punishment itself, so say so if it is still active
        notes = []
        try:
            uid = int(case["user_id"])
            if case["action"] == "mute":
                member = guild.get_member(uid)
                if member and member.is_timed_out():
                    notes.append("This user is still muted. Use `/unmute` to remove the mute.")
            elif case["action"] == "ban":
                try:
                    await guild.fetch_ban(discord.Object(id=uid))
                    notes.append("This user is still banned. Use `/unban` to unban them.")
                except discord.NotFound:
                    pass
        except Exception as e:
            print(f"[Clear Note Error] {e}")
        if notes:
            embed.add_field(name="Note", value="\n".join(notes), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="purge", description="Delete recent messages in this channel (old messages too, but slower).")
    @app_commands.describe(amount="How many messages to delete (1-1000).", user="Only delete messages from this user.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def purge_cmd(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000], user: Optional[discord.User] = None):
        channel = interaction.channel
        if not hasattr(channel, "purge"):
            await interaction.response.send_message(embed=embed_err("Wrong Channel", "I cannot purge messages in this channel."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        def should_delete(m: discord.Message) -> bool:
            if m.pinned:
                return False
            return user is None or m.author.id == user.id

        try:
            # Messages older than 14 days cannot be bulk deleted. discord.py deletes those one by one (slow).
            deleted = await channel.purge(limit=amount, check=should_delete, bulk=True, reason=f"Purge by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need `Manage Messages` and `Read Message History` in this channel."), ephemeral=True)
            return

        embed = embed_ok("Messages Purged", f"Deleted `{len(deleted)}` message(s) in {channel.mention}.")
        embed.set_footer(text="Pinned messages are skipped.")
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            pass  # the interaction can expire during a very long purge

    @app_commands.command(name="lock", description="Stop @everyone from sending messages in a channel.")
    @app_commands.describe(channel="Channel to lock (defaults to this one).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def lock_cmd(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=embed_err("Wrong Channel", "Only text channels can be locked."), ephemeral=True)
            return
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        if overwrite.send_messages is False:
            await interaction.response.send_message(embed=embed_err("Already Locked", f"{channel.mention} is already locked."), ephemeral=True)
            return
        overwrite.send_messages = False
        overwrite.send_messages_in_threads = False
        try:
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Locked by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I need `Manage Channels` or `Manage Roles` there."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_ok("Channel Locked", f"{channel.mention} is now locked."), ephemeral=True)
        if channel.id != interaction.channel_id:
            try:
                await channel.send(embed=embed_err("Channel Locked", "This channel has been locked by the moderators."))
            except Exception:
                pass

    @app_commands.command(name="unlock", description="Let @everyone send messages in a channel again.")
    @app_commands.describe(channel="Channel to unlock (defaults to this one).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def unlock_cmd(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=embed_err("Wrong Channel", "Only text channels can be unlocked."), ephemeral=True)
            return
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        if overwrite.send_messages is not False:
            await interaction.response.send_message(embed=embed_err("Not Locked", f"{channel.mention} is not locked."), ephemeral=True)
            return
        overwrite.send_messages = None
        overwrite.send_messages_in_threads = None
        try:
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I need `Manage Channels` or `Manage Roles` there."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_ok("Channel Unlocked", f"{channel.mention} is now unlocked."), ephemeral=True)
        if channel.id != interaction.channel_id:
            try:
                await channel.send(embed=embed_ok("Channel Unlocked", "This channel has been unlocked."))
            except Exception:
                pass

    @app_commands.command(name="modlog", description="Show all moderation logs for a user (10 per page).")
    @app_commands.describe(user="The user to look up.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def modlog_cmd(self, interaction: discord.Interaction, user: discord.User):
        cases = get_cases(interaction.guild.id, user.id)
        entries = []
        for c in cases:
            label = MOD_ACTION_LABELS.get(c["action"], c["action"].title())
            head = f"Case `#{c['case_id']}` | **{label}** | <t:{int(c['created_at'])}:R>"
            if c.get("duration"):
                head += f" | `{c['duration']}`"
            entries.append(f"{head}\nReason: {str(c['reason'])[:200]}\nModerator: <@{c['moderator_id']}>\n")

        pages = build_pages(
            f"Moderation Log | {user.name}", entries, per_page=10,
            header=f"{user.mention} (`{user.id}`)\nTotal records: `{len(entries)}`\nRemove a record with `/clear` and its case ID.\n\n"
        )
        if not entries:
            pages = [embed_info(f"Moderation Log | {user.name}", f"{user.mention} has no moderation records.")]
        await send_pages(interaction, pages, ephemeral=True)

    # =============================================================
    # GIVEAWAYS
    # =============================================================
    giveaway_group = app_commands.Group(
        name="giveaway", description="Create and manage giveaways.", guild_only=True,
        default_permissions=discord.Permissions(administrator=True)
    )

    def _find_giveaway(self, interaction: discord.Interaction, message_id: str) -> Optional[dict]:
        numbers = re.findall(r"\d+", message_id or "")
        if not numbers:
            return None
        gw = GIVEAWAYS.get(numbers[-1])
        if gw and gw["guild_id"] == str(interaction.guild_id):
            return gw
        return None

    async def _giveaway_choices(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        choices = []
        for gw in GIVEAWAYS.values():
            if gw["guild_id"] != str(interaction.guild_id):
                continue
            state = "ended" if gw.get("ended") else "active"
            label = f"{gw['prize'][:55]} ({state}) {gw['message_id']}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=gw["message_id"]))
        return choices[:25]

    @giveaway_group.command(name="create", description="Start a giveaway.")
    @app_commands.describe(prize="What is being given away.", duration="How long it runs, e.g. 30m, 2h, 1d, 1w.",
                           winners="Number of winners (default 1).", channel="Where to post it (defaults to this channel).",
                           reward_points="Optional. Points given to each winner automatically.",
                           reward_premium_days="Optional. Days of TE Premium given to each winner (30 = one month).")
    async def giveaway_create_cmd(
        self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str,
        winners: Optional[app_commands.Range[int, 1, 25]] = 1, channel: Optional[discord.TextChannel] = None,
        reward_points: Optional[app_commands.Range[int, 1, 1000000]] = None,
        reward_premium_days: Optional[app_commands.Range[int, 1, 3650]] = None
    ):
        delta = parse_duration_strict(duration)
        if delta is None or delta < timedelta(seconds=30) or delta > timedelta(days=365):
            await interaction.response.send_message(embed=embed_err("Invalid Duration", "Use a duration between `30s` and `1y`, for example `30m`, `2h`, `1d` or `1w`."), ephemeral=True)
            return
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=embed_err("Wrong Channel", "Giveaways can only be posted in text channels."), ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(embed=embed_err("Missing Permission", f"I need `Send Messages` and `Embed Links` in {channel.mention}."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        gw = {
            "message_id": "",
            "channel_id": str(channel.id),
            "guild_id": str(interaction.guild.id),
            "host_id": str(interaction.user.id),
            "prize": prize,
            "winners": int(winners or 1),
            "end_ts": datetime.now(timezone.utc).timestamp() + delta.total_seconds(),
            "entries": [],
            "ended": False,
            "winner_ids": [],
            "reward_points": int(reward_points or 0),
            "reward_premium_days": int(reward_premium_days or 0),
        }
        try:
            msg = await channel.send(allowed_mentions=NO_PINGS, **giveaway_payload(gw))
        except Exception as e:
            await interaction.followup.send(embed=embed_err("Could Not Post Giveaway", f"`{e}`"), ephemeral=True)
            return
        gw["message_id"] = str(msg.id)
        save_giveaway(gw)
        embed = embed_ok("Giveaway Created", f"Posted in {channel.mention}.\n[Jump to giveaway]({msg.jump_url})")
        if giveaway_reward_text(gw):
            embed.add_field(name="Auto Reward", value=f"`{giveaway_reward_text(gw)}` for each winner", inline=False)
        embed.add_field(name="Message ID", value=f"`{msg.id}`", inline=True)
        embed.add_field(name="Ends", value=f"<t:{int(gw['end_ts'])}:R>", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @giveaway_group.command(name="edit", description="Change the prize, winners or time left of a running giveaway.")
    @app_commands.describe(message_id="The giveaway to edit.", prize="New prize.", winners="New number of winners.",
                           duration="New time left from now, e.g. 1h, 2d.",
                           reward_points="New auto reward points per winner (0 = none).",
                           reward_premium_days="New auto TE Premium days per winner (0 = none).")
    async def giveaway_edit_cmd(
        self, interaction: discord.Interaction, message_id: str, prize: Optional[app_commands.Range[str, 1, 200]] = None,
        winners: Optional[app_commands.Range[int, 1, 25]] = None, duration: Optional[str] = None,
        reward_points: Optional[app_commands.Range[int, 0, 1000000]] = None,
        reward_premium_days: Optional[app_commands.Range[int, 0, 3650]] = None
    ):
        gw = self._find_giveaway(interaction, message_id)
        if not gw:
            await interaction.response.send_message(embed=embed_err("Giveaway Not Found", "No giveaway in this server matches that message ID."), ephemeral=True)
            return
        if gw.get("ended"):
            await interaction.response.send_message(embed=embed_err("Giveaway Ended", "Ended giveaways cannot be edited. Use `/giveaway reroll` instead."), ephemeral=True)
            return
        if prize is None and winners is None and duration is None and reward_points is None and reward_premium_days is None:
            await interaction.response.send_message(embed=embed_err("Nothing To Change", "Give at least one option to change."), ephemeral=True)
            return

        if duration is not None:
            delta = parse_duration_strict(duration)
            if delta is None or delta < timedelta(seconds=30) or delta > timedelta(days=365):
                await interaction.response.send_message(embed=embed_err("Invalid Duration", "Use a duration between `30s` and `1y`."), ephemeral=True)
                return
            gw["end_ts"] = datetime.now(timezone.utc).timestamp() + delta.total_seconds()
        if prize is not None:
            gw["prize"] = prize
        if winners is not None:
            gw["winners"] = int(winners)
        if reward_points is not None:
            gw["reward_points"] = int(reward_points)
        if reward_premium_days is not None:
            gw["reward_premium_days"] = int(reward_premium_days)

        await interaction.response.defer(ephemeral=True)
        save_giveaway(gw)
        message = await get_giveaway_message(self.bot, gw)
        if message:
            try:
                await message.edit(**giveaway_payload(gw, message))
            except Exception as e:
                await interaction.followup.send(embed=embed_err("Saved, But Could Not Update Message", f"`{e}`"), ephemeral=True)
                return
        embed = embed_ok("Giveaway Updated", f"**{gw['prize']}**")
        embed.add_field(name="Winners", value=f"`{gw['winners']}`", inline=True)
        embed.add_field(name="Ends", value=f"<t:{int(gw['end_ts'])}:R>", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @giveaway_group.command(name="status", description="Check why a giveaway is not ending (tracking, timer, errors).")
    @app_commands.describe(message_id="The giveaway message ID.")
    async def giveaway_status_cmd(self, interaction: discord.Interaction, message_id: str):
        now = datetime.now(timezone.utc).timestamp()
        gw = GIVEAWAYS.get(message_id.strip())
        loop = self.bot.get_cog("FeaturesCog").scheduler_loop if self.bot.get_cog("FeaturesCog") else None
        embed = embed_info("Giveaway Status")
        embed.add_field(name="Tracked", value=f"`{gw is not None}`", inline=True)
        embed.add_field(name="Giveaways Tracked", value=f"`{len(GIVEAWAYS)}`", inline=True)
        embed.add_field(name="Timer Running", value=f"`{bool(loop and loop.is_running())}`", inline=True)
        tick = GIVEAWAY_DEBUG.get("last_tick") or 0
        embed.add_field(name="Last Timer Tick", value=f"<t:{int(tick)}:R>" if tick else "`Never`", inline=True)
        if gw:
            embed.add_field(name="Ended", value=f"`{bool(gw.get('ended'))}`", inline=True)
            embed.add_field(name="Ends", value=f"<t:{int(gw['end_ts'])}:R>", inline=True)
            embed.add_field(name="Due Now", value=f"`{gw['end_ts'] <= now}`", inline=True)
            embed.add_field(name="Entries", value=f"`{len(gw['entries'])}`", inline=True)
            embed.add_field(name="Winner Message Posted", value=f"`{gw.get('announced')}`", inline=True)
        else:
            embed.add_field(name="Problem", value="This giveaway is not tracked. The bot forgot it (restart without the database?). Use `/giveaway delete` and create a new one.", inline=False)
        embed.add_field(name="Timer Step", value=f"`{GIVEAWAY_DEBUG.get('stage', 'unknown')}`", inline=False)
        embed.add_field(name="Last Error", value=f"```{GIVEAWAY_DEBUG.get('last_error') or 'None'}```"[:1000], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @giveaway_group.command(name="end", description="End a giveaway now and pick the winners.")
    @app_commands.describe(message_id="The giveaway to end.")
    async def giveaway_end_cmd(self, interaction: discord.Interaction, message_id: str):
        gw = self._find_giveaway(interaction, message_id)
        if not gw:
            await interaction.response.send_message(embed=embed_err("Giveaway Not Found", "No giveaway in this server matches that message ID."), ephemeral=True)
            return
        if gw.get("ended"):
            await interaction.response.send_message(embed=embed_err("Already Ended", "That giveaway has already ended."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winners = await finish_giveaway(self.bot, gw, ended_early=True)
        text = ", ".join(f"<@{w}>" for w in winners) if winners else "No valid entries"
        await interaction.followup.send(embed=embed_ok("Giveaway Ended", f"**{gw['prize']}**\nWinners: {text}"), ephemeral=True, allowed_mentions=NO_PINGS)

    @giveaway_group.command(name="reroll", description="Pick new winner(s) for an ended giveaway.")
    @app_commands.describe(message_id="The ended giveaway.", winners="How many new winners (defaults to the original number).")
    async def giveaway_reroll_cmd(self, interaction: discord.Interaction, message_id: str, winners: Optional[app_commands.Range[int, 1, 25]] = None):
        gw = self._find_giveaway(interaction, message_id)
        if not gw:
            await interaction.response.send_message(embed=embed_err("Giveaway Not Found", "No giveaway in this server matches that message ID."), ephemeral=True)
            return
        if not gw.get("ended"):
            await interaction.response.send_message(embed=embed_err("Still Running", "That giveaway has not ended yet. Use `/giveaway end` first."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        new_winners = await reroll_giveaway(self.bot, gw, winners)
        if not new_winners:
            await interaction.followup.send(embed=embed_err("No One Left To Pick", "There are no other valid entries."), ephemeral=True)
            return
        text = ", ".join(f"<@{w}>" for w in new_winners)
        await interaction.followup.send(embed=embed_ok("Giveaway Rerolled", f"**{gw['prize']}**\nNew winner(s): {text}"), ephemeral=True, allowed_mentions=NO_PINGS)

    @giveaway_group.command(name="delete", description="Delete a giveaway and its message.")
    @app_commands.describe(message_id="The giveaway to delete.")
    async def giveaway_delete_cmd(self, interaction: discord.Interaction, message_id: str):
        gw = self._find_giveaway(interaction, message_id)
        if not gw:
            await interaction.response.send_message(embed=embed_err("Giveaway Not Found", "No giveaway in this server matches that message ID."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        message = await get_giveaway_message(self.bot, gw)
        if message:
            try:
                await message.delete()
            except Exception as e:
                print(f"[Giveaway Delete Message Error] {e}")
        delete_giveaway_record(gw["message_id"])
        await interaction.followup.send(embed=embed_ok("Giveaway Deleted", f"**{gw['prize']}** was deleted."), ephemeral=True)

    # Autocomplete: shows the server's giveaways when typing message_id
    @giveaway_edit_cmd.autocomplete("message_id")
    async def _ac_edit(self, interaction: discord.Interaction, current: str):
        return await self._giveaway_choices(interaction, current)

    @giveaway_end_cmd.autocomplete("message_id")
    async def _ac_end(self, interaction: discord.Interaction, current: str):
        return await self._giveaway_choices(interaction, current)

    @giveaway_reroll_cmd.autocomplete("message_id")
    async def _ac_reroll(self, interaction: discord.Interaction, current: str):
        return await self._giveaway_choices(interaction, current)

    @giveaway_delete_cmd.autocomplete("message_id")
    async def _ac_delete(self, interaction: discord.Interaction, current: str):
        return await self._giveaway_choices(interaction, current)


    # =============================================================
    # PRICES: /change-price
    # =============================================================
    @app_commands.command(name="change-price", description="Change a price: dev product, subscription display, or points (owner only).")
    @app_commands.describe(
        type="Which price to change.",
        amount="Set the price to exactly this number.",
        percentage="Change the current price by this percent. -20 = 20% cheaper, 15 = 15% more."
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="Dev Product (Robux)", value="dev_product"),
        app_commands.Choice(name="Subscription (Robux)", value="subscription"),
        app_commands.Choice(name="Points (build cost)", value="points"),
    ])
    async def change_price_cmd(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        amount: Optional[app_commands.Range[int, 1, 1000000]] = None,
        percentage: Optional[app_commands.Range[float, -99.0, 1000.0]] = None
    ):
        if (amount is None) == (percentage is None):
            await interaction.response.send_message(
                embed=embed_err("Choose One", "Give either `amount` (exact price) or `percentage` (change by percent). Not both, and not neither."),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        kind = type.value
        prices = get_prices()
        api = None
        note = None

        if kind == "points":
            current = prices["points_cost"]
        elif kind == "subscription":
            current = prices["subscription_robux"]
        else:
            api_key = os.getenv("ROBLOX_OPEN_CLOUD_KEY", "").strip()
            product_id = os.getenv("ROBLOX_DEV_PRODUCT_ID", "").strip()
            if not api_key:
                await interaction.followup.send(embed=embed_err("Missing API Key", "`ROBLOX_OPEN_CLOUD_KEY` is not set."), ephemeral=True)
                return
            if not product_id:
                await interaction.followup.send(embed=embed_err("Missing Product ID", "`ROBLOX_DEV_PRODUCT_ID` is not set."), ephemeral=True)
                return
            async with aiohttp.ClientSession() as session:
                universe_id = await resolve_universe_id(session)
                if not universe_id:
                    await interaction.followup.send(embed=embed_err("Universe Not Found", "Set `ROBLOX_UNIVERSE_ID`, or check `ROBLOX_PLACE_ID`."), ephemeral=True)
                    return
                live_price, live_error = await fetch_dev_product_price(session, api_key, universe_id, product_id)
            api = {"key": api_key, "universe": universe_id, "product": product_id}
            current = live_price if live_price is not None else prices["dev_product_robux"]
            if live_price is None:
                note = f"Could not read the current price from Roblox ({live_error}). Using the saved price."

        if percentage is not None:
            if current is None:
                await interaction.followup.send(
                    embed=embed_err("Current Price Unknown", "I cannot work out a percentage without the current price. Use `amount` instead."),
                    ephemeral=True
                )
                return
            new_price = max(1, round(current * (1 + percentage / 100)))
        else:
            new_price = int(amount)

        if new_price > PRICE_MAX:
            await interaction.followup.send(embed=embed_err("Price Too High", f"The highest allowed price is `{PRICE_MAX:,}`."), ephemeral=True)
            return
        if current is not None and new_price == current:
            await interaction.followup.send(embed=embed_err("No Change", f"The price is already {fmt_price(kind, current)}."), ephemeral=True)
            return

        embed = discord.Embed(title="Confirm Price Change", color=COLOR_GOLD)
        embed.add_field(name="Type", value=f"`{PRICE_LABELS[kind][0]}`", inline=True)
        embed.add_field(name="Current", value=fmt_price(kind, current), inline=True)
        embed.add_field(name="New", value=fmt_price(kind, new_price), inline=True)
        if percentage is not None:
            embed.add_field(name="Change", value=f"`{percentage:+g}%`", inline=True)
        embed.add_field(name="Will Show In", value=PRICE_SHOWN_IN[kind], inline=False)
        if kind == "dev_product":
            embed.add_field(name="Roblox", value="This changes the real dev product price on Roblox right away.", inline=False)
        elif kind == "subscription":
            embed.add_field(
                name="Important",
                value=(
                    "Roblox does not let bots change subscription prices. This only changes the price **shown** in "
                    "the website text. Change the real price in Creator Hub (Roblox allows one change every 60 days, "
                    "and price increases start 30 days after players are told)."
                ),
                inline=False
            )
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        embed.set_footer(text="Press Confirm within 60 seconds.")

        view = PriceChangeConfirmView(interaction.user.id, kind, current, new_price, api)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # =============================================================
    # PROMO CODES
    # =============================================================
    @app_commands.command(name="redeem", description="Redeem a promo code for points.")
    @app_commands.describe(code="The promo code.")
    @app_commands.checks.cooldown(1, 5.0)
    async def redeem_cmd(self, interaction: discord.Interaction, code: app_commands.Range[str, 1, 64]):
        normalized = normalize_promo_code(code)
        if not PROMO_CODE_RE.match(normalized):
            status, promo = "not_found", None
        else:
            status, promo = redeem_promo(normalized, interaction.user.id)

        if status != "ok":
            messages = {
                "not_found": ("Invalid Code", "That code does not exist. Check the spelling and try again."),
                "already_redeemed": ("Already Redeemed", "You already used this code."),
                "expired": ("Code Expired", "This code is no longer valid."),
                "used_up": ("Code Used Up", "This code has reached its usage limit."),
            }
            title, text = messages.get(status, ("Could Not Redeem", "Something went wrong. Please try again."))
            await interaction.response.send_message(embed=embed_err(title, text), ephemeral=True)
            return

        reward = int(promo.get("points", 0))
        premium_days = int(promo.get("premium_days", 0))
        new_total = add_user_points(str(interaction.user.id), reward) if reward > 0 else add_user_points(str(interaction.user.id), 0)
        old_total = new_total - reward

        premium_until = None
        if premium_days > 0:
            try:
                premium_until = extend_user_premium(str(interaction.user.id), premium_days)
            except Exception as e:
                print(f"[Promo Premium Error] {e}")

        parts = []
        if reward > 0:
            parts.append(f"**{reward:,} points**")
        if premium_days > 0:
            parts.append(f"**{premium_days} days of TE Premium**")
        embed = embed_ok("Code Redeemed!", "You got " + " and ".join(parts) + ".")
        embed.add_field(name="Code", value=f"`{promo['code']}`", inline=True)
        if reward > 0:
            embed.add_field(name="Points", value=f"`+{reward:,} pts`", inline=True)
            embed.add_field(name="New Balance", value=f"`{new_total:,} pts`", inline=True)
        if premium_until is not None:
            embed.add_field(name="TE Premium Until", value=f"<t:{int(premium_until.timestamp())}:F>", inline=False)
        elif premium_days > 0:
            embed.add_field(name="TE Premium", value="Could not be saved. Please contact staff.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        try:
            where = interaction.guild.name if interaction.guild else "Direct Message"
            await send_promo_log(self.bot, interaction.user, promo, old_total, new_total, where, premium_until)
        except Exception as e:
            print(f"[Promo Log Error] {e}")

    @set_group.command(name="code", description="Create a promo code that gives points and/or TE Premium.")
    @app_commands.describe(
        code="The code people type, e.g. WELCOME100 (letters, numbers, - and _).",
        points="How many points the code gives. Optional if you set premium_days.",
        premium_days="Optional. Days of TE Premium the code gives (30 = one month).",
        duration="Optional. How long the code works, e.g. 12h, 7d, 2w.",
        usage_limit="Optional. How many people can use it in total."
    )
    async def set_code_cmd(
        self,
        interaction: discord.Interaction,
        code: app_commands.Range[str, 3, 32],
        points: Optional[app_commands.Range[int, 0, 1000000]] = None,
        premium_days: Optional[app_commands.Range[int, 1, 3650]] = None,
        duration: Optional[str] = None,
        usage_limit: Optional[app_commands.Range[int, 1, 1000000]] = None
    ):
        points = int(points or 0)
        premium_days = int(premium_days or 0)
        if points == 0 and premium_days == 0:
            await interaction.response.send_message(
                embed=embed_err("No Reward", "Give `points`, `premium_days`, or both."), ephemeral=True)
            return
        normalized = normalize_promo_code(code)
        if not PROMO_CODE_RE.match(normalized):
            await interaction.response.send_message(
                embed=embed_err("Invalid Code", "Use `3` to `32` letters, numbers, `-` or `_`. No spaces."),
                ephemeral=True
            )
            return

        expires_at = None
        if duration and duration.strip():
            delta = parse_duration_strict(duration)
            if delta is None:
                await interaction.response.send_message(embed=embed_err("Invalid Duration", "Use a time like `12h`, `7d` or `2w`."), ephemeral=True)
                return
            expires_at = datetime.now(timezone.utc).timestamp() + delta.total_seconds()

        promo = create_promo(normalized, points, interaction.user.id, expires_at, usage_limit, premium_days)
        if promo is None:
            await interaction.response.send_message(
                embed=embed_err("Code Already Exists", f"`{normalized}` already exists. Use `/manage codes` to edit or delete it."),
                ephemeral=True
            )
            return

        embed = embed_ok("Promo Code Created", f"People can now use `/redeem {normalized}`.")
        embed.add_field(name="Code", value=f"`{normalized}`", inline=True)
        embed.add_field(name="Reward", value=f"`{promo_reward_text(promo)}`", inline=True)
        embed.add_field(name="Usage Limit", value=f"`{usage_limit:,}`" if usage_limit else "`Unlimited`", inline=True)
        embed.add_field(name="Expires", value=f"<t:{int(expires_at)}:R>" if expires_at else "`Never`", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    promo_group = app_commands.Group(name="promo", description="Promo code tools.")

    @promo_group.command(name="codes", description="Show all active promo codes, one per line.")
    async def promo_codes_cmd(self, interaction: discord.Interaction):
        promos = list_promos(active_only=True)
        if not promos:
            await send_pages(interaction, [embed_info("Active Promo Codes", "There are no active promo codes. Create one with `/set code`.")], ephemeral=True)
            return
        lines = [promo_line(p) for p in promos]
        pages = build_pages("Active Promo Codes", lines, per_page=15, header=f"Active codes: `{len(lines)}`\n\n")
        await send_pages(interaction, pages, ephemeral=True)

    manage_group = app_commands.Group(name="manage", description="Owner management tools.")

    @manage_group.command(name="codes", description="Edit points, limits or delete active promo codes.")
    async def manage_codes_cmd(self, interaction: discord.Interaction):
        view = ManageCodesView(interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


    # =============================================================
    # STAFF (gets TE Premium automatically)
    # =============================================================
    @app_commands.command(name="staff", description="Add or remove a staff member. Staff get TE Premium automatically.")
    @app_commands.describe(action="Add or remove.", user="The staff member.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove"),
    ])
    async def staff_cmd(self, interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.User):
        if action.value == "add":
            if is_staff(user.id):
                await interaction.response.send_message(embed=embed_err("Already Staff", f"{user.mention} (`{user.id}`) is already staff."), ephemeral=True, allowed_mentions=NO_PINGS)
                return
            saved = add_staff(user.id)
            embed = embed_ok("Staff Added", f"{user.mention} (`{user.id}`) is now staff and has **TE Premium**.")
            if not saved:
                embed.add_field(name="Warning", value="The database could not be reached. This only lasts until the bot restarts.", inline=False)
            role_info = get_staff_role()
            if role_info and interaction.guild and interaction.guild.id == role_info["guild_id"]:
                role = interaction.guild.get_role(role_info["role_id"])
                member = interaction.guild.get_member(user.id)
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(user.id)
                    except Exception:
                        member = None
                if role and member:
                    try:
                        await member.add_roles(role, reason=f"Staff added by {interaction.user}")
                        embed.add_field(name="Role Given", value=role.mention, inline=False)
                    except Exception as e:
                        embed.add_field(name="Role", value=f"Could not give {role.mention}: `{e}`", inline=False)
                else:
                    embed.add_field(name="Role", value="Not given (the user is not in this server, or the role was deleted).", inline=False)
            elif not role_info:
                embed.add_field(name="Role", value="No staff role is set. Pick one in `/staff-list`.", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)
            return
        else:
            if str(user.id) in HARDCODED_STAFF:
                await interaction.response.send_message(embed=embed_err("Cannot Remove", f"{user.mention} (`{user.id}`) is permanent staff. This is set in the code."), ephemeral=True, allowed_mentions=NO_PINGS)
                return
            role_info = get_staff_role()
            if not remove_staff(user.id):
                await interaction.response.send_message(embed=embed_err("Not Staff", f"{user.mention} (`{user.id}`) is not on the staff list."), ephemeral=True, allowed_mentions=NO_PINGS)
                return
            embed = embed_ok("Staff Removed", f"{user.mention} (`{user.id}`) is no longer staff. Their TE Premium was removed too.")
            if role_info and interaction.guild and interaction.guild.id == role_info.get("guild_id"):
                member = interaction.guild.get_member(user.id)
                role = interaction.guild.get_role(role_info["role_id"])
                if member and role:
                    try:
                        await member.remove_roles(role, reason="Removed from staff")
                        embed.add_field(name="Role Removed", value=role.mention, inline=False)
                    except Exception as e:
                        embed.add_field(name="Role", value=f"Could not remove {role.mention}: `{e}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="staff-list", description="Show staff (one per line) and choose the role new staff get.")
    async def staff_list_cmd(self, interaction: discord.Interaction):
        view = StaffListView(interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True, allowed_mentions=NO_PINGS)


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
