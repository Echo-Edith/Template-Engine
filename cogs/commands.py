import os
import re
import json
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands

from cogs.features import (
    build_ticket_panel_embed,
    is_free_mode,
    set_free_mode,
    user_has_premium,
    SITE_THEMES,
    WEBSITE_URL,
    get_site_theme,
    set_site_theme,
    BOT_OWNER_ID,
    RobloxVerificationView,
    RobloxUnsyncView,
    GiveawayView,
    giveaway_payload,
    giveaway_reward_text,
    giveaway_luck_text,
    GIVEAWAYS,
    GIVEAWAY_DEBUG,
    MOD_ACTION_LABELS,
    MAX_REMINDERS_PER_USER,
    COLOR_PURPLE,
    COLOR_GOLD,
    COLOR_GRAY,
    get_roblox_cookie_header,
    get_user_points,
    is_user_blacklisted,
    get_trivia_event,
    set_trivia_event,
    OWNER_IDS,
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
    get_limits,
    set_limit,
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
    list_premium_users,
    get_guild_config,
    save_guild_config,
    render_greeting,
    build_greeting_embed,
    DEFAULT_GREETINGS,
    BACKUP_LIMIT_PER_GUILD,
    BACKUP_SAVED,
    BACKUP_NOT_SAVED,
    backup_counts,
    normalize_backup_id,
    snapshot_guild,
    save_backup,
    get_backup,
    latest_auto_backup,
    list_backups,
    delete_backup,
    restore_guild,
    list_shop_items,
    get_shop_item,
    add_shop_item,
    remove_shop_item,
    take_stock,
    return_stock,
    count_shop_sale,
    shop_key,
    HAS_CARD_LAYOUT,
    TicketPanelView,
    get_ticket_by_channel,
    close_ticket,
    create_ticket,
    claim_daily,
    daily_reward_for,
    next_daily_reset_ts,
    DAILY_LOCKS,
    DAILY_MILESTONE_EVERY,
    DAILY_MILESTONE_REWARD,
    build_server_summary,
    LIFETIME_PREMIUM_DAYS,
    has_lifetime_premium,
    set_user_premium,
    GIVEAWAY_DIVIDER,
    SHOP_LOG_CHANNEL_ID,
    log_payload,
    SHOP_LOCKS,
    HARDCODED_STAFF,
    COMMAND_ADMINS,
    extend_user_premium,
    set_staff_role,
    get_staff_role,
    get_user_settings,
    set_user_settings,
    remove_inbox_from,
)

# -------------------------------------------------------------
# WHO CAN USE WHAT (edit these sets to change access)
#   PUBLIC_COMMANDS : anyone
#   ADMIN_COMMANDS  : server members with the Administrator permission
#   anything else   : bot owner only (set, premium, change, game, revenue, cookie)
# -------------------------------------------------------------
STAFF_COMMANDS = {"staff", "premiums"}  # only HARDCODED_STAFF may run these

PUBLIC_COMMANDS = {
    "points", "leaderboard", "roblox", "gameinfo", "timestamp", "remind",
    "afk", "info", "redeem", "promo", "shop", "daily", "settings",
}
ADMIN_COMMANDS = {
    "groupcheck", "nick", "role", "send", "mod", "channel", "cases", "giveaway", "greeting", "messagelog", "backup", "analyze", "ticket",
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
# PRICE CHANGES (/change price)
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
    """/staff list: staff members (paged) + a role picker. The chosen role is saved and given by /staff add."""
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
# SERVER BACKUPS (/backup)
# -------------------------------------------------------------
def backup_embed(backup: dict, title: str, description: str, color: int = COLOR_GREEN, detailed: bool = False) -> discord.Embed:
    channels, roles, categories = backup_counts(backup)
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_author(name="Template Engine \u2022 Server Backup")
    embed.add_field(name="Backup ID", value=f"`{backup['backup_id']}`", inline=True)
    embed.add_field(name="Server", value=f"`{backup['guild_name']}`", inline=True)
    embed.add_field(name="Created", value=f"<t:{int(backup['created_at'])}:R>", inline=True)
    embed.add_field(name="Channels", value=f"`{channels}`", inline=True)
    embed.add_field(name="Roles", value=f"`{roles}`", inline=True)
    embed.add_field(name="Categories", value=f"`{categories}`", inline=True)
    if detailed:
        names = [c["name"] for c in backup.get("categories", [])][:10]
        if names:
            embed.add_field(name="Categories Saved", value="\n".join(f"`{n}`" for n in names), inline=False)
        cfg = backup.get("bot_config") or {}
        bot_bits = [label for key, label in (("join", "Join message"), ("leave", "Leave message"), ("message_log", "Message log")) if key in cfg]
        embed.add_field(name="Bot Settings Saved", value=", ".join(f"`{b}`" for b in bot_bits) or "`None`", inline=False)
    embed.add_field(name="Saved", value="\n".join(f"\u2022 {x}" for x in BACKUP_SAVED), inline=False)
    embed.add_field(name="Not Saved", value="\n".join(f"\u2022 {x}" for x in BACKUP_NOT_SAVED), inline=False)
    return embed


THEME_LABELS = {"default": "Server default (seasonal)", "dark": "Dark", "light": "Light"}


class SettingsView(discord.ui.View):
    """Personal settings window for /settings. Only the person who opened it can use it."""

    def __init__(self, bot, user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id

    async def _name(self, uid: str) -> str:
        user = self.bot.get_user(int(uid))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(uid))
            except Exception:
                user = None
        return f"{user.name}" if user else f"User {uid[-4:]}"

    async def build_embed(self, s: dict) -> discord.Embed:
        embed = discord.Embed(title="Your Settings", description="Change how the bot and the website treat you. Only you can see this.", color=COLOR_GOLD)
        embed.add_field(
            name="Bot Messages",
            value=f"`{'On' if s['bot_dms'] else 'Off'}`\nBoost notices, build finished notes and limit notices by DM.",
            inline=True
        )
        embed.add_field(
            name="Inbox Notifications",
            value=f"`{'On' if s['inbox_notify'] else 'Off'}`\nA DM when someone sends you a design." + ("" if s["bot_dms"] else "\nNeeds Bot Messages turned on."),
            inline=True
        )
        embed.add_field(
            name="Public Profile",
            value=f"`{'On' if s['profile_public'] else 'Off'}`\nA web page with the designs you choose to show. No Discord ID is shown.",
            inline=False
        )
        embed.add_field(name="Website Theme", value=f"`{THEME_LABELS[s['theme_mode']]}`\nOnly changes the website for you.", inline=False)
        if s["blocked"]:
            shown = ", ".join(f"<@{b}>" for b in s["blocked"][:15])
            more = f" and `{len(s['blocked']) - 15}` more" if len(s["blocked"]) > 15 else ""
            embed.add_field(name=f"Blocked on the Website ({len(s['blocked'])})", value=shown + more, inline=False)
        else:
            embed.add_field(name="Blocked on the Website", value="`No one`", inline=False)
        embed.set_footer(text="Payment, moderation and reminder DMs are always sent.")
        return embed

    async def build_items(self, s: dict):
        self.clear_items()
        dms = discord.ui.Button(label=f"Bot Messages: {'On' if s['bot_dms'] else 'Off'}",
                                style=discord.ButtonStyle.success if s["bot_dms"] else discord.ButtonStyle.secondary, row=0)
        dms.callback = self.toggle_dms
        notify = discord.ui.Button(label=f"Inbox Notifications: {'On' if s['inbox_notify'] else 'Off'}",
                                   style=discord.ButtonStyle.success if s["inbox_notify"] else discord.ButtonStyle.secondary, row=0)
        notify.callback = self.toggle_notify
        self.add_item(dms)
        self.add_item(notify)
        profile = discord.ui.Button(label=f"Public Profile: {'On' if s['profile_public'] else 'Off'}",
                                    style=discord.ButtonStyle.success if s["profile_public"] else discord.ButtonStyle.secondary, row=0)
        profile.callback = self.toggle_profile
        self.add_item(profile)

        theme = discord.ui.Select(placeholder="Website theme (only for you)", row=1, options=[
            discord.SelectOption(label=label, value=key, default=(key == s["theme_mode"]),
                                 description="Seasonal themes can show up" if key == "default" else "No seasonal themes for you")
            for key, label in THEME_LABELS.items()
        ])
        theme.callback = self.pick_theme
        self.add_item(theme)

        block = discord.ui.UserSelect(placeholder="Block someone on the website", min_values=1, max_values=5, row=2)
        block.callback = self.block_users
        self.add_item(block)

        if s["blocked"]:
            names = [await self._name(b) for b in s["blocked"][:25]]
            unblock = discord.ui.Select(placeholder="Unblock someone", row=3, options=[
                discord.SelectOption(label=names[i][:100], value=b) for i, b in enumerate(s["blocked"][:25])
            ])
            unblock.callback = self.unblock_user
            self.add_item(unblock)
            clear = discord.ui.Button(label="Unblock everyone", style=discord.ButtonStyle.danger, row=4)
            clear.callback = self.unblock_all
            self.add_item(clear)

    async def refresh(self, interaction: discord.Interaction):
        s = get_user_settings(self.user_id)
        await self.build_items(s)
        await interaction.response.edit_message(embed=await self.build_embed(s), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("These are not your settings. Run `/settings` yourself.", ephemeral=True)
            return False
        return True

    async def toggle_dms(self, interaction: discord.Interaction):
        set_user_settings(self.user_id, bot_dms=not get_user_settings(self.user_id)["bot_dms"])
        await self.refresh(interaction)

    async def toggle_notify(self, interaction: discord.Interaction):
        set_user_settings(self.user_id, inbox_notify=not get_user_settings(self.user_id)["inbox_notify"])
        await self.refresh(interaction)

    async def toggle_profile(self, interaction: discord.Interaction):
        set_user_settings(self.user_id, profile_public=not get_user_settings(self.user_id)["profile_public"])
        await self.refresh(interaction)

    async def pick_theme(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        if value in THEME_LABELS:
            set_user_settings(self.user_id, theme_mode=value)
        await self.refresh(interaction)

    async def block_users(self, interaction: discord.Interaction):
        s = get_user_settings(self.user_id)
        blocked = list(s["blocked"])
        added = []
        for user in interaction.data.get("values", []):
            uid = str(user)
            if uid != str(self.user_id) and uid not in blocked and len(blocked) < 200:
                blocked.append(uid)
                added.append(uid)
        if added:
            set_user_settings(self.user_id, blocked=blocked)
            remove_inbox_from(self.user_id, added)
        await self.refresh(interaction)

    async def unblock_user(self, interaction: discord.Interaction):
        target = interaction.data["values"][0]
        s = get_user_settings(self.user_id)
        set_user_settings(self.user_id, blocked=[b for b in s["blocked"] if b != target])
        await self.refresh(interaction)

    async def unblock_all(self, interaction: discord.Interaction):
        set_user_settings(self.user_id, blocked=[])
        await self.refresh(interaction)


class BackupConfirmView(discord.ui.View):
    """Confirm step for Restore (clears the server) and Delete."""

    def __init__(self, cog, user_id: int, mode: str, backup: dict):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        self.mode = mode
        self.backup = backup
        self.confirm_btn.label = "Restore Backup" if mode == "restore" else "Delete Backup"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=embed_err("Not Yours", "Only the person who ran the command can use these buttons."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        if self.mode == "delete":
            removed = delete_backup(self.backup["backup_id"])
            if removed:
                await interaction.response.edit_message(embed=embed_ok("Backup Deleted", f"`{self.backup['backup_id']}` was deleted."), view=None)
            else:
                await interaction.response.edit_message(embed=embed_err("Not Deleted", "The database could not delete it. Try again."), view=None)
            return
        await interaction.response.edit_message(
            embed=embed_info("Restoring...", "Clearing the server and rebuilding it from the backup. This can take a few minutes. Do not use the server meanwhile.", COLOR_GOLD),
            view=None
        )
        await self.cog.run_restore(interaction, self.backup)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=embed_info("Cancelled", "Nothing was changed."), view=None)


# -------------------------------------------------------------
# POINTS SHOP VIEWS
# -------------------------------------------------------------
def shop_reward_text(item: dict) -> str:
    if item["reward"] == "premium":
        return "Lifetime TE Premium" if item.get("permanent") else f"{int(item['premium_days'])} days TE Premium"
    if item["reward"] == "role":
        return f"<@&{item['role_id']}> role"
    return "Delivered by staff"


def shop_stock_text(item: dict) -> str:
    return "unlimited" if item.get("stock") is None else ("SOLD OUT" if item["stock"] <= 0 else f"{item['stock']} left")


def shop_price_for(item: dict, user_id) -> int:
    """The price this user pays. TE Premium members get the item's premium discount (percent)."""
    price = int(item["price"])
    discount = int(item.get("premium_discount") or 0)
    if discount > 0 and user_id is not None and user_has_premium(str(user_id)):
        price = max(1, round(price * (100 - discount) / 100))
    return price


def shop_price_text(item: dict, user_id) -> str:
    base = int(item["price"])
    discount = int(item.get("premium_discount") or 0)
    if discount <= 0:
        return f"`{base:,} pts`"
    premium_price = max(1, round(base * (100 - discount) / 100))
    if user_id is not None and user_has_premium(str(user_id)):
        return f"`{premium_price:,} pts` (~~{base:,}~~, Premium {discount}% off)"
    return f"`{base:,} pts` (Premium: `{premium_price:,} pts`)"


def shop_line(item: dict, user_id=None) -> str:
    return f"**{item['name']}** | {shop_price_text(item, user_id)} | {shop_reward_text(item)} | `{shop_stock_text(item)}`"


class ShopView(discord.ui.View):
    """Lists the items (1 per line). Pick an item in the menu to buy it."""
    PER_PAGE = 10

    def __init__(self, user_id: int, page: int = 0):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.items = list_shop_items()
        self.pages = max(1, -(-len(self.items) // self.PER_PAGE))
        self.page = max(0, min(page, self.pages - 1))
        chunk = self.items[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]
        if chunk:
            select = discord.ui.Select(
                placeholder="Choose an item to buy...",
                options=[discord.SelectOption(label=i["name"][:100], value=i["key"][:100],
                                              description=f"{shop_price_for(i, self.user_id):,} pts | {shop_stock_text(i)}"[:100]) for i in chunk]
            )
            select.callback = self._on_select
            self.add_item(select)
        if self.pages > 1:
            prev_btn = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
            next_btn = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= self.pages - 1, row=1)
            prev_btn.callback = self._on_prev
            next_btn.callback = self._on_next
            self.add_item(prev_btn)
            self.add_item(next_btn)

    def build_embed(self) -> discord.Embed:
        balance = get_user_points(str(self.user_id))
        chunk = self.items[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]
        lines = f"\n{GIVEAWAY_DIVIDER}\n".join(shop_line(i, self.user_id) for i in chunk) or "The shop is empty right now."
        embed = discord.Embed(title="Points Shop", description=f"Your balance: `{balance:,} pts`\n\n{lines}", color=COLOR_GOLD)
        embed.set_footer(text=f"Page {self.page + 1}/{self.pages} | Choose an item below to buy it")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=embed_err("Not Yours", "Run `/shop` to open your own shop."), ephemeral=True)
            return False
        return True

    async def _on_prev(self, interaction: discord.Interaction):
        view = ShopView(self.user_id, self.page - 1)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_next(self, interaction: discord.Interaction):
        view = ShopView(self.user_id, self.page + 1)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_select(self, interaction: discord.Interaction):
        item = get_shop_item(interaction.data["values"][0])
        if item is None:
            await interaction.response.send_message(embed=embed_err("Item Not Found", "That item is no longer for sale."), ephemeral=True)
            return
        balance = get_user_points(str(self.user_id))
        embed = discord.Embed(title=f"Buy {item['name']}?", color=COLOR_GOLD)
        if item.get("description"):
            embed.description = item["description"]
        price_now = shop_price_for(item, self.user_id)
        embed.add_field(name="Price", value=shop_price_text(item, self.user_id), inline=True)
        embed.add_field(name="Your Balance", value=f"`{balance:,} pts`", inline=True)
        embed.add_field(name="After Buying", value=f"`{balance - price_now:,} pts`" if balance >= price_now else "`Not enough points`", inline=True)
        embed.add_field(name="You Get", value=shop_reward_text(item), inline=False)
        await interaction.response.send_message(embed=embed, view=ShopConfirmView(self.user_id, item["key"]), ephemeral=True)


if HAS_CARD_LAYOUT:
    class ShopLayout(discord.ui.LayoutView):
        """Shop as one card: a thin separator line between every item (needs discord.py 2.6+)."""
        PER_PAGE = 10

        def __init__(self, user_id: int, page: int = 0):
            super().__init__(timeout=300)
            self.user_id = user_id
            items = list_shop_items()
            pages = max(1, -(-len(items) // self.PER_PAGE))
            self.page = max(0, min(page, pages - 1))
            chunk = items[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]
            balance = get_user_points(str(user_id))

            container = discord.ui.Container(accent_colour=discord.Colour(COLOR_GOLD))
            container.add_item(discord.ui.TextDisplay(f"### Points Shop\nYour balance: `{balance:,} pts`"))
            container.add_item(discord.ui.Separator())
            if not chunk:
                container.add_item(discord.ui.TextDisplay("The shop is empty right now."))
            for item in chunk:
                container.add_item(discord.ui.TextDisplay(shop_line(item, user_id)))
                container.add_item(discord.ui.Separator())  # the line between items
            if chunk:
                select = discord.ui.Select(
                    placeholder="Choose an item to buy...",
                    options=[discord.SelectOption(label=i["name"][:100], value=i["key"][:100],
                                                  description=f"{shop_price_for(i, self.user_id):,} pts | {shop_stock_text(i)}"[:100]) for i in chunk]
                )
                select.callback = self._on_select
                row = discord.ui.ActionRow()
                row.add_item(select)
                container.add_item(row)
            if pages > 1:
                prev_btn = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
                next_btn = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= pages - 1)
                prev_btn.callback = self._on_prev
                next_btn.callback = self._on_next
                nav = discord.ui.ActionRow()
                nav.add_item(prev_btn)
                nav.add_item(next_btn)
                container.add_item(nav)
                container.add_item(discord.ui.TextDisplay(f"-# Page {self.page + 1}/{pages}"))
            self.add_item(container)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(embed=embed_err("Not Yours", "Run `/shop` to open your own shop."), ephemeral=True)
                return False
            return True

        async def _on_prev(self, interaction: discord.Interaction):
            await interaction.response.edit_message(view=ShopLayout(self.user_id, self.page - 1))

        async def _on_next(self, interaction: discord.Interaction):
            await interaction.response.edit_message(view=ShopLayout(self.user_id, self.page + 1))

        async def _on_select(self, interaction: discord.Interaction):
            await ShopView._on_select(self, interaction)


class ShopConfirmView(discord.ui.View):
    def __init__(self, user_id: int, key: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.key = key

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=embed_err("Not Yours", "Only the buyer can use these buttons."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success)
    async def buy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        lock = SHOP_LOCKS.setdefault(self.user_id, asyncio.Lock())
        async with lock:  # stops double clicks from buying twice
            embed = await self.buy(interaction)
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=embed_info("Cancelled", "Nothing was bought."), view=None)

    async def buy(self, interaction: discord.Interaction) -> discord.Embed:
        uid = str(self.user_id)
        item = get_shop_item(self.key)
        if item is None:
            return embed_err("Item Not Found", "That item is no longer for sale.")
        price = shop_price_for(item, uid)
        discount_used = int(item.get("premium_discount") or 0) if price != int(item["price"]) else 0
        balance = get_user_points(uid)
        if balance < price:
            return embed_err("Not Enough Points", f"You need `{price:,} pts` but you have `{balance:,} pts`.")
        if item["reward"] == "premium" and item.get("permanent") and has_lifetime_premium(uid):
            return embed_err("Already Owned", "You already have lifetime TE Premium.")
        if item["reward"] == "role":
            if not interaction.guild or interaction.guild.id != int(item["guild_id"]):
                return embed_err("Wrong Server", "This role can only be bought in the server it belongs to.")
            role = interaction.guild.get_role(int(item["role_id"]))
            member = interaction.guild.get_member(self.user_id)
            if role is None or member is None:
                return embed_err("Role Missing", "That role does not exist anymore. Tell staff.")
            if role in member.roles:
                return embed_err("Already Owned", f"You already have {role.mention}.")
            if role >= interaction.guild.me.top_role:
                return embed_err("Cannot Give Role", "That role is higher than my role. Tell staff.")
        if not take_stock(self.key):
            return embed_err("Sold Out", f"**{item['name']}** is sold out.")

        new_balance = add_user_points(uid, -price)
        premium_until = None
        try:
            if item["reward"] == "premium" and item.get("permanent"):
                set_user_premium(uid, timedelta(days=LIFETIME_PREMIUM_DAYS))
                premium_until = datetime.now(timezone.utc) + timedelta(days=LIFETIME_PREMIUM_DAYS)
            elif item["reward"] == "premium":
                premium_until = extend_user_premium(uid, int(item["premium_days"]))
            elif item["reward"] == "role":
                await member.add_roles(role, reason=f"Bought in the points shop: {item['name']}")
        except Exception as e:
            print(f"[Shop Delivery Error] {e!r}")
            add_user_points(uid, price)  # refund
            return_stock(self.key)
            return embed_err("Purchase Failed", "Something went wrong, so you were **not** charged.")
        if item.get("stock") is None:
            count_shop_sale(self.key)

        embed = embed_ok("Purchase Complete!", f"You bought **{item['name']}**.")
        embed.add_field(name="Paid", value=f"`{price:,} pts`" + (f" (Premium {discount_used}% off)" if discount_used else ""), inline=True)
        embed.add_field(name="New Balance", value=f"`{new_balance:,} pts`", inline=True)
        if item["reward"] == "premium" and item.get("permanent"):
            embed.add_field(name="TE Premium", value="`Lifetime`", inline=False)
        elif premium_until is not None:
            embed.add_field(name="TE Premium Until", value=f"<t:{int(premium_until.timestamp())}:F>", inline=False)
        ticket_channel = None
        if item["reward"] == "custom":
            if interaction.guild:  # open a private ticket so staff can deliver it
                ticket_channel, _ = await create_ticket(
                    interaction.guild, interaction.user, "order", f"Order: {item['name']}", item.get("description", ""),
                    [("Item", f"`{item['name']}`"), ("Paid", f"`{price:,} pts`")]
                )
            if ticket_channel is not None:
                embed.add_field(name="Your Ticket", value=f"{ticket_channel.mention}\nStaff will deliver your item there.", inline=False)
            else:
                embed.add_field(name="Next Step", value="Staff were told and will deliver it to you soon.", inline=False)

        try:  # purchase log
            channel = interaction.client.get_channel(SHOP_LOG_CHANNEL_ID)
            if channel:
                items = [
                    ("Buyer", f"{interaction.user.mention}\n`{interaction.user.id}`"),
                    ("Item", f"`{item['name']}`"),
                    ("Price", f"`{price:,} pts`" + (f" (Premium {discount_used}% off, normal `{int(item['price']):,} pts`)" if discount_used else "")),
                    ("New Balance", f"`{new_balance:,} pts`"),
                    ("Reward", shop_reward_text(item)),
                ]
                if item["reward"] == "custom":
                    items.append(("Action Needed",
                                  f"Deliver this item in {ticket_channel.mention}." if ticket_channel else "Deliver this item to the buyer."))
                log = log_payload("Shop Purchase", COLOR_GOLD, items)
                await channel.send(**log, allowed_mentions=NO_PINGS)
        except Exception as e:
            print(f"[Shop Log Error] {e!r}")
        return embed


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
    @set_group.command(name="points", description="Add or deduct points for a specified user.")
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
    premium_group = app_commands.Group(name="premium", description="Owner tools for TE Premium.")

    @premium_group.command(name="set", description="Grant or revoke TE Premium subscription status for a user.")
    @app_commands.describe(
        user="Target user to modify premium status for.",
        action="Select whether to grant or revoke premium.",
        duration="Duration format e.g. 1d, 7d, 30d, 1y (Defaults to 30d).",
        permanent="Give premium that never runs out (ignores duration)."
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
        duration: Optional[str] = "30d",
        permanent: Optional[bool] = False
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            if action.value == "add":
                if permanent:
                    duration_td = timedelta(days=36500)  # about 100 years
                else:
                    duration_td = parse_duration_to_timedelta(duration or "30d")
                set_user_premium(str(user.id), duration_td)
                exp_date = datetime.now(timezone.utc) + duration_td
                exp_ts = int(exp_date.timestamp())

                if permanent:
                    text = f"Successfully granted **permanent** TE Premium to {user.mention}!\n\nDuration: `Permanent`\nExpires: `Never`"
                else:
                    text = f"Successfully granted TE Premium to {user.mention}!\n\nDuration: `{duration}`\nExpires: <t:{exp_ts}:F> (<t:{exp_ts}:R>)"
                embed = discord.Embed(title="TE Premium Granted", description=text, color=0xF1C40F)
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

    roblox_group = app_commands.Group(name="roblox", description="Roblox lookups.")

    @roblox_group.command(name="user", description="Look up a Roblox account by username.")
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

    cookie_group = app_commands.Group(name="cookie", description="Roblox cookie tools (owner only).")

    @cookie_group.command(name="check", description="Check if the saved Roblox cookie is still valid (owner only).")
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

    async def rolelist_cmd(self, interaction: discord.Interaction):
        roles = [r for r in reversed(interaction.guild.roles) if not r.is_default()]
        lines = [f"{r.mention} | `{r.id}` | {len(r.members)} members" for r in roles]
        pages = build_pages(f"Roles in {interaction.guild.name}", lines, per_page=20, header=f"Total roles: `{len(roles)}`\n\n")
        await send_pages(interaction, pages, ephemeral=False)

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

    async def say_cmd(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 2000], channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        except Exception as e:
            await interaction.response.send_message(embed=embed_err("Could Not Send", f"`{e}`"), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_ok("Message Sent", f"Posted in {channel.mention}."), ephemeral=True)

    async def embed_cmd(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await interaction.response.send_modal(EmbedBuilderModal(channel or interaction.channel))

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

    async def warn_cmd(self, interaction: discord.Interaction, user: discord.Member, reason: str, moderator_message: Optional[str] = None):
        problem = check_hierarchy(interaction, user)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Warn", problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = add_case(interaction.guild.id, user.id, "warn", reason, interaction.user.id, moderator_message=moderator_message)
        dm_sent = await dm_moderation_notice(user, interaction.guild, "warn", reason, case["case_id"], moderator_message=moderator_message)
        await interaction.followup.send(embed=self._mod_result("Member Warned", user, case, dm_sent), ephemeral=True, allowed_mentions=NO_PINGS)

    async def mute_cmd(
        self, interaction: discord.Interaction, user: discord.Member, duration: str,
        reason: str = "No reason provided", moderator_message: Optional[str] = None
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

        case = add_case(interaction.guild.id, user.id, "mute", reason, interaction.user.id, duration=format_timedelta(delta), moderator_message=moderator_message)
        dm_sent = await dm_moderation_notice(user, interaction.guild, "mute", reason, case["case_id"], duration=format_timedelta(delta), moderator_message=moderator_message)
        until = int((datetime.now(timezone.utc) + delta).timestamp())
        await interaction.followup.send(
            embed=self._mod_result("Member Muted", user, case, dm_sent, {"Duration": f"`{format_timedelta(delta)}`", "Ends": f"<t:{until}:R>"}),
            ephemeral=True, allowed_mentions=NO_PINGS
        )

    async def unmute_cmd(self, interaction: discord.Interaction, user: discord.Member, moderator_message: Optional[str] = None):
        if not user.is_timed_out():
            await interaction.response.send_message(embed=embed_err("Not Muted", f"{user.mention} is not muted."), ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await user.timeout(None, reason=f"Unmuted by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need the `Moderate Members` permission."), ephemeral=True)
            return
        case = add_case(interaction.guild.id, user.id, "unmute", "Mute removed", interaction.user.id, moderator_message=moderator_message)
        dm_sent = await dm_moderation_notice(user, interaction.guild, "unmute", "Your mute was removed.", case["case_id"], moderator_message=moderator_message)
        await interaction.followup.send(embed=self._mod_result("Member Unmuted", user, case, dm_sent), ephemeral=True, allowed_mentions=NO_PINGS)

    async def kick_cmd(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided", moderator_message: Optional[str] = None):
        problem = check_hierarchy(interaction, user)
        if problem:
            await interaction.response.send_message(embed=embed_err("Cannot Kick", problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        case_id = None
        # DM first: after a kick the bot may no longer be able to reach the user
        case = add_case(interaction.guild.id, user.id, "kick", reason, interaction.user.id, moderator_message=moderator_message)
        case_id = case["case_id"]
        dm_sent = await dm_moderation_notice(user, interaction.guild, "kick", reason, case_id, moderator_message=moderator_message)
        try:
            await user.kick(reason=f"{reason} | by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            delete_cases(interaction.guild.id, user.id, "kick", case_id)
            await interaction.followup.send(embed=embed_err("Missing Permission", "I need the `Kick Members` permission."), ephemeral=True)
            return
        await interaction.followup.send(embed=self._mod_result("Member Kicked", user, case, dm_sent), ephemeral=True, allowed_mentions=NO_PINGS)

    async def ban_cmd(
        self, interaction: discord.Interaction, user: discord.User,
        reason: str = "No reason provided",
        delete_days: int = 0, moderator_message: Optional[str] = None
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
        case = add_case(interaction.guild.id, user.id, "ban", reason, interaction.user.id, moderator_message=moderator_message)
        dm_sent = await dm_moderation_notice(user, interaction.guild, "ban", reason, case["case_id"], moderator_message=moderator_message) if member else None
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
                    notes.append("This user is still muted. Use `/mod action:Unmute` to remove the mute.")
            elif case["action"] == "ban":
                try:
                    await guild.fetch_ban(discord.Object(id=uid))
                    notes.append("This user is still banned. Use `/mod action:Unban` to unban them.")
                except discord.NotFound:
                    pass
        except Exception as e:
            print(f"[Clear Note Error] {e}")
        if notes:
            embed.add_field(name="Note", value="\n".join(notes), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

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

    async def modlog_cmd(self, interaction: discord.Interaction, user: discord.User):
        cases = get_cases(interaction.guild.id, user.id)
        entries = []
        for c in cases:
            label = MOD_ACTION_LABELS.get(c["action"], c["action"].title())
            head = f"Case `#{c['case_id']}` | **{label}** | <t:{int(c['created_at'])}:R>"
            if c.get("duration"):
                head += f" | `{c['duration']}`"
            note = f"\nMessage to user: {str(c['moderator_message'])[:200]}" if c.get("moderator_message") else ""
            entries.append(f"{head}\nReason: {str(c['reason'])[:200]}{note}\nModerator: <@{c['moderator_id']}>\n")

        pages = build_pages(
            f"Moderation Log | {user.name}", entries, per_page=10,
            header=f"{user.mention} (`{user.id}`)\nTotal records: `{len(entries)}`\nRemove a record with `/cases action:Remove` and its case ID.\n\n"
        )
        if not entries:
            pages = [embed_info(f"Moderation Log | {user.name}", f"{user.mention} has no moderation records.")]
        await send_pages(interaction, pages, ephemeral=True)

    @app_commands.command(name="settings", description="Your personal settings: bot messages, inbox notifications, blocked users and website theme.")
    async def settings_cmd(self, interaction: discord.Interaction):
        view = SettingsView(self.bot, interaction.user.id)
        s = get_user_settings(interaction.user.id)
        await view.build_items(s)
        await interaction.response.send_message(embed=await view.build_embed(s), view=view, ephemeral=True)

    # =============================================================
    # MERGED COMMANDS: one command, many actions
    # =============================================================
    @app_commands.command(name="mod", description="Moderate a user: warn, mute, unmute, kick, ban or unban.")
    @app_commands.describe(
        action="What to do.",
        user="The user.",
        duration="Mute only: for example 10m, 2h, 1d (max 28d).",
        reason="Why. Saved in the log and shown to the user.",
        moderator_message="A message for the user. It is sent in their DM.",
        delete_days="Ban only: delete their messages from the last 0-7 days."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Warn", value="warn"),
        app_commands.Choice(name="Mute", value="mute"),
        app_commands.Choice(name="Unmute", value="unmute"),
        app_commands.Choice(name="Kick", value="kick"),
        app_commands.Choice(name="Ban", value="ban"),
        app_commands.Choice(name="Unban", value="unban"),
    ])
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def mod_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.User,
        duration: Optional[str] = None,
        reason: Optional[app_commands.Range[str, 1, 500]] = None,
        moderator_message: Optional[app_commands.Range[str, 1, 500]] = None,
        delete_days: Optional[app_commands.Range[int, 0, 7]] = 0
    ):
        act = action.value
        if act == "unban":
            await self.unban_cmd(interaction, str(user.id))
            return
        if act == "ban":
            await self.ban_cmd(interaction, user, reason or "No reason provided", delete_days or 0, moderator_message)
            return

        member = interaction.guild.get_member(user.id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(user.id)
            except Exception:
                member = None
        if member is None:
            await interaction.response.send_message(
                embed=embed_err("Not In Server", f"{user.mention} is not a member of this server. Only `Ban` and `Unban` work on people who are not in the server."),
                ephemeral=True, allowed_mentions=NO_PINGS
            )
            return

        if act == "warn":
            if not reason:
                await interaction.response.send_message(embed=embed_err("Reason Needed", "Warnings need a `reason`."), ephemeral=True)
                return
            await self.warn_cmd(interaction, member, reason, moderator_message)
        elif act == "mute":
            if not duration:
                await interaction.response.send_message(embed=embed_err("Duration Needed", "Muting needs a `duration`, for example `10m`, `2h` or `1d`."), ephemeral=True)
                return
            await self.mute_cmd(interaction, member, duration, reason or "No reason provided", moderator_message)
        elif act == "unmute":
            await self.unmute_cmd(interaction, member, moderator_message)
        else:
            await self.kick_cmd(interaction, member, reason or "No reason provided", moderator_message)

    @app_commands.command(name="channel", description="Channel tools: lock, unlock, slowmode or purge.")
    @app_commands.describe(
        action="What to do.",
        channel="Channel (defaults to this one). Purge always works in this channel.",
        duration="Slowmode only: 5s, 30s, 2m, 1h (max 6h) or off.",
        amount="Purge only: how many messages to delete (1-1000).",
        user="Purge only: delete only this user's messages."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Lock", value="lock"),
        app_commands.Choice(name="Unlock", value="unlock"),
        app_commands.Choice(name="Slowmode", value="slowmode"),
        app_commands.Choice(name="Purge", value="purge"),
    ])
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def channel_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
        duration: Optional[str] = None,
        amount: Optional[app_commands.Range[int, 1, 1000]] = None,
        user: Optional[discord.User] = None
    ):
        act = action.value
        if act == "lock":
            await self.lock_cmd(interaction, channel)
        elif act == "unlock":
            await self.unlock_cmd(interaction, channel)
        elif act == "slowmode":
            if not duration:
                await interaction.response.send_message(embed=embed_err("Duration Needed", "Slowmode needs a `duration`, for example `5s`, `2m` or `off`."), ephemeral=True)
                return
            await self.slowmode_cmd(interaction, duration, channel)
        else:
            if not amount:
                await interaction.response.send_message(embed=embed_err("Amount Needed", "Purge needs an `amount` from 1 to 1000."), ephemeral=True)
                return
            await self.purge_cmd(interaction, amount, user)

    @app_commands.command(name="info", description="Show info: a member, their permissions, the role list or channel stats.")
    @app_commands.describe(type="What to show.", user="Member or Permissions only: who (defaults to you).")
    @app_commands.choices(type=[
        app_commands.Choice(name="Member", value="member"),
        app_commands.Choice(name="Permissions", value="permissions"),
        app_commands.Choice(name="Roles", value="roles"),
        app_commands.Choice(name="Channels", value="channels"),
    ])
    @app_commands.guild_only()
    async def info_cmd(self, interaction: discord.Interaction, type: app_commands.Choice[str], user: Optional[discord.Member] = None):
        if type.value == "member":
            await self.whois_cmd(interaction, user)
        elif type.value == "permissions":
            await self.permissions_cmd(interaction, user)
        elif type.value == "roles":
            await self.rolelist_cmd(interaction)
        else:
            await self.channelstats_cmd(interaction)

    @app_commands.command(name="cases", description="Moderation log: view a user's cases or remove a case.")
    @app_commands.describe(action="What to do.", user="View only: the user.", case_id="Remove only: the case ID, e.g. 12.")
    @app_commands.choices(action=[
        app_commands.Choice(name="View", value="view"),
        app_commands.Choice(name="Remove", value="remove"),
    ])
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def cases_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: Optional[discord.User] = None,
        case_id: Optional[app_commands.Range[int, 1, 100000000]] = None
    ):
        if action.value == "view":
            if not user:
                await interaction.response.send_message(embed=embed_err("User Needed", "Viewing cases needs a `user`."), ephemeral=True)
                return
            await self.modlog_cmd(interaction, user)
        else:
            if not case_id:
                await interaction.response.send_message(embed=embed_err("Case ID Needed", "Removing a case needs a `case_id`."), ephemeral=True)
                return
            await self.clear_cmd(interaction, case_id)

    @app_commands.command(name="send", description="Send as the bot: a plain message, a custom embed or an announcement.")
    @app_commands.describe(
        type="What to send.",
        channel="Where to send it (defaults to this channel).",
        message="Message only: what the bot should say.",
        ping="Announcement only: optional @everyone or @here ping.",
        role="Announcement only: optional role to ping."
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Message", value="message"),
            app_commands.Choice(name="Embed", value="embed"),
            app_commands.Choice(name="Announcement", value="announce"),
        ],
        ping=[
            app_commands.Choice(name="No ping", value="none"),
            app_commands.Choice(name="@everyone", value="everyone"),
            app_commands.Choice(name="@here", value="here"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def send_cmd(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
        message: Optional[app_commands.Range[str, 1, 2000]] = None,
        ping: Optional[app_commands.Choice[str]] = None,
        role: Optional[discord.Role] = None
    ):
        if type.value == "message":
            if not message:
                await interaction.response.send_message(embed=embed_err("Message Needed", "Add the `message` the bot should say."), ephemeral=True)
                return
            await self.say_cmd(interaction, message, channel)
        elif type.value == "embed":
            await self.embed_cmd(interaction, channel)
        else:
            await self.announce_cmd(interaction, channel or interaction.channel, ping, role)

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
                           reward_premium_days="Optional. Days of TE Premium given to each winner (30 = one month).",
                           luck="Optional. How much more likely luck-role members are to win.",
                           luck_role="Role that gets the luck boost (needs luck).",
                           luck_role_2="Another role that gets the luck boost.",
                           luck_role_3="Another role that gets the luck boost.",
                           ping_everyone="True pings @everyone when the giveaway starts (default False).",
                           host_message="A message from the host, shown on the giveaway.")
    @app_commands.choices(luck=[
        app_commands.Choice(name="2x luck", value=2), app_commands.Choice(name="3x luck", value=3),
        app_commands.Choice(name="5x luck", value=5), app_commands.Choice(name="10x luck", value=10),
    ])
    async def giveaway_create_cmd(
        self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str,
        winners: Optional[app_commands.Range[int, 1, 25]] = 1, channel: Optional[discord.TextChannel] = None,
        reward_points: Optional[app_commands.Range[int, 1, 1000000]] = None,
        reward_premium_days: Optional[app_commands.Range[int, 1, 3650]] = None,
        luck: Optional[app_commands.Choice[int]] = None,
        luck_role: Optional[discord.Role] = None,
        luck_role_2: Optional[discord.Role] = None,
        luck_role_3: Optional[discord.Role] = None,
        ping_everyone: Optional[bool] = False,
        host_message: Optional[app_commands.Range[str, 1, 500]] = None
    ):
        luck_roles = list(dict.fromkeys(r.id for r in (luck_role, luck_role_2, luck_role_3) if r is not None))
        if bool(luck) != bool(luck_roles):
            await interaction.response.send_message(embed=embed_err("Luck Needs Both Options", "Choose the `luck` amount and at least one `luck_role`, or leave both empty."), ephemeral=True)
            return
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
        if ping_everyone and not perms.mention_everyone:
            await interaction.response.send_message(embed=embed_err("Missing Permission", f"I need `Mention Everyone` in {channel.mention} to ping @everyone. Give me the permission or set `ping_everyone` to False."), ephemeral=True)
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
            "luck": int(luck.value) if luck else 1,
            "luck_roles": [str(r) for r in luck_roles],
            "host_message": host_message or "",
        }
        try:
            msg = await channel.send(allowed_mentions=NO_PINGS, **giveaway_payload(gw))
        except Exception as e:
            await interaction.followup.send(embed=embed_err("Could Not Post Giveaway", f"`{e}`"), ephemeral=True)
            return
        gw["message_id"] = str(msg.id)
        save_giveaway(gw)
        if ping_everyone:
            try:
                await channel.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
            except Exception as e:
                print(f"[Giveaway Ping Error] {e!r}")
        embed = embed_ok("Giveaway Created", f"Posted in {channel.mention}.\n[Jump to giveaway]({msg.jump_url})")
        if giveaway_reward_text(gw):
            embed.add_field(name="Auto Reward", value=f"`{giveaway_reward_text(gw)}` for each winner", inline=False)
        if giveaway_luck_text(gw):
            embed.add_field(name="Luck Boost", value=giveaway_luck_text(gw).replace("**Luck:** ", ""), inline=False)
        if host_message:
            embed.add_field(name="Message From Host", value=host_message, inline=False)
        embed.add_field(name="Ping", value="`@everyone`" if ping_everyone else "`No ping`", inline=True)
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
    # FREE MODE: /free (on, off, status)
    # =============================================================
    @app_commands.command(name="free", description="Make server builds free for everyone, or turn it off again (owner only).")
    @app_commands.describe(action="Turn free mode on, off, or check it.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Turn On (builds are free)", value="on"),
        app_commands.Choice(name="Turn Off (back to paid)", value="off"),
        app_commands.Choice(name="Status", value="status"),
    ])
    async def free_cmd(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        if action.value == "status":
            on = is_free_mode()
            await interaction.followup.send(embed=embed_info("Free Mode", f"Free mode is **{'ON' if on else 'OFF'}**.", COLOR_GOLD if on else COLOR_GRAY), ephemeral=True)
            return
        want = action.value == "on"
        saved = set_free_mode(want, str(interaction.user.id))
        if want:
            embed = embed_ok("Free Mode Is ON", "Server builds need **no payment** now. Standard members are built right away, with no DM and no points.")
        else:
            embed = embed_ok("Free Mode Is OFF", "Builds are paid again (Robux, points or TE Premium).")
        embed.add_field(name="Change It Back", value="`/free`", inline=False)
        if not saved:
            embed.add_field(name="Warning", value="It could not be saved to the database, so the website may not show the change.", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # =============================================================
    # WEBSITE THEME: /theme set, /theme view
    # =============================================================
    theme_group = app_commands.Group(name="theme", description="Website theme tools (owner only).")

    @theme_group.command(name="set", description="Change the website theme, for example for a season.")
    @app_commands.describe(theme="The theme to show on the website.")
    @app_commands.choices(theme=[app_commands.Choice(name=label, value=key) for key, label in SITE_THEMES.items()])
    async def theme_set(self, interaction: discord.Interaction, theme: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        old = get_site_theme()
        saved = set_site_theme(theme.value)
        embed = embed_ok("Website Theme Changed", "The website will show the new theme the next time a page loads.")
        embed.add_field(name="Old Theme", value=f"`{SITE_THEMES.get(old, old)}`", inline=True)
        embed.add_field(name="New Theme", value=f"`{SITE_THEMES[theme.value]}`", inline=True)
        if WEBSITE_URL:
            embed.add_field(name="Website", value=WEBSITE_URL, inline=False)
        if not saved:
            embed.add_field(name="Warning", value="The theme could not be saved to the database. The website may not change.", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @theme_group.command(name="view", description="Show the current website theme and all themes.")
    async def theme_view(self, interaction: discord.Interaction):
        current = get_site_theme()
        embed = embed_info("Website Theme", f"Current theme: `{SITE_THEMES.get(current, current)}`", COLOR_PURPLE)
        embed.add_field(name="All Themes", value="\n".join(f"`{label}`" for label in SITE_THEMES.values()), inline=False)
        embed.add_field(name="Change It", value="`/theme set`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # =============================================================
    # PRICES: /change price
    # =============================================================
    change_group = app_commands.Group(name="change", description="Owner change tools.")

    @change_group.command(name="price", description="Change a price: dev product, subscription display, or points (owner only).")
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

    @change_group.command(name="limits", description="Change the free limits for standard members (owner only).")
    @app_commands.describe(
        ai_per_day="AI generations per day on the website. 0 = no limit.",
        builds_per_week="Server builds per week. 0 = no limit."
    )
    async def change_limits_cmd(
        self,
        interaction: discord.Interaction,
        ai_per_day: Optional[app_commands.Range[int, 0, 1000]] = None,
        builds_per_week: Optional[app_commands.Range[int, 0, 1000]] = None
    ):
        changed = []
        if ai_per_day is not None:
            set_limit("ai_per_day", ai_per_day)
            changed.append("ai_per_day")
        if builds_per_week is not None:
            set_limit("builds_per_week", builds_per_week)
            changed.append("builds_per_week")
        limits = get_limits()

        def show(v):
            return "`no limit`" if v == 0 else f"`{v}`"

        embed = discord.Embed(title="Free Limits" if not changed else "Free Limits Updated", color=COLOR_GOLD)
        embed.add_field(name="AI Generations Per Day", value=show(limits["ai_per_day"]), inline=True)
        embed.add_field(name="Builds Per Week", value=show(limits["builds_per_week"]), inline=True)
        embed.add_field(name="Who", value="Standard members only. TE Premium has no limits. Days and weeks use Tokyo time.", inline=False)
        if not changed:
            embed.add_field(name="Change It", value="`/change limits ai_per_day:5 builds_per_week:2`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
    staff_group = app_commands.Group(name="staff", description="Staff tools. Staff get TE Premium automatically.")

    @staff_group.command(name="add", description="Add a staff member. They get TE Premium and the saved staff role.")
    @app_commands.describe(user="The new staff member.")
    async def staff_add_cmd(self, interaction: discord.Interaction, user: discord.User):
        await self._staff_action(interaction, "add", user)

    @staff_group.command(name="remove", description="Remove a staff member and their TE Premium.")
    @app_commands.describe(user="The staff member to remove.")
    async def staff_remove_cmd(self, interaction: discord.Interaction, user: discord.User):
        await self._staff_action(interaction, "remove", user)

    async def _staff_action(self, interaction: discord.Interaction, action: str, user: discord.User):
        if action == "add":
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
                embed.add_field(name="Role", value="No staff role is set. Pick one in `/staff list`.", inline=False)
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

    @app_commands.command(name="premiums", description="Show everyone with TE Premium (owner only).")
    async def premiums_cmd(self, interaction: discord.Interaction):
        ids = list_premium_users()
        if not ids:
            await send_pages(interaction, [embed_info("TE Premium Users", "Nobody has TE Premium right now.")], ephemeral=True)
            return
        lines = [f"<@{uid}>\n`{uid}`\n" for uid in ids]
        pages = build_pages("TE Premium Users", lines, per_page=10, header=f"Premium users: `{len(lines)}`\n\n")
        await send_pages(interaction, pages, ephemeral=True)

    @staff_group.command(name="list", description="Show staff (one per line) and choose the role new staff get.")
    async def staff_list_cmd(self, interaction: discord.Interaction):
        view = StaffListView(interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True, allowed_mentions=NO_PINGS)


    # =============================================================
    # SERVER SETTINGS: join / leave messages and message log
    # =============================================================
    @app_commands.command(name="greeting", description="Set the join or leave message and the channel it is sent in.")
    @app_commands.describe(
        action="Which message to set.",
        channel="Where to send it. Keeps the old channel if empty.",
        message="Use {user} {username} {server} {count} {id}. Empty keeps the old or default message.",
        enabled="Turn this message on or off (default on)."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Join (new member)", value="join"),
        app_commands.Choice(name="Leave (member left)", value="leave"),
    ])
    @app_commands.guild_only()
    async def greeting_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
        message: Optional[app_commands.Range[str, 1, 1500]] = None,
        enabled: Optional[bool] = True
    ):
        kind = action.value
        cfg = get_guild_config(interaction.guild.id)
        entry = dict(cfg.get(kind) or {})

        if channel is not None:
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                await interaction.response.send_message(
                    embed=embed_err("Missing Permission", f"I need `View Channel`, `Send Messages` and `Embed Links` in {channel.mention}."), ephemeral=True)
                return
            entry["channel_id"] = channel.id
        if not entry.get("channel_id"):
            await interaction.response.send_message(
                embed=embed_err("Channel Needed", "Choose a `channel` the first time you set this message."), ephemeral=True)
            return
        if message is not None:
            entry["message"] = message
        entry.setdefault("message", DEFAULT_GREETINGS[kind])
        entry["enabled"] = bool(enabled)

        cfg[kind] = entry
        saved = save_guild_config(interaction.guild.id)

        label = "Join" if kind == "join" else "Leave"
        status = "`On`" if entry["enabled"] else "`Off`"
        embed = embed_ok(f"{label} Message Saved", f"Channel: <#{entry['channel_id']}>\nStatus: {status}")
        embed.add_field(name="Your Message", value=f"```{entry['message'][:900]}```", inline=False)
        embed.add_field(name="Placeholders", value="`{user}` `{username}` `{server}` `{count}` `{id}`", inline=False)
        if not saved:
            embed.add_field(name="Warning", value="The database could not be reached. This only lasts until the bot restarts.", inline=False)
        preview = build_greeting_embed(kind, entry["message"], interaction.user, interaction.guild)
        preview.set_author(name="Preview (this is how it will look)")
        await interaction.response.send_message(embeds=[embed, preview], ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="messagelog", description="Set the channel that logs edited and deleted messages.")
    @app_commands.describe(
        channel="The log channel.",
        enabled="Turn the message log on or off (default on)."
    )
    @app_commands.guild_only()
    async def messagelog_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        enabled: Optional[bool] = True
    ):
        cfg = get_guild_config(interaction.guild.id)
        if not enabled:
            cfg.pop("message_log", None)
            saved = save_guild_config(interaction.guild.id)
            embed = embed_ok("Message Log Off", "Edited and deleted messages are no longer logged.")
        else:
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                await interaction.response.send_message(
                    embed=embed_err("Missing Permission", f"I need `View Channel`, `Send Messages` and `Embed Links` in {channel.mention}."), ephemeral=True)
                return
            cfg["message_log"] = channel.id
            saved = save_guild_config(interaction.guild.id)
            embed = embed_ok("Message Log On", f"Edited and deleted messages will be logged in {channel.mention}.")
            embed.add_field(name="Logged", value="`Edited messages`\n`Deleted messages`", inline=False)
            embed.add_field(name="Note", value="Messages sent before the bot started may show as `Unknown`.", inline=False)
        if not saved:
            embed.add_field(name="Warning", value="The database could not be reached. This only lasts until the bot restarts.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # =============================================================
    # SERVER BACKUPS
    # =============================================================
    async def backup_id_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        items = list_backups(None if interaction.user.id in COMMAND_ADMINS or str(interaction.user.id) in COMMAND_ADMINS else interaction.guild.id)
        text = current.strip().upper()
        return [
            app_commands.Choice(name=f"{b['backup_id']} | {b['guild_name']}"[:100], value=b["backup_id"])
            for b in items if text in b["backup_id"]
        ][:25]

    @app_commands.command(name="backup", description="Save, view, list, restore or delete a backup of this server.")
    @app_commands.describe(action="What to do.", backup_id="The backup ID, e.g. TE-BK-00001 (not needed for Create and List).")
    @app_commands.choices(action=[
        app_commands.Choice(name="Create", value="create"),
        app_commands.Choice(name="View", value="view"),
        app_commands.Choice(name="List", value="list"),
        app_commands.Choice(name="Restore", value="restore"),
        app_commands.Choice(name="Undo Last Build", value="undo"),
        app_commands.Choice(name="Delete", value="delete"),
    ])
    @app_commands.autocomplete(backup_id=backup_id_autocomplete)
    @app_commands.guild_only()
    async def backup_cmd(self, interaction: discord.Interaction, action: app_commands.Choice[str], backup_id: Optional[str] = None):
        guild = interaction.guild
        is_admin_user = str(interaction.user.id) in COMMAND_ADMINS
        act = action.value

        if act == "create":
            await interaction.response.defer(ephemeral=True)
            if len([b for b in list_backups(guild.id) if not b.get("auto_build")]) >= BACKUP_LIMIT_PER_GUILD:
                await interaction.followup.send(embed=embed_err("Backup Limit Reached", f"You can keep `{BACKUP_LIMIT_PER_GUILD}` backups. Delete one first with `/backup` Delete."), ephemeral=True)
                return
            backup = snapshot_guild(guild, interaction.user.id)
            saved = save_backup(backup)
            embed = backup_embed(backup, "Backup Successfully Created", "Your server configuration has been saved.")
            if not saved:
                embed.add_field(name="Warning", value="The database could not be reached. This backup only lasts until the bot restarts.", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if act == "undo":
            auto = latest_auto_backup(guild.id)
            if auto is None:
                await interaction.response.send_message(embed=embed_err("Nothing To Undo", "There is no saved build for this server. TE Premium builds save a backup first."), ephemeral=True)
                return
            if interaction.user.id != guild.owner_id and not is_admin_user and str(auto.get("created_by")) != str(interaction.user.id):
                await interaction.response.send_message(embed=embed_err("Not Allowed", "Only the server owner or the person who built the layout can undo it."), ephemeral=True)
                return
            me_perms = guild.me.guild_permissions
            if not (me_perms.administrator or (me_perms.manage_channels and me_perms.manage_roles and me_perms.manage_guild)):
                await interaction.response.send_message(embed=embed_err("Missing Permission", "I need `Administrator` (or Manage Channels, Manage Roles and Manage Server) to undo a build."), ephemeral=True)
                return
            view = BackupConfirmView(self, interaction.user.id, "restore", auto)
            embed = backup_embed(
                auto, "Undo Last Build?",
                "**This deletes ALL current channels, categories and roles** and brings back the server as it was before the last TE Premium build.\nMessages in deleted channels are lost for good.",
                COLOR_RED
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        if act == "list":
            backups = list_backups(None if is_admin_user else guild.id)
            if not backups:
                await send_pages(interaction, [embed_info("Server Backups", "There are no backups yet. Use `/backup` Create.")], ephemeral=True)
                return
            lines = []
            for b in backups:
                ch, ro, ca = backup_counts(b)
                lines.append(f"`{b['backup_id']}` | **{b['guild_name']}** | `{ch}` channels, `{ro}` roles | <t:{int(b['created_at'])}:R>")
            pages = build_pages("Server Backups", lines, per_page=10, header=f"Backups: `{len(lines)}`\n\n")
            await send_pages(interaction, pages, ephemeral=True)
            return

        # View / Restore / Delete need an ID
        if not backup_id:
            await interaction.response.send_message(embed=embed_err("Backup ID Needed", "Give the `backup_id`, for example `TE-BK-00001`. Use List to see your backups."), ephemeral=True)
            return
        backup = get_backup(backup_id)
        if backup is None or (backup["guild_id"] != guild.id and not is_admin_user):
            await interaction.response.send_message(embed=embed_err("Backup Not Found", f"No backup `{normalize_backup_id(backup_id)}` for this server."), ephemeral=True)
            return

        if act == "view":
            await interaction.response.send_message(embed=backup_embed(backup, "Backup Details", "Saved server configuration.", COLOR_PURPLE, detailed=True), ephemeral=True)
            return

        if act == "delete":
            view = BackupConfirmView(self, interaction.user.id, "delete", backup)
            embed = backup_embed(backup, "Delete This Backup?", "This cannot be undone.", COLOR_RED)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        # Restore: only the server owner (or the main staff) can do this
        if interaction.user.id != guild.owner_id and not is_admin_user:
            await interaction.response.send_message(embed=embed_err("Owner Only", "Only the server owner can restore a backup."), ephemeral=True)
            return
        if backup["guild_id"] != guild.id and not is_admin_user:
            await interaction.response.send_message(embed=embed_err("Wrong Server", "This backup was made in another server."), ephemeral=True)
            return
        me_perms = guild.me.guild_permissions
        if not (me_perms.administrator or (me_perms.manage_channels and me_perms.manage_roles and me_perms.manage_guild)):
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I need `Administrator` (or Manage Channels, Manage Roles and Manage Server) to restore a backup."), ephemeral=True)
            return
        view = BackupConfirmView(self, interaction.user.id, "restore", backup)
        embed = backup_embed(
            backup, "Restore This Backup?",
            "**This deletes ALL current channels, categories and roles in this server** and rebuilds them from the backup.\nMessages in deleted channels are lost for good.",
            COLOR_RED
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def run_restore(self, interaction: discord.Interaction, backup: dict):
        guild = interaction.guild
        features = self.bot.get_cog("FeaturesCog")
        try:
            await features.nuke_guild_structure(guild)
            result = await restore_guild(guild, backup)
        except Exception as e:
            print(f"[Backup Restore Error] {e!r}")
            await interaction.edit_original_response(embed=embed_err("Restore Failed", f"`{str(e)[:500]}`"), view=None)
            return
        made, problems = result["made"], result["problems"]
        if backup.get("auto_build"):
            delete_backup(backup["backup_id"])  # used up, so the next undo goes one build further back
        embed = backup_embed(backup, "Backup Restored", "The server was rebuilt from the backup.")
        embed.add_field(name="Created", value=f"`{made['roles']}` roles, `{made['categories']}` categories, `{made['channels']}` channels", inline=False)
        if problems:
            embed.add_field(name=f"Problems ({len(problems)})", value="\n".join(f"\u2022 {p[:150]}" for p in problems[:8])[:1000], inline=False)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except Exception as e:
            print(f"[Backup Restore Reply Error] {e!r}")


    # =============================================================
    # POINTS SHOP
    # =============================================================
    @app_commands.command(name="shop", description="Spend your points on rewards.")
    async def shop_cmd(self, interaction: discord.Interaction):
        if HAS_CARD_LAYOUT:
            await interaction.response.send_message(view=ShopLayout(interaction.user.id), ephemeral=True)
            return
        view = ShopView(interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @app_commands.command(name="sell", description="Add or remove an item in the points shop (owner only).")
    @app_commands.describe(
        action="Add a new item or remove one.",
        name="The item name.",
        price="Price in points (for Add).",
        reward="What the buyer gets (for Add).",
        premium_days="Days of TE Premium (for the Premium reward).",
        permanent="Sell lifetime TE Premium that never runs out (no premium_days needed).",
        role="The role to give (for the Role reward).",
        stock="Optional. How many can be sold. Empty = unlimited.",
        premium_discount="Optional. Percent off for TE Premium members (10 = 10% cheaper).",
        description="Optional. Shown when someone buys. Good for Custom items."
    )
    @app_commands.choices(
        action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")],
        reward=[
            app_commands.Choice(name="TE Premium", value="premium"),
            app_commands.Choice(name="Role", value="role"),
            app_commands.Choice(name="Custom (staff delivers)", value="custom"),
        ]
    )
    async def sell_cmd(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        name: app_commands.Range[str, 1, 60],
        price: Optional[app_commands.Range[int, 1, 10000000]] = None,
        reward: Optional[app_commands.Choice[str]] = None,
        premium_days: Optional[app_commands.Range[int, 1, 3650]] = None,
        permanent: Optional[bool] = False,
        role: Optional[discord.Role] = None,
        stock: Optional[app_commands.Range[int, 1, 1000000]] = None,
        premium_discount: Optional[app_commands.Range[int, 1, 90]] = None,
        description: Optional[app_commands.Range[str, 1, 300]] = None
    ):
        if action.value == "remove":
            if remove_shop_item(name):
                await interaction.response.send_message(embed=embed_ok("Item Removed", f"`{name}` is no longer for sale."), ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed_err("Item Not Found", f"No item named `{name}`."), ephemeral=True)
            return

        if price is None or reward is None:
            await interaction.response.send_message(embed=embed_err("Missing Options", "To add an item, give `price` and `reward`."), ephemeral=True)
            return
        if reward.value == "premium" and premium_days is None and not permanent:
            await interaction.response.send_message(embed=embed_err("Missing Options", "The TE Premium reward needs `premium_days` (30 = one month) or `permanent: True` for lifetime."), ephemeral=True)
            return
        if reward.value == "role":
            if role is None or not interaction.guild:
                await interaction.response.send_message(embed=embed_err("Missing Options", "The Role reward needs a `role`, and you must run this in a server."), ephemeral=True)
                return
            if role.is_default() or role.managed:
                await interaction.response.send_message(embed=embed_err("Cannot Use This Role", "Pick a normal role (not @everyone and not a bot role)."), ephemeral=True)
                return
        if reward.value == "custom" and not description:
            await interaction.response.send_message(embed=embed_err("Missing Options", "A Custom item needs a `description` so the buyer knows what they get."), ephemeral=True)
            return

        item = {
            "key": shop_key(name), "name": name.strip(), "price": int(price), "reward": reward.value,
            "premium_days": int(premium_days or 0), "permanent": bool(permanent and reward.value == "premium"), "role_id": role.id if role else None,
            "guild_id": interaction.guild.id if interaction.guild else None,
            "stock": int(stock) if stock else None, "description": description or "",
            "premium_discount": int(premium_discount or 0), "sold": 0, "created_at": datetime.now(timezone.utc).timestamp(),
        }
        if not add_shop_item(item):
            await interaction.response.send_message(embed=embed_err("Item Exists", f"`{name}` is already in the shop. Remove it first to change it."), ephemeral=True)
            return
        embed = embed_ok("Item Added", "It is now in `/shop`.")
        embed.add_field(name="Item", value=f"`{item['name']}`", inline=True)
        embed.add_field(name="Price", value=f"`{item['price']:,} pts`", inline=True)
        if item["premium_discount"]:
            embed.add_field(name="Premium Price", value=f"`{max(1, round(item['price'] * (100 - item['premium_discount']) / 100)):,} pts` ({item['premium_discount']}% off)", inline=True)
        embed.add_field(name="Stock", value=f"`{shop_stock_text(item)}`", inline=True)
        embed.add_field(name="Reward", value=shop_reward_text(item), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)


    # =============================================================
    # AI SERVER CONSULTANT
    # =============================================================
    @app_commands.command(name="analyze", description="AI server consultant: analyze this server and suggest improvements.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: i.guild_id)
    async def analyze_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        summary, findings = build_server_summary(guild)

        try:
            import ai_brain
            result = await asyncio.wait_for(ai_brain.analyze_server(summary, findings), timeout=90)
        except Exception as e:
            print(f"[Analyze Error] {e!r}")
            result = {"success": False, "error": str(e)}

        if not result.get("success"):
            # The AI is down: still show what the automatic checks found
            embed = embed_info("AI Server Consultant", "The AI could not answer right now, so this is only the automatic check.", COLOR_GOLD)
            embed.add_field(name="Problems Found", value="\n".join(f"\u2022 {f}" for f in findings)[:1000] or "Nothing found.", inline=False)
            embed.add_field(name="AI Error", value=f"```{str(result.get('error'))[:300]}```", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        data = result["data"]
        score = int(data["score"])
        color = COLOR_GREEN if score >= 75 else (COLOR_GOLD if score >= 50 else COLOR_RED)
        bar = "\u2588" * (score // 10) + "\u2591" * (10 - score // 10)

        def cut(text: str, limit: int) -> str:
            text = str(text)
            return text if len(text) <= limit else text[:limit - 1] + "\u2026"

        pages = []
        overview = discord.Embed(title=f"AI Server Consultant | {guild.name}", description=cut(data["summary"], 1500), color=color)
        overview.add_field(name="Score", value=f"`{score}/100`\n`{bar}`", inline=False)
        if data.get("strengths"):
            overview.add_field(name="What Is Good", value=cut("\n".join(f"\u2022 {s}" for s in data["strengths"][:5]), 1000), inline=False)
        overview.add_field(name="Checked", value=f"`{len(guild.roles) - 1}` roles | `{len(guild.categories)}` categories | `{len(guild.channels) - len(guild.categories)}` channels", inline=False)
        pages.append(overview)

        if data.get("problems"):
            lines = []
            for p in data["problems"][:6]:
                sev = str(p.get("severity", "low")).upper()
                lines.append(f"`{sev}` **{cut(p['title'], 80)}**\n{cut(p['detail'], 300)}")
            pages.append(discord.Embed(title="Problems Found", description=cut("\n\n".join(lines), 3900), color=COLOR_RED))

        if data.get("suggestions"):
            lines = [f"**{n}. {cut(s['title'], 80)}**\n{cut(s['detail'], 300)}" for n, s in enumerate(data["suggestions"][:8], start=1)]
            pages.append(discord.Embed(title="How To Improve", description=cut("\n\n".join(lines), 3900), color=COLOR_PURPLE))

        for n, page in enumerate(pages, start=1):
            page.set_footer(text=f"Page {n}/{len(pages)} | AI advice can be wrong. Check it before you change things.")
        await send_pages(interaction, pages, ephemeral=True)


    # =============================================================
    # DAILY REWARD
    # =============================================================
    @app_commands.command(name="daily", description="Claim your daily points. Come back every day to raise the reward.")
    async def daily_cmd(self, interaction: discord.Interaction):
        lock = DAILY_LOCKS.setdefault(interaction.user.id, asyncio.Lock())
        async with lock:  # stops double clicks from claiming twice
            result = claim_daily(str(interaction.user.id))

        if result["status"] == "already":
            embed = discord.Embed(title="Already Claimed", color=COLOR_GOLD,
                                  description=f"You already claimed today.\nCome back <t:{next_daily_reset_ts()}:R> to keep your streak.")
            embed.add_field(name="Streak", value=f"`{result['streak']} days`", inline=True)
            embed.add_field(name="Next Reward", value=f"`+{daily_reward_for(result['streak'] + 1)} pts`", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        streak = result["streak"]
        milestone = streak % DAILY_MILESTONE_EVERY == 0
        title = f"Day {streak} Streak Bonus!" if milestone else "Daily Reward Claimed!"
        embed = discord.Embed(title=title, color=COLOR_GREEN, description=f"You got **{result['reward']} points**.")
        embed.add_field(name="Reward", value=f"`+{result['reward']} pts`", inline=True)
        embed.add_field(name="Streak", value=f"`{streak} days`", inline=True)
        embed.add_field(name="New Balance", value=f"`{result['new_total']:,} pts`", inline=True)
        embed.add_field(name="Next Reward", value=f"`+{daily_reward_for(streak + 1)} pts` <t:{next_daily_reset_ts()}:R>", inline=False)
        to_milestone = DAILY_MILESTONE_EVERY - (streak % DAILY_MILESTONE_EVERY)
        if not milestone:
            embed.add_field(name="Big Bonus", value=f"`{DAILY_MILESTONE_REWARD} pts` in `{to_milestone}` days", inline=False)
        if result.get("was_reset"):
            embed.add_field(name="Streak Reset", value="You missed a day, so your streak started again.", inline=False)
        embed.set_footer(text=f"Best streak: {result['best']} days")
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # =============================================================
    # TICKETS
    # =============================================================
    ticket_group = app_commands.Group(name="ticket", description="Ticket system tools (server admins).", guild_only=True)

    @ticket_group.command(name="setup", description="Set up tickets: category, staff role, transcript channel, and post the panel.")
    @app_commands.describe(
        category="Where new ticket channels are created.",
        staff_role="The role that can see and close tickets.",
        log_channel="Channel ID for transcripts. Can be in another server (the bot must be there).",
        panel_channel="Optional. Post the ticket buttons in this channel."
    )
    async def ticket_setup_cmd(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        staff_role: discord.Role,
        log_channel: str,
        panel_channel: Optional[discord.TextChannel] = None
    ):
        digits = "".join(ch for ch in log_channel if ch.isdigit())
        log_channel = interaction.client.get_channel(int(digits)) if digits else None
        if log_channel is None or not hasattr(log_channel, "send"):
            await interaction.response.send_message(
                embed=embed_err("Channel Not Found", "I cannot see that channel. Use the channel ID, and make sure the bot is in that server."), ephemeral=True)
            return
        me = interaction.guild.me
        if not me.guild_permissions.manage_channels:
            await interaction.response.send_message(embed=embed_err("Missing Permission", "I need `Manage Channels` to create ticket channels."), ephemeral=True)
            return
        perms = log_channel.permissions_for(log_channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.attach_files and perms.embed_links):
            await interaction.response.send_message(
                embed=embed_err("Missing Permission", f"I need `View Channel`, `Send Messages`, `Embed Links` and `Attach Files` in {log_channel.mention}."), ephemeral=True)
            return
        if staff_role.is_default() or staff_role.managed:
            await interaction.response.send_message(embed=embed_err("Cannot Use This Role", "Pick a normal staff role."), ephemeral=True)
            return

        cfg = get_guild_config(interaction.guild.id)
        cfg["tickets"] = {"category_id": category.id, "staff_role_id": staff_role.id, "log_channel_id": log_channel.id}
        saved = save_guild_config(interaction.guild.id)

        embed = embed_ok("Tickets Ready", "People can now open tickets.")
        embed.add_field(name="Category", value=f"`{category.name}`", inline=True)
        embed.add_field(name="Staff Role", value=staff_role.mention, inline=True)
        embed.add_field(name="Transcripts", value=f"{log_channel.mention}\n`{log_channel.guild.name}`", inline=True)
        if panel_channel is not None:
            panel = build_ticket_panel_embed()
            try:
                await panel_channel.send(embed=panel, view=TicketPanelView())
                embed.add_field(name="Panel Posted", value=panel_channel.mention, inline=False)
            except Exception as e:
                embed.add_field(name="Panel Not Posted", value=f"`{str(e)[:200]}`", inline=False)
        else:
            embed.add_field(name="Panel", value="Run `/ticket setup` again with `panel_channel` to post the buttons.", inline=False)
        if not saved:
            embed.add_field(name="Warning", value="The database could not be reached. This only lasts until the bot restarts.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @ticket_group.command(name="close", description="Close this ticket and save the transcript.")
    async def ticket_close_cmd(self, interaction: discord.Interaction):
        if get_ticket_by_channel(interaction.channel.id) is None:
            await interaction.response.send_message(embed=embed_err("Not A Ticket", "Use this inside a ticket channel."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_info("Closing Ticket", "Saving the transcript. This channel will be deleted in a moment.", COLOR_GOLD))
        ok = await close_ticket(self.bot, interaction.channel, interaction.user)
        if not ok:
            await interaction.followup.send(embed=embed_err("Could Not Close", "I could not save the transcript, so the ticket stays open. Check the log channel and my permissions."))

    @ticket_group.command(name="add", description="Add a person to this ticket.")
    @app_commands.describe(user="The person to add.")
    async def ticket_add_cmd(self, interaction: discord.Interaction, user: discord.Member):
        if get_ticket_by_channel(interaction.channel.id) is None:
            await interaction.response.send_message(embed=embed_err("Not A Ticket", "Use this inside a ticket channel."), ephemeral=True)
            return
        await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
        await interaction.response.send_message(embed=embed_ok("Person Added", f"{user.mention} can now see this ticket."), allowed_mentions=discord.AllowedMentions(users=True))



# =====================================================================
# GAMBLING with TE points: blackjack, roulette, slots, higher/lower.
# Every command works as a slash command and as a prefix command (for example !slots 5).
# Why it cannot be rigged:
#  - all random choices use secrets.SystemRandom (the operating system's secure random source)
#  - the bet is taken BEFORE the game starts, so leaving a game never saves points
#  - the result is decided only on the bot, a button press can only choose an action
#  - the odds are shown to the player and the house edge is small and fixed
# =====================================================================
import time as _time
import secrets as _secrets

_RNG = _secrets.SystemRandom()
GAMBLE_MIN_BET = 1
GAMBLE_MAX_BET = 100
GAMBLE_COOLDOWN = 1          # seconds between two games of the same person
GAMBLE_MAX_OPEN = 5          # games one person can have open at the same time
GAMBLE_OPEN = {}             # user id -> number of open games
GAMBLE_LAST = {}
C_WIN, C_LOSE, C_PUSH, C_GAME = 0x22C55E, 0xEF4444, 0xF59E0B, 0x8B5CF6
FAIR_FOOTER = "Fair play: every result uses the secure random generator. Max bet is 100 points."


def gamble_begin(uid: str, bet) -> Optional[str]:
    """Checks the bet and takes it. Returns an error text, or None when the game can start."""
    if isinstance(bet, bool) or not isinstance(bet, int) or not GAMBLE_MIN_BET <= bet <= GAMBLE_MAX_BET:
        return f"You can bet from `{GAMBLE_MIN_BET}` to `{GAMBLE_MAX_BET}` points in one round."
    if GAMBLE_OPEN.get(uid, 0) >= GAMBLE_MAX_OPEN:
        return f"You already have `{GAMBLE_MAX_OPEN}` games open. Finish one first."
    wait = GAMBLE_COOLDOWN - (_time.monotonic() - GAMBLE_LAST.get(uid, -1e9))
    if wait > 0:
        return f"Slow down! You can play again in `{int(wait) + 1}` second(s)."
    balance = get_user_points(uid)
    if balance < bet:
        return f"You only have `{balance:,}` points."
    add_user_points(uid, -bet)           # no await between the check and the payment, so it cannot be abused
    GAMBLE_OPEN[uid] = GAMBLE_OPEN.get(uid, 0) + 1
    GAMBLE_LAST[uid] = _time.monotonic()
    return None


def gamble_end(uid: str, payout: int) -> int:
    """Pays the winnings (the total that goes back to the player) and returns the new balance."""
    gamble_forget(uid)
    if payout > 0:
        return add_user_points(uid, payout)
    return get_user_points(uid)


def gamble_forget(uid: str):
    """One game of this person is closed."""
    GAMBLE_OPEN[uid] = max(0, GAMBLE_OPEN.get(uid, 0) - 1)


def gamble_net(payout: int, bet: int) -> str:
    net = payout - bet
    return f"`+{net:,}`" if net > 0 else (f"`{net:,}`" if net < 0 else "`0`")


def gamble_embed(title: str, desc: str, color: int = C_GAME) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text=FAIR_FOOTER)
    return e


def gamble_error(text: str) -> discord.Embed:
    return discord.Embed(title="Cannot Play", description=text, color=C_LOSE)



# ---------------- games that survive a restart ----------------
# Every open game is saved in the database (collection gamble_games). The buttons use fixed custom ids,
# and when the bot starts it loads the saved games and connects their buttons to the old messages again.
import uuid as _uuid
import cogs.features as _features

GAME_VIEWS = {}           # game id -> view that is waiting for a button
GAME_KINDS = {}           # kind name -> class
GAME_IDLE = 600           # seconds without a button press before blackjack and higher/lower finish by themselves
GAME_BOT = {"bot": None}


class _Person:
    """A small copy of a Discord user, so a game can be rebuilt after a restart."""
    def __init__(self, uid, name: str, avatar: str = ""):
        self.id = int(uid)
        self.display_name = name
        self.mention = f"<@{int(uid)}>"
        self.display_avatar = type("Avatar", (), {"url": avatar})()

    @classmethod
    def of(cls, member) -> "_Person":
        return cls(member.id, member.display_name, str(member.display_avatar.url))

    def dump(self) -> dict:
        return {"id": self.id, "name": self.display_name, "avatar": self.display_avatar.url}

    @classmethod
    def load(cls, d: dict) -> "_Person":
        return cls(d["id"], d["name"], d.get("avatar", ""))


class PersistentGame(discord.ui.View):
    kind = ""

    def __init__(self):
        super().__init__(timeout=None)
        self.gid = _uuid.uuid4().hex[:16]
        self.channel_id = 0
        self.message_id = 0
        self.expires_at = 0.0        # 0 = never finishes by itself
        self.done = False
        self.saved = False
        self.lock = asyncio.Lock()

    def owner_ids(self) -> list:
        return []

    def get_state(self) -> dict:
        return {}

    def on_restore(self, bot):
        pass

    def attach(self, msg, channel_id: int):
        self.message_id, self.channel_id = msg.id, channel_id

    def touch(self, seconds: int = GAME_IDLE):
        self.expires_at = _time.time() + seconds

    async def save(self):
        GAME_VIEWS[self.gid] = self
        col = _features.db_gamble
        if col is None:
            return
        doc = {"_id": self.gid, "kind": self.kind, "channel_id": self.channel_id, "message_id": self.message_id,
               "expires_at": self.expires_at, "owners": self.owner_ids(), "state": self.get_state(), "saved_at": _time.time()}
        try:
            await asyncio.to_thread(lambda: col.replace_one({"_id": self.gid}, doc, upsert=True))
            self.saved = True
        except Exception as e:
            print(f"[Game Save Error] {e}")

    async def claim(self) -> bool:
        """Takes the game out of the database before the points are paid. This way a restart can never pay twice."""
        GAME_VIEWS.pop(self.gid, None)
        col = _features.db_gamble
        if col is None or not self.saved:
            return True
        try:
            res = await asyncio.to_thread(lambda: col.delete_one({"_id": self.gid}))
            return res.deleted_count > 0
        except Exception as e:
            print(f"[Game Claim Error] {e}")
            return True

    async def edit(self, **kwargs):
        """Edits the game message (works also after a restart)."""
        bot = GAME_BOT["bot"]
        channel = bot.get_channel(self.channel_id) or await bot.fetch_channel(self.channel_id)
        await channel.get_partial_message(self.message_id).edit(**kwargs)

    async def expire(self):
        pass


async def restore_games(bot):
    col = _features.db_gamble
    if col is None:
        return
    try:
        docs = await asyncio.to_thread(lambda: list(col.find({})))
    except Exception as e:
        print(f"[Game Restore Error] {e}")
        return
    count = 0
    for doc in docs:
        cls = GAME_KINDS.get(doc.get("kind"))
        try:
            view = cls.from_state(doc["state"])
            view.gid, view.saved = doc["_id"], True
            view.channel_id, view.message_id = int(doc["channel_id"]), int(doc["message_id"])
            view.expires_at = float(doc.get("expires_at", 0))
            bot.add_view(view, message_id=view.message_id)
            GAME_VIEWS[view.gid] = view
            for owner in view.owner_ids():
                GAMBLE_OPEN[owner] = GAMBLE_OPEN.get(owner, 0) + 1
            view.on_restore(bot)
            count += 1
        except Exception as e:
            print(f"[Game Restore Error] {doc.get('kind')} {doc.get('_id')}: {e}")
    print(f"[Games] {count} open game(s) restored.")


# ---------------- cards ----------------
CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]
SUIT_LETTER = {"♠": "S", "♥": "H", "♦": "D", "♣": "C"}
CARD_EMOJI = {}          # "QC" -> "<:te_QC:123>" (filled by the bot at start, see ensure_card_emojis)
CARDS_READY = False      # true when all 53 card pictures exist; before that, cards are shown as text


def new_deck(decks: int = 1) -> list:
    deck = [(r, s) for _ in range(decks) for r in CARD_RANKS for s in CARD_SUITS]
    _RNG.shuffle(deck)
    return deck


def card_key(card) -> str:
    return f"{card[0]}{SUIT_LETTER[card[1]]}"


def card_text(card) -> str:
    if CARDS_READY:
        return CARD_EMOJI[card_key(card)]
    return f"`{card[0]}{card[1]}`"


def hand_text(cards, hide_second: bool = False) -> str:
    if hide_second:
        return card_text(cards[0]) + " " + (CARD_EMOJI["back"] if CARDS_READY else "`??`")
    return " ".join(card_text(c) for c in cards)


def hand_value(cards) -> int:
    total, aces = 0, 0
    for rank, _ in cards:
        if rank == "A":
            aces += 1
            total += 11
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def card_points(card) -> int:
    """Value used to check if two cards are a pair for splitting (every 10, J, Q and K counts as 10)."""
    return min(10, CARD_RANKS.index(card[0]) + 1)


# The card pictures are drawn with Pillow and uploaded once as application emojis of the bot.
def render_card(rank: str, suit: str) -> bytes:
    """Draws one playing card on a 128x128 transparent square (used as a Discord emoji). suit: S H D C or 'back'."""
    from PIL import Image, ImageDraw, ImageFont
    SS = 4  # draw big, then shrink for smooth edges
    W = 128 * SS
    im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = 12 * SS, 2 * SS, 116 * SS, 126 * SS
    r = 12 * SS
    if suit == "back":
        d.rounded_rectangle((x0, y0, x1, y1), r, fill=(255, 255, 255, 255))
        d.rounded_rectangle((x0 + 4 * SS, y0 + 4 * SS, x1 - 4 * SS, y1 - 4 * SS), r - 3 * SS, fill=(30, 110, 215, 255))
        return _out(im)
    d.rounded_rectangle((x0, y0, x1, y1), r, fill=(255, 255, 255, 255), outline=(190, 190, 195, 255), width=SS)
    red = suit in ("H", "D")
    color = (196, 30, 48, 255) if red else (20, 20, 24, 255)
    try:
        font = ImageFont.load_default(size=58 * SS)
    except TypeError:
        font = ImageFont.load_default()
    cx = (x0 + x1) // 2
    d.text((cx, y0 + 4 * SS), rank, fill=color, font=font, anchor="mt")
    _suit(d, suit, cx, y0 + 92 * SS, 30 * SS, color)
    return _out(im)

def _out(im):
    import io
    from PIL import Image
    im = im.resize((128, 128), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "PNG", optimize=True)
    return buf.getvalue()

def _suit(d, suit, cx, cy, s, color):
    """Draws a suit symbol centered at (cx, cy) with half-size s."""
    if suit == "D":
        d.polygon([(cx, cy - s * 1.1), (cx + s * 0.8, cy), (cx, cy + s * 1.1), (cx - s * 0.8, cy)], fill=color)
    elif suit in ("H", "S"):
        flip = suit == "S"
        sign = -1 if flip else 1
        def P(x, y): return (cx + x, cy - sign * y)
        rr = s * 0.52
        for sx in (-1, 1):
            c = P(sx * rr, s * 0.25)
            d.ellipse((c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr), fill=color)
        d.polygon([P(-s * 1.03, s * 0.12), P(s * 1.03, s * 0.12), P(0, -s * 1.0)], fill=color)
        if flip:
            d.polygon([P(0, s * 0.2), P(-s * 0.42, s * 1.15), P(s * 0.42, s * 1.15)], fill=color)
    else:  # club
        rr = s * 0.5
        for (x, y) in ((0, s * 0.5), (-s * 0.55, -s * 0.2), (s * 0.55, -s * 0.2)):
            d.ellipse((cx + x - rr, cy - y - rr, cx + x + rr, cy - y + rr), fill=color)
        d.polygon([(cx, cy), (cx - s * 0.45, cy + s * 1.1), (cx + s * 0.45, cy + s * 1.1)], fill=color)



async def ensure_card_emojis(bot) -> None:
    """Makes sure the 53 card emojis exist (creates the missing ones, slowly) and turns on the picture cards."""
    global CARDS_READY
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("[Cards] Pillow is not installed, cards are shown as text.")
        return
    wanted = {"back": "tc_back"}
    for r in CARD_RANKS:
        for letter in "SHDC":
            wanted[f"{r}{letter}"] = f"tc_{r}{letter}"
    try:
        have = {e.name: e for e in await bot.fetch_application_emojis()}
        for old in [e for n, e in have.items() if n.startswith("te_") and (n == "te_back" or n[3:-1] in CARD_RANKS)]:
            try:                       # the first version of the pictures was smaller: remove it
                await old.delete()
                await asyncio.sleep(0.5)
            except Exception:
                pass
        for key, name in wanted.items():
            emoji = have.get(name)
            if emoji is None:
                if key == "back":
                    data = await asyncio.to_thread(render_card, "", "back")
                else:
                    data = await asyncio.to_thread(render_card, key[:-1], key[-1])
                emoji = await bot.create_application_emoji(name=name, image=data)
                await asyncio.sleep(1)       # be kind to the rate limit
            CARD_EMOJI[key] = str(emoji)
        CARDS_READY = True
        print("[Cards] Card pictures are ready.")
    except Exception as e:
        print(f"[Cards] Could not prepare card emojis, using text cards: {e}")


# ---------------- blackjack ----------------
BJ_BLUE = 0x0EA5E9
BJ_DECKS = 3


class BJHand:
    def __init__(self, cards: list, bet: int):
        self.cards, self.bet = cards, bet
        self.done = False
        self.doubled = False


class BlackjackView(PersistentGame):
    kind = "blackjack"

    def __init__(self, user, bet: int, deck: list, player: list, dealer: list):
        super().__init__()
        self.user, self.owner_id, self.deck, self.dealer = user, user.id, deck, dealer
        self.hands = [BJHand(player, bet)]
        self.idx = 0
        self.touch()
        self.update_buttons()

    def owner_ids(self) -> list:
        return [str(self.owner_id)]

    def get_state(self) -> dict:
        return {"user": self.user.dump(), "idx": self.idx, "dealer": [list(c) for c in self.dealer], "deck": [list(c) for c in self.deck],
                "hands": [{"cards": [list(c) for c in h.cards], "bet": h.bet, "done": h.done, "doubled": h.doubled} for h in self.hands]}

    @classmethod
    def from_state(cls, st: dict):
        view = cls(_Person.load(st["user"]), 1, [tuple(c) for c in st["deck"]], [], [tuple(c) for c in st["dealer"]])
        view.hands = []
        for h in st["hands"]:
            hand = BJHand([tuple(c) for c in h["cards"]], h["bet"])
            hand.done, hand.doubled = h["done"], h["doubled"]
            view.hands.append(hand)
        view.idx = st["idx"]
        view.update_buttons()
        return view

    @property
    def total_bet(self) -> int:
        return sum(h.bet for h in self.hands)

    def update_buttons(self):
        hand = self.hands[self.idx]
        two = len(hand.cards) == 2 and not hand.doubled
        room = self.total_bet + hand.bet <= GAMBLE_MAX_BET and get_user_points(str(self.owner_id)) >= hand.bet
        self.double.disabled = self.done or not (two and room)
        self.split.disabled = self.done or not (two and room and len(self.hands) == 1 and card_points(hand.cards[0]) == card_points(hand.cards[1]))
        self.hit.disabled = self.stand.disabled = self.done

    def make_embed(self, final: bool = False, note: str = "", color: int = BJ_BLUE, results=None, payout: int = 0, balance: Optional[int] = None) -> discord.Embed:
        # Cards sit alone in their own field: Discord then shows them as big pictures. Few words, big numbers.
        top = note
        if final:
            net = payout - self.total_bet
            word = "Win" if net > 0 else ("Lose" if net < 0 else "Push")
            top = f"**{word}** {gamble_net(payout, self.total_bet)}  \u2022  Balance `{balance:,}`" + (f"\n{note}" if note else "")
        e = discord.Embed(description=top or None, color=color)
        e.set_author(name=self.user.display_name, icon_url=self.user.display_avatar.url)
        for i, hand in enumerate(self.hands):
            if len(self.hands) == 1:
                name = "Your Hand"
            else:
                name = f"Hand {i + 1}" + (" \u25C0" if (not final and i == self.idx) else "")
            value = f"Value: {hand_value(hand.cards)}"
            if hand.doubled:
                value += " (2x)"
            if results:
                value += f"  \u2022  {results[i]}"
            e.add_field(name=name, value=hand_text(hand.cards), inline=False)
            e.add_field(name="\u200b", value=value, inline=False)
        shown = self.dealer if final else self.dealer[:1]
        e.add_field(name="Dealer Hand", value=hand_text(self.dealer, hide_second=not final), inline=False)
        e.add_field(name="\u200b", value=f"Value: {hand_value(shown)}", inline=False)
        e.set_footer(text=f"Bet {self.total_bet}  \u2022  Cards remaining: {len(self.deck)}")
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is not your game. Start your own with `/blackjack`.", ephemeral=True)
            return False
        return True

    async def show(self, interaction: Optional[discord.Interaction], embed: discord.Embed):
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.edit(embed=embed, view=self)
        if not self.done:               # the game goes on: remember it
            self.touch()
            await self.save()

    async def advance(self, interaction: Optional[discord.Interaction]):
        """Goes to the next hand that is still playing, or lets the dealer play when all hands are finished."""
        for i, hand in enumerate(self.hands):
            if not hand.done:
                self.idx = i
                self.update_buttons()
                return await self.show(interaction, self.make_embed())
        await self.settle(interaction)

    async def settle(self, interaction: Optional[discord.Interaction], note: str = ""):
        self.done = True
        if not await self.claim():      # this game was already paid
            return
        self.update_buttons()
        if any(hand_value(h.cards) <= 21 for h in self.hands):
            while hand_value(self.dealer) < 17:     # the dealer stands on every 17
                self.dealer.append(self.deck.pop())
        dv = hand_value(self.dealer)
        results, payout = [], 0
        for hand in self.hands:
            pv = hand_value(hand.cards)
            if pv > 21:
                results.append("Bust")
            elif dv > 21 or pv > dv:
                results.append("Win")
                payout += hand.bet * 2
            elif pv == dv:
                results.append("Push")
                payout += hand.bet
            else:
                results.append("Lose")
        balance = gamble_end(str(self.owner_id), payout)
        net = payout - self.total_bet
        color = C_WIN if net > 0 else (C_LOSE if net < 0 else C_PUSH)
        await self.show(interaction, self.make_embed(True, note, color, results, payout, balance))
        self.stop()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, custom_id="te_bj_hit")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            hand = self.hands[self.idx]
            hand.cards.append(self.deck.pop())
            if hand_value(hand.cards) >= 21:
                hand.done = True
            await self.advance(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.success, custom_id="te_bj_stand")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            self.hands[self.idx].done = True
            await self.advance(interaction)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.secondary, custom_id="te_bj_double")
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            hand = self.hands[self.idx]
            if self.done or button.disabled:
                return await interaction.response.defer()
            if get_user_points(str(self.owner_id)) < hand.bet or self.total_bet + hand.bet > GAMBLE_MAX_BET:
                return await interaction.response.send_message("You cannot double now (max bet is 100 points in one round).", ephemeral=True)
            add_user_points(str(self.owner_id), -hand.bet)
            hand.bet *= 2
            hand.doubled = True
            hand.cards.append(self.deck.pop())
            hand.done = True
            await self.advance(interaction)

    @discord.ui.button(label="Split", style=discord.ButtonStyle.secondary, custom_id="te_bj_split")
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            hand = self.hands[self.idx]
            if self.done or button.disabled:
                return await interaction.response.defer()
            if get_user_points(str(self.owner_id)) < hand.bet or self.total_bet + hand.bet > GAMBLE_MAX_BET:
                return await interaction.response.send_message("You cannot split now (max bet is 100 points in one round).", ephemeral=True)
            add_user_points(str(self.owner_id), -hand.bet)
            second = BJHand([hand.cards.pop()], hand.bet)
            hand.cards.append(self.deck.pop())
            second.cards.append(self.deck.pop())
            self.hands.append(second)
            for h in self.hands:
                if hand_value(h.cards) == 21:
                    h.done = True
            await self.advance(interaction)

    @discord.ui.button(label="Help", style=discord.ButtonStyle.secondary, emoji="❓", custom_id="te_bj_help")
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = discord.Embed(title="Blackjack Help", color=BJ_BLUE, description=(
            "Get closer to `21` than the dealer without going over.\n"
            "**Hit** takes a card. **Stand** keeps your hand.\n"
            "**Double Down** doubles your bet and gives you one last card (first move only).\n"
            "**Split** makes two hands from a pair (needs a second bet).\n"
            "Win pays `2x`, blackjack pays `2.5x`, a tie gives your bet back. The dealer stands on `17`.\n"
            f"You can use at most `{GAMBLE_MAX_BET}` points in one round, so Double Down and Split need room under that."))
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def expire(self):
        async with self.lock:
            if not self.done:  # no answer for a long time: every hand stands
                try:
                    for h in self.hands:
                        h.done = True
                    await self.settle(None, "No answer for a long time, so your hand stood.")
                except Exception as e:
                    print(f"[Blackjack Expire Error] {e}")


GAME_KINDS["blackjack"] = BlackjackView


# ---------------- roulette ----------------
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_OUTSIDE = {
    "red": ("Red", lambda n: n in RED_NUMBERS, 2),
    "black": ("Black", lambda n: n != 0 and n not in RED_NUMBERS, 2),
    "even": ("Even", lambda n: n != 0 and n % 2 == 0, 2),
    "odd": ("Odd", lambda n: n % 2 == 1, 2),
    "low": ("Low (1-18)", lambda n: 1 <= n <= 18, 2),
    "high": ("High (19-36)", lambda n: 19 <= n <= 36, 2),
    "1st": ("1st dozen (1-12)", lambda n: 1 <= n <= 12, 3),
    "2nd": ("2nd dozen (13-24)", lambda n: 13 <= n <= 24, 3),
    "3rd": ("3rd dozen (25-36)", lambda n: 25 <= n <= 36, 3),
}
ROULETTE_ALIASES = {"1-18": "low", "19-36": "high", "first": "1st", "second": "2nd", "third": "3rd", "dozen1": "1st", "dozen2": "2nd", "dozen3": "3rd",
                    "1-12": "1st", "13-24": "2nd", "25-36": "3rd", "r": "red", "b": "black", "e": "even", "o": "odd", "l": "low", "h": "high"}


def roulette_parse(text: str):
    """Returns (label, test function, total payout multiplier) or None."""
    key = str(text or "").strip().lower()
    key = ROULETTE_ALIASES.get(key, key)
    if key in ROULETTE_OUTSIDE:
        return ROULETTE_OUTSIDE[key]
    if key.isdigit() and 0 <= int(key) <= 36:
        n = int(key)
        return (f"Number {n}", lambda x, n=n: x == n, 36)
    return None


def roulette_color(n: int) -> str:
    return "\U0001F7E2" if n == 0 else ("\U0001F534" if n in RED_NUMBERS else "⚫")


# ---------------- slots ----------------
SLOT_WEIGHTS = {"\U0001F352": 30, "\U0001F34B": 25, "\U0001F34A": 20, "\U0001F347": 12, "\U0001F514": 8, "⭐": 4, "\U0001F48E": 1}
SLOT_TRIPLE = {"\U0001F352": 7, "\U0001F34B": 10, "\U0001F34A": 14, "\U0001F347": 24, "\U0001F514": 45, "⭐": 90, "\U0001F48E": 250}
SLOT_TWO_CHERRIES = 2   # exactly two cherries anywhere pay 2 times the bet


def slot_payout_multiplier(reels) -> int:
    if reels[0] == reels[1] == reels[2]:
        return SLOT_TRIPLE[reels[0]]
    if reels.count("\U0001F352") == 2:
        return SLOT_TWO_CHERRIES
    return 0


def slot_rtp() -> float:
    """Exact return to player (for tests and the help text)."""
    from itertools import product
    total = sum(SLOT_WEIGHTS.values())
    rtp = 0.0
    for reels in product(SLOT_WEIGHTS, repeat=3):
        p = 1.0
        for r in reels:
            p *= SLOT_WEIGHTS[r] / total
        rtp += p * slot_payout_multiplier(list(reels))
    return rtp


# ---------------- higher / lower ----------------
HL_EDGE = 0.94          # fair odds times 0.94: the house keeps about 6 percent of each step
HL_MAX_STEPS = 8
HL_MAX_PAYOUT = 5000


def card_rank(card) -> int:
    return CARD_RANKS.index(card[0]) + 1    # Ace is low (1), King is high (13)


class HigherLowerView(PersistentGame):
    kind = "highlow"

    def __init__(self, owner_id: int, bet: int, deck: list):
        super().__init__()
        self.owner_id, self.bet, self.deck = owner_id, bet, deck
        self.current = deck.pop()
        self.mult = 1.0
        self.steps = 0
        self.touch()
        self.refresh_buttons()

    def owner_ids(self) -> list:
        return [str(self.owner_id)]

    def get_state(self) -> dict:
        return {"owner_id": self.owner_id, "bet": self.bet, "deck": [list(c) for c in self.deck], "current": list(self.current),
                "mult": self.mult, "steps": self.steps}

    @classmethod
    def from_state(cls, st: dict):
        deck = [tuple(c) for c in st["deck"]]
        deck.append(tuple(st["current"]))        # the constructor takes the current card from the end
        view = cls(st["owner_id"], st["bet"], deck)
        view.mult, view.steps = st["mult"], st["steps"]
        view.refresh_buttons()
        return view

    def chances(self):
        rank = card_rank(self.current)
        left = len(self.deck)
        higher = sum(1 for c in self.deck if card_rank(c) > rank)
        lower = sum(1 for c in self.deck if card_rank(c) < rank)
        return higher / left, lower / left, higher, lower

    @staticmethod
    def step_multiplier(p: float) -> float:
        return max(1.0, HL_EDGE / p) if p > 0 else 1.0

    def cash_value(self) -> int:
        return min(HL_MAX_PAYOUT, int(self.bet * self.mult))

    def refresh_buttons(self):
        ph, pl, hi, lo = self.chances()
        self.higher_btn.disabled = hi == 0 or self.done
        self.lower_btn.disabled = lo == 0 or self.done
        self.cash_btn.disabled = self.steps == 0 or self.done
        self.higher_btn.label = f"Higher ({ph * 100:.0f}%)"
        self.lower_btn.label = f"Lower ({pl * 100:.0f}%)"
        self.cash_btn.label = f"Cash out ({self.cash_value()})"

    def make_embed(self, note: str = "", color: int = C_GAME, final: bool = False, payout: int = 0, balance: Optional[int] = None, last=None) -> discord.Embed:
        e = gamble_embed("Higher or Lower", note or "Will the next card be higher or lower? The same rank is a loss. Ace is low, King is high.", color)
        if last is not None:
            e.add_field(name="Next card", value=card_text(last), inline=True)
        e.add_field(name="Current card" if not final else "Last card", value=card_text(self.current), inline=True)
        e.add_field(name="Bet", value=f"`{self.bet}`", inline=True)
        e.add_field(name="Streak", value=f"`{self.steps}/{HL_MAX_STEPS}`", inline=True)
        e.add_field(name="Multiplier", value=f"`x{self.mult:.2f}`", inline=True)
        if final:
            e.add_field(name="Result", value=gamble_net(payout, self.bet), inline=True)
            e.add_field(name="Balance", value=f"`{balance:,}`", inline=True)
        else:
            ph, pl, _, _ = self.chances()
            e.add_field(name="If you win", value=f"Higher `x{self.mult * self.step_multiplier(ph):.2f}` | Lower `x{self.mult * self.step_multiplier(pl):.2f}`", inline=False)
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is not your game. Start your own with `/highlow`.", ephemeral=True)
            return False
        return True

    async def finish(self, interaction: Optional[discord.Interaction], payout: int, note: str, color: int, last=None):
        self.done = True
        if not await self.claim():      # this game was already paid
            return
        for item in self.children:
            item.disabled = True
        balance = gamble_end(str(self.owner_id), payout)
        embed = self.make_embed(note, color, True, payout, balance, last)
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.edit(embed=embed, view=self)
        self.stop()

    async def guess(self, interaction: discord.Interaction, higher: bool):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            ph, pl, _, _ = self.chances()
            p = ph if higher else pl
            nxt = self.deck.pop()
            old, new = card_rank(self.current), card_rank(nxt)
            won = new > old if higher else new < old
            previous = self.current
            self.current = nxt
            if not won:
                return await self.finish(interaction, 0, f"Wrong! The card was {card_text(nxt)}. You lost `{self.bet}` points.", C_LOSE, last=None)
            self.steps += 1
            self.mult *= self.step_multiplier(p)
            if self.steps >= HL_MAX_STEPS:
                return await self.finish(interaction, self.cash_value(), f"Perfect streak of {HL_MAX_STEPS}! Paid out automatically.", C_WIN)
            self.refresh_buttons()
            await interaction.response.edit_message(embed=self.make_embed(f"Correct! {card_text(previous)} then {card_text(nxt)}. Keep going or cash out.", C_WIN), view=self)
            self.touch()
            await self.save()

    @discord.ui.button(label="Higher", style=discord.ButtonStyle.success, custom_id="te_hl_higher")
    async def higher_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.guess(interaction, True)

    @discord.ui.button(label="Lower", style=discord.ButtonStyle.danger, custom_id="te_hl_lower")
    async def lower_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.guess(interaction, False)

    @discord.ui.button(label="Cash out", style=discord.ButtonStyle.secondary, custom_id="te_hl_cash")
    async def cash_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            await self.finish(interaction, self.cash_value(), "You cashed out!", C_WIN)

    async def expire(self):
        async with self.lock:
            if not self.done:  # no answer for a long time: cash out a streak, but a player who never guessed loses the bet (no free skip)
                try:
                    if self.steps == 0:
                        await self.finish(None, 0, "No answer in time. The bet is lost.", C_LOSE)
                    else:
                        await self.finish(None, self.cash_value(), "No answer in time, so you were cashed out.", C_PUSH)
                except Exception as e:
                    print(f"[HigherLower Expire Error] {e}")


GAME_KINDS["highlow"] = HigherLowerView



# ---------------- coin flip duel ----------------
class CoinFlipView(PersistentGame):
    kind = "coinflip"

    def __init__(self, challenger, target, bet: int):
        super().__init__()
        self.challenger, self.target, self.bet = challenger, target, bet

    def owner_ids(self) -> list:
        return [str(self.challenger.id)]

    def get_state(self) -> dict:
        return {"challenger": self.challenger.dump(), "target": self.target.dump(), "bet": self.bet}

    @classmethod
    def from_state(cls, st: dict):
        return cls(_Person.load(st["challenger"]), _Person.load(st["target"]), st["bet"])

    async def refund(self, text: str, color: int = C_PUSH) -> Optional[discord.Embed]:
        self.done = True
        if not await self.claim():          # already finished somewhere else
            return None
        for item in self.children:
            item.disabled = True
        gamble_end(str(self.challenger.id), self.bet)    # the challenger gets the bet back
        return gamble_embed("Coin Flip Duel", text, color)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="te_cf_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            if interaction.user.id != self.target.id:
                return await interaction.response.send_message(f"Only {self.target.mention} can accept this duel.", ephemeral=True)
            error = gamble_begin(str(self.target.id), self.bet)
            if error:
                return await interaction.response.send_message(embed=gamble_error(error), ephemeral=True)
            self.done = True
            if not await self.claim():      # already finished somewhere else: give the new bet back
                gamble_end(str(self.target.id), self.bet)
                return await interaction.response.defer()
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=gamble_embed("Coin Flip Duel", f"\U0001FA99 The coin is flying...\n{self.challenger.mention} vs {self.target.mention} for `{self.bet * 2}` points.", C_GAME), view=self)
            await asyncio.sleep(2)
            winner, loser = _RNG.sample([self.challenger, self.target], 2)   # a fair 50/50, decided only on the bot
            win_balance = gamble_end(str(winner.id), self.bet * 2)
            lose_balance = gamble_end(str(loser.id), 0)
            e = gamble_embed("Coin Flip Duel", f"\U0001FA99 {winner.mention} wins the flip!", C_WIN)
            e.add_field(name="Winner gets", value=f"`+{self.bet}`", inline=True)
            e.add_field(name=f"{winner.display_name}", value=f"`{win_balance:,}`", inline=True)
            e.add_field(name=f"{loser.display_name}", value=f"`{lose_balance:,}` (`-{self.bet}`)", inline=True)
            try:
                await self.edit(embed=e, view=self)
            except Exception as err:
                print(f"[CoinFlip Edit Error] {err}")
            self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="te_cf_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            if interaction.user.id not in (self.target.id, self.challenger.id):
                return await interaction.response.send_message("This duel is not for you.", ephemeral=True)
            embed = await self.refund(f"{interaction.user.mention} ended the duel. The bet was returned.")
            if embed is None:
                return await interaction.response.defer()
            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()


GAME_KINDS["coinflip"] = CoinFlipView


# ---------------- crash ----------------
CRASH_EDGE = 0.94        # the chance to reach x is 0.94 / x, so every cash-out plan returns at most 94 percent
CRASH_GROWTH = 1.18
CRASH_TICK = 1.5         # seconds between two steps
CRASH_MAX = 50.0


def crash_point() -> float:
    """Secret crash multiplier. The chance that it is at least x equals 0.94 / x (x from 1)."""
    u = _RNG.random()
    return max(1.0, int(CRASH_EDGE * 100 / (1 - u)) / 100)


class CrashView(PersistentGame):
    kind = "crash"

    def __init__(self, user, bet: int, crash_at: float):
        super().__init__()
        self.user, self.bet, self.crash_at = user, bet, crash_at
        self.step, self.mult = 0, 1.0

    def owner_ids(self) -> list:
        return [str(self.user.id)]

    def get_state(self) -> dict:
        return {"user": self.user.dump(), "bet": self.bet, "crash_at": self.crash_at, "step": self.step, "mult": self.mult}

    @classmethod
    def from_state(cls, st: dict):
        view = cls(_Person.load(st["user"]), st["bet"], st["crash_at"])
        view.step, view.mult = st["step"], st["mult"]
        return view

    def on_restore(self, bot):
        bot.loop.create_task(self.run())      # the rocket goes on climbing

    def cash_value(self) -> int:
        return min(HL_MAX_PAYOUT, int(self.bet * self.mult))

    def make_embed(self, note: str = "", color: int = C_GAME, final: bool = False, payout: int = 0, balance: Optional[int] = None) -> discord.Embed:
        icon = "\U0001F4A5" if final and payout == 0 else "\U0001F680"
        e = gamble_embed("Crash", f"## {icon} x{self.mult:.2f}\n{note or 'Press Cash Out before the rocket crashes!'}", color)
        e.add_field(name="Bet", value=f"`{self.bet}`", inline=True)
        if final:
            e.add_field(name="Result", value=gamble_net(payout, self.bet), inline=True)
            e.add_field(name="Balance", value=f"`{balance:,}`", inline=True)
        else:
            e.add_field(name="Cash out now", value=f"`{self.cash_value()}`", inline=True)
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not your game. Start your own with `/crash`.", ephemeral=True)
            return False
        return True

    async def close(self, payout: int, note: str, color: int, saved: bool = True) -> Optional[discord.Embed]:
        self.done = True
        if saved and not await self.claim():     # already paid somewhere else
            return None
        for item in self.children:
            item.disabled = True
        balance = gamble_end(str(self.user.id), payout)
        return self.make_embed(note, color, True, payout, balance)

    @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.success, custom_id="te_cr_cash")
    async def cash(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            payout = self.cash_value()
            embed = await self.close(payout, f"You cashed out at `x{self.mult:.2f}`!", C_WIN if payout > self.bet else C_PUSH)
            if embed is None:
                return await interaction.response.defer()
            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()

    async def run(self):
        """The rocket climbs one step at a time until it crashes (the crash point was chosen before the first step)."""
        try:
            while True:
                await asyncio.sleep(CRASH_TICK)
                async with self.lock:
                    if self.done:
                        return
                    self.step += 1
                    nxt = round(CRASH_GROWTH ** self.step, 2)
                    if nxt >= self.crash_at:
                        self.mult = self.crash_at
                        embed = await self.close(0, f"The rocket crashed at `x{self.crash_at:.2f}`. You lost `{self.bet}` points.", C_LOSE)
                    elif nxt >= CRASH_MAX:
                        self.mult = nxt
                        embed = await self.close(self.cash_value(), "The rocket hit the top! Paid out automatically.", C_WIN)
                    else:
                        self.mult = nxt
                        self.cash.label = f"Cash Out ({self.cash_value()})"
                        embed = self.make_embed()
                    if embed is None:
                        return
                    await self.edit(embed=embed, view=self)
                    if self.done:
                        self.stop()
                        return
                    await self.save()
        except Exception as e:
            print(f"[Crash Error] {e}")
            if not self.done:       # the message is gone: cash out what the player has
                self.done = True
                if await self.claim():
                    gamble_end(str(self.user.id), self.cash_value())


GAME_KINDS["crash"] = CrashView


# ---------------- heist ----------------
HEIST_MAX = 1000          # the vault never gives more than this in one heist
HEIST_MAX_INVITES = 5     # people you can invite (the crew is at most 6)
HEIST_WAIT = 90           # seconds the crew has to answer
HEIST_COOLDOWN = 1800     # seconds before the same person can join another heist
HEIST_LAST = {}           # user id -> time of the last heist
HEIST_RUN = 60            # seconds the crew waits before the heist starts (this is when someone can snitch)
HEIST_CHANCE = (0.40, 0.90)   # every heist rolls its own success chance in this range
SNITCH_OUT = (0.10, 0.40)     # a snitch from outside the crew rolls a win chance in this range
SNITCH_IN = (0.40, 0.70)      # a crew member who snitches has a better chance (it is a risky move)
SNITCH_REWARD = 1000
SNITCH_COOLDOWN = 1800
SNITCH_LAST = {}          # user id -> time of the last snitch try


def heist_loot(crew: int) -> int:
    """The best of `crew` fair rolls from 1 to 1000: a bigger crew gets a higher loot most of the time."""
    return max(_RNG.randint(1, HEIST_MAX) for _ in range(max(1, crew)))


def heist_split(total: int, ids: list) -> dict:
    """Equal shares. Points that cannot be split are given one by one to random people."""
    share, rest = divmod(total, len(ids))
    out = {i: share for i in ids}
    for i in _RNG.sample(ids, rest):
        out[i] += 1
    return out


def heist_wait_left(uid) -> int:
    return int(HEIST_COOLDOWN - (_time.time() - HEIST_LAST.get(str(uid), 0)))


class HeistView(PersistentGame):
    kind = "heist"

    def __init__(self, leader, invited: list, joined: list, declined: list):
        super().__init__()
        self.leader, self.invited = leader, invited          # invited = list of _Person
        self.joined, self.declined = list(joined), list(declined)   # lists of user ids (the leader is always in joined)
        self.phase = "invite"       # invite -> running (snitch time) -> finished
        self.snitch_tried = False   # only one snitch try for each heist

    @property
    def code(self) -> str:
        return self.gid[:6]

    def owner_ids(self) -> list:
        return []

    def get_state(self) -> dict:
        return {"leader": self.leader.dump(), "invited": [p.dump() for p in self.invited], "joined": self.joined, "declined": self.declined,
                "phase": self.phase, "snitch_tried": self.snitch_tried}

    @classmethod
    def from_state(cls, st: dict):
        view = cls(_Person.load(st["leader"]), [_Person.load(d) for d in st["invited"]], st["joined"], st["declined"])
        view.phase, view.snitch_tried = st.get("phase", "invite"), st.get("snitch_tried", False)
        return view

    def crew_people(self) -> list:
        crew = {self.leader.id: self.leader, **{p.id: p for p in self.invited}}
        return [crew[i] for i in dict.fromkeys(self.joined) if i in crew]

    def make_embed(self, note: str = "", color: int = C_GAME) -> discord.Embed:
        lines = [f"✅ {self.leader.mention} (leader)"]
        for p in self.invited:
            mark = "✅" if p.id in self.joined else ("❌" if p.id in self.declined else "⏳")
            lines.append(f"{mark} {p.mention}")
        e = discord.Embed(title="\U0001F4B0 Vault Heist", color=color,
                          description=note or f"{self.leader.mention} wants to rob the **Template Engine Vault**!\nPress **Join** to be part of the crew.")
        e.add_field(name="Crew", value="\n".join(lines), inline=False)
        e.add_field(name="Loot", value=f"`1` - `{HEIST_MAX:,}` points, shared equally. More people means more loot!", inline=False)
        e.set_footer(text=f"The crew has {HEIST_WAIT} seconds to answer. Anyone who says no is left out, the others can still go.")
        return e

    def answered(self) -> bool:
        return all(p.id in self.joined or p.id in self.declined for p in self.invited)

    def update_buttons(self):
        self.start_btn.disabled = len(self.joined) < 2

    async def cancel(self, text: str):
        self.done = True
        await self.claim()
        for item in self.children:
            item.disabled = True
        return discord.Embed(title="\U0001F4B0 Vault Heist", description=text, color=C_LOSE)

    async def go(self, interaction=None):
        """Called with the lock held. The crew breaks in. For a short time anyone can snitch."""
        self.done = False
        for item in self.children:
            item.disabled = True
        self.phase = "running"
        self.touch(HEIST_RUN)
        now = _time.time()
        crew = self.crew_people()
        for p in crew:
            HEIST_LAST[str(p.id)] = now
        e = discord.Embed(title="\U0001F4B0 Vault Heist", color=C_GAME,
                          description=f"\U0001F9E4 The crew is getting ready...\n{', '.join(p.mention for p in crew)}")
        e.add_field(name="Heist ID", value=f"`{self.code}`", inline=True)
        e.add_field(name="Starts", value=f"<t:{int(self.expires_at)}:R>", inline=True)
        e.set_footer(text=f"Anyone can use /snitch {self.code} before the heist starts.")
        await self.save()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=e, view=self)
        else:
            await self.edit(embed=e, view=self)

    async def finish(self):
        """Called with the lock held when the running time is over: the vault pays the crew."""
        self.done = True
        if not await self.claim():
            return
        self.phase = "finished"
        crew = self.crew_people()
        ids = [p.id for p in crew]
        chance = _RNG.uniform(*HEIST_CHANCE)           # every heist has its own random chance to work
        if _RNG.random() >= chance:
            e = discord.Embed(title="\U0001F6A8 Vault Heist: Failed!", color=C_LOSE,
                              description="The alarm went off! The crew ran away with nothing.")
            e.add_field(name="Crew", value=", ".join(p.mention for p in crew), inline=False)
            e.set_footer(text=f"Success chance was {chance * 100:.0f}%.")
            try:
                await self.edit(embed=e, view=self)
            except Exception as err:
                print(f"[Heist Edit Error] {err}")
            self.stop()
            return
        total = heist_loot(len(ids))
        shares = heist_split(total, ids)
        lines = []
        for p in crew:
            bal = add_user_points(str(p.id), shares[p.id])
            lines.append(f"{p.mention}  `+{shares[p.id]:,}`  \u2022  Balance `{bal:,}`")
        e = discord.Embed(title="\U0001F4B0 Vault Heist: Success!", color=C_WIN,
                          description=f"The crew got away with `{total:,}` points!")
        e.add_field(name="Crew", value=f"`{len(ids)}` people", inline=True)
        e.add_field(name="Each share", value=f"`{total // len(ids):,}`", inline=True)
        e.add_field(name="Payout", value="\n".join(lines), inline=False)
        e.set_footer(text=f"Success chance was {chance * 100:.0f}%. Crew of {len(ids)}: the loot is the best of {len(ids)} rolls from 1 to {HEIST_MAX:,}.")
        try:
            await self.edit(embed=e, view=self)
        except Exception as err:
            print(f"[Heist Edit Error] {err}")
        self.stop()

    async def snitch(self, user_id: int):
        """One person tells the guards. Returns (status, crew). status: late, tried, won or lost."""
        async with self.lock:
            if self.done or self.phase != "running":
                return "late", []
            if self.snitch_tried:
                return "tried", []
            self.snitch_tried = True
            crew = self.crew_people()
            inside = user_id in [p.id for p in crew]
            chance = _RNG.uniform(*(SNITCH_IN if inside else SNITCH_OUT))    # random chance, better for someone inside the crew
            if _RNG.random() >= chance:                # the snitch is caught: the crew learns who it was
                await self.save()
                return "lost", crew
            self.done = True
            if not await self.claim():
                return "late", []
            self.phase = "finished"
            add_user_points(str(user_id), SNITCH_REWARD)
            e = discord.Embed(title="\U0001F6A8 Vault Heist: Failed!", color=C_LOSE,
                              description="The guards got a tip about the heist. The crew was caught and got nothing!")
            e.add_field(name="Crew", value=", ".join(p.mention for p in crew), inline=False)
            e.set_footer(text="Nobody knows who the snitch was.")
            try:
                await self.edit(embed=e, view=self)
            except Exception as err:
                print(f"[Heist Edit Error] {err}")
            self.stop()
            return "won", crew

    async def after_answer(self, interaction: discord.Interaction):
        """Updates the message after one person answered."""
        if self.answered():
            if len(self.joined) >= 2:
                return await self.go(interaction)
            embed = await self.cancel("Everyone said no. The heist was called off.")
            await interaction.response.edit_message(embed=embed, view=self)
            return self.stop()
        self.update_buttons()
        await self.save()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="te_heist_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done or self.phase != "invite":
                return await interaction.response.defer()
            uid = interaction.user.id
            if uid not in [p.id for p in self.invited]:
                return await interaction.response.send_message("You were not invited to this heist.", ephemeral=True)
            if uid in self.joined:
                return await interaction.response.send_message("You already joined.", ephemeral=True)
            wait = heist_wait_left(uid)
            if wait > 0:
                return await interaction.response.send_message(f"You are resting after the last heist. Try again <t:{int(_time.time()) + wait}:R>.", ephemeral=True)
            if uid in self.declined:
                self.declined.remove(uid)
            self.joined.append(uid)
            await self.after_answer(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="te_heist_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done or self.phase != "invite":
                return await interaction.response.defer()
            uid = interaction.user.id
            if uid == self.leader.id:
                embed = await self.cancel(f"{interaction.user.mention} called off the heist.")
                await interaction.response.edit_message(embed=embed, view=self)
                return self.stop()
            if uid not in [p.id for p in self.invited]:
                return await interaction.response.send_message("You were not invited to this heist.", ephemeral=True)
            if uid in self.joined:
                self.joined.remove(uid)
            if uid not in self.declined:
                self.declined.append(uid)
            await self.after_answer(interaction)

    @discord.ui.button(label="Start Now", style=discord.ButtonStyle.primary, custom_id="te_heist_start", disabled=True)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done or self.phase != "invite":
                return await interaction.response.defer()
            if interaction.user.id != self.leader.id:
                return await interaction.response.send_message(f"Only {self.leader.mention} can start the heist.", ephemeral=True)
            if len(self.joined) < 2:
                return await interaction.response.send_message("At least one more person must join first.", ephemeral=True)
            await self.go(interaction)

    async def expire(self):
        async with self.lock:
            if self.done:
                return
            try:
                if self.phase == "running":
                    await self.finish()
                elif len(self.joined) >= 2:      # the people who did not answer are left out
                    await self.go()
                else:
                    embed = await self.cancel("Nobody joined in time. The heist was called off.")
                    await self.edit(embed=embed, view=self)
                    self.stop()
            except Exception as e:
                print(f"[Heist Expire Error] {e}")


GAME_KINDS["heist"] = HeistView


# ---------------- trivia ----------------
TRIVIA_REWARD = {"easy": 2, "medium": 4, "hard": 6}
TRIVIA_DAILY_LIMIT = 10           # rewarded answers per person per day
TRIVIA_ACTIVE = set()             # channels with a running question
TRIVIA_LAST = {}
TRIVIA_WINS = {}                  # user id -> (date, count)
TRIVIA_BANK = [
    ("What is the capital of Japan?", "Tokyo", ["Osaka", "Kyoto", "Nagoya"], "easy"),
    ("How many continents are there?", "7", ["5", "6", "8"], "easy"),
    ("Which planet is known as the Red Planet?", "Mars", ["Venus", "Jupiter", "Mercury"], "easy"),
    ("What is the largest ocean on Earth?", "Pacific Ocean", ["Atlantic Ocean", "Indian Ocean", "Arctic Ocean"], "easy"),
    ("How many days are there in a leap year?", "366", ["365", "364", "367"], "easy"),
    ("Which animal is the largest mammal?", "Blue whale", ["African elephant", "Giraffe", "Hippopotamus"], "easy"),
    ("What color do you get by mixing blue and yellow?", "Green", ["Purple", "Orange", "Brown"], "easy"),
    ("How many players are on the field for one soccer team?", "11", ["9", "10", "12"], "easy"),
    ("What gas do plants take in from the air?", "Carbon dioxide", ["Oxygen", "Nitrogen", "Hydrogen"], "easy"),
    ("Which company created the game Minecraft?", "Mojang", ["Nintendo", "Valve", "Epic Games"], "easy"),
    ("What is the chemical symbol for gold?", "Au", ["Ag", "Go", "Gd"], "medium"),
    ("Who painted the Mona Lisa?", "Leonardo da Vinci", ["Michelangelo", "Raphael", "Vincent van Gogh"], "medium"),
    ("What is the smallest prime number?", "2", ["1", "3", "0"], "medium"),
    ("Which country has the most people living in it (2023 and later)?", "India", ["China", "United States", "Indonesia"], "medium"),
    ("What is the hardest natural material?", "Diamond", ["Gold", "Iron", "Quartz"], "medium"),
    ("In which year did the first Roblox game platform launch to the public?", "2006", ["2001", "2010", "2013"], "medium"),
    ("Which element has the atomic number 1?", "Hydrogen", ["Helium", "Oxygen", "Carbon"], "medium"),
    ("How many bones does an adult human body have?", "206", ["186", "256", "300"], "medium"),
    ("What is the longest river in South America?", "Amazon", ["Orinoco", "Parana", "Madeira"], "medium"),
    ("Which language has the most native speakers?", "Mandarin Chinese", ["English", "Spanish", "Hindi"], "medium"),
    ("What is the speed of light in a vacuum (about)?", "300,000 km/s", ["150,000 km/s", "30,000 km/s", "3,000,000 km/s"], "hard"),
    ("Who wrote the play Hamlet?", "William Shakespeare", ["Christopher Marlowe", "Charles Dickens", "Ben Jonson"], "hard"),
    ("Which planet has the most known moons (as of 2023)?", "Saturn", ["Jupiter", "Uranus", "Neptune"], "hard"),
    ("What is the capital of Australia?", "Canberra", ["Sydney", "Melbourne", "Perth"], "hard"),
    ("What is the square root of 144?", "12", ["14", "11", "13"], "easy"),
]


async def fetch_trivia() -> dict:
    """Gets one question from the Open Trivia Database. If that fails, a question from the built-in list is used."""
    import html
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://opentdb.com/api.php?amount=1&type=multiple") as resp:
                data = await resp.json()
        if data.get("response_code") == 0 and data.get("results"):
            q = data["results"][0]
            return {"question": html.unescape(q["question"]), "correct": html.unescape(q["correct_answer"]),
                    "wrong": [html.unescape(x) for x in q["incorrect_answers"]], "difficulty": q.get("difficulty", "medium")}
    except Exception as e:
        print(f"[Trivia API Error] {e}")
    question, correct, wrong, difficulty = _RNG.choice(TRIVIA_BANK)
    return {"question": question, "correct": correct, "wrong": list(wrong), "difficulty": difficulty}


def trivia_wins_today(uid: str) -> int:
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    day, count = TRIVIA_WINS.get(uid, (today, 0))
    return count if day == today else 0


class TriviaView(PersistentGame):
    """A trivia question stays open (no timer) until the next trivia is posted in the same channel.
    Everyone has one try. The first correct answer wins the prize, later correct answers only get a message."""
    kind = "trivia"
    LETTERS = "ABCD"

    def __init__(self, data: dict, channel_id: int, seconds: int = 0, options: Optional[list] = None):
        super().__init__()
        self.data, self.channel_id = data, channel_id
        if options is None:
            options = [data["correct"]] + data["wrong"][:3]
            _RNG.shuffle(options)
        self.options = options
        self.correct_index = options.index(data["correct"])
        self.reward = TRIVIA_REWARD.get(data["difficulty"], 4)
        self.tried = set()            # everyone who already answered (right or wrong)
        self.winner = None            # {"id": ..., "name": ..., "prize": ...} of the first correct answer
        for i in range(len(options)):
            button = discord.ui.Button(label=self.LETTERS[i], style=discord.ButtonStyle.primary, custom_id=f"te_tv_{i}")
            button.callback = self.make_callback(i)
            self.add_item(button)

    def get_state(self) -> dict:
        return {"data": self.data, "channel_id": self.channel_id, "options": self.options, "tried": list(self.tried), "winner": self.winner}

    @classmethod
    def from_state(cls, st: dict):
        view = cls(st["data"], st["channel_id"], 0, st["options"])
        view.tried = set(st.get("tried") or st.get("locked_out") or [])
        view.winner = st.get("winner")
        return view

    def make_embed(self, note: str = "", color: int = C_GAME) -> discord.Embed:
        lines = "\n".join(f"**{self.LETTERS[i]}.** {o}" for i, o in enumerate(self.options))
        if not note:
            if self.winner:
                prize = f"They won `{self.winner['prize']}` points." if self.winner["prize"] else "No points (daily limit reached)."
                note = f"\u2705 <@{self.winner['id']}> answered correctly first! {prize}\nYou can still answer until the next trivia, but only one try each."
                color = C_WIN
            else:
                note = "First correct answer wins the prize. One try each. The question stays open until the next trivia."
        e = gamble_embed("Trivia", f"**{self.data['question']}**\n\n{lines}\n\n{note}", color)
        e.add_field(name="Difficulty", value=f"`{self.data['difficulty']}`", inline=True)
        e.add_field(name="Prize", value=f"`{self.reward}` points", inline=True)
        return e

    def make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            async with self.lock:
                if self.done:
                    return await interaction.response.defer()
                uid = interaction.user.id
                if uid in self.tried:
                    return await interaction.response.send_message("You already answered this question.", ephemeral=True)
                self.tried.add(uid)
                if index != self.correct_index:
                    await self.save()
                    return await interaction.response.send_message("Wrong answer! You cannot answer this question again.", ephemeral=True)
                if self.winner is not None:      # correct, but someone was first
                    await self.save()
                    return await interaction.response.send_message(f"\u2705 Correct! But <@{self.winner['id']}> answered first, so there is no prize.", ephemeral=True)
                key = str(uid)
                count = trivia_wins_today(key)
                prize = 0
                if count < TRIVIA_DAILY_LIMIT:
                    TRIVIA_WINS[key] = (datetime.now(timezone(timedelta(hours=9))).date().isoformat(), count + 1)
                    add_user_points(key, self.reward)
                    prize = self.reward
                self.winner = {"id": uid, "name": interaction.user.display_name, "prize": prize}
                await self.save()
                await interaction.response.edit_message(embed=self.make_embed(), view=self)
        return callback

    async def expire(self):
        """Called when the next trivia is posted in this channel: the question closes and shows the answer."""
        async with self.lock:
            if self.done:
                return
            self.done = True
            await self.claim()
            for item in self.children:
                item.disabled = True
            answer = self.options[self.correct_index]
            if self.winner:
                note, color = f"\u2705 Solved by <@{self.winner['id']}>. The answer was **{answer}**.", C_WIN
            else:
                note, color = f"\u23F0 Nobody got it. The answer was **{answer}**.", C_LOSE
            try:
                await self.edit(embed=self.make_embed(note, color), view=self)
            except Exception as e:
                print(f"[Trivia Expire Error] {e}")


async def close_old_trivia(channel_id: int):
    """A new trivia is coming: close the earlier question(s) of this channel."""
    for view in list(GAME_VIEWS.values()):
        if getattr(view, "kind", "") == "trivia" and view.channel_id == channel_id and not view.done:
            try:
                await view.expire()
            except Exception as e:
                print(f"[Trivia Close Error] {e}")


GAME_KINDS["trivia"] = TriviaView


class GamblingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        GAME_BOT["bot"] = self.bot
        self.bot.loop.create_task(self.prepare_cards())
        self.bot.loop.create_task(self.restore())
        self.trivia_event_loop.start()
        self.game_sweeper.start()

    async def cog_unload(self):
        self.trivia_event_loop.cancel()
        self.game_sweeper.cancel()

    async def restore(self):
        await self.bot.wait_until_ready()
        await restore_games(self.bot)

    @tasks.loop(seconds=10)
    async def game_sweeper(self):
        """Finishes games where nobody pressed a button for a long time (works also after a restart)."""
        now = _time.time()
        for view in list(GAME_VIEWS.values()):
            if view.expires_at and now >= view.expires_at and not view.done:
                try:
                    await view.expire()
                except Exception as e:
                    print(f"[Game Sweeper Error] {e}")

    @game_sweeper.before_loop
    async def before_game_sweeper(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def trivia_event_loop(self):
        """Trivia is a timed event: the bot asks a question in the chosen channel every N minutes."""
        try:
            cfg = get_trivia_event()
            if not cfg["enabled"]:
                return
            now = _time.time()
            if cfg["next_at"] <= 0:              # first start: wait one full interval
                set_trivia_event(next_at=now + cfg["interval"] * 60)
                return
            if now < cfg["next_at"]:
                return
            cid = int(cfg["channel_id"])
            set_trivia_event(next_at=now + cfg["interval"] * 60)    # set first, so a slow question can never be asked twice
            channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            view = TriviaView(await fetch_trivia(), cid)
            await close_old_trivia(cid)           # the earlier question closes when the new one comes
            msg = await channel.send(embed=view.make_embed(), view=view)
            view.attach(msg, cid)
            await view.save()
        except Exception as e:
            print(f"[Trivia Event Error] {e}")

    @trivia_event_loop.before_loop
    async def before_trivia_event_loop(self):
        await self.bot.wait_until_ready()

    async def prepare_cards(self):
        await self.bot.wait_until_ready()
        await ensure_card_emojis(self.bot)

    async def cog_check(self, ctx: commands.Context) -> bool:
        if is_user_blacklisted(ctx.author.id):
            raise commands.CheckFailure("blocked")
        return True

    async def cog_command_error(self, ctx: commands.Context, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CheckFailure):
            return  # blocked people get no answer (slash commands are answered by the global block check)
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.BadLiteralArgument)):
            usage = f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            return await ctx.send(embed=gamble_error(f"Please use it like this:\n`{usage}`\nThe bet must be a whole number from `{GAMBLE_MIN_BET}` to `{GAMBLE_MAX_BET}`."))
        print(f"[Gambling Error] {error!r}")
        gamble_forget(str(ctx.author.id))
        try:
            await ctx.send(embed=gamble_error("Something went wrong. If your bet was taken, it is gone. Please tell the owner."))
        except Exception:
            pass

    # ----- help -----
    @commands.hybrid_command(name="gamble", description="How the gambling games work.")
    async def gamble_cmd(self, ctx: commands.Context):
        p = ctx.clean_prefix
        e = gamble_embed("Gambling with TE Points", f"You can bet `{GAMBLE_MIN_BET}` to `{GAMBLE_MAX_BET}` points in each round. You have `{get_user_points(str(ctx.author.id)):,}` points.")
        e.add_field(name="Blackjack", value=f"`{p}blackjack <bet>`\nBeat the dealer without going over 21. Win pays `2x`, blackjack pays `2.5x`. The dealer stands on 17.", inline=False)
        e.add_field(name="Roulette", value=f"`{p}roulette <bet> <choice>`\nChoices: `red` `black` `even` `odd` `low` `high` (pay `2x`), `1st` `2nd` `3rd` dozen (pay `3x`), or a number `0`-`36` (pays `36x`). There is one green `0`.", inline=False)
        e.add_field(name="Slots", value=f"`{p}slots <bet>`\nThree matching symbols win up to `250x`. Exactly two cherries pay `2x`. Return to player: `{slot_rtp() * 100:.1f}%`.", inline=False)
        e.add_field(name="Higher or Lower", value=f"`{p}highlow <bet>`\nGuess if the next card is higher or lower. The payout follows the real chance. Cash out any time after one correct guess.", inline=False)
        e.add_field(name="Vault Heist", value=f"`{p}heist @Ann, @Bob`\nRob the vault with friends. Loot is `1`-`1000` points, shared equally. A bigger crew gets more loot. No bet needed, but a heist can fail. The heist starts `60` seconds after the crew joins. Watch out for snitches!", inline=False)
        e.add_field(name="Snitch", value=f"`{p}snitch <heist id>`\nTell the guards before a heist starts. You get a random chance to make it fail and win `1000` points. Crew members have a better chance. If it fails, the crew sees your name!", inline=False)
        e.add_field(name="Coin Flip Duel", value=f"`{p}coinflip @user <bet>`\nChallenge a friend. Both bet the same, the winner takes it all. No house edge.", inline=False)
        e.add_field(name="Crash", value=f"`{p}crash <bet>`\nA rocket climbs higher every step. Cash out before it crashes!", inline=False)
        e.add_field(name="Trivia", value=f"Timed event: the bot asks a question by itself. Use `{p}trivia` to see when the next one comes. First correct answer wins `2`-`6` points (up to `{TRIVIA_DAILY_LIMIT}` rewards a day). One try each.", inline=False)
        await ctx.send(embed=e)

    # ----- blackjack -----
    @commands.hybrid_command(name="blackjack", aliases=["bj"], description="Play blackjack against the dealer with TE points (bet 1-100).")
    @app_commands.describe(bet="How many points to bet (1-100).")
    async def blackjack_cmd(self, ctx: commands.Context, bet: int):
        uid = str(ctx.author.id)
        error = gamble_begin(uid, bet)
        if error:
            return await ctx.send(embed=gamble_error(error), ephemeral=True)
        deck = new_deck(BJ_DECKS)
        view = BlackjackView(_Person.of(ctx.author), bet, deck, [deck.pop(), deck.pop()], [deck.pop(), deck.pop()])
        pv, dv = hand_value(view.hands[0].cards), hand_value(view.dealer)
        if pv == 21 or dv == 21:  # a natural blackjack ends the round at once
            view.done = True
            view.update_buttons()
            if pv == 21 and dv == 21:
                text, color, payout, result = "Both have blackjack.", C_PUSH, bet, "Push"
            elif pv == 21:
                text, color, payout, result = "Blackjack! `2.5x`", C_WIN, bet * 5 // 2, "Blackjack"
            else:
                text, color, payout, result = "Dealer has blackjack.", C_LOSE, 0, "Lose"
            balance = gamble_end(uid, payout)
            return await ctx.send(embed=view.make_embed(True, text, color, [result], payout, balance), view=view)
        msg = await ctx.send(embed=view.make_embed(), view=view)
        view.attach(msg, ctx.channel.id)
        await view.save()

    # ----- roulette -----
    @commands.hybrid_command(name="roulette", description="Bet on the roulette wheel with TE points (bet 1-100).")
    @app_commands.describe(bet="How many points to bet (1-100).", choice="red, black, even, odd, low, high, 1st, 2nd, 3rd or a number 0-36.")
    async def roulette_cmd(self, ctx: commands.Context, bet: int, choice: str):
        parsed = roulette_parse(choice)
        if parsed is None:
            return await ctx.send(embed=gamble_error("I do not know that choice. Use `red`, `black`, `even`, `odd`, `low`, `high`, `1st`, `2nd`, `3rd` or a number from `0` to `36`."), ephemeral=True)
        uid = str(ctx.author.id)
        error = gamble_begin(uid, bet)
        if error:
            return await ctx.send(embed=gamble_error(error), ephemeral=True)
        label, test, multiplier = parsed
        number = _RNG.randrange(37)          # decided now; the timer below only shows it
        won = bool(test(number))
        payout = bet * multiplier if won else 0
        msg = None
        try:
            spin = gamble_embed("Roulette", "\U0001F3A1 The wheel is spinning...", C_GAME)
            spin.add_field(name="Your bet", value=f"`{label}`", inline=True)
            spin.add_field(name="Stake", value=f"`{bet}`", inline=True)
            msg = await ctx.send(embed=spin)
            for left in (3, 2, 1):
                await asyncio.sleep(1.1)
                fake = _RNG.randrange(37)    # a number flying past, only for the look
                spin.description = f"\U0001F3A1 The wheel is spinning...\n## {roulette_color(fake)} `{fake}`\nResult in `{left}`..."
                await msg.edit(embed=spin)
            await asyncio.sleep(1.1)
        except Exception as e:
            print(f"[Roulette Animation Error] {e}")   # the result is still paid below
        balance = gamble_end(uid, payout)
        e = gamble_embed("Roulette", f"The ball landed on {roulette_color(number)} `{number}`.", C_WIN if won else C_LOSE)
        e.add_field(name="Your bet", value=f"`{label}`", inline=True)
        e.add_field(name="Stake", value=f"`{bet}`", inline=True)
        e.add_field(name="Result", value=gamble_net(payout, bet), inline=True)
        e.add_field(name="Balance", value=f"`{balance:,}`", inline=True)
        if msg is not None:
            try:
                return await msg.edit(embed=e)
            except Exception as err:
                print(f"[Roulette Edit Error] {err}")
        await ctx.send(embed=e)

    @roulette_cmd.autocomplete("choice")
    async def roulette_choices(self, interaction: discord.Interaction, current: str):
        names = {"red": "Red", "black": "Black", "even": "Even", "odd": "Odd", "low": "Low (1-18)", "high": "High (19-36)",
                 "1st": "1st dozen (1-12)", "2nd": "2nd dozen (13-24)", "3rd": "3rd dozen (25-36)"}
        out = [app_commands.Choice(name=v, value=k) for k, v in names.items() if current.lower() in k or current.lower() in v.lower()]
        if current.isdigit() and 0 <= int(current) <= 36:
            out.insert(0, app_commands.Choice(name=f"Number {int(current)}", value=str(int(current))))
        return out[:25]

    # ----- slots -----
    @commands.hybrid_command(name="slots", aliases=["slot"], description="Spin the slot machine with TE points (bet 1-100).")
    @app_commands.describe(bet="How many points to bet (1-100).")
    async def slots_cmd(self, ctx: commands.Context, bet: int):
        uid = str(ctx.author.id)
        error = gamble_begin(uid, bet)
        if error:
            return await ctx.send(embed=gamble_error(error), ephemeral=True)
        try:
            symbols, weights = list(SLOT_WEIGHTS), list(SLOT_WEIGHTS.values())
            reels = _RNG.choices(symbols, weights=weights, k=3)   # decided now, the animation below only shows it
            multiplier = slot_payout_multiplier(reels)
            msg = await ctx.send(embed=gamble_embed("Slot Machine", "❓ ❓ ❓\nSpinning...", C_GAME))
            await asyncio.sleep(1.2)
        except Exception:
            gamble_end(uid, bet)  # the game could not even start: give the bet back
            raise
        payout = bet * multiplier
        balance = gamble_end(uid, payout)
        text = " ".join(reels)
        if multiplier >= 24:
            note, color = f"JACKPOT! `x{multiplier}`", C_WIN
        elif multiplier:
            note, color = f"You win `x{multiplier}`!", C_WIN
        else:
            note, color = "No match. Try again!", C_LOSE
        e = gamble_embed("Slot Machine", f"## {text}\n{note}", color)
        e.add_field(name="Bet", value=f"`{bet}`", inline=True)
        e.add_field(name="Result", value=gamble_net(payout, bet), inline=True)
        e.add_field(name="Balance", value=f"`{balance:,}`", inline=True)
        await msg.edit(embed=e)


    # ----- coin flip duel -----
    @commands.hybrid_command(name="coinflip", aliases=["cf", "duel"], description="Challenge someone to a coin flip for TE points (bet 1-100 each).")
    @app_commands.describe(user="Who you want to play against.", bet="How many points each of you bets (1-100).")
    async def coinflip_cmd(self, ctx: commands.Context, user: discord.Member, bet: int):
        if user.bot or user.id == ctx.author.id:
            return await ctx.send(embed=gamble_error("Choose another real person to play against."), ephemeral=True)
        if is_user_blacklisted(user.id):
            return await ctx.send(embed=gamble_error("That person cannot play."), ephemeral=True)
        error = gamble_begin(str(ctx.author.id), bet)
        if error:
            return await ctx.send(embed=gamble_error(error), ephemeral=True)
        view = CoinFlipView(_Person.of(ctx.author), _Person.of(user), bet)
        embed = gamble_embed("Coin Flip Duel", f"{ctx.author.mention} challenges {user.mention}!\nEach person bets `{bet}` points. The winner takes `{bet * 2}`.\n{user.mention}, press **Accept** or **Decline**.\n{ctx.author.mention} can press **Decline** to cancel.", C_GAME)
        try:
            msg = await ctx.send(content=user.mention, embed=embed, view=view, allowed_mentions=discord.AllowedMentions(users=[user]))
            view.attach(msg, ctx.channel.id)
            await view.save()
        except Exception:
            gamble_end(str(ctx.author.id), bet)     # the challenge could not be sent: give the bet back
            raise

    # ----- crash -----
    @commands.hybrid_command(name="crash", description="Cash out before the rocket crashes (bet 1-100).")
    @app_commands.describe(bet="How many points to bet (1-100).")
    async def crash_cmd(self, ctx: commands.Context, bet: int):
        uid = str(ctx.author.id)
        error = gamble_begin(uid, bet)
        if error:
            return await ctx.send(embed=gamble_error(error), ephemeral=True)
        crash_at = crash_point()               # chosen now, nobody can change it
        view = CrashView(_Person.of(ctx.author), bet, crash_at)
        if crash_at <= 1.0:                    # some rockets crash right at the start
            embed = await view.close(0, "The rocket crashed at once! Better luck next time.", C_LOSE, saved=False)
            return await ctx.send(embed=embed)
        try:
            msg = await ctx.send(embed=view.make_embed(), view=view)
            view.attach(msg, ctx.channel.id)
            await view.save()
        except Exception:
            gamble_end(uid, bet)
            raise
        self.bot.loop.create_task(view.run())

    # ----- heist -----
    @commands.hybrid_command(name="heist", description="Rob the Template Engine Vault with friends. Share 1-1000 points.")
    @app_commands.describe(users="Who you want in your crew. Use commas: @Ann, @Bob (max 5).")
    async def heist_cmd(self, ctx: commands.Context, *, users: str):
        if ctx.guild is None:
            return await ctx.send(embed=gamble_error("Heists only work in a server."), ephemeral=True)
        members, missing = [], []
        for raw in [x.strip() for x in users.split(",") if x.strip()]:
            m = None
            mid = re.fullmatch(r"<@!?(\d+)>|(\d{15,20})", raw)
            if mid:
                uid = int(mid.group(1) or mid.group(2))
                m = ctx.guild.get_member(uid)
                if m is None:
                    try:
                        m = await ctx.guild.fetch_member(uid)
                    except Exception:
                        m = None
            else:
                name = raw.lstrip("@").lower()
                pool = ctx.guild.members
                m = (discord.utils.find(lambda x: name in (x.name.lower(), x.display_name.lower(), (x.global_name or "").lower()), pool)
                     or discord.utils.find(lambda x: x.display_name.lower().startswith(name) or x.name.lower().startswith(name), pool))
            if m is None:
                missing.append(raw)
            elif m.bot or m.id == ctx.author.id or is_user_blacklisted(m.id) or any(m.id == k.id for k in members):
                continue
            else:
                members.append(m)
        if missing:
            return await ctx.send(embed=gamble_error("I could not find: " + ", ".join(f"`{x[:32]}`" for x in missing[:5]) + "\nUse mentions with commas, like `@Ann, @Bob`."), ephemeral=True)
        if not members:
            return await ctx.send(embed=gamble_error("Invite at least one other real person.\nExample: `!heist @Ann, @Bob`"), ephemeral=True)
        if len(members) > HEIST_MAX_INVITES:
            return await ctx.send(embed=gamble_error(f"You can invite at most `{HEIST_MAX_INVITES}` people."), ephemeral=True)
        wait = heist_wait_left(ctx.author.id)
        if wait > 0:
            return await ctx.send(embed=gamble_error(f"You are resting after the last heist. Try again <t:{int(_time.time()) + wait}:R>."), ephemeral=True)
        view = HeistView(_Person.of(ctx.author), [_Person.of(m) for m in members], [ctx.author.id], [])
        view.update_buttons()
        view.touch(HEIST_WAIT)
        msg = await ctx.send(content=" ".join(m.mention for m in members), embed=view.make_embed(), view=view,
                             allowed_mentions=discord.AllowedMentions(users=members))
        view.attach(msg, ctx.channel.id)
        await view.save()

    # ----- snitch -----
    @commands.hybrid_command(name="snitch", description="Tell the guards about a heist before it starts. Random chance to win 1000 points.")
    @app_commands.describe(heist_id="The Heist ID shown on the running heist.")
    async def snitch_cmd(self, ctx: commands.Context, heist_id: str):
        code = heist_id.strip().strip("`").lower()
        if ctx.interaction is None:                    # a prefix message would show who snitched
            try:
                await ctx.message.delete()
            except Exception:
                pass

        async def private(embed):
            if ctx.interaction is not None:
                return await ctx.send(embed=embed, ephemeral=True)
            try:
                return await ctx.author.send(embed=embed)
            except Exception:
                return await ctx.send(embed=discord.Embed(title="Snitch", description="Done. Open your DMs to see the result.", color=C_PUSH), delete_after=10)

        view = next((v for v in GAME_VIEWS.values() if isinstance(v, HeistView) and v.phase == "running" and v.code == code), None)
        if view is None:
            return await private(gamble_error(f"There is no waiting heist with the ID `{code[:12]}`."))
        wait = int(SNITCH_COOLDOWN - (_time.time() - SNITCH_LAST.get(str(ctx.author.id), 0)))
        if wait > 0:
            return await private(gamble_error(f"The guards do not trust you now. Try again <t:{int(_time.time()) + wait}:R>."))
        SNITCH_LAST[str(ctx.author.id)] = _time.time()
        status, crew = await view.snitch(ctx.author.id)
        if status == "late":
            SNITCH_LAST.pop(str(ctx.author.id), None)
            return await private(gamble_error("Too late. That heist has already started."))
        if status == "tried":
            SNITCH_LAST.pop(str(ctx.author.id), None)
            return await private(gamble_error("Someone already tried to snitch on this heist."))
        if status == "won":
            balance = get_user_points(str(ctx.author.id))
            return await private(discord.Embed(title="\U0001F6A8 The guards believed you!",
                                               description=f"The heist failed and you got away. Nobody knows it was you.\nYou got `+{SNITCH_REWARD:,}` points. Balance `{balance:,}`", color=C_WIN))
        crew_mentions = " ".join(p.mention for p in crew)
        e = discord.Embed(title="\U0001F440 Snitch caught!", color=C_LOSE,
                          description=f"\U0001F575\uFE0F An intel said {ctx.author.mention} is a snitch!\nThey told the guards about {crew_mentions}, but nobody believed it. The heist goes on!")
        await ctx.send(content=crew_mentions, embed=e,
                       allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=p.id) for p in crew]))

    # ----- trivia -----
    @commands.hybrid_command(name="trivia", description="Trivia is a timed event. See when the next question comes.")
    async def trivia_cmd(self, ctx: commands.Context):
        cfg = get_trivia_event()
        if str(ctx.author.id) in OWNER_IDS:       # the owner can test a question anywhere
            cid = ctx.channel.id
            view = TriviaView(await fetch_trivia(), cid)
            await close_old_trivia(cid)
            msg = await ctx.send(embed=view.make_embed(), view=view)
            view.attach(msg, cid)
            await view.save()
            return
        if not cfg["enabled"]:
            return await ctx.send(embed=discord.Embed(title="Trivia is locked", description="The trivia event is off right now.", color=C_PUSH), ephemeral=True)
        when = f"<t:{int(cfg['next_at'])}:R>" if cfg["next_at"] > _time.time() else "soon"
        e = discord.Embed(title="\U0001F512 Trivia is a timed event", color=C_PUSH,
                          description=f"Questions are asked by the bot in <#{cfg['channel_id']}> every `{cfg['interval']}` minutes.\nNext question: {when}.")
        await ctx.send(embed=e, ephemeral=True)

    # ----- higher / lower -----
    @commands.hybrid_command(name="highlow", aliases=["hl", "higherlower"], description="Guess if the next card is higher or lower (bet 1-100).")
    @app_commands.describe(bet="How many points to bet (1-100).")
    async def highlow_cmd(self, ctx: commands.Context, bet: int):
        error = gamble_begin(str(ctx.author.id), bet)
        if error:
            return await ctx.send(embed=gamble_error(error), ephemeral=True)
        view = HigherLowerView(ctx.author.id, bet, new_deck())
        msg = await ctx.send(embed=view.make_embed(), view=view)
        view.attach(msg, ctx.channel.id)
        await view.save()


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
    await bot.add_cog(GamblingCog(bot))
