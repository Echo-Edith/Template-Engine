import os
import asyncio
import threading
from flask import Flask, jsonify
import discord
from discord.ext import commands

# Render Web Services require an open HTTP port. This lightweight server satisfies port scanning.
web_app = Flask(__name__)


@web_app.route("/")
@web_app.route("/health")
def health_check():
    return jsonify({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing"
    }), 200


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    # Suppress verbose Flask logging in bot console
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    web_app.run(host="0.0.0.0", port=port)


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

    # 1. Load Extension Cog
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
    # Start HTTP health server thread so Render detects open port 10000
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print("Background health server started on port 10000.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")


if __name__ == "__main__":
    main()
