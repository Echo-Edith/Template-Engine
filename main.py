import os
import asyncio
import threading
import requests
from flask import Flask, jsonify
import discord
from discord.ext import commands
from pymongo import MongoClient

SECRET_KEY = os.environ.get("SESSION_SECRET", "template_engine_secret_key_1337").strip()
MONGO_URI = os.environ.get("MONGO_URI")
ROBLOX_OPEN_CLOUD_KEY = (os.environ.get("ROBLOX_OPEN_CLOUD_KEY") or "").strip()
ROBLOX_UNIVERSE_ID = (os.environ.get("ROBLOX_UNIVERSE_ID") or "").strip()
ROBLOX_GROUP_ID = (os.environ.get("ROBLOX_GROUP_ID") or "").strip()
ROBLOX_COOKIE = (os.environ.get("ROBLOX_COOKIE") or os.environ.get("ROBLOSECURITY") or "").strip()
SYSTEM_LOG_WEBHOOK_URL = (os.environ.get("SYSTEM_LOG_WEBHOOK_URL") or "").strip()

def get_formatted_roblox_cookie():
    if not ROBLOX_COOKIE:
        return ""
    clean = ROBLOX_COOKIE.strip('"\'')
    if clean.startswith(".ROBLOSECURITY="):
        return clean
    return f".ROBLOSECURITY={clean}"

web_app = Flask(__name__)
web_app.secret_key = SECRET_KEY

@web_app.route("/")
@web_app.route("/health")
def health_check():
    return jsonify({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing"
    }), 200

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    web_app.run(host="0.0.0.0", port=port)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True

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

    try:
        if "cogs.bot" not in bot.extensions:
            await bot.load_extension("cogs.bot")
            print("Successfully loaded extension cog: cogs.bot")
        else:
            await bot.reload_extension("cogs.bot")
            print("Successfully reloaded extension cog: cogs.bot")
    except Exception as e:
        print(f"CRITICAL ERROR loading cog cogs.bot: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"Successfully registered and synced {len(synced)} global slash command(s).")
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
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print("Background API server started on port 10000.")

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Bot runner shutdown gracefully.")

if __name__ == "__main__":
    main()
