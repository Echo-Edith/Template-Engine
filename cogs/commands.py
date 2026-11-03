import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

from cogs.features import (
    BOT_OWNER_ID,
    RobloxVerificationView,
    RobloxUnsyncView,
    get_user_points,
    add_user_points,
    get_top_leaderboard,
    parse_duration_to_timedelta,
    set_user_premium,
    remove_user_premium,
)


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
# COMMANDS COG: all slash commands
# -------------------------------------------------------------
class CommandsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------
    # OWNER LOCK: every slash command in this cog is hard-coded to BOT_OWNER_ID
    # -------------------------------------------------------------
    PUBLIC_COMMANDS = {"points", "leaderboard"}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == BOT_OWNER_ID:
            return True

        # Public commands: anyone can use these
        if interaction.command and interaction.command.name in self.PUBLIC_COMMANDS:
            return True

        embed = discord.Embed(
            title="Permission Denied",
            description=f"These commands can only be used by the bot owner (`{BOT_OWNER_ID}`).",
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
        print(f"[App Command Error] {error}")

    # -------------------------------------------------------------
    # SLASH COMMAND: SET VERIFICATION OR UNSYNC INTERACTIVE PANELS
    # -------------------------------------------------------------
    @app_commands.command(name="set", description="Deploy verification or un-sync interactive embed panels into a channel.")
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


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
