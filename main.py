import os
import asyncio
import discord
from discord.ext import commands

# Configure Bot Gateway Intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)


@bot.event
async def on_ready():
    print("-----------------------------------------")
    print(f"Logged in as: {bot.user} (ID: {bot.user.id})")
    print("-----------------------------------------")

    # 1. Load Slash Command Extension Cog
    try:
        if "cogs.bot" not in bot.extensions:
            await bot.load_extension("cogs.bot")
            print("Successfully loaded extension cog: cogs.bot")
    except Exception as e:
        print(f"CRITICAL ERROR loading cog cogs.bot: {e}")

    # 2. Sync Slash Commands Globally across Discord
    try:
        synced = await bot.tree.sync()
        print(f"Successfully registered & synced {len(synced)} global slash command(s) with Discord.")
    except Exception as e:
        print(f"Failed to sync global slash commands: {e}")


async def runner():
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token or not token.strip():
        print("CRITICAL ERROR: DISCORD_BOT_TOKEN environment variable is missing!")
        return

    async with bot:
        await bot.start(token.strip())


def main():
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")


if __name__ == "__main__":
    main()
